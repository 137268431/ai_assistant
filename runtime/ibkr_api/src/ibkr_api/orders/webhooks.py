from __future__ import annotations

from typing import Any, Callable

from ibkr_api.modes import request_broker_mode
from ibkr_api.orders.group_cancel import CancelBrokerOrder, build_order_cancel_group_response
from ibkr_api.orders.group_close import build_order_close_group_response
from ibkr_api.orders.group_common import load_order_action_context, normalize_order_row
from ibkr_api.orders.values import to_text
from ibkr_api.webhooks.pages import fail_page, ok_page, warn_page


HTML_CONTENT_TYPE = "text/html; charset=utf-8"

CancelPageRenderer = Callable[[Any, Any, Any], str]
NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]

_CANCEL_WARNING_PAGE_MAP = {
    "止盈/止损等子单不能直接取消，请操作主入场单": ("只能取消主单", "止盈/止损等子单不能直接取消，请操作主入场单"),
    "主单已部分成交，不能直接取消，请改用平仓整组": ("主单已部分成交", "主单已部分成交，不能直接取消，请改用平仓整组"),
    "订单已成交，无法取消": ("订单已成交", "订单已成交，无法取消"),
    "订单已取消，无需重复操作": ("订单已取消", "无需重复操作"),
    "订单已平仓，无法取消": ("订单已平仓", "无法取消"),
}

_CLOSE_WARNING_PAGE_MAP = {
    "只有成交的订单才能平仓": ("订单未成交", "只有成交的订单才能平仓"),
    "请先取消挂单": ("订单未成交", "请先取消挂单"),
    "订单已取消，无法平仓": ("订单已取消", "无法平仓"),
    "订单已平仓，无需重复操作": ("订单已平仓", "无需重复操作"),
}


def _page_response(body: str, *, status_code: int, title: str, detail: str, symbol: str, page_kind: str, action: str) -> tuple[dict[str, Any], int]:
    return (
        {
            "body": body,
            "content_type": HTML_CONTENT_TYPE,
            "title": title,
            "detail": detail,
            "symbol": symbol,
            "page_kind": page_kind,
            "action": action,
        },
        int(status_code or 200),
    )


def _fail_response(title: str, detail: str, symbol: str = "", *, status_code: int, action: str) -> tuple[dict[str, Any], int]:
    return _page_response(
        fail_page(title, detail, symbol),
        status_code=status_code,
        title=title,
        detail=detail,
        symbol=symbol,
        page_kind="fail",
        action=action,
    )


def _warn_response(title: str, detail: str, symbol: str, *, action: str) -> tuple[dict[str, Any], int]:
    return _page_response(
        warn_page(title, detail, symbol),
        status_code=200,
        title=title,
        detail=detail,
        symbol=symbol,
        page_kind="warn",
        action=action,
    )


def _ok_response(title: str, detail: str, symbol: str, *, action: str) -> tuple[dict[str, Any], int]:
    return _page_response(
        ok_page(title, detail, symbol),
        status_code=200,
        title=title,
        detail=detail,
        symbol=symbol,
        page_kind="ok",
        action=action,
    )


def _resolve_payload_target(payload: dict[str, Any] | None) -> str:
    data = payload or {}
    return to_text(
        data.get("id")
        or data.get("unique_id")
        or data.get("entry_order_unique_id")
        or data.get("trade_group_id")
        or data.get("order_id")
    )


def _resolve_context_summary(
    pb: Any,
    *,
    payload: dict[str, Any],
    environment: str,
    escape_filter_string: EscapeFilterString,
) -> dict[str, Any]:
    context = load_order_action_context(
        pb,
        payload=payload,
        environment=environment,
        escape_filter_string=escape_filter_string,
    )
    action_row = context.get("action_row")
    primary_row = context.get("primary_row") or action_row
    snapshot = normalize_order_row(primary_row or action_row)
    return {
        "context": context,
        "target_id": to_text(context.get("target_id") or context.get("broker_lookup_id") or _resolve_payload_target(payload)),
        "symbol": snapshot.get("symbol") or _resolve_payload_target(payload),
        "trade_group_id": to_text(context.get("trade_group_id") or snapshot.get("trade_group_id")),
    }


def _warning_page_metadata(action: str, message: str) -> tuple[str, str]:
    normalized_action = to_text(action)
    normalized_message = to_text(message)
    if normalized_action == "cancel_group":
        return _CANCEL_WARNING_PAGE_MAP.get(normalized_message, ("订单无法取消", normalized_message or "订单当前状态无法取消"))
    return _CLOSE_WARNING_PAGE_MAP.get(normalized_message, ("订单无法平仓", normalized_message or "订单当前状态无法平仓"))


def build_order_cancel_webhook_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    cancel_broker_order: CancelBrokerOrder,
) -> tuple[dict[str, Any], int]:
    target_id = _resolve_payload_target(payload)
    if not target_id:
        return _fail_response("参数错误", "缺少订单ID", status_code=400, action="cancel_group")

    environment = request_broker_mode(payload)
    context_summary = _resolve_context_summary(
        pb,
        payload={**dict(payload or {}), "id": target_id},
        environment=environment,
        escape_filter_string=escape_filter_string,
    )
    if not context_summary["context"].get("action_row"):
        return _fail_response("订单不存在", "找不到订单", target_id, status_code=404, action="cancel_group")

    action_payload = {
        **dict(payload or {}),
        "id": target_id,
        "environment": environment,
        "source": to_text((payload or {}).get("source")) or "webhook/order/cancel",
        "reason": to_text((payload or {}).get("reason")) or "页面取消主单",
    }
    result, status_code = build_order_cancel_group_response(
        pb,
        payload=action_payload,
        normalize_environment=normalize_environment,
        escape_filter_string=escape_filter_string,
        cancel_broker_order=cancel_broker_order,
    )
    symbol = to_text(result.get("symbol") or context_summary["symbol"] or target_id)

    if bool(result.get("warning")) and int(status_code or 200) < 400:
        title, detail = _warning_page_metadata(result.get("action") or "cancel_group", result.get("message"))
        return _warn_response(title, detail, symbol, action="cancel_group")
    if int(status_code or 200) >= 400:
        detail = to_text(result.get("error") or result.get("message") or "订单取消失败")
        title = "订单取消失败" if int(status_code or 200) >= 500 else "订单无法取消"
        return _fail_response(title, detail, symbol, status_code=int(status_code or 500), action="cancel_group")
    return _ok_response("订单已取消", "状态已更新", symbol, action="cancel_group")


def build_order_close_webhook_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
) -> tuple[dict[str, Any], int]:
    target_id = _resolve_payload_target(payload)
    if not target_id:
        return _fail_response("参数错误", "缺少订单ID", status_code=400, action="close_group")

    environment = request_broker_mode(payload)
    context_summary = _resolve_context_summary(
        pb,
        payload={**dict(payload or {}), "id": target_id},
        environment=environment,
        escape_filter_string=escape_filter_string,
    )
    if not context_summary["context"].get("primary_row"):
        return _fail_response("订单不存在", "找不到订单", target_id, status_code=404, action="close_group")

    trade_group_id = context_summary["trade_group_id"]
    reason = to_text((payload or {}).get("reason")) or (
        f"页面平仓交易组 {trade_group_id}" if trade_group_id else "页面平仓交易组"
    )
    result, status_code = build_order_close_group_response(
        pb,
        payload={
            **dict(payload or {}),
            "id": target_id,
            "environment": environment,
            "source": to_text((payload or {}).get("source")) or "webhook/order/close",
            "reason": reason,
        },
        normalize_environment=normalize_environment,
        escape_filter_string=escape_filter_string,
    )
    symbol = to_text(result.get("symbol") or context_summary["symbol"] or target_id)

    if bool(result.get("warning")) and int(status_code or 200) < 400:
        title, detail = _warning_page_metadata(result.get("action") or "close_group", result.get("message"))
        return _warn_response(title, detail, symbol, action="close_group")
    if int(status_code or 200) >= 400:
        detail = to_text(result.get("error") or result.get("message") or "交易组平仓失败")
        title = "交易组平仓失败" if int(status_code or 200) >= 500 else "订单无法平仓"
        return _fail_response(title, detail, symbol, status_code=int(status_code or 500), action="close_group")
    return _ok_response("交易组已平仓", "状态已更新", symbol, action="close_group")
