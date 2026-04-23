from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.group_cancel import build_order_cancel_group_response
from ibkr_api.orders.values import to_text

CancelBrokerOrder = Callable[[str, str, dict[str, Any]], dict[str, Any]]


def build_order_cancel_sync_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    cancel_broker_order: CancelBrokerOrder,
) -> tuple[dict[str, Any], int]:
    next_payload = dict(payload or {})
    next_payload["source"] = to_text(next_payload.get("source") or "/api/custom/ibkr/orders/cancel_sync")
    next_payload["reason"] = to_text(next_payload.get("reason") or "页面取消主单")
    response, status_code = build_order_cancel_group_response(
        pb,
        payload=next_payload,
        normalize_environment=normalize_environment,
        escape_filter_string=escape_filter_string,
        cancel_broker_order=cancel_broker_order,
    )
    result = dict(response or {})
    result["action"] = "cancel_sync"
    if status_code == 200 and bool(result.get("ok")):
        result["message"] = "主单与系统订单已同步取消"
    result["source"] = "ibkr-api"
    return result, status_code


__all__ = ["build_order_cancel_sync_response"]
