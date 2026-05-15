from __future__ import annotations

from datetime import datetime
from typing import Any

from ibkr_api.orders.values import ensure_object, to_float, to_int, to_text


OPEN_ORDER_FILTER_PER_PAGE = 800

EXIT_EXPOSURE_ROLES = {
    "take_profit",
    "stop_loss",
    "repair_tp",
    "repair_sl",
    "close",
    "manual_close",
    "market_close",
    "close_order",
    "reverse_close",
}


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            import json

            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _parse_time_ms(value: Any) -> int:
    text = to_text(value)
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).timestamp() * 1000)
        except Exception:
            continue
    return 0


def _pick_first_non_empty(*values: Any) -> str:
    for value in values:
        text = to_text(value)
        if text:
            return text
    return ""


def _clone_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    items: list[str] = []
    for value in values:
        text = to_text(value)
        if text:
            items.append(text)
    return items


def canonical_order_status(status: Any) -> str:
    key = to_text(status).upper()
    if key in {"PENDING", "PRESUBMITTED", "SUBMITTED", "PENDINGSUBMIT", "INPROGRESS", "INIT", "APIPENDING", "API_PENDING"}:
        return "SUBMITTED"
    if key in {"FILLED", "EXECUTED"}:
        return "FILLED"
    if key in {"CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}:
        return "CANCELED"
    return key or "UNKNOWN"


def _order_status_weight(status: Any) -> int:
    key = canonical_order_status(status)
    if key == "FILLED":
        return 90
    if key == "SUBMITTED":
        return 70
    if key == "CANCELED":
        return 10
    return 20


def _signal_status_weight(status: Any) -> int:
    key = to_text(status).lower()
    if key == "executed":
        return 90
    if key == "confirmed":
        return 80
    if key == "awaiting_confirm":
        return 70
    if key == "pending":
        return 60
    if key in {"rejected", "expired"}:
        return 20
    return 30


def normalize_order_record(record: dict[str, Any]) -> dict[str, Any]:
    extra = _parse_json_object(record.get("extra"))
    quantity = to_float(record.get("quantity")) or 0.0
    filled_qty_raw = to_float(record.get("filled_qty")) or 0.0
    status = _pick_first_non_empty(record.get("status"), extra.get("status"))
    role = _pick_first_non_empty(record.get("role"), extra.get("role")) or "entry"
    trade_group_id = _pick_first_non_empty(
        record.get("trade_group_id"),
        extra.get("trade_group_id"),
        record.get("entry_order_unique_id"),
        extra.get("entry_order_unique_id"),
        record.get("unique_id"),
    )
    entry_order_unique_id = _pick_first_non_empty(
        record.get("entry_order_unique_id"),
        extra.get("entry_order_unique_id"),
        record.get("unique_id"),
    )
    signal_id = _pick_first_non_empty(record.get("signal_id"), extra.get("signal_id"))
    updated = _pick_first_non_empty(record.get("updated"), record.get("us_time"), record.get("created"))
    relation_status = _pick_first_non_empty(record.get("relation_status"), extra.get("relation_status"))
    broker_order_id = _pick_first_non_empty(record.get("broker_order_id"), extra.get("broker_order_id"), record.get("order_id"))
    order_id = _pick_first_non_empty(record.get("order_id"), extra.get("order_id"), broker_order_id)
    unique_id = _pick_first_non_empty(record.get("unique_id"), extra.get("unique_id"))
    group_key = _pick_first_non_empty(trade_group_id, entry_order_unique_id, unique_id, broker_order_id, order_id)
    filled_qty = filled_qty_raw if filled_qty_raw > 0 else (quantity if canonical_order_status(status) == "FILLED" else 0.0)
    return {
        "record_id": to_text(record.get("id")),
        "symbol": _pick_first_non_empty(record.get("symbol"), extra.get("symbol")).upper(),
        "status": status,
        "status_key": canonical_order_status(status),
        "signal_id": signal_id,
        "trade_group_id": trade_group_id,
        "entry_order_unique_id": entry_order_unique_id,
        "parent_order_unique_id": _pick_first_non_empty(record.get("parent_order_unique_id"), extra.get("parent_order_unique_id")),
        "sibling_order_unique_id": _pick_first_non_empty(record.get("sibling_order_unique_id"), extra.get("sibling_order_unique_id")),
        "unique_id": unique_id,
        "broker_order_id": broker_order_id,
        "order_id": order_id,
        "role": role,
        "relation_status": relation_status,
        "direction": _pick_first_non_empty(record.get("direction"), extra.get("direction")),
        "position_side": _pick_first_non_empty(record.get("position_side"), extra.get("position_side")),
        "quantity": quantity,
        "filled_qty": filled_qty,
        "limit_price": to_float(record.get("limit_price") if record.get("limit_price") not in (None, "") else extra.get("limit_price")) or 0.0,
        "fill_price": to_float(record.get("fill_price") if record.get("fill_price") not in (None, "") else extra.get("fill_price")) or 0.0,
        "commission": abs(to_float(record.get("commission") if record.get("commission") not in (None, "") else extra.get("commission")) or 0.0),
        "commission_currency": _pick_first_non_empty(record.get("commission_currency"), extra.get("commission_currency"), "USD").upper(),
        "updated": updated,
        "updated_ms": _parse_time_ms(updated),
        "status_weight": _order_status_weight(status),
        "group_key": group_key,
        "extra": extra,
    }


def normalize_signal_record(record: dict[str, Any]) -> dict[str, Any]:
    extra = _parse_json_object(record.get("extra"))
    updated = _pick_first_non_empty(record.get("updated"), record.get("us_time"), record.get("created"))
    status = _pick_first_non_empty(record.get("status"), extra.get("status"))
    return {
        "signal_id": _pick_first_non_empty(record.get("signal_id"), extra.get("signal_id")),
        "symbol": _pick_first_non_empty(record.get("symbol"), extra.get("symbol")).upper(),
        "status": status,
        "note": _pick_first_non_empty(record.get("note"), extra.get("status_reason"), extra.get("note")),
        "updated": updated,
        "updated_ms": _parse_time_ms(updated),
        "status_weight": _signal_status_weight(status),
    }


def _is_closed_order_status(status: Any) -> bool:
    return canonical_order_status(status) in {"FILLED", "CANCELED", "CLOSED", "REJECTED", "INACTIVE", "EXPIRED"}


def _is_open_like_order(order: dict[str, Any]) -> bool:
    if not order or _is_closed_order_status(order.get("status")):
        return False
    relation_status = to_text(order.get("relation_status")).lower()
    if relation_status in {"active", "planned"}:
        return True
    return to_int(order.get("status_weight"), 0) >= 40


def _is_exit_exposure_role(role: Any) -> bool:
    return to_text(role).lower().replace("-", "_").replace(" ", "_") in EXIT_EXPOSURE_ROLES


def _serialize_managed_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": to_text(order.get("record_id")),
        "symbol": to_text(order.get("symbol")),
        "unique_id": to_text(order.get("unique_id")),
        "order_id": to_text(order.get("order_id")),
        "broker_order_id": to_text(order.get("broker_order_id")),
        "signal_id": to_text(order.get("signal_id")),
        "trade_group_id": to_text(order.get("trade_group_id")),
        "entry_order_unique_id": to_text(order.get("entry_order_unique_id")),
        "parent_order_unique_id": to_text(order.get("parent_order_unique_id")),
        "sibling_order_unique_id": to_text(order.get("sibling_order_unique_id")),
        "role": to_text(order.get("role")),
        "relation_status": to_text(order.get("relation_status")),
        "direction": to_text(order.get("direction")),
        "position_side": to_text(order.get("position_side")),
        "status": to_text(order.get("status")),
        "quantity": to_float(order.get("quantity")) or 0.0,
        "filled_qty": to_float(order.get("filled_qty")) or 0.0,
        "limit_price": to_float(order.get("limit_price")) or 0.0,
        "fill_price": to_float(order.get("fill_price")) or 0.0,
        "commission": abs(to_float(order.get("commission")) or 0.0),
        "commission_currency": to_text(order.get("commission_currency") or "USD").upper(),
        "commission_known": bool(order.get("commission_known")) or abs(to_float(order.get("commission")) or 0.0) > 0,
        "commission_source": to_text(order.get("commission_source")),
        "commission_fill_count": to_int(order.get("commission_fill_count"), 0),
        "updated": to_text(order.get("updated")),
    }


__all__ = [
    "OPEN_ORDER_FILTER_PER_PAGE",
    "EXIT_EXPOSURE_ROLES",
    "_clone_string_list",
    "_is_closed_order_status",
    "_is_exit_exposure_role",
    "_is_open_like_order",
    "_pick_first_non_empty",
    "_serialize_managed_order",
    "canonical_order_status",
    "normalize_order_record",
    "normalize_signal_record",
    "_parse_time_ms",
]
