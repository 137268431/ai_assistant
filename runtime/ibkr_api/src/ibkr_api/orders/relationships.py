from __future__ import annotations

from typing import Any

from ibkr_api.orders.values import ORDER_STATUS_TEXT_MAP, ensure_object, first_defined, to_text


def normalize_order_role(role: Any, order_type: Any) -> str:
    normalized_role = to_text(role)
    if normalized_role:
        return normalized_role
    order_type_text = to_text(order_type).lower()
    if order_type_text == "entry":
        return "entry"
    if order_type_text == "takeprofit":
        return "take_profit"
    if order_type_text == "stoploss":
        return "stop_loss"
    return ""


def normalize_relation_status(relation_status: Any, status: Any) -> str:
    normalized = to_text(relation_status)
    if normalized:
        return normalized
    normalized_status = to_text(status).lower()
    if normalized_status in {"canceled", "closed"}:
        return "closed"
    return "active"


def get_order_status_text(status: Any) -> str:
    text = to_text(status)
    return ORDER_STATUS_TEXT_MAP.get(text, text or "未知")


def get_order_status_transition_text(previous_status: Any, current_status: Any) -> str:
    previous = to_text(previous_status)
    current = to_text(current_status)
    if not previous or previous == current:
        return get_order_status_text(current)
    return f"{get_order_status_text(previous)} -> {get_order_status_text(current)}"


def resolve_order_relationship(payload: dict[str, Any], existing_row: dict[str, Any] | None) -> dict[str, Any]:
    existing = existing_row or {}
    extra = ensure_object(payload.get("extra"))
    existing_extra = ensure_object(existing.get("extra"))
    unique_id = to_text(
        first_defined(
            payload.get("unique_id"),
            extra.get("unique_id"),
            existing.get("unique_id"),
            existing_extra.get("unique_id"),
            "",
        )
    )
    order_type = to_text(
        first_defined(
            payload.get("order_type"),
            extra.get("order_type"),
            existing.get("order_type"),
            existing_extra.get("order_type"),
            "",
        )
    )
    direction = to_text(
        first_defined(
            payload.get("direction"),
            extra.get("direction"),
            existing.get("direction"),
            existing_extra.get("direction"),
            "",
        )
    )
    role = normalize_order_role(
        first_defined(
            payload.get("role"),
            extra.get("role"),
            existing.get("role"),
            existing_extra.get("role"),
            "",
        ),
        order_type,
    )
    position_side = to_text(
        first_defined(
            payload.get("position_side"),
            extra.get("position_side"),
            existing.get("position_side"),
            existing_extra.get("position_side"),
            direction,
            "",
        )
    )
    entry_order_unique_id = to_text(
        first_defined(
            payload.get("entry_order_unique_id"),
            extra.get("entry_order_unique_id"),
            existing.get("entry_order_unique_id"),
            existing_extra.get("entry_order_unique_id"),
            unique_id if role == "entry" else "",
            unique_id,
        )
    )
    trade_group_id = to_text(
        first_defined(
            payload.get("trade_group_id"),
            extra.get("trade_group_id"),
            existing.get("trade_group_id"),
            existing_extra.get("trade_group_id"),
            entry_order_unique_id,
            unique_id,
        )
    )
    parent_order_unique_id = to_text(
        first_defined(
            payload.get("parent_order_unique_id"),
            extra.get("parent_order_unique_id"),
            existing.get("parent_order_unique_id"),
            existing_extra.get("parent_order_unique_id"),
            entry_order_unique_id if role and role != "entry" else "",
            "",
        )
    )
    sibling_order_unique_id = to_text(
        first_defined(
            payload.get("sibling_order_unique_id"),
            extra.get("sibling_order_unique_id"),
            existing.get("sibling_order_unique_id"),
            existing_extra.get("sibling_order_unique_id"),
            "",
        )
    )
    broker_order_id = to_text(
        first_defined(
            payload.get("broker_order_id"),
            extra.get("broker_order_id"),
            existing.get("broker_order_id"),
            existing_extra.get("broker_order_id"),
            payload.get("order_id"),
            extra.get("order_id"),
            existing.get("order_id"),
            existing_extra.get("order_id"),
            "",
        )
    )
    relation_status = normalize_relation_status(
        first_defined(
            payload.get("relation_status"),
            extra.get("relation_status"),
            existing.get("relation_status"),
            existing_extra.get("relation_status"),
            "",
        ),
        first_defined(
            payload.get("status"),
            extra.get("status"),
            existing.get("status"),
            existing_extra.get("status"),
            "",
        ),
    )
    return {
        "trade_group_id": trade_group_id,
        "entry_order_unique_id": entry_order_unique_id,
        "parent_order_unique_id": parent_order_unique_id,
        "sibling_order_unique_id": sibling_order_unique_id,
        "role": role,
        "relation_status": relation_status,
        "broker_order_id": broker_order_id,
        "position_side": position_side,
    }
