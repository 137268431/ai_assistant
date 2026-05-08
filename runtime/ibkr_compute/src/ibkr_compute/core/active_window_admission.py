from __future__ import annotations

from copy import deepcopy
from typing import Any

from ibkr_compute.core.timeline_builder import build_runtime_timeline


DEFAULT_SIGNAL_WINDOW_MAX_BARS = 12

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


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _to_text(value: Any) -> str:
    return str(value or "").strip()


def component_value(component_flags: dict[str, Any], key: str) -> bool:
    if key == "bull_div_seen":
        return bool(component_flags.get("bull_crsi_div_seen") or component_flags.get("bull_obv_div_seen"))
    if key == "bear_div_seen":
        return bool(component_flags.get("bear_crsi_div_seen") or component_flags.get("bear_obv_div_seen"))
    return bool(component_flags.get(key))


def component_label(key: str) -> str:
    return COMPONENT_LABELS.get(key, key)


def active_component_group_keys(window_flags: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    if bool(window_flags.get("sd_upper_valid")):
        keys.extend(["type1_long_trend", "type3_short_mr"])
    if bool(window_flags.get("sd_lower_valid")):
        keys.extend(["type2_long_mr", "type4_short_trend"])
    return keys


def component_groups(component_flags: dict[str, Any], window_flags: dict[str, Any]) -> dict[str, Any]:
    active_group_keys = set(active_component_group_keys(window_flags))
    groups: dict[str, Any] = {}
    for key, spec in COMPONENT_GROUPS.items():
        required = list(spec["required"])
        present = [item for item in required if component_value(component_flags, item)]
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
            "present_labels": [component_label(item) for item in present],
            "missing_labels": [component_label(item) for item in missing],
            "completed": len(present),
            "total": len(required),
            "progress": (len(present) / len(required)) if required else 0.0,
        }
    return groups


def component_rollup(groups: dict[str, Any], window_flags: dict[str, Any]) -> dict[str, Any]:
    active_keys = active_component_group_keys(window_flags)
    if not active_keys:
        return {"progress": 0.0, "collected": [], "missing": [], "best_group": "", "ready_groups": []}

    best_group = ""
    best_progress = 0.0
    best_completed = -1
    collected: list[str] = []
    missing: list[str] = []
    ready_groups: list[str] = []
    for group_key in active_keys:
        group = dict(groups.get(group_key) or {})
        if not group:
            continue
        completed = _to_int(group.get("completed"), 0)
        total = max(1, _to_int(group.get("total"), 0))
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


def window_state(window_flags: dict[str, Any]) -> str:
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


def side_window(side: str, window_flags: dict[str, Any], max_bars: int) -> dict[str, Any]:
    prefix = f"sd_{side}"
    active = bool(window_flags.get(f"{prefix}_active"))
    valid = bool(window_flags.get(f"{prefix}_valid"))
    used = bool(window_flags.get(f"{prefix}_used"))
    age_bars = _to_int(window_flags.get(f"{prefix}_age_bars"), 0) if active else 0
    remaining = max(0, max_bars - age_bars) if valid and max_bars > 0 else 0
    if valid and remaining <= 2:
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
        "bars_remaining": remaining,
        "status": status,
    }


def bars_remaining(window_flags: dict[str, Any], max_bars: int) -> int:
    if max_bars <= 0:
        return 0
    ages = []
    if bool(window_flags.get("sd_upper_valid")):
        ages.append(_to_int(window_flags.get("sd_upper_age_bars"), 0))
    if bool(window_flags.get("sd_lower_valid")):
        ages.append(_to_int(window_flags.get("sd_lower_age_bars"), 0))
    if not ages:
        return 0
    return max(0, max_bars - min(ages))


def window_status(
    *,
    signal_state: dict[str, Any],
    window_flags: dict[str, Any],
    bars_remaining_value: int,
    blocked_reason: str,
) -> str:
    stage = _to_text(signal_state.get("stage")).lower()
    if stage == "confirmed":
        return "confirmed"
    if stage == "blocked" or blocked_reason:
        return "blocked"
    if stage == "candidate":
        return "candidate"
    if (bool(window_flags.get("sd_upper_valid")) or bool(window_flags.get("sd_lower_valid"))) and bars_remaining_value <= 2:
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


def is_active_window_admitted(item: dict[str, Any]) -> bool:
    if _to_text(item.get("window_status")).lower() == "blocked":
        return False
    if _to_text(item.get("trace_stage")).lower() == "blocked":
        return False
    return bool(item.get("sd_upper_valid") or item.get("sd_lower_valid"))


def build_active_window_admission_item(trace: dict[str, Any], *, signal_window_max_bars: int) -> dict[str, Any]:
    trace_copy = deepcopy(trace or {})
    window_flags = dict(trace_copy.get("window_flags") or {})
    component_flags = dict(trace_copy.get("component_flags") or {})
    signal_state = dict(trace_copy.get("signal_state") or {})
    groups = component_groups(component_flags, window_flags)
    rollup = component_rollup(groups, window_flags)
    blocked_reason = _to_text(signal_state.get("filter_reason"))
    remaining = bars_remaining(window_flags, int(signal_window_max_bars or 0))
    status = window_status(
        signal_state=signal_state,
        window_flags=window_flags,
        bars_remaining_value=remaining,
        blocked_reason=blocked_reason,
    )
    item = {
        "window_status": status,
        "trace_stage": _to_text(signal_state.get("stage")),
        "blocked_reason": blocked_reason,
        "window_state": window_state(window_flags),
        "window_max_bars": int(signal_window_max_bars or 0),
        "window_flags": window_flags,
        "signal_state": signal_state,
        "sd_upper_active": bool(window_flags.get("sd_upper_active")),
        "sd_upper_valid": bool(window_flags.get("sd_upper_valid")),
        "sd_upper_used": bool(window_flags.get("sd_upper_used")),
        "sd_upper_age_bars": _to_int(window_flags.get("sd_upper_age_bars"), 0),
        "sd_lower_active": bool(window_flags.get("sd_lower_active")),
        "sd_lower_valid": bool(window_flags.get("sd_lower_valid")),
        "sd_lower_used": bool(window_flags.get("sd_lower_used")),
        "sd_lower_age_bars": _to_int(window_flags.get("sd_lower_age_bars"), 0),
        "upper_window": side_window("upper", window_flags, int(signal_window_max_bars or 0)),
        "lower_window": side_window("lower", window_flags, int(signal_window_max_bars or 0)),
        "bars_remaining": remaining,
        "component_progress": rollup["progress"],
        "component_detail": groups,
        "component_groups": groups,
        "components": {
            "collected": rollup["collected"],
            "missing": rollup["missing"],
            "best_group": rollup["best_group"],
            "ready_groups": rollup["ready_groups"],
        },
        "collected_components": rollup["collected"],
        "missing_components": rollup["missing"],
    }
    item["admitted"] = is_active_window_admitted(item)
    return item


def build_active_window_trace_for_bars(
    *,
    symbol: str,
    interval: str,
    bars: list[dict[str, Any]],
    signal_window_max_bars: int = DEFAULT_SIGNAL_WINDOW_MAX_BARS,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not bars:
        return {"latest_row": None, "trace": {}, "error": "no_bars"}
    timeline_params = dict(params or {})
    timeline_params.setdefault("signal_window_max_bars", signal_window_max_bars)
    timeline_params.setdefault("signal_enabled_symbols", symbol)
    timeline_params.setdefault("market_monitor_symbols", "")
    try:
        timeline = build_runtime_timeline(
            symbol,
            interval,
            bars,
            params=timeline_params,
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
