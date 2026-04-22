from __future__ import annotations

from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def derive_protection_unique_ids(entry_unique_id: Any) -> dict[str, str]:
    text = str(entry_unique_id or "").strip()
    base = text[:-6] if text.endswith("_entry") else text
    return {
        "tp": f"{base}_take_profit" if base else "",
        "sl": f"{base}_stop_loss" if base else "",
    }


def build_signal_ack_orders(
    signal_record: dict[str, Any],
    payload: dict[str, Any],
    environment: str,
) -> list[dict[str, Any]]:
    order_data = _as_dict(payload.get("order"))
    if not (order_data.get("unique_id") and order_data.get("order_type")):
        return []

    order_extra = _as_dict(order_data.get("extra"))
    signal_id = str(payload.get("signal_id") or signal_record.get("signal_id") or "").strip()
    symbol = str(signal_record.get("symbol") or "").strip()
    direction = str(order_data.get("direction") or signal_record.get("direction") or "").strip()
    resolved_quantity = (
        order_data.get("quantity")
        if order_data.get("quantity") is not None
        else signal_record.get("shares") or order_extra.get("quantity") or 0
    )
    resolved_limit_price = (
        order_data.get("limit_price")
        if order_data.get("limit_price") is not None
        else signal_record.get("entry") or order_extra.get("limit_price") or 0
    )
    resolved_stop_loss = (
        order_data.get("stop_loss")
        if order_data.get("stop_loss") is not None
        else signal_record.get("stop_loss") or order_extra.get("sl_price") or 0
    )
    resolved_take_profit = (
        order_data.get("take_profit")
        if order_data.get("take_profit") is not None
        else signal_record.get("take_profit") or order_extra.get("tp_price") or 0
    )
    entry_order_unique_id = str(
        order_data.get("entry_order_unique_id")
        or order_extra.get("entry_order_unique_id")
        or order_data.get("unique_id")
        or ""
    ).strip()
    trade_group_id = str(
        order_data.get("trade_group_id")
        or order_extra.get("trade_group_id")
        or entry_order_unique_id
    ).strip()
    child_orders_input = payload.get("child_orders")
    if not isinstance(child_orders_input, list):
        child_orders_input = order_data.get("child_orders") if isinstance(order_data.get("child_orders"), list) else []
    protection_ids = derive_protection_unique_ids(entry_order_unique_id)
    shared_fields = {
        "symbol": symbol,
        "environment": environment,
        "direction": direction,
        "position_side": str(order_data.get("position_side") or direction or "").strip(),
        "quantity": resolved_quantity,
        "trade_group_id": trade_group_id,
        "entry_order_unique_id": entry_order_unique_id,
        "signal_id": signal_id,
        "order_time": (
            order_data.get("order_time")
            or order_extra.get("order_time")
            or order_data.get("us_time")
            or signal_record.get("us_time")
            or ""
        ),
        "us_time": order_data.get("us_time") or order_extra.get("us_time") or signal_record.get("us_time") or "",
        "cn_time": order_data.get("cn_time") or order_extra.get("cn_time") or signal_record.get("cn_time") or "",
        "bar_time_ms": (
            order_data.get("bar_time_ms")
            or order_extra.get("bar_time_ms")
            or signal_record.get("bar_time_ms")
            or 0
        ),
    }

    entry_order = {
        **order_data,
        **shared_fields,
        "unique_id": order_data.get("unique_id"),
        "order_type": order_data.get("order_type") or "Entry",
        "role": order_data.get("role") or order_extra.get("role") or "entry",
        "relation_status": order_data.get("relation_status") or order_extra.get("relation_status") or "active",
        "limit_price": resolved_limit_price,
        "status": order_data.get("status") or "Init",
        "filled_qty": order_data.get("filled_qty") if order_data.get("filled_qty") is not None else 0,
        "fill_price": order_data.get("fill_price") if order_data.get("fill_price") is not None else 0,
        "stop_loss": resolved_stop_loss,
        "take_profit": resolved_take_profit,
    }

    def resolve_child_input(kind: str) -> dict[str, Any]:
        for child in child_orders_input:
            child_item = _as_dict(child)
            role = str(child_item.get("role") or "").strip()
            order_type = str(child_item.get("order_type") or "").strip()
            if kind == "take_profit" and role in {"take_profit", "repair_tp"}:
                return child_item
            if kind == "stop_loss" and role in {"stop_loss", "repair_sl"}:
                return child_item
            if kind == "take_profit" and order_type == "TakeProfit":
                return child_item
            if kind == "stop_loss" and order_type == "StopLoss":
                return child_item
        return {}

    def build_child_order(kind: str, default_price: Any, default_unique_id: str, sibling_unique_id: str) -> dict[str, Any] | None:
        child_input = resolve_child_input(kind)
        child_extra = _as_dict(child_input.get("extra"))
        child_price = (
            child_input.get("limit_price")
            if child_input.get("limit_price") is not None
            else child_extra.get("limit_price")
            if child_extra.get("limit_price") is not None
            else default_price
        )
        if not child_price:
            return None
        is_take_profit = kind == "take_profit"
        return {
            **child_input,
            **shared_fields,
            "unique_id": child_input.get("unique_id") or child_extra.get("unique_id") or default_unique_id,
            "order_type": child_input.get("order_type") or ("TakeProfit" if is_take_profit else "StopLoss"),
            "role": child_input.get("role") or child_extra.get("role") or kind,
            "relation_status": child_input.get("relation_status") or child_extra.get("relation_status") or "planned",
            "parent_order_unique_id": (
                child_input.get("parent_order_unique_id")
                or child_extra.get("parent_order_unique_id")
                or entry_order_unique_id
            ),
            "sibling_order_unique_id": (
                child_input.get("sibling_order_unique_id")
                or child_extra.get("sibling_order_unique_id")
                or sibling_unique_id
            ),
            "limit_price": child_price,
            "status": child_input.get("status") or child_extra.get("status") or "Init",
            "filled_qty": child_input.get("filled_qty") if child_input.get("filled_qty") is not None else 0,
            "fill_price": child_input.get("fill_price") if child_input.get("fill_price") is not None else 0,
            "broker_order_id": child_input.get("broker_order_id") or child_input.get("order_id") or "",
        }

    orders = [entry_order]
    take_profit_order = build_child_order("take_profit", resolved_take_profit, protection_ids["tp"], protection_ids["sl"])
    stop_loss_order = build_child_order("stop_loss", resolved_stop_loss, protection_ids["sl"], protection_ids["tp"])
    if take_profit_order:
        orders.append(take_profit_order)
    if stop_loss_order:
        orders.append(stop_loss_order)
    return orders
