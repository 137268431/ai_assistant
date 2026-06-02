from __future__ import annotations

from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _list_values(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def normalize_strategy_capacity_snapshot(runtime_payload: Any) -> dict[str, Any]:
    payload = _as_dict(runtime_payload)
    raw = _as_dict(payload.get("strategy_capacity"))
    if not raw:
        raw = _as_dict(_as_dict(payload.get("order_lifecycle")).get("strategy_capacity"))
    if raw and "available" in raw and not _to_bool(raw.get("available")):
        return unavailable_strategy_capacity(raw.get("error") or "strategy_capacity_unavailable")
    if not raw:
        max_positions = _to_int(_as_dict(payload.get("order_lifecycle")).get("max_strategy_open_positions"), 0)
        if max_positions <= 0:
            return {"available": False, "error": "strategy_capacity_unavailable"}
        raw = {"max_strategy_open_positions": max_positions}

    positions = _to_int(raw.get("strategy_open_positions"), 0)
    entries = _to_int(raw.get("open_strategy_entry_orders"), 0)
    used = _to_int(raw.get("strategy_capacity_used"), positions + entries)
    max_positions = _to_int(raw.get("max_strategy_open_positions"), 0)
    remaining = raw.get("strategy_capacity_remaining")
    if remaining in (None, ""):
        remaining = max(0, max_positions - used) if max_positions > 0 else None
    else:
        remaining = _to_int(remaining, 0)

    return {
        "available": True,
        "strategy_capacity_used": used,
        "max_strategy_open_positions": max_positions,
        "strategy_open_positions": positions,
        "open_strategy_entry_orders": entries,
        "strategy_capacity_remaining": remaining,
        "capacity_full": _to_bool(raw.get("capacity_full")) or (max_positions > 0 and used >= max_positions),
        "strategy_open_position_symbols": _list_values(raw.get("strategy_open_position_symbols")),
        "open_strategy_entry_order_symbols": _list_values(raw.get("open_strategy_entry_order_symbols")),
        "fixed_position_symbols": _list_values(raw.get("fixed_position_symbols")),
        "source": str(raw.get("source") or "runtime_status"),
    }


def unavailable_strategy_capacity(error: Any = "") -> dict[str, Any]:
    return {
        "available": False,
        "strategy_capacity_used": 0,
        "max_strategy_open_positions": 0,
        "strategy_open_positions": 0,
        "open_strategy_entry_orders": 0,
        "strategy_capacity_remaining": None,
        "capacity_full": False,
        "strategy_open_position_symbols": [],
        "open_strategy_entry_order_symbols": [],
        "fixed_position_symbols": [],
        "source": "runtime_status",
        "error": str(error or "strategy_capacity_unavailable"),
    }


__all__ = ["normalize_strategy_capacity_snapshot", "unavailable_strategy_capacity"]
