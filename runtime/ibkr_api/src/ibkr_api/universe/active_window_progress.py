from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlencode

from ibkr_api.orders.values import first_defined, to_float, to_int, to_text
from ibkr_api.universe.maintenance import parse_json_object
from ibkr_api.universe.today_targets_shared import (
    LIVE_ENVIRONMENT,
    TODAY_TARGET_STATUSES,
    build_bar_environment_filter,
    classify_session,
    current_market_date,
    escape_filter,
    format_cn_time,
    format_et_datetime,
    indicator_snapshot,
    interval_to_chart_tf,
    load_records_for_symbols,
    normalize_signal_record,
    pick_latest_signal,
)
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms
from ibkr_compute.core.timeline_builder import build_runtime_timeline
from ibkr_compute.market.timeframe_utils import normalize_interval


NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]

DEFAULT_SIGNAL_WINDOW_MAX_BARS = 12
DEFAULT_LIMIT = 80
TIMELINE_WARMUP_BARS = 300
TIMELINE_TODAY_MAX_PAGES = 4
TIMELINE_WARMUP_MAX_PAGES = 2
SUPPORTED_ENVIRONMENTS = {"live", "paper"}
SUPPORTED_STATUSES = {"active", "candidate", "all"}

COMPONENT_LABELS = {
    "sd_upper_bull_touch_seen": "上轨 EMA 多头触及",
    "sd_upper_bull_fractal_seen": "上轨多头分形",
    "sd_upper_bear_fractal_seen": "上轨空头分形",
    "sd_lower_bull_fractal_seen": "下轨多头分形",
    "sd_lower_bear_touch_seen": "下轨 EMA 空头触及",
    "sd_lower_bear_fractal_seen": "下轨空头分形",
    "bull_div_seen": "多头背离(cRSI/OBV)",
    "bear_div_seen": "空头背离(cRSI/OBV)",
}

COMPONENT_GROUPS = {
    "type1_long_trend": {
        "label": "Type1 顺势多",
        "direction": "long",
        "signal": "trend_sdUpper",
        "window": "upper",
        "required": ["sd_upper_bull_touch_seen", "sd_upper_bull_fractal_seen", "bull_div_seen"],
    },
    "type2_long_mr": {
        "label": "Type2 回归多",
        "direction": "long",
        "signal": "mr_sdLower",
        "window": "lower",
        "required": ["sd_lower_bull_fractal_seen", "bull_div_seen"],
    },
    "type3_short_mr": {
        "label": "Type3 回归空",
        "direction": "short",
        "signal": "mr_sdUpper",
        "window": "upper",
        "required": ["sd_upper_bear_fractal_seen", "bear_div_seen"],
    },
    "type4_short_trend": {
        "label": "Type4 顺势空",
        "direction": "short",
        "signal": "trend_sdLower",
        "window": "lower",
        "required": ["sd_lower_bear_touch_seen", "sd_lower_bear_fractal_seen", "bear_div_seen"],
    },
}


def _resolve_signal_window_max_bars(pb: Any, environment: str) -> int:
    getter = getattr(pb, "get_runtime_config", None)
    if callable(getter):
        try:
            rows = getter(scope="all", environment=environment)
        except TypeError:
            rows = getter(environment=environment)
        except Exception:
            rows = []
        best_value: Any = None
        best_rank = -1
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or to_text(row.get("key")) != "signal_window_max_bars":
                continue
            value = row.get("value")
            if value in (None, ""):
                continue
            row_environment = to_text(row.get("environment")).lower()
            rank = 2 if row_environment == environment else (1 if row_environment in {"", "global"} else -1)
            if rank > best_rank:
                best_rank = rank
                best_value = value
        if best_value not in (None, ""):
            return max(0, to_int(best_value, DEFAULT_SIGNAL_WINDOW_MAX_BARS))
    return DEFAULT_SIGNAL_WINDOW_MAX_BARS


def _status_filter(status: str) -> str:
    if status == "all":
        return '(status = "candidate" || status = "active")'
    return f'status = "{escape_filter(status)}"'


def _pick_latest_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
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


def _group_bars_by_symbol(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows or []:
        symbol = to_text(row.get("symbol")).upper()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        if not symbol or bar_time_ms <= 0:
            continue
        grouped.setdefault(symbol, []).append(
            {
                "symbol": symbol,
                "interval": "5m",
                "environment": to_text(row.get("environment")),
                "exchange": to_text(row.get("exchange")).upper(),
                "bar_time_ms": bar_time_ms,
                "us_time": to_text(row.get("us_time")) or format_et_datetime(bar_time_ms),
                "cn_time": to_text(row.get("cn_time")) or format_cn_time(bar_time_ms),
                "session_type": to_text(row.get("session_type")).lower() or classify_session(bar_time_ms=bar_time_ms),
                "open": to_float(row.get("open")) or 0.0,
                "high": to_float(row.get("high")) or 0.0,
                "low": to_float(row.get("low")) or 0.0,
                "close": to_float(row.get("close")) or 0.0,
                "volume": to_float(row.get("volume")) or 0.0,
            }
        )
    for symbol in list(grouped):
        grouped[symbol].sort(key=lambda item: to_int(item.get("bar_time_ms"), 0))
    return grouped


def _normalize_bar_record(row: dict[str, Any]) -> dict[str, Any]:
    symbol = to_text(row.get("symbol")).upper()
    bar_time_ms = to_int(row.get("bar_time_ms"), 0)
    return {
        "symbol": symbol,
        "interval": "5m",
        "environment": to_text(row.get("environment")),
        "exchange": to_text(row.get("exchange")).upper(),
        "bar_time_ms": bar_time_ms,
        "us_time": to_text(row.get("us_time")) or format_et_datetime(bar_time_ms),
        "cn_time": to_text(row.get("cn_time")) or format_cn_time(bar_time_ms),
        "session_type": to_text(row.get("session_type")).lower() or classify_session(bar_time_ms=bar_time_ms),
        "open": to_float(row.get("open")) or 0.0,
        "high": to_float(row.get("high")) or 0.0,
        "low": to_float(row.get("low")) or 0.0,
        "close": to_float(row.get("close")) or 0.0,
        "volume": to_float(row.get("volume")) or 0.0,
    }


def _load_timeline_bars_by_symbol(
    pb: Any,
    *,
    environment: str,
    symbols: list[str],
    interval: str,
    market_start_ms: int,
    market_end_ms: int,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    environment_filter = build_bar_environment_filter(environment)
    for symbol in symbols:
        symbol_filter = f'symbol = "{escape_filter(symbol)}"'
        today_rows = pb.get_all_records(
            "ibkr_bars",
            filter=(
                f'{symbol_filter} && interval = "{escape_filter(interval)}" && '
                f"{environment_filter} && "
                f"bar_time_ms >= {market_start_ms} && bar_time_ms < {market_end_ms}"
            ),
            sort="bar_time_ms",
            max_pages=TIMELINE_TODAY_MAX_PAGES,
        )
        warmup_rows = []
        if TIMELINE_WARMUP_BARS > 0:
            warmup_rows = pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'{symbol_filter} && interval = "{escape_filter(interval)}" && '
                    f"{environment_filter} && "
                    f"bar_time_ms < {market_start_ms}"
                ),
                sort="-bar_time_ms",
                max_pages=TIMELINE_WARMUP_MAX_PAGES,
            )
            warmup_rows = list(reversed(warmup_rows[:TIMELINE_WARMUP_BARS]))

        merged: dict[int, dict[str, Any]] = {}
        for row in [*(warmup_rows or []), *(today_rows or [])]:
            if not isinstance(row, dict):
                continue
            bar_time_ms = to_int(row.get("bar_time_ms"), 0)
            if bar_time_ms <= 0:
                continue
            merged[bar_time_ms] = _normalize_bar_record(row)
        grouped[symbol] = [merged[key] for key in sorted(merged)]
    return grouped


def _latest_signal_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        normalized = normalize_signal_record(row)
        symbol = to_text(normalized.get("symbol")).upper()
        if not symbol:
            continue
        latest[symbol] = pick_latest_signal(latest.get(symbol), normalized) or normalized
    return latest


def _component_value(component_flags: dict[str, Any], key: str) -> bool:
    if key == "bull_div_seen":
        return bool(component_flags.get("bull_crsi_div_seen") or component_flags.get("bull_obv_div_seen"))
    if key == "bear_div_seen":
        return bool(component_flags.get("bear_crsi_div_seen") or component_flags.get("bear_obv_div_seen"))
    return bool(component_flags.get(key))


def _component_label(key: str) -> str:
    return COMPONENT_LABELS.get(key, key)


def _active_component_group_keys(window_flags: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    if bool(window_flags.get("sd_upper_valid")):
        keys.extend(["type1_long_trend", "type3_short_mr"])
    if bool(window_flags.get("sd_lower_valid")):
        keys.extend(["type2_long_mr", "type4_short_trend"])
    return keys


def _component_groups(component_flags: dict[str, Any], window_flags: dict[str, Any]) -> dict[str, Any]:
    active_group_keys = set(_active_component_group_keys(window_flags))
    groups: dict[str, Any] = {}
    for key, spec in COMPONENT_GROUPS.items():
        required = list(spec["required"])
        present = [item for item in required if _component_value(component_flags, item)]
        missing = [item for item in required if item not in present]
        groups[key] = {
            "label": spec["label"],
            "direction": spec["direction"],
            "signal": spec["signal"],
            "window": spec["window"],
            "active": key in active_group_keys,
            "ready": len(missing) == 0,
            "present": present,
            "missing": missing,
            "present_labels": [_component_label(item) for item in present],
            "missing_labels": [_component_label(item) for item in missing],
            "completed": len(present),
            "total": len(required),
            "progress": (len(present) / len(required)) if required else 0.0,
        }
    return groups


def _component_rollup(component_groups: dict[str, Any], window_flags: dict[str, Any]) -> dict[str, Any]:
    active_keys = _active_component_group_keys(window_flags)
    if not active_keys:
        return {"progress": 0.0, "collected": [], "missing": [], "best_group": "", "ready_groups": []}

    best_group = ""
    best_progress = 0.0
    best_completed = -1
    collected: list[str] = []
    missing: list[str] = []
    ready_groups: list[str] = []
    for group_key in active_keys:
        group = dict(component_groups.get(group_key) or {})
        if not group:
            continue
        completed = to_int(group.get("completed"), 0)
        total = max(1, to_int(group.get("total"), 0))
        progress = completed / total
        if progress > best_progress or (progress == best_progress and completed > best_completed):
            best_group = group_key
            best_progress = progress
            best_completed = completed
        if bool(group.get("ready")):
            ready_groups.append(group_key)
        for label in group.get("present_labels") or []:
            if label not in collected:
                collected.append(label)
        for label in group.get("missing_labels") or []:
            if label not in missing:
                missing.append(label)
    return {
        "progress": round(best_progress, 4),
        "collected": collected,
        "missing": missing,
        "best_group": best_group,
        "ready_groups": ready_groups,
    }


def _window_state(window_flags: dict[str, Any]) -> str:
    upper_valid = bool(window_flags.get("sd_upper_valid"))
    lower_valid = bool(window_flags.get("sd_lower_valid"))
    upper_active = bool(window_flags.get("sd_upper_active"))
    lower_active = bool(window_flags.get("sd_lower_active"))
    if upper_valid and lower_valid:
        return "both_active"
    if upper_valid:
        return "upper_active"
    if lower_valid:
        return "lower_active"
    if upper_active or lower_active:
        return "used"
    return "no_window"


def _side_window(side: str, window_flags: dict[str, Any], max_bars: int) -> dict[str, Any]:
    prefix = f"sd_{side}"
    active = bool(window_flags.get(f"{prefix}_active"))
    valid = bool(window_flags.get(f"{prefix}_valid"))
    used = bool(window_flags.get(f"{prefix}_used"))
    age_bars = to_int(window_flags.get(f"{prefix}_age_bars"), 0) if active else 0
    bars_remaining = max(0, max_bars - age_bars) if valid and max_bars > 0 else 0
    if valid and bars_remaining <= 2:
        status = "near_expiry"
    elif valid:
        status = f"{side}_active"
    elif used:
        status = "used"
    elif active:
        status = "expired"
    else:
        status = "inactive"
    return {
        "active": active,
        "valid": valid,
        "used": used,
        "age_bars": age_bars,
        "bars_remaining": bars_remaining,
        "status": status,
    }


def _window_status(
    *,
    signal_state: dict[str, Any],
    window_flags: dict[str, Any],
    bars_remaining: int,
    blocked_reason: str,
) -> str:
    stage = to_text(signal_state.get("stage")).lower()
    if stage == "confirmed":
        return "confirmed"
    if stage == "blocked" or blocked_reason:
        return "blocked"
    if stage == "candidate":
        return "candidate"
    if (bool(window_flags.get("sd_upper_valid")) or bool(window_flags.get("sd_lower_valid"))) and bars_remaining <= 2:
        return "near_expiry"
    if bool(window_flags.get("sd_upper_valid")) and bool(window_flags.get("sd_lower_valid")):
        return "both_active"
    if bool(window_flags.get("sd_upper_valid")):
        return "upper_active"
    if bool(window_flags.get("sd_lower_valid")):
        return "lower_active"
    if bool(window_flags.get("sd_upper_used")) or bool(window_flags.get("sd_lower_used")):
        return "used"
    if bool(window_flags.get("sd_upper_active")) or bool(window_flags.get("sd_lower_active")):
        return "expired"
    return "no_window"


def _bars_remaining(window_flags: dict[str, Any], max_bars: int) -> int:
    if max_bars <= 0:
        return 0
    ages = []
    if bool(window_flags.get("sd_upper_valid")):
        ages.append(to_int(window_flags.get("sd_upper_age_bars"), 0))
    if bool(window_flags.get("sd_lower_valid")):
        ages.append(to_int(window_flags.get("sd_lower_age_bars"), 0))
    if not ages:
        return 0
    return max(0, max_bars - min(ages))


def _build_chart_trace_url(
    *,
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> str:
    query = urlencode(
        {
            "environment": environment,
            "symbol": symbol,
            "interval": interval,
            "start_ms": max(0, int(start_ms or 0)),
            "end_ms": max(0, int(end_ms or 0)),
            "include_signals": "true",
            "include_trace": "true",
        }
    )
    return f"/ibkr_chart.html?{query}"


def _build_chart_trace_request(
    *,
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    return {
        "path": "/api/custom/ibkr/proxy",
        "method": "POST",
        "body": {
            "action": "chart/timeline",
            "environment": environment,
            "symbol": symbol,
            "interval": interval,
            "start_ms": max(0, int(start_ms or 0)),
            "end_ms": max(0, int(end_ms or 0)),
            "include_signals": True,
            "include_trace": True,
        },
    }


def _build_trace_for_symbol(
    *,
    environment: str,
    symbol: str,
    bars: list[dict[str, Any]],
    signal_window_max_bars: int,
) -> dict[str, Any]:
    if not bars:
        return {"latest_row": None, "trace": {}, "error": "no_bars"}
    try:
        timeline = build_runtime_timeline(
            symbol,
            "5m",
            bars,
            params={
                "signal_window_max_bars": signal_window_max_bars,
                "signal_enabled_symbols": symbol,
                "market_monitor_symbols": "",
            },
            include_signals=True,
            include_trace=True,
        )
    except Exception as exc:
        return {"latest_row": None, "trace": {}, "error": str(exc)}
    latest_row = timeline.get("latest_row") if isinstance(timeline, dict) else None
    return {
        "latest_row": latest_row if isinstance(latest_row, dict) else None,
        "trace": dict((latest_row or {}).get("trace") or {}) if isinstance(latest_row, dict) else {},
        "error": "",
    }


def build_active_window_progress_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
) -> tuple[dict[str, Any], int]:
    runtime_environment = normalize_environment(payload.get("environment"), LIVE_ENVIRONMENT)
    if runtime_environment not in SUPPORTED_ENVIRONMENTS:
        return {"ok": False, "error": "unsupported_environment", "environment": runtime_environment}, 400

    interval = normalize_interval(to_text(payload.get("interval")) or "5m")
    if interval != "5m":
        return {"ok": False, "error": "unsupported_interval", "interval": interval, "supported_intervals": ["5m"]}, 400

    requested_status = to_text(payload.get("status")).lower() or "active"
    if requested_status not in SUPPORTED_STATUSES:
        return {"ok": False, "error": "unsupported_status", "status": requested_status}, 400

    limit = max(1, min(200, to_int(payload.get("limit"), DEFAULT_LIMIT)))
    current_date = current_market_date(time_strings)
    requested_market_date = (
        to_text(first_defined(payload.get("marketDate"), payload.get("market_date"), payload.get("date")))
        or current_date
    )
    try:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(requested_market_date)
        market_date = requested_market_date
    except Exception:
        market_start_ms, market_end_ms = parse_market_date_bounds_ms(current_date)
        market_date = current_date

    computed_at_ms = int(time.time() * 1000)
    signal_window_max_bars = _resolve_signal_window_max_bars(pb, runtime_environment)

    target_rows = pb.get_records(
        "ibkr_targets",
        filter=(
            f'environment = "{escape_filter(runtime_environment)}" && '
            f'date = "{escape_filter(market_date)}" && '
            f"{_status_filter(requested_status)}"
        ),
        sort="-updated",
        per_page=200,
        page=1,
    )
    target_by_symbol: dict[str, dict[str, Any]] = {}
    ordered_symbols: list[str] = []
    for row in target_rows or []:
        if not isinstance(row, dict):
            continue
        symbol = to_text(row.get("symbol")).upper()
        status = to_text(row.get("status")).lower()
        if not symbol or symbol in target_by_symbol or status not in TODAY_TARGET_STATUSES:
            continue
        target_by_symbol[symbol] = dict(row)
        ordered_symbols.append(symbol)
        if len(ordered_symbols) >= limit:
            break

    empty_summary = {
        "total": 0,
        "active_count": 0,
        "candidate_count": 0,
        "with_live_bar_count": 0,
        "window_active_count": 0,
        "window_valid_count": 0,
        "candidate_signal_count": 0,
        "blocked_count": 0,
        "near_expiry_count": 0,
        "confirmed_count": 0,
        "trace_error_count": 0,
    }
    if not ordered_symbols:
        return {
            "ok": True,
            "environment": runtime_environment,
            "market_date": market_date,
            "current_market_date": current_date,
            "status": requested_status,
            "interval": interval,
            "limit": limit,
            "signal_window_max_bars": signal_window_max_bars,
            "computed_at_ms": computed_at_ms,
            "computed_at_us": format_et_datetime(computed_at_ms),
            "computed_at_cn": format_cn_time(computed_at_ms),
            "summary": empty_summary,
            "items": [],
            "source": "ibkr-api",
        }, 200

    bars_by_symbol = _load_timeline_bars_by_symbol(
        pb,
        environment=runtime_environment,
        symbols=ordered_symbols,
        interval=interval,
        market_start_ms=market_start_ms,
        market_end_ms=market_end_ms,
    )
    indicator_records = load_records_for_symbols(
        pb,
        "ibkr_indicators",
        base_filter_parts=[
            f'interval = "{interval_to_chart_tf(interval)}"',
            f'environment = "{escape_filter(runtime_environment)}"',
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="-bar_time_ms",
        max_pages=6,
    )
    signal_records = load_records_for_symbols(
        pb,
        "ibkr_signals",
        base_filter_parts=[
            f'environment = "{escape_filter(runtime_environment)}"',
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=ordered_symbols,
        sort="-bar_time_ms",
        max_pages=6,
    )

    latest_indicator_by_symbol = _pick_latest_by_symbol(indicator_records)
    latest_signal_by_symbol = _latest_signal_by_symbol(signal_records)

    items: list[dict[str, Any]] = []
    summary = dict(empty_summary)
    active_count = 0
    candidate_count = 0
    with_live_bar_count = 0
    window_active_count = 0
    window_valid_count = 0
    candidate_signal_count = 0
    blocked_count = 0
    near_expiry_count = 0
    confirmed_count = 0
    trace_error_count = 0

    for symbol in ordered_symbols:
        target = target_by_symbol.get(symbol) or {}
        target_extra = parse_json_object(target.get("extra"))
        symbol_bars = bars_by_symbol.get(symbol, [])
        today_bars = [row for row in symbol_bars if market_start_ms <= to_int(row.get("bar_time_ms"), 0) < market_end_ms]
        latest_bar = today_bars[-1] if today_bars else (symbol_bars[-1] if symbol_bars else {})
        trace_result = _build_trace_for_symbol(
            environment=runtime_environment,
            symbol=symbol,
            bars=symbol_bars,
            signal_window_max_bars=signal_window_max_bars,
        )
        trace = dict(trace_result.get("trace") or {})
        trace_error = to_text(trace_result.get("error"))
        latest_row = trace_result.get("latest_row") if isinstance(trace_result.get("latest_row"), dict) else {}
        window_flags = dict(trace.get("window_flags") or {})
        component_flags = dict(trace.get("component_flags") or {})
        signal_state = dict(trace.get("signal_state") or {})
        component_groups = _component_groups(component_flags, window_flags)
        component_rollup = _component_rollup(component_groups, window_flags)
        indicator_extra = indicator_snapshot(latest_indicator_by_symbol.get(symbol))
        latest_signal = latest_signal_by_symbol.get(symbol) or {}
        latest_bar_time_ms = to_int(first_defined(latest_row.get("bar_time_ms"), latest_bar.get("bar_time_ms")), 0)
        price = to_float(first_defined(latest_row.get("close"), latest_bar.get("close"))) or 0.0
        freshness_min = max(0, int((computed_at_ms - latest_bar_time_ms) // 60000)) if latest_bar_time_ms > 0 else None
        target_status = to_text(target.get("status")).lower()
        direction_bias = to_text(first_defined(target.get("direction_bias"), "neutral")).lower() or "neutral"
        blocked_reason = to_text(signal_state.get("filter_reason"))
        filter_reasons: list[str] = []
        for reason in [blocked_reason, *(trace.get("filters") or [])]:
            reason_text = to_text(reason)
            if reason_text and reason_text not in filter_reasons:
                filter_reasons.append(reason_text)
        candidate_signal = dict(signal_state.get("signal_payload") or {}) if signal_state.get("signal_payload") else None
        if not candidate_signal and latest_signal:
            candidate_signal = {
                "signal_id": to_text(latest_signal.get("signal_id")),
                "direction": to_text(latest_signal.get("direction")),
                "signal": to_text(latest_signal.get("signal")),
                "status": to_text(latest_signal.get("status")),
                "bar_time_ms": to_int(latest_signal.get("bar_time_ms"), 0),
                "us_time": to_text(latest_signal.get("us_time")),
            }
        candidate_signal_label = (
            to_text(signal_state.get("label"))
            if candidate_signal and to_text(signal_state.get("label")) != "无信号"
            else to_text(first_defined(
                (candidate_signal or {}).get("signal"),
                (candidate_signal or {}).get("direction"),
            ))
        )
        bars_remaining = _bars_remaining(window_flags, signal_window_max_bars)
        status = _window_status(
            signal_state=signal_state,
            window_flags=window_flags,
            bars_remaining=bars_remaining,
            blocked_reason=blocked_reason,
        )
        chart_trace_url = _build_chart_trace_url(
            environment=runtime_environment,
            symbol=symbol,
            interval=interval,
            start_ms=market_start_ms,
            end_ms=latest_bar_time_ms or market_end_ms,
        )

        item = {
            "symbol": symbol,
            "status": status,
            "window_status": status,
            "target_status": target_status,
            "score": round(to_float(target.get("score")) or 0.0, 4),
            "target_score": round(to_float(target.get("score")) or 0.0, 4),
            "direction_bias": direction_bias,
            "latest_bar_time_ms": latest_bar_time_ms,
            "latest_us_time": to_text(first_defined(latest_row.get("us_time"), latest_bar.get("us_time"))),
            "freshness_min": freshness_min,
            "price": round(price, 4) if price > 0 else 0.0,
            "atr_pct": round(to_float(first_defined(latest_row.get("atr_pct"), indicator_extra.get("atr_pct"), target_extra.get("atr_pct"))) or 0.0, 2),
            "window_state": _window_state(window_flags),
            "window_max_bars": signal_window_max_bars,
            "window_flags": window_flags,
            "signal_state": signal_state,
            "sd_upper_active": bool(window_flags.get("sd_upper_active")),
            "sd_upper_valid": bool(window_flags.get("sd_upper_valid")),
            "sd_upper_used": bool(window_flags.get("sd_upper_used")),
            "sd_upper_age_bars": to_int(window_flags.get("sd_upper_age_bars"), 0),
            "sd_lower_active": bool(window_flags.get("sd_lower_active")),
            "sd_lower_valid": bool(window_flags.get("sd_lower_valid")),
            "sd_lower_used": bool(window_flags.get("sd_lower_used")),
            "sd_lower_age_bars": to_int(window_flags.get("sd_lower_age_bars"), 0),
            "upper_window": _side_window("upper", window_flags, signal_window_max_bars),
            "lower_window": _side_window("lower", window_flags, signal_window_max_bars),
            "bars_remaining": bars_remaining,
            "component_progress": component_rollup["progress"],
            "component_detail": component_groups,
            "component_groups": component_groups,
            "components": {
                "collected": component_rollup["collected"],
                "missing": component_rollup["missing"],
                "best_group": component_rollup["best_group"],
                "ready_groups": component_rollup["ready_groups"],
            },
            "collected_components": component_rollup["collected"],
            "missing_components": component_rollup["missing"],
            "candidate_signal": candidate_signal,
            "candidate_signal_label": candidate_signal_label,
            "blocked_reason": blocked_reason,
            "filter_reasons": filter_reasons,
            "chart_trace_url": chart_trace_url,
            "trace_url": chart_trace_url,
            "chart_trace_request": _build_chart_trace_request(
                environment=runtime_environment,
                symbol=symbol,
                interval=interval,
                start_ms=market_start_ms,
                end_ms=latest_bar_time_ms or market_end_ms,
            ),
            "trace_stage": to_text(signal_state.get("stage")) or "none",
            "trace_error": trace_error,
        }
        items.append(item)

        if target_status == "active":
            active_count += 1
        if target_status == "candidate":
            candidate_count += 1
        if latest_bar_time_ms > 0:
            with_live_bar_count += 1
        if item["sd_upper_active"] or item["sd_lower_active"]:
            window_active_count += 1
        if item["sd_upper_valid"] or item["sd_lower_valid"]:
            window_valid_count += 1
        if candidate_signal:
            candidate_signal_count += 1
        if status == "blocked":
            blocked_count += 1
        if status == "near_expiry":
            near_expiry_count += 1
        if status == "confirmed":
            confirmed_count += 1
        if trace_error:
            trace_error_count += 1

    summary.update(
        {
            "total": len(items),
            "active_count": active_count,
            "candidate_count": candidate_count,
            "with_live_bar_count": with_live_bar_count,
            "window_active_count": window_active_count,
            "window_valid_count": window_valid_count,
            "candidate_signal_count": candidate_signal_count,
            "blocked_count": blocked_count,
            "near_expiry_count": near_expiry_count,
            "confirmed_count": confirmed_count,
            "trace_error_count": trace_error_count,
        }
    )

    return {
        "ok": True,
        "environment": runtime_environment,
        "market_date": market_date,
        "current_market_date": current_date,
        "status": requested_status,
        "interval": interval,
        "limit": limit,
        "signal_window_max_bars": signal_window_max_bars,
        "computed_at_ms": computed_at_ms,
        "computed_at_us": format_et_datetime(computed_at_ms),
        "computed_at_cn": format_cn_time(computed_at_ms),
        "summary": summary,
        "returned_count": len(items),
        "items": items,
        "source": "ibkr-api",
    }, 200


__all__ = ["build_active_window_progress_response"]
