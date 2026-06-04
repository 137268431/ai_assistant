from __future__ import annotations

import time
from typing import Any, Callable

from ibkr_api.orders.values import first_defined, to_float, to_int, to_text
from ibkr_api.universe.maintenance import parse_json_object
from ibkr_api.universe.today_targets_shared import (
    LIVE_ENVIRONMENT,
    TODAY_TARGET_STATUSES,
    build_bar_environment_filter,
    build_daily_change_fields,
    classify_session,
    current_market_date,
    escape_filter,
    format_cn_time,
    format_et_date,
    format_et_datetime,
    indicator_snapshot,
    interval_to_chart_tf,
    load_daily_scan_state,
    load_records_for_symbols,
    load_watch_meta,
    normalize_signal_record,
    parse_et_datetime_ms,
    pick_latest_signal,
    pick_reason_list,
    effective_target_status,
    signal_status_is_open,
    signal_status_is_terminal,
    target_extra_deactivated_after_close,
)
from ibkr_api.universe.today_targets_workflow import (
    build_aligned_technical_flags,
    build_filtered_summary,
    build_ready_explanation,
    build_technical_flags,
    build_workflow_guide,
    ensure_row_details,
    matches_filters,
    normalize_filters,
    resolve_attention_state,
    resolve_technical_state,
    sort_rows,
)
from ibkr_compute.core.broker_mode import normalize_broker_mode, resolve_data_environment
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms
from ibkr_compute.api.market.screener.scoring import (
    TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN,
    TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME,
    TRADABILITY_OPERABLE_MIN_SCORE,
    build_tradability_assessment,
)
from ibkr_compute.universe.target_execution import build_target_execution_metadata


NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
TV_SD_TOUCH_BASIS = "tv_pre_alert_window_activation"
SUBMITTED_SIGNAL_STATUSES = {"submitted", "submitted_waiting_fill"}
PROTECTED_ACTIVE_SIGNAL_STATUSES = {"protected_active", "filled_repricing_protection", "filled_position"}
PROTECTION_INCOMPLETE_SIGNAL_STATUSES = {"protection_incomplete", "protection_reprice_failed"}
ENTRY_MISSED_SIGNAL_STATUSES = {"entry_missed_limit_cap"}


def _empty_tv_sd_touch_summary() -> dict[str, Any]:
    return {
        "tv_sd_upper_touch_count": 0,
        "tv_sd_lower_touch_count": 0,
        "tv_sd_touch_count": 0,
        "tv_sd_touch_interval_label": "TV",
        "tv_sd_touch_basis": TV_SD_TOUCH_BASIS,
    }


def _tv_sd_payload_parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    payload = parse_json_object(row.get("payload"))
    extra = parse_json_object(row.get("extra"))
    payload_extra = parse_json_object(payload.get("extra")) if isinstance(payload, dict) else {}
    return [dict(row or {}), payload, extra, payload_extra]


def _first_tv_sd_value(row: dict[str, Any], *keys: str) -> Any:
    for source in _tv_sd_payload_parts(row):
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _first_tv_sd_text(row: dict[str, Any], *keys: str) -> str:
    return to_text(_first_tv_sd_value(row, *keys))


def _normalize_tv_interval_label(value: Any) -> str:
    text = to_text(value).strip()
    if not text:
        return ""
    lowered = text.lower()
    if "entry=" in lowered:
        for part in text.split(";"):
            key, _, raw_value = part.partition("=")
            if key.strip().lower() == "entry":
                text = raw_value.strip()
                lowered = text.lower()
                break
    if lowered.startswith("chart="):
        text = text.split("=", 1)[1].strip()
        lowered = text.lower()
    if lowered.endswith("min"):
        lowered = lowered[:-3].strip() + "m"
    if lowered.isdigit():
        return f"{int(lowered)}m"
    if lowered.endswith("m") and lowered[:-1].isdigit():
        return f"{int(lowered[:-1])}m"
    if lowered in {"d", "1d"}:
        return "1d"
    return lowered


def _tv_sd_touch_side(row: dict[str, Any]) -> str:
    event_type = _first_tv_sd_text(row, "event_type").lower()
    if event_type and event_type != "pre_alert":
        return ""
    stage = _first_tv_sd_text(row, "pre_alert_stage").lower()
    activation_window = _first_tv_sd_text(row, "activation_window", "sd_activation_window", "window").lower()
    if stage and stage not in {"window_activation", "sd_window_activation"}:
        return ""
    if activation_window in {"upper", "sd_upper", "window_upper", "upper_window"}:
        return "upper"
    if activation_window in {"lower", "sd_lower", "window_lower", "lower_window"}:
        return "lower"
    fallback_text = " ".join(
        _first_tv_sd_text(row, key).lower()
        for key in ("event_id", "position_id", "signal_id", "reason")
    )
    if any(token in fallback_text for token in ("window_upper", "upper_window", "pending_window_upper")):
        return "upper"
    if any(token in fallback_text for token in ("window_lower", "lower_window", "pending_window_lower")):
        return "lower"
    return ""


def _tv_sd_touch_time_ms(row: dict[str, Any]) -> int:
    return to_int(
        _first_tv_sd_value(
            row,
            "bar_time_ms",
            "bar_open_ms",
            "last_bar_time_ms",
            "first_bar_time_ms",
        ),
        0,
    )


def _tv_sd_touch_item(row: dict[str, Any], side: str) -> dict[str, Any]:
    bar_time_ms = _tv_sd_touch_time_ms(row)
    interval = _normalize_tv_interval_label(
        _first_tv_sd_value(row, "interval", "entry_tf", "chart_tf", "timeframe", "timeframe_stack")
    )
    return {
        "side": side,
        "interval": interval,
        "event_id": _first_tv_sd_text(row, "event_id", "tv_event_id"),
        "bar_time_ms": bar_time_ms,
        "us_time": _first_tv_sd_text(row, "us_time", "time", "timestamp") or (format_et_datetime(bar_time_ms) if bar_time_ms > 0 else ""),
    }


def _build_tv_sd_touch_payload(
    pb: Any,
    *,
    data_environment: str,
    market_date: str,
    market_start_ms: int,
    market_end_ms: int,
    symbols: list[str],
) -> dict[str, Any]:
    symbol_set = {to_text(symbol).upper() for symbol in symbols if to_text(symbol)}
    if not symbol_set:
        return {"by_symbol": {}, "summary": _empty_tv_sd_touch_summary()}
    records = load_records_for_symbols(
        pb,
        "tv_webhook_events",
        base_filter_parts=[
            f'environment = "{escape_filter(data_environment)}"',
            f'date = "{escape_filter(market_date)}"',
            'event_type = "pre_alert"',
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=list(symbol_set),
        sort="-bar_time_ms,-updated",
        max_pages=20,
    )
    latest_by_symbol: dict[str, dict[str, Any]] = {}
    latest_sort_key: dict[str, tuple[int, str, str, str]] = {}
    for raw_row in records:
        row = dict(raw_row or {})
        symbol = _first_tv_sd_text(row, "symbol").upper()
        if not symbol or symbol not in symbol_set:
            continue
        event_date = _first_tv_sd_text(row, "date", "market_date")
        if event_date and event_date != market_date:
            continue
        side = _tv_sd_touch_side(row)
        if side not in {"upper", "lower"}:
            continue
        bar_time_ms = _tv_sd_touch_time_ms(row)
        if bar_time_ms > 0 and not (market_start_ms <= bar_time_ms < market_end_ms):
            continue
        item = _tv_sd_touch_item(row, side)
        sort_key = (
            to_int(item.get("bar_time_ms"), 0),
            to_text(row.get("updated")),
            to_text(row.get("created")),
            to_text(item.get("event_id")),
        )
        if sort_key >= latest_sort_key.get(symbol, (0, "", "", "")):
            latest_sort_key[symbol] = sort_key
            latest_by_symbol[symbol] = item

    upper_count = sum(1 for item in latest_by_symbol.values() if item.get("side") == "upper")
    lower_count = sum(1 for item in latest_by_symbol.values() if item.get("side") == "lower")
    intervals = sorted({to_text(item.get("interval")) for item in latest_by_symbol.values() if to_text(item.get("interval"))})
    interval_label = "TV"
    if len(intervals) == 1:
        interval_label = f"TV {intervals[0]}"
    elif len(intervals) > 1:
        interval_label = "TV mixed"
    return {
        "by_symbol": latest_by_symbol,
        "summary": {
            "tv_sd_upper_touch_count": upper_count,
            "tv_sd_lower_touch_count": lower_count,
            "tv_sd_touch_count": upper_count + lower_count,
            "tv_sd_touch_interval_label": interval_label,
            "tv_sd_touch_basis": TV_SD_TOUCH_BASIS,
        },
    }


def build_today_targets_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
) -> tuple[dict[str, Any], int]:
    data_environment = resolve_data_environment(
        payload.get("market_data_mode") or payload.get("data_environment") or payload.get("environment")
    )
    runtime_environment = data_environment
    broker_default = data_environment if data_environment in {"live", "paper"} else "paper"
    broker_mode = normalize_broker_mode(
        first_defined(payload.get("broker_mode"), payload.get("brokerMode"), payload.get("runtime_environment"), broker_default),
        broker_default,
    )
    current_date = current_market_date(time_strings)
    requested_market_date = to_text(first_defined(payload.get("marketDate"), payload.get("market_date"), payload.get("date"))) or current_date
    try:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(requested_market_date)
        market_date = requested_market_date
    except Exception:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(current_date)
        market_date = current_date
    filters = normalize_filters(payload)
    workflow_guide = build_workflow_guide(runtime_environment, market_date)
    paginate = bool(payload.get("paginate"))
    requested_per_page = max(1, min(200, to_int(first_defined(payload.get("per_page"), payload.get("perPage")), 10)))
    requested_page = max(1, to_int(payload.get("page"), 1)) if paginate else 1
    computed_at_ms = int(time.time() * 1000)
    daily_scan = load_daily_scan_state(pb, data_environment)

    target_rows = pb.get_records(
        "ibkr_targets",
        filter=(
            f'environment = "{escape_filter(data_environment)}" && '
            f'date = "{escape_filter(market_date)}" && '
            '(status = "candidate" || status = "active")'
        ),
        sort="-updated",
        per_page=500,
        page=1,
    )
    target_by_symbol: dict[str, dict[str, Any]] = {}
    ordered_symbols: list[str] = []
    for row in target_rows or []:
        if not isinstance(row, dict):
            continue
        symbol = to_text(row.get("symbol")).upper()
        if not symbol or symbol in target_by_symbol:
            continue
        target_by_symbol[symbol] = dict(row)
        ordered_symbols.append(symbol)

    if not ordered_symbols:
        return {
            "ok": True,
            "environment": runtime_environment,
            "broker_mode": broker_mode,
            "data_environment": data_environment,
            "market_date": market_date,
            "current_market_date": current_date,
            "computed_at_ms": computed_at_ms,
            "computed_at_us": format_et_datetime(computed_at_ms),
            "computed_at_cn": format_cn_time(computed_at_ms),
            "workflow": workflow_guide,
            "daily_scan": daily_scan,
            "summary": {
                "total": 0,
                "active_count": 0,
                "candidate_count": 0,
                "operable_count": 0,
                "technical_ready_count": 0,
                "signaled_count": 0,
                "awaiting_confirm_count": 0,
                "pending_count": 0,
                "submitted_count": 0,
                "protected_active_count": 0,
                "protection_incomplete_count": 0,
                "executed_count": 0,
                "entry_missed_count": 0,
                "missed_count": 0,
                "stale_count": 0,
                "execution_eligible_count": 0,
                "observe_only_count": 0,
                "watch_only_count": 0,
                **_empty_tv_sd_touch_summary(),
            },
            "filters": filters,
            "filtered_summary": {"total": 0, "ready_count": 0, "signaled_count": 0, "needs_action_count": 0},
            "filtered_total": 0,
            "pagination_enabled": paginate,
            "page": 1,
            "per_page": requested_per_page if paginate else 0,
            "total_pages": 1,
            "has_prev_page": False,
            "has_next_page": False,
            "returned_count": 0,
            "items": [],
            "source": "ibkr-api",
        }, 200

    watch_meta = load_watch_meta(pb, runtime_environment, ordered_symbols)
    lookback_daily_ms = market_start_ms - 20 * 24 * 60 * 60 * 1000
    indicator_lookback_ms = market_start_ms

    daily_records = load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "1d"',
            build_bar_environment_filter(runtime_environment),
            f"bar_time_ms >= {lookback_daily_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="bar_time_ms",
        max_pages=12,
    )
    intraday_records = load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "5m"',
            build_bar_environment_filter(runtime_environment),
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="bar_time_ms",
        max_pages=30,
    )
    indicator_records = load_records_for_symbols(
        pb,
        "ibkr_indicators",
        base_filter_parts=[
            f'interval = "{interval_to_chart_tf("5m")}"',
            f'environment = "{escape_filter(data_environment)}"',
            f"bar_time_ms >= {indicator_lookback_ms}",
        ],
        symbols=ordered_symbols,
        sort="-bar_time_ms",
        max_pages=12,
    )
    signal_records = load_records_for_symbols(
        pb,
        "ibkr_signals",
        base_filter_parts=[
            f'environment = "{escape_filter(data_environment)}"',
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="-bar_time_ms",
        max_pages=12,
    )
    tv_sd_touch_payload = _build_tv_sd_touch_payload(
        pb,
        data_environment=data_environment,
        market_date=market_date,
        market_start_ms=market_start_ms,
        market_end_ms=market_end_ms,
        symbols=ordered_symbols,
    )
    tv_sd_touch_by_symbol = tv_sd_touch_payload.get("by_symbol") if isinstance(tv_sd_touch_payload.get("by_symbol"), dict) else {}
    tv_sd_touch_summary = tv_sd_touch_payload.get("summary") if isinstance(tv_sd_touch_payload.get("summary"), dict) else _empty_tv_sd_touch_summary()

    daily_history_by_symbol: dict[str, list[dict[str, Any]]] = {}
    fallback_daily_by_symbol: dict[str, dict[str, Any]] = {}
    for row in daily_records:
        symbol = to_text(row.get("symbol")).upper()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        close = to_float(row.get("close")) or 0.0
        if not symbol or bar_time_ms <= 0 or close <= 0:
            continue
        payload_row = {
            "bar_time_ms": bar_time_ms,
            "close": close,
            "volume": to_float(row.get("volume")) or 0.0,
            "us_time": to_text(row.get("us_time")),
            "date": format_et_date(bar_time_ms),
        }
        daily_history_by_symbol.setdefault(symbol, []).append(payload_row)
        if payload_row["date"] < market_date:
            fallback_daily_by_symbol[symbol] = payload_row

    latest_intraday_by_symbol: dict[str, dict[str, Any]] = {}
    volume_stats_by_symbol: dict[str, dict[str, float]] = {}
    for row in intraday_records:
        symbol = to_text(row.get("symbol")).upper()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        if not symbol or bar_time_ms <= 0:
            continue
        payload_row = {
            "bar_time_ms": bar_time_ms,
            "close": to_float(row.get("close")) or 0.0,
            "exchange": to_text(row.get("exchange")).upper(),
            "session_type": to_text(row.get("session_type")).lower() or classify_session(bar_time_ms=bar_time_ms),
            "us_time": to_text(row.get("us_time")),
            "volume": to_float(row.get("volume")) or 0.0,
        }
        current_latest = latest_intraday_by_symbol.get(symbol)
        if current_latest is None or payload_row["bar_time_ms"] >= current_latest["bar_time_ms"]:
            latest_intraday_by_symbol[symbol] = payload_row
        stats = volume_stats_by_symbol.setdefault(symbol, {"premarket": 0.0, "today": 0.0})
        stats["today"] += payload_row["volume"]
        if payload_row["session_type"] == "premarket":
            stats["premarket"] += payload_row["volume"]

    latest_indicator_by_symbol: dict[str, dict[str, Any]] = {}
    for row in indicator_records:
        symbol = to_text(row.get("symbol")).upper()
        if not symbol or symbol in latest_indicator_by_symbol:
            continue
        latest_indicator_by_symbol[symbol] = row

    signal_agg_by_symbol: dict[str, dict[str, Any]] = {}
    for row in signal_records:
        normalized_signal = normalize_signal_record(row, broker_mode=broker_mode)
        symbol = normalized_signal.get("symbol")
        if not symbol:
            continue
        bucket = signal_agg_by_symbol.setdefault(symbol, {"count": 0, "latest": None})
        bucket["count"] += 1
        if signal_status_is_open(normalized_signal.get("status")):
            bucket["has_open"] = True
        bucket["latest"] = pick_latest_signal(bucket.get("latest"), normalized_signal)

    items: list[dict[str, Any]] = []
    active_count = 0
    candidate_count = 0
    operable_count = 0
    technical_ready_count = 0
    signaled_count = 0
    awaiting_confirm_count = 0
    pending_count = 0
    submitted_count = 0
    protected_active_count = 0
    protection_incomplete_count = 0
    executed_count = 0
    entry_missed_count = 0
    stale_count = 0
    execution_eligible_count = 0
    observe_only_count = 0
    watch_only_count = 0

    for symbol in ordered_symbols:
        target = target_by_symbol.get(symbol)
        if not target:
            continue
        target_extra = parse_json_object(target.get("extra"))
        screener_snapshot = parse_json_object(target_extra.get("screener_snapshot"))
        meta = watch_meta.get(symbol, {})
        intraday = latest_intraday_by_symbol.get(symbol)
        fallback_daily = fallback_daily_by_symbol.get(symbol)
        indicator_record = latest_indicator_by_symbol.get(symbol)
        indicator_extra = indicator_snapshot(indicator_record)
        signal_agg = signal_agg_by_symbol.get(symbol, {"count": 0, "latest": None})
        latest_signal = signal_agg.get("latest")
        has_open_signal = bool(signal_agg.get("has_open"))
        history = [row for row in daily_history_by_symbol.get(symbol, []) if row.get("date") < market_date]
        last_10 = history[-10:]
        avg_10d_volume = round(sum(to_float(row.get("volume")) or 0.0 for row in last_10) / len(last_10), 2) if last_10 else 0.0
        price = to_float((intraday or {}).get("close")) or 0.0
        price_source = "5m"
        if price <= 0:
            price = to_float((fallback_daily or {}).get("close")) or 0.0
            price_source = "1d_close" if price > 0 else ""
        compare_history = build_daily_change_fields(history, price) if price > 0 else {
            "day_change_pct": 0.0,
            "prev_close_change_pct": 0.0,
            "change_7d": 0.0,
        }
        latest_signal_status = to_text((latest_signal or {}).get("status"))
        stored_target_status = to_text(target.get("status")).lower()
        target_status = effective_target_status(target, latest_signal_status, has_open_signal=has_open_signal)
        target_status_reason = ""
        if stored_target_status == "active" and target_status == "candidate":
            if signal_status_is_terminal(latest_signal_status) and not has_open_signal:
                target_status_reason = "latest_signal_terminal"
            elif target_extra_deactivated_after_close(target_extra):
                target_status_reason = "deactivated_after_close"
            else:
                target_status_reason = "active_gate_not_effective"
        direction_bias = to_text(first_defined(target.get("direction_bias"), "neutral")).lower() or "neutral"
        execution_meta = build_target_execution_metadata(
            target_extra,
            direction_bias=direction_bias,
            status=target_status,
        )
        target_extra = {**target_extra, **execution_meta}
        score = round(to_float(target.get("score")) or 0.0, 2)
        scan_reason = to_text(target.get("scan_reason"))
        intraday_bar_time_ms = to_int((intraday or {}).get("bar_time_ms"), 0)
        latest_bar_time_ms = intraday_bar_time_ms or to_int((fallback_daily or {}).get("bar_time_ms"), 0)
        freshness_min = max(0, int((computed_at_ms - intraday_bar_time_ms) // 60000)) if intraday_bar_time_ms > 0 else None
        volume_stats = volume_stats_by_symbol.get(symbol, {"premarket": 0.0, "today": 0.0})
        row = {
            "symbol": symbol,
            "record_id": to_text(target.get("id")),
            "status": target_status,
            "target_status": target_status,
            "stored_target_status": stored_target_status,
            "target_status_reason": target_status_reason,
            "direction_bias": direction_bias,
            "score": score,
            "target_score": score,
            "scan_reason": scan_reason,
            "exchange": to_text(first_defined(meta.get("exchange"), (intraday or {}).get("exchange"), target.get("exchange"))).upper(),
            "industry": to_text(meta.get("industry")),
            "note": to_text(meta.get("note")),
            "price": round(price, 4) if price > 0 else 0.0,
            "price_source": price_source,
            "atr_pct": round(to_float(first_defined(indicator_extra.get("atr_pct"), target_extra.get("atr_pct"))) or 0.0, 2),
            "avg_10d_volume": avg_10d_volume,
            "premarket_volume": round(to_float(first_defined(volume_stats.get("premarket"), screener_snapshot.get("premarket_volume"))) or 0.0, 2),
            "today_volume": round(to_float(first_defined(volume_stats.get("today"), screener_snapshot.get("today_volume"))) or 0.0, 2),
            "latest_bar_time_ms": latest_bar_time_ms,
            "latest_intraday_bar_time_ms": intraday_bar_time_ms,
            "latest_us_time": to_text((intraday or {}).get("us_time")) or to_text(target.get("us_time")) or to_text((fallback_daily or {}).get("us_time")),
            "freshness_min": freshness_min,
            "has_live_bar": intraday_bar_time_ms > 0,
            "day_change_pct": compare_history["day_change_pct"],
            "prev_close_change_pct": compare_history["prev_close_change_pct"],
            "change_7d": compare_history["change_7d"],
            "extra": target_extra,
            "updated": to_text(target.get("updated")),
            "execution_eligible": bool(execution_meta.get("execution_eligible")),
            "is_execution_eligible": bool(execution_meta.get("execution_eligible")),
            "execution_blockers": list(execution_meta.get("execution_blockers") or []),
            "target_layer": to_text(execution_meta.get("target_layer")),
            "execution_allowed_sides": list(execution_meta.get("execution_allowed_sides") or []),
            "context_allowed_sides": list(execution_meta.get("context_allowed_sides") or []),
            "subscription_selected": bool(target_extra.get("subscription_selected")),
            "within_subscription_budget": bool(target_extra.get("within_subscription_budget")),
            "tv_sd_touch": dict(tv_sd_touch_by_symbol.get(symbol) or {}),
        }
        tradability_score, assessment_notes = build_tradability_assessment(row)
        row["tradability_score"] = tradability_score
        row["operable_reasons"] = pick_reason_list(assessment_notes, screener_snapshot.get("operable_reasons") if isinstance(screener_snapshot.get("operable_reasons"), list) else [])
        row["is_operable"] = bool(
            row["has_live_bar"]
            and (to_float(row.get("price")) or 0.0) > 0
            and (to_float(row.get("avg_10d_volume")) or 0.0) >= TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME
            and (to_float(row.get("tradability_score")) or 0.0) >= TRADABILITY_OPERABLE_MIN_SCORE
            and isinstance(row.get("freshness_min"), int)
            and row["freshness_min"] <= TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN
        )
        row["technical_flags"] = build_technical_flags(indicator_extra)
        row["technical_aligned_flags"] = build_aligned_technical_flags(indicator_extra, direction_bias)
        row["technical_state"] = resolve_technical_state(row, row["technical_aligned_flags"])
        row["ready_explanation"] = build_ready_explanation(row)
        row["has_signal_today"] = bool(signal_agg.get("count"))
        row["has_open_signal_today"] = has_open_signal
        row["signal_count_today"] = int(signal_agg.get("count") or 0)
        row["latest_signal_id"] = to_text((latest_signal or {}).get("signal_id"))
        row["latest_signal_status"] = latest_signal_status
        row["latest_signal_status_reason"] = to_text((latest_signal or {}).get("status_reason"))
        row["latest_signal_status_reason_human"] = to_text((latest_signal or {}).get("status_reason_human"))
        row["latest_signal_effective_broker_mode"] = to_text((latest_signal or {}).get("effective_broker_mode")) or broker_mode
        row["latest_signal_direction"] = to_text((latest_signal or {}).get("direction"))
        row["latest_signal_time"] = to_text((latest_signal or {}).get("us_time"))
        row["latest_signal_time_ms"] = to_int((latest_signal or {}).get("sort_ms"), 0)
        row["latest_signal_note"] = to_text((latest_signal or {}).get("note"))
        attention_state, attention_rank = resolve_attention_state(row)
        row["attention_state"] = attention_state
        row["attention_rank"] = attention_rank

        if target_status == "active":
            active_count += 1
        if target_status == "candidate":
            candidate_count += 1
        if row["is_operable"]:
            operable_count += 1
        if row["technical_state"] == "ready":
            technical_ready_count += 1
        if row["technical_state"] == "stale":
            stale_count += 1
        if row["has_signal_today"]:
            signaled_count += 1
        if row["latest_signal_status"] == "awaiting_confirm":
            awaiting_confirm_count += 1
        if row["latest_signal_status"] == "pending":
            pending_count += 1
        if row["latest_signal_status"] in SUBMITTED_SIGNAL_STATUSES:
            submitted_count += 1
        if row["latest_signal_status"] in PROTECTED_ACTIVE_SIGNAL_STATUSES:
            protected_active_count += 1
        if row["latest_signal_status"] in PROTECTION_INCOMPLETE_SIGNAL_STATUSES:
            protection_incomplete_count += 1
        if row["latest_signal_status"] == "executed":
            executed_count += 1
        if row["latest_signal_status"] in ENTRY_MISSED_SIGNAL_STATUSES:
            entry_missed_count += 1
        if row["execution_eligible"]:
            execution_eligible_count += 1
        else:
            observe_only_count += 1
        if "watch_only" in row["execution_blockers"]:
            watch_only_count += 1
        items.append(row)

    filtered_items = [row for row in sort_rows(items, to_text(filters.get("sort_by"))) if matches_filters(row, filters)]
    filtered_summary = build_filtered_summary(filtered_items)
    total_pages = max(1, (len(filtered_items) + requested_per_page - 1) // requested_per_page) if paginate else 1
    page = min(requested_page, total_pages) if paginate else 1
    offset = (page - 1) * requested_per_page if paginate else 0
    paged_items = filtered_items[offset:offset + requested_per_page] if paginate else filtered_items
    for row in paged_items:
        ensure_row_details(row)

    return {
        "ok": True,
        "environment": runtime_environment,
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "market_date": market_date,
        "current_market_date": current_date,
        "computed_at_ms": computed_at_ms,
        "computed_at_us": format_et_datetime(computed_at_ms),
        "computed_at_cn": format_cn_time(computed_at_ms),
        "workflow": workflow_guide,
        "daily_scan": daily_scan,
        "summary": {
            "total": len(items),
            "active_count": active_count,
            "candidate_count": candidate_count,
            "operable_count": operable_count,
            "technical_ready_count": technical_ready_count,
            "signaled_count": signaled_count,
            "awaiting_confirm_count": awaiting_confirm_count,
            "pending_count": pending_count,
            "submitted_count": submitted_count,
            "protected_active_count": protected_active_count,
            "protection_incomplete_count": protection_incomplete_count,
            "executed_count": executed_count,
            "entry_missed_count": entry_missed_count,
            "missed_count": entry_missed_count,
            "stale_count": stale_count,
            "execution_eligible_count": execution_eligible_count,
            "observe_only_count": observe_only_count,
            "watch_only_count": watch_only_count,
            **tv_sd_touch_summary,
        },
        "filters": filters,
        "filtered_summary": filtered_summary,
        "filtered_total": len(filtered_items),
        "pagination_enabled": paginate,
        "page": page,
        "per_page": requested_per_page if paginate else len(filtered_items),
        "total_pages": total_pages,
        "has_prev_page": page > 1 if paginate else False,
        "has_next_page": page < total_pages if paginate else False,
        "returned_count": len(paged_items),
        "items": paged_items,
        "source": "ibkr-api",
    }, 200


__all__ = ["TODAY_TARGET_STATUSES", "build_today_targets_response"]
