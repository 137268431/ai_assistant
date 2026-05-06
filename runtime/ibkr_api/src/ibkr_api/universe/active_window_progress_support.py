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
    normalize_symbols,
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

__all__ = [name for name in globals() if not name.startswith("__")]
