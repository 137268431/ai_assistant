from __future__ import annotations

from typing import Any, Callable

from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode

from ibkr_api.reverse.shared import (
    ACTIVE_ENTRY_ORDER_LIMIT,
    ORDERS_COLLECTION,
    ensure_object,
    escape_filter_string,
    first_non_empty,
    record_value,
    to_float,
    to_text,
)


def _to_number(value: Any, fallback: float = 0.0) -> float:
    parsed = to_float(value)
    return fallback if parsed is None else parsed


def build_order_context(order_record: Any, *, default_environment: str = "live") -> dict[str, Any] | None:
    if not order_record:
        return None
    order_extra = ensure_object(record_value(order_record, "extra"))
    order_status = to_text(record_value(order_record, "status") or order_extra.get("status"))
    direction = to_text(first_non_empty(record_value(order_record, "direction"), order_extra.get("direction"), ""))
    trade_group_id = to_text(
        first_non_empty(
            record_value(order_record, "trade_group_id"),
            order_extra.get("trade_group_id"),
            record_value(order_record, "entry_order_unique_id"),
            record_value(order_record, "unique_id"),
        )
    )
    entry_order_unique_id = to_text(
        first_non_empty(
            record_value(order_record, "entry_order_unique_id"),
            order_extra.get("entry_order_unique_id"),
            record_value(order_record, "unique_id"),
        )
    )
    unique_id = to_text(
        first_non_empty(
            record_value(order_record, "unique_id"),
            order_extra.get("order_unique_id"),
            entry_order_unique_id,
            trade_group_id,
            "",
        )
    )
    broker_order_id = to_text(
        first_non_empty(
            record_value(order_record, "broker_order_id"),
            record_value(order_record, "order_id"),
            order_extra.get("broker_order_id"),
            order_extra.get("order_id"),
            "",
        )
    )

    return {
        "environment": normalize_broker_mode(
            record_value(order_record, "environment") or order_extra.get("environment") or default_environment,
            default_environment,
        ),
        "symbol": to_text(record_value(order_record, "symbol") or order_extra.get("symbol")),
        "direction": direction,
        "target_state": "filled_position" if order_status == "Filled" else "pending_entry",
        "order_status": order_status,
        "relation_status": to_text(first_non_empty(record_value(order_record, "relation_status"), order_extra.get("relation_status"), "")),
        "position_side": to_text(first_non_empty(record_value(order_record, "position_side"), order_extra.get("position_side"), direction, "")),
        "signal_id": to_text(first_non_empty(record_value(order_record, "signal_id"), order_extra.get("signal_id"), "")),
        "trade_group_id": trade_group_id,
        "entry_order_unique_id": entry_order_unique_id,
        "order_unique_id": unique_id,
        "broker_order_id": broker_order_id,
        "entry_price": _to_number(
            first_non_empty(
                record_value(order_record, "fill_price"),
                record_value(order_record, "limit_price"),
                order_extra.get("fill_price"),
                order_extra.get("limit_price"),
                0,
            ),
            0,
        ),
        "quantity": _to_number(
            first_non_empty(
                record_value(order_record, "filled_qty"),
                record_value(order_record, "quantity"),
                order_extra.get("filled_qty"),
                order_extra.get("quantity"),
                0,
            ),
            0,
        ),
        "take_profit": _to_number(first_non_empty(record_value(order_record, "tp_price"), order_extra.get("tp_price"), 0), 0),
        "stop_loss": _to_number(first_non_empty(record_value(order_record, "sl_price"), order_extra.get("sl_price"), 0), 0),
        "order_record": order_record,
    }


def find_latest_active_entry_order(
    pb: Any,
    symbol: str,
    direction: str,
    environment: str,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
    per_page: int = ACTIVE_ENTRY_ORDER_LIMIT,
) -> Any:
    normalized_symbol = to_text(symbol).upper()
    normalized_direction = to_text(direction).lower()
    runtime_environment = normalize_broker_mode(environment, configured_broker_mode())
    records = list(
        pb.get_records(
            ORDERS_COLLECTION,
            filter=(
                f'symbol = "{escape_filter(normalized_symbol)}" && '
                f'environment = "{escape_filter(runtime_environment)}" && '
                'order_type = "Entry" && '
                '(status = "Submitted" || status = "Filled")'
            ),
            sort="-created",
            per_page=per_page,
            page=1,
        )
        or []
    )
    if not records:
        return None
    if not normalized_direction:
        return records[0]
    for record in records:
        if to_text(record_value(record, "direction")).lower() == normalized_direction:
            return record
    return records[0]


__all__ = ["build_order_context", "find_latest_active_entry_order"]
