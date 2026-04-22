from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.group_common import (
    CANCEL_GROUP_ACTION,
    append_group_order_detail,
    is_closed_status,
    load_order_action_context,
    normalize_order_row,
    resolve_cancelable_broker_order_id,
)
from ibkr_api.orders.group_common import build_group_status_patch
from ibkr_api.orders.values import to_text


CancelBrokerOrder = Callable[[str, str, dict[str, Any]], dict[str, Any]]


CANCEL_STATUS_HINTS = {
    "Filled": "订单已成交，无法取消",
    "Canceled": "订单已取消，无需重复操作",
    "Closed": "订单已平仓，无法取消",
}


def _warning_response(message: str, *, target_id: str, symbol: str, trade_group_id: str) -> tuple[dict[str, Any], int]:
    return (
        {
            "ok": False,
            "warning": True,
            "action": CANCEL_GROUP_ACTION,
            "message": message,
            "target_id": target_id,
            "symbol": symbol,
            "trade_group_id": trade_group_id,
            "source": "ibkr-api",
        },
        200,
    )


def _error_response(message: str, status_code: int, *, target_id: str) -> tuple[dict[str, Any], int]:
    return (
        {
            "ok": False,
            "error": message,
            "action": CANCEL_GROUP_ACTION,
            "target_id": target_id,
            "source": "ibkr-api",
        },
        status_code,
    )


def _cancel_open_broker_orders(
    cancel_ids: list[str],
    *,
    environment: str,
    payload: dict[str, Any],
    cancel_broker_order: CancelBrokerOrder,
) -> tuple[list[str], list[dict[str, Any]]]:
    cancelled_ids: list[str] = []
    failed_ids: list[dict[str, Any]] = []
    for order_id in cancel_ids:
        result = cancel_broker_order(environment, order_id, payload)
        if result.get("ok"):
            cancelled_ids.append(order_id)
            continue
        failed_ids.append(
            {
                "order_id": order_id,
                "error": to_text(
                    result.get("error")
                    or result.get("message")
                    or result.get("payload", {}).get("error")
                    or result.get("payload", {}).get("message")
                    or f"status_{result.get('status_code') or 500}"
                ),
            }
        )
    return cancelled_ids, failed_ids


def build_order_cancel_group_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    cancel_broker_order: CancelBrokerOrder,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    context = load_order_action_context(
        pb,
        payload=payload,
        environment=environment,
        escape_filter_string=escape_filter_string,
    )
    target_id = context["target_id"] or context["broker_lookup_id"]
    if not target_id:
        return _error_response("缺少订单ID", 400, target_id="")

    action_row = context["action_row"]
    primary_row = context["primary_row"] or action_row
    if not action_row or not primary_row:
        return _error_response("找不到订单", 404, target_id=target_id)

    action_snapshot = normalize_order_row(action_row)
    primary_snapshot = normalize_order_row(primary_row)
    trade_group_id = context["trade_group_id"] or primary_snapshot["trade_group_id"]
    symbol = primary_snapshot["symbol"] or action_snapshot["symbol"] or target_id

    if action_snapshot["role"] and action_snapshot["role"] != "entry":
        return _warning_response(
            "止盈/止损等子单不能直接取消，请操作主入场单",
            target_id=target_id,
            symbol=symbol,
            trade_group_id=trade_group_id,
        )
    if primary_snapshot["filled_qty"] > 0:
        return _warning_response(
            "主单已部分成交，不能直接取消，请改用平仓整组",
            target_id=target_id,
            symbol=symbol,
            trade_group_id=trade_group_id,
        )
    if primary_snapshot["status"] in CANCEL_STATUS_HINTS:
        return _warning_response(
            CANCEL_STATUS_HINTS[primary_snapshot["status"]],
            target_id=target_id,
            symbol=symbol,
            trade_group_id=trade_group_id,
        )

    related_rows = context["related_rows"] or [primary_row]
    cancel_ids: list[str] = []
    for row in related_rows:
        snapshot = normalize_order_row(row)
        if is_closed_status(snapshot["status"]):
            continue
        cancel_id = resolve_cancelable_broker_order_id(row)
        if cancel_id and cancel_id not in cancel_ids:
            cancel_ids.append(cancel_id)

    cancelled_ids: list[str] = []
    failed_ids: list[dict[str, Any]] = []
    if cancel_ids:
        cancelled_ids, failed_ids = _cancel_open_broker_orders(
            cancel_ids,
            environment=environment,
            payload=payload,
            cancel_broker_order=cancel_broker_order,
        )
    if failed_ids:
        return (
            {
                "ok": False,
                "error": f"账户撤单失败 {len(failed_ids)} 条",
                "action": CANCEL_GROUP_ACTION,
                "target_id": target_id,
                "trade_group_id": trade_group_id,
                "cancelled_order_ids": cancelled_ids,
                "failed_order_ids": failed_ids,
                "source": "ibkr-api",
            },
            500,
        )

    updated_record_ids: list[str] = []
    detail_record_ids: list[str] = []
    source = to_text(payload.get("source")) or "orders/cancel_group"
    reason = to_text(payload.get("reason")) or "页面取消主单"
    for row in related_rows:
        snapshot = normalize_order_row(row)
        if is_closed_status(snapshot["status"]):
            continue
        patch, event_times = build_group_status_patch(
            row,
            next_status="Canceled",
            source=source,
            reason=reason,
        )
        updated_row = pb.update_record("orders", to_text(row.get("id")), patch)
        updated_record_ids.append(to_text((updated_row or {}).get("id") or row.get("id") or snapshot["unique_id"]))
        detail_row = append_group_order_detail(
            pb,
            updated_row if isinstance(updated_row, dict) else {**row, **patch},
            source=source,
            reason=reason,
            event_times=event_times,
            extra_patch={
                "previous_status": snapshot["status"],
                "action": "cancel",
                "trade_group_id": trade_group_id,
                "cancelled_order_ids": list(cancelled_ids),
            },
        )
        detail_record_ids.append(to_text((detail_row or {}).get("id")))

    return (
        {
            "ok": True,
            "action": CANCEL_GROUP_ACTION,
            "target_id": target_id,
            "symbol": symbol,
            "environment": environment,
            "trade_group_id": trade_group_id,
            "cancelled_order_ids": cancelled_ids,
            "failed_order_ids": [],
            "updated_record_ids": updated_record_ids,
            "detail_record_ids": detail_record_ids,
            "source": "ibkr-api",
        },
        200,
    )
