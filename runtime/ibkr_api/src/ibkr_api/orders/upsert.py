from __future__ import annotations

import json
from typing import Any, Callable

from ibkr_api.modes import request_broker_mode
from ibkr_api.orders.details import build_order_detail_payload
from ibkr_api.orders.relationships import get_order_status_transition_text, resolve_order_relationship
from ibkr_api.orders.timestamps import resolve_order_status_event_times
from ibkr_api.orders.values import ensure_object, first_defined, to_float, to_int, to_text


def build_order_record_payload(payload: dict[str, Any], existing_row: dict[str, Any] | None, environment: str) -> dict[str, Any]:
    existing = existing_row or {}
    existing_extra = ensure_object(existing.get("extra"))
    extra = {
        **existing_extra,
        **ensure_object(payload.get("extra")),
        "environment": environment,
    }
    previous_status = to_text(existing.get("status"))
    status = to_text(payload.get("status") or "Submitted") or "Submitted"
    resolved_order_id = first_defined(payload.get("order_id"), existing.get("order_id"), existing_extra.get("order_id"), "") or ""
    resolved_direction = first_defined(payload.get("direction"), existing.get("direction"), existing_extra.get("direction"), "") or ""
    resolved_quantity = first_defined(payload.get("quantity"), existing.get("quantity"), existing_extra.get("quantity"), 0)
    incoming_limit_price = payload.get("limit_price")
    incoming_role = to_text(first_defined(payload.get("role"), existing.get("role"), existing_extra.get("role"), ""))
    incoming_order_type = to_text(first_defined(payload.get("order_type"), existing.get("order_type"), existing_extra.get("order_type"), "")).lower()
    incoming_stop_trigger = first_defined(
        payload.get("auxPrice"),
        payload.get("aux_price"),
        payload.get("stop_price"),
        extra.get("auxPrice"),
        extra.get("aux_price"),
        extra.get("stop_price"),
        None,
    )
    is_stop_order = incoming_role in {"stop_loss", "repair_sl"} or incoming_order_type in {"stp", "stop", "stoploss"}
    if is_stop_order and to_float(incoming_limit_price) == 0:
        incoming_limit_price = incoming_stop_trigger if (to_float(incoming_stop_trigger) or 0) > 0 else None
    resolved_limit_price = first_defined(incoming_limit_price, existing.get("limit_price"), existing_extra.get("limit_price"), 0)
    resolved_filled_qty = first_defined(
        payload.get("filled_qty"),
        payload.get("quantity") if status == "Filled" else None,
        existing.get("filled_qty"),
        existing_extra.get("filled_qty"),
        payload.get("quantity"),
        0,
    )
    resolved_fill_price = first_defined(payload.get("fill_price"), existing.get("fill_price"), existing_extra.get("fill_price"), 0)
    resolved_signal_id = first_defined(payload.get("signal_id"), existing.get("signal_id"), existing_extra.get("signal_id"), "") or ""
    relation = resolve_order_relationship(payload, existing_row)
    event_times = resolve_order_status_event_times(
        existing_row,
        {
            "status": status,
            "previous_status": previous_status,
            "us_time": first_defined(payload.get("us_time"), extra.get("us_time")),
            "cn_time": first_defined(payload.get("cn_time"), extra.get("cn_time")),
            "bar_time_ms": payload.get("bar_time_ms") if payload.get("bar_time_ms") is not None else extra.get("bar_time_ms"),
        },
    )
    resolved_order_time = first_defined(payload.get("order_time"), extra.get("order_time"), existing.get("order_time"), event_times["us_time"]) or event_times["us_time"]
    resolved_fill_time = first_defined(payload.get("fill_time"), existing.get("fill_time"), existing_extra.get("fill_time"), "")
    status_history_previous = (
        previous_status
        if previous_status and previous_status != status
        else to_text(existing_extra.get("previous_status"))
    )
    patch_extra = {
        **extra,
        "order_id": str(resolved_order_id or ""),
        "broker_order_id": relation["broker_order_id"],
        "order_time": str(resolved_order_time or ""),
        "us_time": event_times["us_time"],
        "cn_time": event_times["cn_time"],
        "bar_time_ms": event_times["bar_time_ms"],
        "trade_group_id": relation["trade_group_id"],
        "entry_order_unique_id": relation["entry_order_unique_id"],
        "parent_order_unique_id": relation["parent_order_unique_id"],
        "sibling_order_unique_id": relation["sibling_order_unique_id"],
        "role": relation["role"],
        "relation_status": relation["relation_status"],
        "position_side": relation["position_side"],
        "previous_status": status_history_previous,
        "current_status": status,
        "status_transition_text": get_order_status_transition_text(previous_status, status),
        "status_updated_us_time": event_times["us_time"],
        "status_updated_cn_time": event_times["cn_time"],
        "status_updated_bar_time_ms": event_times["bar_time_ms"],
        "last_status_source": "orders/upsert",
        "last_status_reason": str(extra.get("reason") or ""),
    }
    if not existing_extra.get("created_us_time") and not previous_status:
        patch_extra["created_us_time"] = event_times["us_time"]
        patch_extra["created_cn_time"] = event_times["cn_time"]
        patch_extra["created_bar_time_ms"] = event_times["bar_time_ms"]
    if resolved_fill_time:
        patch_extra["fill_time"] = resolved_fill_time
    if status == "Filled":
        patch_extra["filled_us_time"] = str(first_defined(payload.get("fill_us_time"), resolved_fill_time, event_times["us_time"]) or event_times["us_time"])
        patch_extra["filled_cn_time"] = str(first_defined(payload.get("fill_cn_time"), event_times["cn_time"]) or event_times["cn_time"])
        patch_extra["filled_bar_time_ms"] = to_int(first_defined(payload.get("fill_bar_time_ms"), event_times["bar_time_ms"]), event_times["bar_time_ms"])
    elif existing_extra.get("filled_us_time"):
        patch_extra["filled_us_time"] = existing_extra.get("filled_us_time")
        patch_extra["filled_cn_time"] = existing_extra.get("filled_cn_time") or ""
        patch_extra["filled_bar_time_ms"] = existing_extra.get("filled_bar_time_ms") or 0

    order_payload = {
        "unique_id": to_text(payload.get("unique_id") or existing.get("unique_id")),
        "order_type": to_text(payload.get("order_type") or existing.get("order_type")),
        "order_id": str(resolved_order_id or ""),
        "broker_order_id": relation["broker_order_id"],
        "symbol": to_text(payload.get("symbol") or existing.get("symbol")),
        "environment": environment,
        "direction": str(resolved_direction or ""),
        "quantity": resolved_quantity,
        "limit_price": resolved_limit_price,
        "status": status,
        "filled_qty": resolved_filled_qty,
        "fill_price": resolved_fill_price,
        "signal_id": str(resolved_signal_id or ""),
        "bar_time_ms": event_times["bar_time_ms"],
        "us_time": event_times["us_time"],
        "cn_time": event_times["cn_time"],
        "order_time": str(resolved_order_time or ""),
        "fill_time": str(resolved_fill_time or ""),
        "trade_group_id": relation["trade_group_id"],
        "entry_order_unique_id": relation["entry_order_unique_id"],
        "parent_order_unique_id": relation["parent_order_unique_id"],
        "sibling_order_unique_id": relation["sibling_order_unique_id"],
        "role": relation["role"],
        "relation_status": relation["relation_status"],
        "position_side": relation["position_side"],
        "extra": patch_extra,
    }
    for field in ("tp_price", "sl_price", "pnl", "commission", "rr_ratio"):
        resolved = first_defined(payload.get(field), existing.get(field), existing_extra.get(field), None)
        if resolved is not None and resolved != "":
            order_payload[field] = resolved
    return order_payload


ORDER_HEARTBEAT_EXTRA_KEYS = {
    "broker_callback_received_at",
    "broker_callback_received_at_ms",
    "broker_callback_source",
    "broker_realtime_callback",
    "ib_callback_type",
    "us_time",
    "cn_time",
    "bar_time_ms",
    "order_time",
    "status_updated_us_time",
    "status_updated_cn_time",
    "status_updated_bar_time_ms",
    "last_status_source",
    "last_status_reason",
    "previous_status",
    "current_status",
    "status_transition_text",
}

ORDER_REALTIME_STATUS_CONFIRMATION_STATUSES = {
    "filled",
    "closed",
    "executed",
    "canceled",
    "cancelled",
    "rejected",
    "expired",
    "inactive",
}


def _comparable_extra(extra: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in ensure_object(extra).items()
        if str(key) not in ORDER_HEARTBEAT_EXTRA_KEYS
    }


def is_idempotent_order_payload(existing_row: dict[str, Any] | None, next_payload: dict[str, Any]) -> bool:
    if not existing_row or not existing_row.get("id"):
        return False
    comparable_fields = (
        "unique_id",
        "order_type",
        "order_id",
        "broker_order_id",
        "symbol",
        "environment",
        "direction",
        "quantity",
        "limit_price",
        "status",
        "filled_qty",
        "fill_price",
        "signal_id",
        "trade_group_id",
        "entry_order_unique_id",
        "parent_order_unique_id",
        "sibling_order_unique_id",
        "role",
        "relation_status",
        "position_side",
        "order_time",
        "fill_time",
        "tp_price",
        "sl_price",
        "pnl",
        "commission",
        "rr_ratio",
    )
    for field in comparable_fields:
        left = existing_row.get(field)
        right = next_payload.get(field)
        if isinstance(left, (int, float)) or isinstance(right, (int, float)):
            if to_float(left) != to_float(right):
                return False
        elif str(left or "") != str(right or ""):
            return False
    left_extra = ensure_object(existing_row.get("extra"))
    right_extra = ensure_object(next_payload.get("extra"))
    if (
        to_text(next_payload.get("status")).lower() in ORDER_REALTIME_STATUS_CONFIRMATION_STATUSES
        and to_text(right_extra.get("broker_realtime_callback")).lower() in {"1", "true", "yes", "y", "on"}
        and to_text(left_extra.get("broker_realtime_callback")).lower() not in {"1", "true", "yes", "y", "on"}
    ):
        return False
    return json.dumps(_comparable_extra(left_extra), sort_keys=True, ensure_ascii=True) == json.dumps(
        _comparable_extra(right_extra),
        sort_keys=True,
        ensure_ascii=True,
    )


def build_order_upsert_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    notify_order_status: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    notify_order_callback_ledger: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    environment = request_broker_mode(payload)
    unique_id = to_text(payload.get("unique_id"))
    order_type = to_text(payload.get("order_type"))
    symbol = to_text(payload.get("symbol"))
    if not unique_id or not order_type or not symbol:
        return {"error": "Missing required fields"}, 400

    existing_row = pb.get_first_record(
        "orders",
        filter=(
            f'unique_id = "{escape_filter_string(unique_id)}" && '
            f'environment = "{escape_filter_string(environment)}"'
        ),
    )
    existing_dict = existing_row if isinstance(existing_row, dict) else None
    next_payload = build_order_record_payload(payload, existing_dict, environment)
    is_idempotent = is_idempotent_order_payload(existing_dict, next_payload)
    previous_status = to_text((existing_dict or {}).get("status"))
    status = to_text(next_payload.get("status"))
    reason = to_text(ensure_object(next_payload.get("extra")).get("reason"))

    if existing_dict and existing_dict.get("id"):
        if not is_idempotent:
            saved_order = pb.update_record("orders", str(existing_dict.get("id")), next_payload)
        else:
            saved_order = dict(existing_dict)
    else:
        saved_order = pb.create_record("orders", next_payload)

    if not is_idempotent:
        detail_payload = build_order_detail_payload(
            pb,
            saved_order if isinstance(saved_order, dict) else next_payload,
            source="orders/upsert",
            reason=reason,
        )
        detail_row = pb.create_record("ibkr_order_details", detail_payload)
    else:
        detail_row = {}

    response_order = saved_order if isinstance(saved_order, dict) else next_payload
    trade_ledger_notification: dict[str, Any] = {}
    response_extra = ensure_object(response_order.get("extra"))
    is_realtime_callback = to_text(response_extra.get("broker_realtime_callback")).lower() in {"1", "true", "yes", "y", "on"}
    if (
        not is_idempotent
        and environment in {"live", "paper"}
        and is_realtime_callback
        and callable(notify_order_callback_ledger)
    ):
        try:
            trade_ledger_notification = dict(
                notify_order_callback_ledger(
                    status,
                    response_order,
                    {
                        "previous_order": existing_dict or {},
                    },
                )
                or {}
            )
        except Exception as exc:
            trade_ledger_notification = {"success": False, "error": str(exc), "skipped": True}

    notification_result: dict[str, Any] = {}
    if not is_idempotent and callable(notify_order_status):
        transition_text = get_order_status_transition_text(previous_status, status)
        message = "订单已提交" if status == "Submitted" else transition_text
        try:
            notification_result = dict(
                notify_order_status(
                    status,
                    response_order,
                    {
                        "message": message,
                        "message_id": to_text(ensure_object(response_order.get("extra")).get("feishu_order_message_id")),
                    },
                )
                or {}
            )
        except Exception as exc:
            notification_result = {"success": False, "error": str(exc), "skipped": True}
    return (
        {
            "success": True,
            "source": "ibkr-api",
            "idempotent": bool(is_idempotent),
            "previous_status": previous_status,
            "detail_created": not is_idempotent,
            "detail_record_id": str((detail_row or {}).get("id") or ""),
            "notification_mode": "order_group_card",
            "notification": notification_result,
            "trade_ledger_notification": trade_ledger_notification,
            "order": {
                "id": response_order.get("id") or "",
                "unique_id": response_order.get("unique_id") or unique_id,
                "order_type": response_order.get("order_type") or order_type,
                "order_id": response_order.get("order_id") or "",
                "symbol": response_order.get("symbol") or symbol,
                "status": status,
                "environment": response_order.get("environment") or environment,
            },
        },
        200,
    )
