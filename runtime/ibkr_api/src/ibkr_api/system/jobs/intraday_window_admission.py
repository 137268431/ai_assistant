from __future__ import annotations

import time
from typing import Any, Callable

from ibkr_api.modes import request_market_data_mode
from ibkr_api.orders.values import first_defined, parse_boolean, to_float, to_int, to_text
from ibkr_api.universe.active_window_progress import build_active_window_items_for_symbols
from ibkr_api.universe.maintenance import (
    call_universe_reconcile,
    ensure_target_watchlist_record,
    get_runtime_market_date,
    normalize_direction_bias,
    normalize_watchlist_role,
    parse_json_object,
    upsert_record,
)
from ibkr_api.universe.today_targets_shared import (
    LIVE_ENVIRONMENT,
    build_bar_environment_filter,
    build_daily_change_fields,
    classify_session,
    escape_filter,
    format_cn_time,
    format_et_date,
    indicator_snapshot,
    interval_to_chart_tf,
    load_records_for_symbols,
    normalize_symbols,
)
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms
from ibkr_compute.api.market.screener.scoring import (
    TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN,
    TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME,
    TRADABILITY_OPERABLE_MIN_SCORE,
    build_tradability_assessment,
)
from ibkr_compute.core.active_window_admission import is_active_window_admitted, signal_pressure_from_item
from ibkr_compute.market.timeframe_utils import normalize_interval


NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]
TimeStrings = Callable[[], dict[str, str]]
RequestJsonRequest = Callable[..., dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
WriteSystemEventRecord = Callable[..., dict[str, Any]]

SUPPORTED_ENVIRONMENTS = {"live", "paper"}
TARGET_STATUSES = {"active", "candidate"}
DEFAULT_MAX_SCAN_SYMBOLS = 160
DEFAULT_MAX_ADMIT_PER_RUN = 8
DEFAULT_MIN_ATR_PCT = 0.15
DEFAULT_MIN_PREMARKET_VOLUME = 5_000
DEFAULT_MIN_TODAY_VOLUME = 300_000
DEFAULT_MARKET_MONITOR_SYMBOLS = "SPY,QQQ,VIX"
WINDOW_ADMISSION_SOURCE = "intraday_window_admission"


def _config_int(config_value: ConfigValue, key: str, default: int, environment: str, *, minimum: int = 0) -> int:
    try:
        raw = config_value(key, str(default), environment)
    except Exception:
        raw = default
    return max(int(minimum), to_int(raw, default))


def _config_float(config_value: ConfigValue, key: str, default: float, environment: str, *, minimum: float = 0.0) -> float:
    try:
        raw = config_value(key, str(default), environment)
    except Exception:
        raw = default
    parsed = to_float(raw)
    return max(float(minimum), float(parsed if parsed is not None else default))


def _config_bool(config_value: ConfigValue, key: str, default: bool, environment: str) -> bool:
    try:
        raw = config_value(key, "TRUE" if default else "FALSE", environment)
    except Exception:
        raw = default
    return parse_boolean(raw, default)


def _current_market_date(
    environment: str,
    *,
    payload: dict[str, Any],
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
    time_strings: TimeStrings,
) -> str:
    requested = to_text(first_defined(payload.get("market_date"), payload.get("marketDate"), payload.get("date")))
    if requested:
        return requested
    try:
        return get_runtime_market_date(
            environment,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
            time_strings=time_strings,
        )
    except Exception:
        return to_text((time_strings() or {}).get("date"))


def _time_hhmm(value: Any) -> int:
    text = to_text(value)
    parts = text.split()
    token = parts[-1] if parts else text
    hour_minute = token[:5].split(":")
    if len(hour_minute) != 2:
        return -1
    try:
        return int(hour_minute[0]) * 60 + int(hour_minute[1])
    except Exception:
        return -1


def _is_admission_window(times: dict[str, str], *, start_et: str, end_et: str) -> bool:
    current = _time_hhmm(times.get("us"))
    start = _time_hhmm(start_et)
    end = _time_hhmm(end_et)
    if current < 0 or start < 0 or end < 0:
        return True
    return start <= current <= end


def _load_effective_watchlist(
    pb: Any,
    environment: str,
    *,
    escape_filter_string: EscapeFilterString,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    filter_expr = (
        f'environment = "{escape_filter_string(environment)}" '
        '|| environment = "global" '
        '|| environment = ""'
    )
    try:
        getter = getattr(pb, "get_all_records", None)
        if callable(getter):
            rows = getter("watchlist", filter=filter_expr, sort="-updated", max_pages=30)
        else:
            rows = pb.get_records("watchlist", filter=filter_expr, sort="-updated", per_page=500, page=1)
    except Exception:
        rows = []

    priority = {"": 0, "global": 1, environment: 2}
    applied: dict[str, int] = {}
    trade_rows: dict[str, dict[str, Any]] = {}
    monitor_symbols: set[str] = set()
    for raw_row in rows or []:
        if not isinstance(raw_row, dict):
            continue
        symbol = to_text(raw_row.get("symbol")).upper()
        if not symbol:
            continue
        row_environment = to_text(raw_row.get("environment")).lower()
        rank = priority.get(row_environment, -1)
        if rank < 0:
            continue
        role = normalize_watchlist_role(raw_row.get("symbol_role"))
        if symbol in applied and applied[symbol] > rank:
            continue
        applied[symbol] = rank
        row = dict(raw_row)
        row["symbol_role"] = role
        if role == "market_monitor":
            monitor_symbols.add(symbol)
            trade_rows.pop(symbol, None)
        else:
            monitor_symbols.discard(symbol)
            trade_rows[symbol] = row
    return trade_rows, monitor_symbols


def _configured_monitor_symbols(config_value: ConfigValue, environment: str) -> set[str]:
    raw = ""
    try:
        if _config_bool(config_value, "ibkr_market_ws_enabled", True, environment):
            raw = config_value("ibkr_market_ws_symbols", DEFAULT_MARKET_MONITOR_SYMBOLS, environment)
    except Exception:
        raw = DEFAULT_MARKET_MONITOR_SYMBOLS
    return set(normalize_symbols(raw))


def _load_today_targets(
    pb: Any,
    environment: str,
    market_date: str,
    *,
    escape_filter_string: EscapeFilterString,
) -> dict[str, dict[str, Any]]:
    filter_expr = (
        f'environment = "{escape_filter_string(environment)}" && '
        f'date = "{escape_filter_string(market_date)}" && '
        '(status = "candidate" || status = "active")'
    )
    try:
        rows = pb.get_records("ibkr_targets", filter=filter_expr, sort="-updated", per_page=500, page=1)
    except Exception:
        rows = []
    result: dict[str, dict[str, Any]] = {}
    for raw_row in rows or []:
        if not isinstance(raw_row, dict):
            continue
        symbol = to_text(raw_row.get("symbol")).upper()
        status = to_text(raw_row.get("status")).lower()
        if not symbol or status not in TARGET_STATUSES:
            continue
        row_environment = to_text(raw_row.get("environment")).lower()
        row_date = to_text(raw_row.get("date"))
        if row_environment and row_environment != environment:
            continue
        if row_date and row_date != market_date:
            continue
        if symbol not in result:
            result[symbol] = dict(raw_row)
    return result


def _trade_budget(config_value: ConfigValue, environment: str, monitor_count: int) -> int | None:
    target_limit = _config_int(config_value, "ibkr_target_subscription_limit", 80, environment)
    total_limit = _config_int(config_value, "ibkr_total_subscription_limit", 80, environment)
    budget: int | None = target_limit if target_limit > 0 else None
    if total_limit > 0:
        total_budget = max(0, total_limit - max(0, int(monitor_count or 0)))
        budget = total_budget if budget is None else min(budget, total_budget)
    return budget


def _window_is_valid(item: dict[str, Any]) -> bool:
    return is_active_window_admitted(item)


def _infer_direction_bias(item: dict[str, Any], indicator_extra: dict[str, Any]) -> str:
    signal_state = item.get("signal_state") if isinstance(item.get("signal_state"), dict) else {}
    candidate_signal = item.get("candidate_signal") if isinstance(item.get("candidate_signal"), dict) else {}
    for value in (signal_state.get("direction"), candidate_signal.get("direction")):
        direction = to_text(value).lower()
        if direction in {"long", "short"}:
            return direction
    components = item.get("components") if isinstance(item.get("components"), dict) else {}
    component_detail = item.get("component_detail") if isinstance(item.get("component_detail"), dict) else {}
    for group_key in list(components.get("ready_groups") or []) + [to_text(components.get("best_group"))]:
        group = component_detail.get(group_key) if isinstance(component_detail.get(group_key), dict) else {}
        direction = to_text(group.get("direction")).lower()
        if direction in {"long", "short"}:
            return direction
    trend_dir = to_int(indicator_extra.get("trend_dir"), 0)
    if trend_dir > 0:
        return "long"
    if trend_dir < 0:
        return "short"
    return "neutral"


def _latest_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        symbol = to_text(row.get("symbol")).upper()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        if not symbol or bar_time_ms <= 0:
            continue
        current = latest.get(symbol)
        if current is None or bar_time_ms >= to_int(current.get("bar_time_ms"), 0):
            latest[symbol] = row
    return latest


def _symbols_with_fresh_bars(
    pb: Any,
    *,
    environment: str,
    symbols: list[str],
    market_start_ms: int,
    market_end_ms: int,
    computed_at_ms: int,
    max_freshness_min: int,
) -> tuple[list[str], dict[str, str]]:
    records = load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "5m"',
            build_bar_environment_filter(environment),
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=symbols,
        sort="-bar_time_ms",
        max_pages=4,
    )
    symbol_set = set(normalize_symbols(symbols))
    latest: dict[str, dict[str, Any]] = {}
    for row in records:
        symbol = to_text(row.get("symbol")).upper()
        interval = to_text(row.get("interval")).lower()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        if symbol not in symbol_set or interval != "5m" or not (market_start_ms <= bar_time_ms < market_end_ms):
            continue
        current = latest.get(symbol)
        if current is None or bar_time_ms >= to_int(current.get("bar_time_ms"), 0):
            latest[symbol] = row
    fresh_symbols: list[str] = []
    rejected: dict[str, str] = {}
    for symbol in symbols:
        row = latest.get(symbol)
        bar_time_ms = to_int((row or {}).get("bar_time_ms"), 0)
        if bar_time_ms <= 0:
            rejected[symbol] = "no_live_5m_bar"
            continue
        freshness_min = max(0, int((computed_at_ms - bar_time_ms) // 60000))
        if freshness_min > max_freshness_min:
            rejected[symbol] = f"stale_bar:{freshness_min}m"
            continue
        fresh_symbols.append(symbol)
    return fresh_symbols, rejected


def _build_metrics_by_symbol(
    pb: Any,
    *,
    environment: str,
    symbols: list[str],
    market_date: str,
    market_start_ms: int,
    market_end_ms: int,
    computed_at_ms: int,
    window_items: dict[str, dict[str, Any]],
    target_by_symbol: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    lookback_daily_ms = market_start_ms - 25 * 24 * 60 * 60 * 1000
    daily_records = load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "1d"',
            build_bar_environment_filter(environment),
            f"bar_time_ms >= {lookback_daily_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=symbols,
        sort="bar_time_ms",
        max_pages=12,
    )
    intraday_records = load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "5m"',
            build_bar_environment_filter(environment),
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=symbols,
        sort="bar_time_ms",
        max_pages=30,
    )
    indicator_records = load_records_for_symbols(
        pb,
        "ibkr_indicators",
        base_filter_parts=[
            f'interval = "{interval_to_chart_tf("5m")}"',
            f'environment = "{escape_filter(environment)}"',
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=symbols,
        sort="-bar_time_ms",
        max_pages=8,
    )
    symbol_set = set(normalize_symbols(symbols))
    daily_history_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in daily_records:
        symbol = to_text(row.get("symbol")).upper()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        interval = to_text(row.get("interval")).lower()
        close = to_float(row.get("close")) or 0.0
        if symbol not in symbol_set or interval != "1d" or bar_time_ms <= 0 or close <= 0:
            continue
        daily_history_by_symbol.setdefault(symbol, []).append(
            {
                "bar_time_ms": bar_time_ms,
                "close": close,
                "volume": to_float(row.get("volume")) or 0.0,
                "date": format_et_date(bar_time_ms),
            }
        )
    for history in daily_history_by_symbol.values():
        history.sort(key=lambda row: to_int(row.get("bar_time_ms"), 0))

    latest_intraday_by_symbol: dict[str, dict[str, Any]] = {}
    volume_stats_by_symbol: dict[str, dict[str, float]] = {}
    for row in intraday_records:
        symbol = to_text(row.get("symbol")).upper()
        interval = to_text(row.get("interval")).lower()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        if symbol not in symbol_set or interval != "5m" or not (market_start_ms <= bar_time_ms < market_end_ms):
            continue
        session_type = to_text(row.get("session_type")).lower() or classify_session(bar_time_ms=bar_time_ms)
        payload_row = {
            "bar_time_ms": bar_time_ms,
            "close": to_float(row.get("close")) or 0.0,
            "exchange": to_text(row.get("exchange")).upper(),
            "session_type": session_type,
            "volume": to_float(row.get("volume")) or 0.0,
        }
        current_latest = latest_intraday_by_symbol.get(symbol)
        if current_latest is None or bar_time_ms >= to_int(current_latest.get("bar_time_ms"), 0):
            latest_intraday_by_symbol[symbol] = payload_row
        stats = volume_stats_by_symbol.setdefault(symbol, {"premarket": 0.0, "today": 0.0})
        stats["today"] += payload_row["volume"]
        if session_type == "premarket":
            stats["premarket"] += payload_row["volume"]

    latest_indicator_by_symbol = _latest_by_symbol([
        row
        for row in indicator_records
        if to_text(row.get("symbol")).upper() in symbol_set
    ])

    metrics: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        item = window_items.get(symbol) or {}
        target = target_by_symbol.get(symbol) or {}
        target_extra = parse_json_object(target.get("extra"))
        screener_snapshot = parse_json_object(target_extra.get("screener_snapshot"))
        indicator_extra = indicator_snapshot(latest_indicator_by_symbol.get(symbol))
        intraday = latest_intraday_by_symbol.get(symbol) or {}
        history = [
            row for row in daily_history_by_symbol.get(symbol, [])
            if to_text(row.get("date")) < market_date
        ]
        last_10 = history[-10:]
        computed_avg_10d = round(sum(to_float(row.get("volume")) or 0.0 for row in last_10) / len(last_10), 2) if last_10 else 0.0
        price = to_float(first_defined(item.get("price"), intraday.get("close"), screener_snapshot.get("price"))) or 0.0
        compare_history = (
            build_daily_change_fields(history, price)
            if price > 0 and history
            else {"day_change_pct": 0.0, "prev_close_change_pct": 0.0, "change_7d": 0.0}
        )
        latest_bar_time_ms = to_int(first_defined(item.get("latest_bar_time_ms"), intraday.get("bar_time_ms")), 0)
        freshness_min = item.get("freshness_min") if isinstance(item.get("freshness_min"), int) else None
        if freshness_min is None and latest_bar_time_ms > 0:
            freshness_min = max(0, int((computed_at_ms - latest_bar_time_ms) // 60000))
        volume_stats = volume_stats_by_symbol.get(symbol, {"premarket": 0.0, "today": 0.0})
        direction_bias = _infer_direction_bias(item, indicator_extra)
        row = {
            "symbol": symbol,
            "price": round(price, 4) if price > 0 else 0.0,
            "atr_pct": round(
                to_float(first_defined(
                    item.get("atr_pct"),
                    indicator_extra.get("atr_pct"),
                    target_extra.get("atr_pct"),
                    screener_snapshot.get("atr_pct"),
                )) or 0.0,
                4,
            ),
            "avg_10d_volume": float(first_defined(computed_avg_10d if computed_avg_10d > 0 else None, screener_snapshot.get("avg_10d_volume")) or 0.0),
            "premarket_volume": float(first_defined(volume_stats.get("premarket") if volume_stats.get("premarket") else None, screener_snapshot.get("premarket_volume")) or 0.0),
            "today_volume": float(first_defined(volume_stats.get("today") if volume_stats.get("today") else None, screener_snapshot.get("today_volume")) or 0.0),
            "latest_bar_time_ms": latest_bar_time_ms,
            "freshness_min": freshness_min,
            "day_change_pct": compare_history["day_change_pct"],
            "prev_close_change_pct": compare_history["prev_close_change_pct"],
            "change_7d": compare_history["change_7d"],
            "target_score": to_float(first_defined(target.get("score"), item.get("target_score"))) or 0.0,
            "direction_bias": direction_bias,
            "exchange": to_text(first_defined(target.get("exchange"), intraday.get("exchange"), item.get("exchange"))).upper(),
            "indicator_extra": indicator_extra,
        }
        tradability_score, operable_reasons = build_tradability_assessment(row)
        row["tradability_score"] = tradability_score
        row["operable_reasons"] = operable_reasons
        metrics[symbol] = row
    return metrics


def _reject_reason(
    item: dict[str, Any],
    metrics: dict[str, Any],
    *,
    min_avg_10d_volume: int,
    min_atr_pct: float,
    min_premarket_volume: int,
    min_today_volume: int,
    min_tradability_score: int,
    max_freshness_min: int,
) -> str:
    if not _window_is_valid(item):
        pressure = signal_pressure_from_item(item)
        if not pressure.get("signal_window_time_passed"):
            return "outside_signal_window"
        if not pressure.get("signal_pressure_keys"):
            return "signal_pressure_not_ready"
        return to_text(item.get("window_status")) or "no_current_valid_window"
    freshness_min = metrics.get("freshness_min") if isinstance(metrics.get("freshness_min"), int) else None
    if freshness_min is None:
        return "missing_freshness"
    if freshness_min > max_freshness_min:
        return f"stale_bar:{freshness_min}m"
    if (to_float(metrics.get("price")) or 0.0) <= 0:
        return "price_not_ready"
    if (to_float(metrics.get("avg_10d_volume")) or 0.0) < min_avg_10d_volume:
        return "avg_10d_volume_below_threshold"
    if abs(to_float(metrics.get("atr_pct")) or 0.0) < min_atr_pct:
        return "atr_pct_below_threshold"
    premarket_volume = to_float(metrics.get("premarket_volume")) or 0.0
    today_volume = to_float(metrics.get("today_volume")) or 0.0
    if premarket_volume < min_premarket_volume and today_volume < min_today_volume:
        return "activity_below_threshold"
    if (to_float(metrics.get("tradability_score")) or 0.0) < min_tradability_score:
        return "tradability_score_below_threshold"
    return ""


def _admission_score(item: dict[str, Any], metrics: dict[str, Any]) -> float:
    score = max(to_float(metrics.get("tradability_score")) or 0.0, to_float(item.get("target_score")) or 0.0)
    if item.get("signal_pressure_passed"):
        score += 8
    if item.get("sd_upper_valid") and item.get("sd_lower_valid"):
        score += 6
    elif item.get("sd_upper_valid") or item.get("sd_lower_valid"):
        score += 3
    if to_text(item.get("trace_stage")).lower() == "confirmed":
        score += 10
    elif to_text(item.get("trace_stage")).lower() == "candidate":
        score += 6
    score += min(8.0, max(0.0, float(to_float(item.get("component_progress")) or 0.0) * 8.0))
    return round(min(100.0, score), 2)


def _scan_reason(item: dict[str, Any], metrics: dict[str, Any], score: float) -> str:
    return (
        f"intraday_window {to_text(item.get('window_status')) or 'active'}, "
        f"rem={to_int(item.get('bars_remaining'), 0)}, "
        f"score={score:.1f}, "
        f"trad={to_int(metrics.get('tradability_score'), 0)}, "
        f"avg10d={round(to_float(metrics.get('avg_10d_volume')) or 0)}, "
        f"atr={round(to_float(metrics.get('atr_pct')) or 0.0, 4)}, "
        f"day={round(to_float(metrics.get('day_change_pct')) or 0.0, 2)}%"
    )


def _target_extra(
    *,
    existing: dict[str, Any],
    item: dict[str, Any],
    metrics: dict[str, Any],
    market_date: str,
    admitted_at_ms: int,
    score: float,
) -> dict[str, Any]:
    existing_extra = parse_json_object(existing.get("extra"))
    existing_source = to_text(existing_extra.get("source"))
    return {
        **existing_extra,
        "source": existing_source or WINDOW_ADMISSION_SOURCE,
        "screener_snapshot": {
            "symbol": to_text(item.get("symbol")).upper(),
            "price": float(to_float(metrics.get("price")) or 0.0),
            "atr_pct": float(to_float(metrics.get("atr_pct")) or 0.0),
            "avg_10d_volume": float(to_float(metrics.get("avg_10d_volume")) or 0.0),
            "premarket_volume": float(to_float(metrics.get("premarket_volume")) or 0.0),
            "today_volume": float(to_float(metrics.get("today_volume")) or 0.0),
            "tradability_score": float(to_float(metrics.get("tradability_score")) or 0.0),
            "operable_reasons": list(metrics.get("operable_reasons") or []),
            "freshness_min": int(metrics.get("freshness_min") or 0),
            "is_operable": True,
        },
        "intraday_window_admission": {
            "source": WINDOW_ADMISSION_SOURCE,
            "market_date": market_date,
            "admitted_at_ms": admitted_at_ms,
            "admission_gate_version": to_text(item.get("admission_gate_version")) or "signal_window_v1",
            "signal_window_gate_passed": bool(item.get("signal_window_gate_passed")),
            "signal_pressure_passed": bool(item.get("signal_pressure_passed")),
            "signal_pressure_keys": list(item.get("signal_pressure_keys") or []),
            "signal_pressure_sides": list(item.get("signal_pressure_sides") or []),
            "admission_trigger_family": to_text(item.get("admission_trigger_family")),
            "window_status": to_text(item.get("window_status")),
            "trace_stage": to_text(item.get("trace_stage")),
            "window_flags": item.get("window_flags") if isinstance(item.get("window_flags"), dict) else {},
            "bars_remaining": to_int(item.get("bars_remaining"), 0),
            "component_progress": float(to_float(item.get("component_progress")) or 0.0),
            "direction_bias": to_text(metrics.get("direction_bias")),
            "admission_score": score,
            "metrics": {
                "price": metrics.get("price"),
                "atr_pct": metrics.get("atr_pct"),
                "avg_10d_volume": metrics.get("avg_10d_volume"),
                "premarket_volume": metrics.get("premarket_volume"),
                "today_volume": metrics.get("today_volume"),
                "day_change_pct": metrics.get("day_change_pct"),
                "freshness_min": metrics.get("freshness_min"),
                "tradability_score": metrics.get("tradability_score"),
            },
        },
        "market_date": market_date,
        "environment": to_text(existing.get("environment")),
    }


def _rejection_summary(rejected: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in rejected:
        reason = to_text(item.get("reason")) or "unknown"
        summary[reason] = summary.get(reason, 0) + 1
    return dict(sorted(summary.items(), key=lambda pair: (-pair[1], pair[0])))


def build_intraday_window_admission_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    time_strings: TimeStrings,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
    config_value: ConfigValue,
    write_system_event_record: WriteSystemEventRecord | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload if isinstance(payload, dict) else {}
    environment = request_market_data_mode(request_payload)
    if environment not in SUPPORTED_ENVIRONMENTS:
        return {"ok": False, "error": "unsupported_environment", "environment": environment, "source": "ibkr-api"}, 400
    data_environment = environment

    interval = normalize_interval(to_text(request_payload.get("interval")) or "5m")
    if interval != "5m":
        return {"ok": False, "error": "unsupported_interval", "interval": interval, "supported_intervals": ["5m"], "source": "ibkr-api"}, 400

    times = time_strings() or {}
    market_date = _current_market_date(
        environment,
        payload=request_payload,
        request_json_request=request_json_request,
        compute_base_url=compute_base_url,
        time_strings=time_strings,
    )
    try:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(market_date)
    except Exception:
        return {"ok": False, "error": "invalid_market_date", "market_date": market_date, "source": "ibkr-api"}, 400

    force = parse_boolean(request_payload.get("force"), False)
    dry_run = parse_boolean(first_defined(request_payload.get("dry_run"), request_payload.get("dryRun")), False)
    start_et = to_text(request_payload.get("start_et") or "09:35")
    end_et = to_text(request_payload.get("end_et") or "15:30")
    if not force and not _is_admission_window(times, start_et=start_et, end_et=end_et):
        return {
            "ok": True,
            "skipped": True,
            "reason": "outside_admission_window",
            "environment": environment,
            "data_environment": data_environment,
            "market_date": market_date,
            "window": {"start_et": start_et, "end_et": end_et, "current_us": to_text(times.get("us"))},
            "source": "ibkr-api",
        }, 200

    max_scan_symbols = max(1, min(500, to_int(request_payload.get("max_scan_symbols"), DEFAULT_MAX_SCAN_SYMBOLS)))
    max_admit_per_run = max(0, min(100, to_int(first_defined(request_payload.get("max_admit"), request_payload.get("limit")), DEFAULT_MAX_ADMIT_PER_RUN)))
    max_freshness_min = _config_int(
        config_value,
        "intraday_window_admission_max_freshness_min",
        TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN,
        environment,
    )
    min_avg_10d_volume = _config_int(
        config_value,
        "intraday_window_admission_min_avg_10d_volume",
        TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME,
        environment,
    )
    min_atr_pct = _config_float(
        config_value,
        "intraday_window_admission_min_atr_pct",
        DEFAULT_MIN_ATR_PCT,
        environment,
    )
    min_premarket_volume = _config_int(
        config_value,
        "intraday_window_admission_min_premarket_volume",
        DEFAULT_MIN_PREMARKET_VOLUME,
        environment,
    )
    min_today_volume = _config_int(
        config_value,
        "intraday_window_admission_min_today_volume",
        DEFAULT_MIN_TODAY_VOLUME,
        environment,
    )
    min_tradability_score = _config_int(
        config_value,
        "intraday_window_admission_min_tradability_score",
        TRADABILITY_OPERABLE_MIN_SCORE,
        environment,
    )

    computed_at_ms = int(time.time() * 1000)
    trade_watchlist, watchlist_monitor_symbols = _load_effective_watchlist(
        pb,
        data_environment,
        escape_filter_string=escape_filter_string,
    )
    configured_monitor_symbols = _configured_monitor_symbols(config_value, environment)
    monitor_symbols = watchlist_monitor_symbols.union(configured_monitor_symbols)
    today_targets = _load_today_targets(pb, data_environment, market_date, escape_filter_string=escape_filter_string)
    active_symbols = {
        symbol
        for symbol, row in today_targets.items()
        if to_text(row.get("status")).lower() == "active"
    }
    candidate_symbols = {
        symbol
        for symbol, row in today_targets.items()
        if to_text(row.get("status")).lower() == "candidate"
    }
    trade_budget = _trade_budget(config_value, environment, len(monitor_symbols))
    budget_remaining = None if trade_budget is None else max(0, int(trade_budget) - len(active_symbols))
    effective_max_admit = max_admit_per_run if budget_remaining is None else min(max_admit_per_run, budget_remaining)

    allowed_candidate_symbols = [
        symbol for symbol in normalize_symbols(request_payload.get("symbols") or list(trade_watchlist.keys()))
        if symbol in trade_watchlist and symbol not in monitor_symbols and symbol not in active_symbols
    ]
    if request_payload.get("include_existing_candidates") is not None and not parse_boolean(request_payload.get("include_existing_candidates"), True):
        allowed_candidate_symbols = [symbol for symbol in allowed_candidate_symbols if symbol not in candidate_symbols]

    fresh_symbols, prefilter_rejections = _symbols_with_fresh_bars(
        pb,
        environment=environment,
        symbols=allowed_candidate_symbols,
        market_start_ms=market_start_ms,
        market_end_ms=market_end_ms,
        computed_at_ms=computed_at_ms,
        max_freshness_min=max_freshness_min,
    )
    trace_symbols = fresh_symbols[:max_scan_symbols]
    skipped_by_scan_limit = max(0, len(fresh_symbols) - len(trace_symbols))
    window_payload = build_active_window_items_for_symbols(
        pb,
        environment=environment,
        symbols=trace_symbols,
        interval=interval,
        market_start_ms=market_start_ms,
        market_end_ms=market_end_ms,
        market_date=market_date,
        computed_at_ms=computed_at_ms,
        target_by_symbol=today_targets,
    )
    window_items = {
        to_text(item.get("symbol")).upper(): item
        for item in window_payload.get("items") or []
        if isinstance(item, dict) and to_text(item.get("symbol")).upper()
    }
    metrics_by_symbol = _build_metrics_by_symbol(
        pb,
        environment=environment,
        symbols=list(window_items.keys()),
        market_date=market_date,
        market_start_ms=market_start_ms,
        market_end_ms=market_end_ms,
        computed_at_ms=computed_at_ms,
        window_items=window_items,
        target_by_symbol=today_targets,
    )

    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = [
        {"symbol": symbol, "reason": reason}
        for symbol, reason in prefilter_rejections.items()
        if symbol in allowed_candidate_symbols
    ]
    for symbol in trace_symbols:
        item = window_items.get(symbol)
        if not item:
            rejected.append({"symbol": symbol, "reason": "trace_missing"})
            continue
        metrics = metrics_by_symbol.get(symbol) or {}
        reason = _reject_reason(
            item,
            metrics,
            min_avg_10d_volume=min_avg_10d_volume,
            min_atr_pct=min_atr_pct,
            min_premarket_volume=min_premarket_volume,
            min_today_volume=min_today_volume,
            min_tradability_score=min_tradability_score,
            max_freshness_min=max_freshness_min,
        )
        if reason:
            rejected.append({
                "symbol": symbol,
                "reason": reason,
                "window_status": to_text(item.get("window_status")),
                "trace_stage": to_text(item.get("trace_stage")),
            })
            continue
        score = _admission_score(item, metrics)
        eligible.append(
            {
                "symbol": symbol,
                "score": score,
                "window_status": to_text(item.get("window_status")),
                "trace_stage": to_text(item.get("trace_stage")),
                "bars_remaining": to_int(item.get("bars_remaining"), 0),
                "direction_bias": normalize_direction_bias(metrics.get("direction_bias"), default="neutral"),
                "metrics": metrics,
                "item": item,
                "existing_status": to_text((today_targets.get(symbol) or {}).get("status")).lower(),
                "scan_reason": _scan_reason(item, metrics, score),
            }
        )

    eligible.sort(
        key=lambda row: (
            -(to_float(row.get("score")) or 0.0),
            -(to_float((row.get("metrics") or {}).get("tradability_score")) or 0.0),
            -to_int(row.get("bars_remaining"), 0),
            to_text(row.get("symbol")),
        )
    )
    selected = eligible[:effective_max_admit]
    overflow = eligible[effective_max_admit:]
    for row in overflow:
        rejected.append({"symbol": row["symbol"], "reason": "admission_budget_deferred", "window_status": row["window_status"]})

    admitted_symbols: list[str] = []
    admitted_items: list[dict[str, Any]] = []
    write_errors: list[dict[str, Any]] = []
    watchlist_sync: list[dict[str, Any]] = []
    if not dry_run and effective_max_admit > 0:
        for row in selected:
            symbol = row["symbol"]
            item = row["item"]
            metrics = row["metrics"]
            existing = today_targets.get(symbol) or {}
            score = to_float(row.get("score")) or 0.0
            extra = _target_extra(
                existing={**existing, "environment": data_environment},
                item=item,
                metrics=metrics,
                market_date=market_date,
                admitted_at_ms=computed_at_ms,
                score=score,
            )
            row_payload = {
                "symbol": symbol,
                "environment": data_environment,
                "exchange": to_text(first_defined(metrics.get("exchange"), (trade_watchlist.get(symbol) or {}).get("exchange"), item.get("exchange"), "SMART")).upper(),
                "date": market_date,
                "direction_bias": normalize_direction_bias(row.get("direction_bias"), default="neutral"),
                "score": score,
                "scan_reason": row["scan_reason"],
                "status": "active",
                "us_time": to_text(item.get("latest_us_time") or times.get("us")),
                "cn_time": to_text(item.get("latest_cn_time") or format_cn_time(to_int(item.get("latest_bar_time_ms"), 0)) or times.get("cn")),
                "bar_time_ms": to_int(item.get("latest_bar_time_ms"), computed_at_ms),
                "extra": extra,
            }
            filter_expr = (
                f'symbol = "{escape_filter_string(symbol)}" && '
                f'date = "{escape_filter_string(market_date)}" && '
                f'environment = "{escape_filter_string(data_environment)}"'
            )
            try:
                result = upsert_record(
                    pb,
                    "ibkr_targets",
                    filter_expr=filter_expr,
                    data=row_payload,
                    compare_fields=["status", "direction_bias", "score", "scan_reason", "extra"],
                )
                sync = ensure_target_watchlist_record(
                    pb,
                    symbol=symbol,
                    environment=data_environment,
                    exchange=row_payload["exchange"],
                    industry=to_text((trade_watchlist.get(symbol) or {}).get("industry")),
                    escape_filter_string=escape_filter_string,
                    time_strings=time_strings,
                )
                watchlist_sync.append({"symbol": symbol, **sync})
                admitted_symbols.append(symbol)
                admitted_items.append(
                    {
                        "symbol": symbol,
                        "action": to_text(result.get("action")) or "updated",
                        "score": score,
                        "direction_bias": row_payload["direction_bias"],
                        "window_status": row["window_status"],
                        "trace_stage": row["trace_stage"],
                        "bars_remaining": row["bars_remaining"],
                        "scan_reason": row["scan_reason"],
                        "existing_status": row.get("existing_status"),
                    }
                )
            except Exception as exc:
                write_errors.append({"symbol": symbol, "error": str(exc)})

    runtime_reconcile: dict[str, Any] | None = None
    reconcile_error = ""
    if admitted_symbols and not dry_run:
        try:
            reconcile = call_universe_reconcile(
                environment,
                {
                    "source": WINDOW_ADMISSION_SOURCE,
                    "reason": "intraday_window_admission",
                    "prime_symbols": admitted_symbols,
                    "emit_signals": True,
                },
                request_json_request=request_json_request,
                compute_base_url=compute_base_url,
            )
            runtime_payload = dict(reconcile.get("payload") or {})
            runtime_reconcile = {
                **runtime_payload,
                "proxy_upstream": reconcile.get("upstream") or "",
                "status_code": int(reconcile.get("statusCode") or 200),
            }
            if int(runtime_reconcile.get("status_code") or 200) >= 400 or runtime_payload.get("ok") is False:
                reconcile_error = to_text(runtime_payload.get("error")) or "runtime_reconcile_failed"
        except Exception as exc:
            reconcile_error = str(exc)
            runtime_reconcile = {"ok": False, "error": reconcile_error}

    if admitted_symbols and not dry_run and callable(write_system_event_record):
        try:
            write_system_event_record(
                "intraday_window_admission",
                "info",
                "ibkr_api",
                "IBKR 信号窗口入池",
                {
                    "market_date": market_date,
                    "admitted": len(admitted_symbols),
                    "symbols": ",".join(admitted_symbols[:20]),
                    "dry_run": dry_run,
                },
                environment,
                False,
            )
        except Exception:
            pass

    ok = not write_errors and not reconcile_error
    status_code = 200 if ok else 502
    response = {
        "ok": ok,
        "environment": environment,
        "data_environment": data_environment,
        "market_date": market_date,
        "job_id": "ibkr_intraday_window_admission",
        "dry_run": dry_run,
        "computed_at_ms": computed_at_ms,
        "computed_at_us": to_text(times.get("us")),
        "computed_at_cn": to_text(times.get("cn")),
        "budget": {
            "trade_budget": trade_budget,
            "monitor_count": len(monitor_symbols),
            "active_targets": len(active_symbols),
            "candidate_targets": len(candidate_symbols),
            "remaining": budget_remaining,
            "max_admit_per_run": max_admit_per_run,
            "effective_max_admit": effective_max_admit,
        },
        "thresholds": {
            "max_freshness_min": max_freshness_min,
            "min_avg_10d_volume": min_avg_10d_volume,
            "min_atr_pct": min_atr_pct,
            "min_premarket_volume": min_premarket_volume,
            "min_today_volume": min_today_volume,
            "min_tradability_score": min_tradability_score,
        },
        "scanned": len(trace_symbols),
        "source_symbols": len(allowed_candidate_symbols),
        "fresh_symbols": len(fresh_symbols),
        "skipped_by_scan_limit": skipped_by_scan_limit,
        "eligible": len(eligible),
        "would_admit": len(selected),
        "admitted": len(admitted_symbols),
        "admitted_symbols": admitted_symbols,
        "admitted_items": admitted_items if not dry_run else [
            {
                "symbol": row["symbol"],
                "score": row["score"],
                "direction_bias": row["direction_bias"],
                "window_status": row["window_status"],
                "trace_stage": row["trace_stage"],
                "bars_remaining": row["bars_remaining"],
                "scan_reason": row["scan_reason"],
                "existing_status": row.get("existing_status"),
            }
            for row in selected
        ],
        "rejected": len(rejected),
        "rejection_summary": _rejection_summary(rejected),
        "rejected_examples": rejected[:20],
        "write_errors": write_errors,
        "watchlist_sync": watchlist_sync,
        "runtime_reconcile": runtime_reconcile,
        "error": reconcile_error or (write_errors[0]["error"] if write_errors else ""),
        "source": "ibkr-api",
    }
    if effective_max_admit <= 0 and not selected:
        response["skipped"] = True
        response["reason"] = "subscription_budget_full" if budget_remaining == 0 else "max_admit_zero"
    elif not selected:
        response["skipped"] = True
        response["reason"] = "no_eligible_active_windows"
    return response, status_code


__all__ = ["build_intraday_window_admission_response"]
