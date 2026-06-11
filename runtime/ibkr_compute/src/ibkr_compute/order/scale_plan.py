from __future__ import annotations

from typing import Any, Mapping


INDEPENDENT_TWO_LEG_PLAN_TYPE = "independent_two_leg"
INDEPENDENT_TWO_LEG_VERSION = "independent_two_leg_v1"
DEFAULT_LEG_COUNT = 2
DEFAULT_MAX_LEG_NOTIONAL = 5000.0
DEFAULT_MAX_LEG_RISK = 75.0
DEFAULT_MAX_PLAN_RISK = 150.0

_PLAN_TYPE_ALIASES = {
    "independent_two_leg": INDEPENDENT_TWO_LEG_PLAN_TYPE,
    "independent_dual_leg": INDEPENDENT_TWO_LEG_PLAN_TYPE,
    "independent_dual_bracket": INDEPENDENT_TWO_LEG_PLAN_TYPE,
    "two_independent_brackets": INDEPENDENT_TWO_LEG_PLAN_TYPE,
}

_PLAN_HINT_KEYS = {
    "scale_plan_enabled",
    "scale_plan_type",
    "plan_type",
    "scale_plan_id",
    "plan_id",
    "scale_leg_index",
    "leg_index",
    "independent_legs",
    "cross_leg_protection_sync",
    "max_leg_notional",
    "max_leg_risk",
    "max_plan_risk",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "on"}:
            return True
        if text in {"false", "0", "no", "n", "off"}:
            return False
    return default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def _normalized_plan_type(value: Any) -> str:
    text = _text(value).lower().replace("-", "_").replace(" ", "_")
    return _PLAN_TYPE_ALIASES.get(text, text)


def _has_plan_hint(payload: Mapping[str, Any]) -> bool:
    for key in _PLAN_HINT_KEYS:
        if key == "scale_plan_enabled":
            if _coerce_bool(payload.get(key), False):
                return True
            continue
        if payload.get(key) not in (None, "", []):
            return True
    return False


def _derive_plan_id(payload: Mapping[str, Any], *, signal_id: str, trade_group_id: str) -> str:
    explicit = _text(payload.get("plan_id") or payload.get("scale_plan_id"))
    if explicit:
        return explicit
    group = _text(trade_group_id or payload.get("trade_group_id") or payload.get("bracket_group"))
    leg_index = _coerce_int(payload.get("leg_index") or payload.get("scale_leg_index"), 0)
    if group and leg_index > 0:
        suffix = f"_leg{leg_index}"
        if group.endswith(suffix):
            return group[: -len(suffix)]
    return group or _text(signal_id)


def _derive_leg_trade_group_id(payload: Mapping[str, Any], *, plan_id: str, leg_index: int, trade_group_id: str) -> str:
    explicit = _text(payload.get("leg_trade_group_id"))
    if explicit:
        return explicit
    group = _text(trade_group_id or payload.get("trade_group_id") or payload.get("bracket_group"))
    if group:
        return group
    return f"{plan_id}_leg{leg_index}" if plan_id else ""


def normalize_independent_scale_plan_extra(
    extra: Mapping[str, Any] | None,
    *,
    signal_id: str = "",
    trade_group_id: str = "",
    symbol: str = "",
    direction: str = "",
) -> dict[str, Any]:
    """Normalize independent two-leg metadata without changing order semantics."""
    payload = dict(extra or {}) if isinstance(extra, Mapping) else {}
    if not _has_plan_hint(payload):
        return payload
    if "scale_plan_enabled" in payload and not _coerce_bool(payload.get("scale_plan_enabled"), False):
        return payload

    hinted_plan_type = _normalized_plan_type(payload.get("plan_type") or payload.get("scale_plan_type"))
    if hinted_plan_type and hinted_plan_type != INDEPENDENT_TWO_LEG_PLAN_TYPE:
        return payload

    leg_count = max(
        DEFAULT_LEG_COUNT,
        _coerce_int(payload.get("leg_count") or payload.get("scale_leg_count"), DEFAULT_LEG_COUNT),
    )
    leg_index = _coerce_int(payload.get("leg_index") or payload.get("scale_leg_index"), 1)
    leg_index = 1 if leg_index <= 0 else leg_index
    leg_count = max(leg_count, leg_index)

    max_plan_risk = _coerce_float(payload.get("max_plan_risk") or payload.get("scale_plan_max_risk"), DEFAULT_MAX_PLAN_RISK)
    if max_plan_risk <= 0:
        max_plan_risk = DEFAULT_MAX_PLAN_RISK
    default_leg_risk = max_plan_risk / leg_count if leg_count > 0 else DEFAULT_MAX_LEG_RISK
    max_leg_risk = _coerce_float(payload.get("max_leg_risk") or payload.get("scale_leg_max_risk"), default_leg_risk)
    if max_leg_risk <= 0:
        max_leg_risk = default_leg_risk or DEFAULT_MAX_LEG_RISK
    max_leg_notional = _coerce_float(
        payload.get("max_leg_notional") or payload.get("scale_leg_max_notional"),
        DEFAULT_MAX_LEG_NOTIONAL,
    )
    if max_leg_notional <= 0:
        max_leg_notional = DEFAULT_MAX_LEG_NOTIONAL

    plan_id = _derive_plan_id(payload, signal_id=signal_id, trade_group_id=trade_group_id)
    leg_trade_group_id = _derive_leg_trade_group_id(
        payload,
        plan_id=plan_id,
        leg_index=leg_index,
        trade_group_id=trade_group_id,
    )
    leg_role = _text(payload.get("leg_role")) or ("primary" if leg_index == 1 else "secondary")
    leg_trigger = _text(payload.get("leg_trigger")) or ("initial_structure" if leg_index == 1 else "secondary_structure")
    requires_leg1 = _coerce_bool(payload.get("leg2_requires_leg1_protected"), True)

    payload.update(
        {
            "scale_plan_enabled": True,
            "scale_plan_version": INDEPENDENT_TWO_LEG_VERSION,
            "plan_type": INDEPENDENT_TWO_LEG_PLAN_TYPE,
            "scale_plan_type": INDEPENDENT_TWO_LEG_PLAN_TYPE,
            "plan_id": plan_id,
            "leg_index": leg_index,
            "scale_leg_index": leg_index,
            "leg_count": leg_count,
            "leg_role": leg_role,
            "leg_trigger": leg_trigger,
            "leg_trade_group_id": leg_trade_group_id,
            "max_leg_notional": max_leg_notional,
            "max_leg_risk": max_leg_risk,
            "max_plan_risk": max_plan_risk,
            "independent_legs": True,
            "cross_leg_protection_sync": False,
            "aggregate_position_management": False,
            "leg_order_mode": "independent_bracket",
            "scale_plan_risk_model": "fixed_risk_per_independent_leg",
            "plan_risk_budget_used": round(max_leg_risk * leg_count, 6),
            "plan_risk_budget_ok": (max_leg_risk * leg_count) <= (max_plan_risk + 0.000001),
            "leg2_requires_leg1_protected": requires_leg1,
        }
    )
    if symbol:
        payload.setdefault("plan_symbol", str(symbol or "").strip().upper())
    if direction:
        payload.setdefault("plan_direction", str(direction or "").strip().lower())
    return payload
