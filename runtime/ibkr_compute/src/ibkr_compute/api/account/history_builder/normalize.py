from __future__ import annotations

from datetime import datetime

from ibkr_compute.api.account.live import (
    _app_coerce_float,
    _canonical_order_status,
    _direction_from_side,
    _display_order_status,
    _extract_live_order_text,
    _normalize_live_order,
    _order_history_time_value,
)


def _normalize_broker_history_order(order: dict) -> dict:
    live_order = _normalize_live_order(order)
    order_id = str(live_order.get("order_id") or "").strip()
    parent_id = str(live_order.get("parent_id") or "").strip()
    coid = _extract_live_order_text(order, "cOID", "coid", "order_ref", "orderRef")
    unique_id = coid or order_id
    entry_unique_id = parent_id or coid or order_id
    if not unique_id:
        unique_id = order_id
    if not entry_unique_id:
        entry_unique_id = unique_id
    status = _display_order_status(live_order.get("status"))
    canonical_status = _canonical_order_status(status)
    closed_statuses = {"FILLED", "CANCELED"}
    submitted_time = str(live_order.get("submitted_time") or "").strip()
    fill_time = str(live_order.get("last_execution_time") or "").strip()
    updated_time = fill_time or submitted_time or datetime.utcnow().isoformat()
    direction = _direction_from_side(live_order.get("side"))

    return {
        "source": "ibkr_direct",
        "source_kind": "broker_order",
        "source_label": "IBKR Direct",
        "unique_id": unique_id,
        "order_id": order_id,
        "broker_order_id": order_id,
        "order_type": live_order.get("order_type") or "",
        "symbol": live_order.get("symbol") or "",
        "direction": direction,
        "position_side": direction,
        "trade_group_id": entry_unique_id or unique_id,
        "entry_order_unique_id": entry_unique_id,
        "parent_order_unique_id": parent_id,
        "role": live_order.get("role") or "",
        "relation_status": "closed" if canonical_status in closed_statuses else "active",
        "quantity": live_order.get("total_quantity") or 0,
        "limit_price": live_order.get("price") or 0,
        "status": status,
        "filled_qty": live_order.get("filled_quantity") or 0,
        "fill_price": live_order.get("avg_price") or 0,
        "order_time": submitted_time,
        "fill_time": fill_time,
        "us_time": submitted_time,
        "updated": updated_time,
        "diagnostic_state": "",
        "diagnostic_note": "",
        "raw": live_order.get("raw") or {},
    }


def _normalize_pb_history_order(record: dict) -> dict:
    return {
        "record_id": str(record.get("id") or "").strip(),
        "order_id": str(record.get("order_id") or "").strip(),
        "broker_order_id": str(record.get("broker_order_id") or record.get("order_id") or "").strip(),
        "symbol": str(record.get("symbol") or "").strip().upper(),
        "status": _display_order_status(record.get("status")),
        "quantity": float(_app_coerce_float(record.get("quantity"), 0.0) or 0.0),
        "filled_qty": float(_app_coerce_float(record.get("filled_qty"), 0.0) or 0.0),
        "time_value": _order_history_time_value(record),
        "raw": record,
    }


__all__ = [
    "_normalize_broker_history_order",
    "_normalize_pb_history_order",
]
