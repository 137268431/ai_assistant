from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.group_common import CLOSE_GROUP_ACTION, append_group_order_detail, load_order_action_context, normalize_order_row
from ibkr_api.orders.group_common import build_group_status_patch
from ibkr_api.orders.values import to_text


CLOSE_STATUS_HINTS = {
    "Init": "只有成交的订单才能平仓",
    "Submitted": "请先取消挂单",
    "Canceled": "订单已取消，无法平仓",
    "Closed": "订单已平仓，无需重复操作",
}


def _warning_response(message: str, *, target_id: str, symbol: str, trade_group_id: str) -> tuple[dict[str, Any], int]:
    return (
        {
            "ok": False,
            "warning": True,
            "action": CLOSE_GROUP_ACTION,
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
            "action": CLOSE_GROUP_ACTION,
            "target_id": target_id,
            "source": "ibkr-api",
        },
        status_code,
    )


def build_order_close_group_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
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

    primary_row = context["primary_row"]
    if not primary_row:
        return _error_response("找不到订单", 404, target_id=target_id)

    primary_snapshot = normalize_order_row(primary_row)
    trade_group_id = context["trade_group_id"] or primary_snapshot["trade_group_id"]
    symbol = primary_snapshot["symbol"] or target_id
    status = primary_snapshot["status"]
    if status in CLOSE_STATUS_HINTS:
        return _warning_response(
            CLOSE_STATUS_HINTS[status],
            target_id=target_id,
            symbol=symbol,
            trade_group_id=trade_group_id,
        )
    if status != "Filled":
        return _warning_response(
            "只有成交的订单才能平仓",
            target_id=target_id,
            symbol=symbol,
            trade_group_id=trade_group_id,
        )

    related_rows = context["related_rows"] or [primary_row]
    entry_order_unique_id = to_text(
        primary_row.get("entry_order_unique_id") or primary_row.get("unique_id") or primary_snapshot["unique_id"]
    )
    updated_record_ids: list[str] = []
    closed_order_ids: list[str] = []
    cancelled_order_ids: list[str] = []
    detail_record_ids: list[str] = []
    source = to_text(payload.get("source")) or "orders/close_group"
    reason = to_text(payload.get("reason")) or "页面平仓交易组"

    for row in related_rows:
        snapshot = normalize_order_row(row)
        if snapshot["status"] in {"Canceled", "Closed"}:
            continue
        next_status = "Closed" if snapshot["unique_id"] == entry_order_unique_id else "Canceled"
        patch, event_times = build_group_status_patch(
            row,
            next_status=next_status,
            source=source,
            reason=reason,
        )
        updated_row = pb.update_record("orders", to_text(row.get("id")), patch)
        updated_record_ids.append(to_text((updated_row or {}).get("id") or row.get("id") or snapshot["unique_id"]))
        if next_status == "Closed":
            closed_order_ids.append(snapshot["unique_id"])
        else:
            cancelled_order_ids.append(snapshot["unique_id"])
        detail_row = append_group_order_detail(
            pb,
            updated_row if isinstance(updated_row, dict) else {**row, **patch},
            source=source,
            reason=reason,
            event_times=event_times,
            extra_patch={
                "previous_status": snapshot["status"],
                "action": "close_group",
                "trade_group_id": trade_group_id,
            },
        )
        detail_record_ids.append(to_text((detail_row or {}).get("id")))

    return (
        {
            "ok": True,
            "action": CLOSE_GROUP_ACTION,
            "target_id": target_id,
            "symbol": symbol,
            "environment": environment,
            "trade_group_id": trade_group_id,
            "closed_order_ids": closed_order_ids,
            "cancelled_order_ids": cancelled_order_ids,
            "updated_record_ids": updated_record_ids,
            "detail_record_ids": detail_record_ids,
            "source": "ibkr-api",
        },
        200,
    )
