from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.group_cancel import CancelBrokerOrder
from ibkr_api.orders.group_common import (
    append_group_order_detail,
    build_group_status_patch,
    dedupe_order_rows,
    is_closed_status,
    normalize_order_row,
    pick_primary_order_row,
    resolve_cancelable_broker_order_id,
)
from ibkr_api.orders.values import ensure_object, first_defined, to_text


ORDER_QUERY_SORT = "-created,-updated,-bar_time_ms"
ORDER_QUERY_LIMIT = 200
OrderStatusNotifier = Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]


def _query_signal_orders(
    pb: Any,
    *,
    signal_id: str,
    environment: str,
    escape_filter_string: Callable[[Any], str],
) -> list[dict[str, Any]]:
    rows = pb.get_records(
        "orders",
        filter=(
            f'signal_id = "{escape_filter_string(signal_id)}" && '
            f'environment = "{escape_filter_string(environment)}"'
        ),
        sort=ORDER_QUERY_SORT,
        per_page=ORDER_QUERY_LIMIT,
        page=1,
    )
    return [dict(row) for row in rows or []]


def _cancel_open_broker_orders(
    order_ids: list[str],
    *,
    environment: str,
    payload: dict[str, Any],
    cancel_broker_order: CancelBrokerOrder,
) -> tuple[list[str], list[dict[str, Any]]]:
    cancelled_ids: list[str] = []
    failed_ids: list[dict[str, Any]] = []
    for order_id in order_ids:
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


def build_signal_cancel_order_summary(
    pb: Any,
    *,
    signal_id: str,
    environment: str,
    escape_filter_string: Callable[[Any], str],
    cancel_broker_order: CancelBrokerOrder,
    source: str,
    reason: str,
) -> dict[str, Any]:
    related_rows = dedupe_order_rows(
        _query_signal_orders(
            pb,
            signal_id=signal_id,
            environment=environment,
            escape_filter_string=escape_filter_string,
        )
    )
    primary_row = pick_primary_order_row(related_rows)
    primary_snapshot = normalize_order_row(primary_row)
    trade_group_id = primary_snapshot.get("trade_group_id", "")
    symbol = primary_snapshot.get("symbol", "")

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
            payload={"signal_id": signal_id, "source": source, "reason": reason},
            cancel_broker_order=cancel_broker_order,
        )

    if failed_ids:
        return {
            "ok": False,
            "signal_id": signal_id,
            "trade_group_id": trade_group_id,
            "symbol": symbol,
            "cancelled_order_ids": cancelled_ids,
            "failed_order_ids": failed_ids,
            "updated_record_ids": [],
            "detail_record_ids": [],
            "source": "ibkr-api",
        }

    updated_record_ids: list[str] = []
    detail_record_ids: list[str] = []
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

    return {
        "ok": True,
        "signal_id": signal_id,
        "trade_group_id": trade_group_id,
        "symbol": symbol,
        "cancelled_order_ids": cancelled_ids,
        "failed_order_ids": [],
        "updated_record_ids": updated_record_ids,
        "detail_record_ids": detail_record_ids,
        "source": "ibkr-api",
    }


def cancel_signal_related_orders(
    pb: Any,
    *,
    signal_id: str,
    environment: str,
    escape_filter_string: Callable[[Any], str],
    cancel_broker_order: CancelBrokerOrder,
    notify_order_status: OrderStatusNotifier | None = None,
    source: str = "webhook/signal/cancel",
    reason: str = "manual_cancel",
) -> dict[str, Any]:
    summary = build_signal_cancel_order_summary(
        pb,
        signal_id=signal_id,
        environment=environment,
        escape_filter_string=escape_filter_string,
        cancel_broker_order=cancel_broker_order,
        source=source,
        reason=reason,
    )
    if not summary.get("ok") or not callable(notify_order_status):
        return summary

    related_rows = dedupe_order_rows(
        _query_signal_orders(
            pb,
            signal_id=signal_id,
            environment=environment,
            escape_filter_string=escape_filter_string,
        )
    )
    primary_row = pick_primary_order_row(related_rows)
    if not primary_row:
        return summary

    current_message_id = to_text(ensure_object(primary_row.get("extra")).get("feishu_order_message_id"))
    try:
        result = dict(
            notify_order_status(
                "canceled",
                primary_row,
                {
                    "message": "主单已撤销，关联挂单已收尾"
                    if list(summary.get("cancelled_order_ids") or [])
                    else "交易组已取消",
                    "message_id": current_message_id,
                    "messageId": current_message_id,
                },
            )
            or {}
        )
    except Exception:
        return summary

    next_message_id = to_text(first_defined(result.get("message_id"), result.get("messageId")))
    if not bool(result.get("success") or result.get("ok")) or not next_message_id or next_message_id == current_message_id:
        return summary

    pb.update_record(
        "orders",
        to_text(primary_row.get("id")),
        {
            "extra": {
                **ensure_object(primary_row.get("extra")),
                "feishu_order_message_id": next_message_id,
                "feishu_order_card_version": 2,
            }
        },
    )
    return summary
