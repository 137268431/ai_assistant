from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.group_cancel import CancelBrokerOrder
from ibkr_api.orders.values import to_text
from ibkr_api.signals.notifications import SignalStatusNotifier, apply_signal_status_notification, sync_signal_status_notification
from ibkr_api.signals.order_cancel import OrderStatusNotifier, build_signal_cancel_order_summary, cancel_signal_related_orders
from ibkr_api.signals.values import load_signal_record, merge_signal_extra, now_iso_utc, signal_status, signal_symbol
from ibkr_api.webhooks.pages import fail_page, ok_page, warn_page


HTML_CONTENT_TYPE = "text/html; charset=utf-8"

NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]
UpdateSignalCard = Callable[[str, dict[str, Any], str], dict[str, Any]]

_CONFIRM_STATUS_HINTS = {
    "expired": ("fail", "信号已过期", "该信号超时自动失效，无法操作"),
    "rejected": ("fail", "信号已拒绝", "该信号已被拒绝，无法重复操作"),
    "executed": ("ok", "信号已执行", "该信号已执行，无需重复操作"),
    "pending": ("ok", "信号已确认", "该信号已确认，无需重复操作"),
}

_CANCEL_STATUS_HINTS = {
    "expired": ("fail", "信号已过期", "该信号超时自动失效，无法操作"),
    "rejected": ("fail", "信号已拒绝", "该信号已被拒绝，无法重复操作"),
    "executed": ("warn", "信号已执行", "信号已执行，无法取消"),
    "pending": ("warn", "信号已确认", "该信号已确认，无法取消"),
}


def _page_response(
    body: str,
    *,
    status_code: int,
    title: str,
    detail: str,
    symbol: str,
    page_kind: str,
    action: str,
) -> tuple[dict[str, Any], int]:
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


def _render_page(page_kind: str, title: str, detail: str, symbol: str) -> str:
    if page_kind == "ok":
        return ok_page(title, detail, symbol)
    if page_kind == "warn":
        return warn_page(title, detail, symbol)
    return fail_page(title, detail, symbol)


def _status_hint_response(action: str, page_kind: str, title: str, detail: str, symbol: str) -> tuple[dict[str, Any], int]:
    return _page_response(
        _render_page(page_kind, title, detail, symbol),
        status_code=200,
        title=title,
        detail=detail,
        symbol=symbol,
        page_kind=page_kind,
        action=action,
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


def _apply_notification_patch(
    pb: Any,
    record: dict[str, Any],
    *,
    extra_patch: dict[str, Any],
) -> None:
    record_id = str(record.get("id") or "")
    if not record_id or not isinstance(extra_patch, dict) or not extra_patch:
        return
    pb.update_record("ibkr_signals", record_id, {"extra": extra_patch})


def _sync_signal_notification(
    pb: Any,
    record: dict[str, Any],
    *,
    action: str,
    message: str,
    notify_signal_status: SignalStatusNotifier | None,
    update_signal_card: UpdateSignalCard | None,
    console_base_url: str,
) -> None:
    if callable(notify_signal_status):
        apply_signal_status_notification(
            pb,
            signal_row=record,
            status=action,
            message=message,
            notifier=notify_signal_status,
        )
        return
    notify_result = sync_signal_status_notification(
        record,
        action=action,
        message=message,
        update_interactive=update_signal_card,
        console_base_url=console_base_url,
    )
    _apply_notification_patch(pb, record, extra_patch=notify_result.get("extra_patch") or {})


def build_signal_confirm_webhook_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    notify_signal_status: SignalStatusNotifier | None = None,
    update_signal_card: UpdateSignalCard | None = None,
    console_base_url: str = "",
    now_provider: Callable[[], Any] | None = None,
    clock: Callable[[], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    signal_id = str((payload or {}).get("id") or "").strip()
    if not signal_id:
        return _fail_response("参数错误", "缺少信号ID", status_code=400, action="signal_confirm")

    environment = normalize_environment((payload or {}).get("environment"), "live")
    record = load_signal_record(pb, signal_id, environment, escape_filter=escape_filter_string)
    if not record or not record.get("id"):
        return _fail_response("信号不存在", "找不到信号", signal_id, status_code=404, action="signal_confirm")

    current_status = signal_status(record)
    symbol = signal_symbol(record, signal_id)
    if current_status not in {"pending", "awaiting_confirm"}:
        page_kind, title, detail = _CONFIRM_STATUS_HINTS.get(
            current_status,
            ("warn", "无法操作", f"状态: {current_status or '--'}"),
        )
        return _status_hint_response("signal_confirm", page_kind, title, detail, symbol)

    extra_patch = merge_signal_extra(
        record,
        {
            "confirmed_by": "manual",
            "confirmed_at": now_iso_utc(now_provider or clock),
            "status_reason": "confirmed_by_user",
        },
    )
    updated_record = pb.update_record(
        "ibkr_signals",
        str(record.get("id")),
        {
            "status": "pending",
            "note": "",
            "extra": extra_patch,
        },
    )
    updated_row = updated_record if isinstance(updated_record, dict) else {**dict(record), "status": "pending", "note": "", "extra": extra_patch}
    try:
        _sync_signal_notification(
            pb,
            updated_row,
            action="pending",
            message="信号已确认，等待执行",
            notify_signal_status=notify_signal_status,
            update_signal_card=update_signal_card,
            console_base_url=console_base_url,
        )
    except Exception:
        pass

    return _page_response(
        ok_page("信号已确认", "确认成功", symbol),
        status_code=200,
        title="信号已确认",
        detail="确认成功",
        symbol=symbol,
        page_kind="ok",
        action="signal_confirm",
    )


def build_signal_cancel_webhook_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    cancel_broker_order: CancelBrokerOrder,
    notify_signal_status: SignalStatusNotifier | None = None,
    notify_order_status: OrderStatusNotifier | None = None,
    update_signal_card: UpdateSignalCard | None = None,
    console_base_url: str = "",
    now_provider: Callable[[], Any] | None = None,
    clock: Callable[[], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    signal_id = str((payload or {}).get("id") or "").strip()
    if not signal_id:
        return _fail_response("参数错误", "缺少信号ID", status_code=400, action="signal_cancel")

    environment = normalize_environment((payload or {}).get("environment"), "live")
    record = load_signal_record(pb, signal_id, environment, escape_filter=escape_filter_string)
    if not record or not record.get("id"):
        return _fail_response("信号不存在", "找不到信号", signal_id, status_code=404, action="signal_cancel")

    current_status = signal_status(record)
    symbol = signal_symbol(record, signal_id)
    if current_status not in {"pending", "awaiting_confirm"}:
        page_kind, title, detail = _CANCEL_STATUS_HINTS.get(
            current_status,
            ("warn", "无法操作", f"状态: {current_status or '--'}"),
        )
        return _status_hint_response("signal_cancel", page_kind, title, detail, symbol)

    cancel_summary = {
        "ok": True,
        "cancelled_order_ids": [],
        "failed_order_ids": [],
        "trade_group_id": "",
        "updated_record_ids": [],
        "detail_record_ids": [],
    }
    if current_status == "pending":
        if callable(notify_order_status):
            cancel_summary = cancel_signal_related_orders(
                pb,
                signal_id=to_text(record.get("signal_id") or signal_id),
                environment=environment,
                escape_filter_string=escape_filter_string,
                cancel_broker_order=cancel_broker_order,
                notify_order_status=notify_order_status,
                source="webhook/signal/cancel",
                reason=f"信号拒绝触发撤单 {to_text(record.get('signal_id') or signal_id)}",
            )
        else:
            cancel_summary = build_signal_cancel_order_summary(
                pb,
                signal_id=to_text(record.get("signal_id") or signal_id),
                environment=environment,
                escape_filter_string=escape_filter_string,
                cancel_broker_order=cancel_broker_order,
                source="webhook/signal/cancel",
                reason=f"信号拒绝触发撤单 {to_text(record.get('signal_id') or signal_id)}",
            )

    failure_count = len(cancel_summary.get("failed_order_ids") or [])
    rejection_note = "manual_rejected" if cancel_summary.get("ok") else f"manual_rejected_with_cancel_failures:{failure_count}"
    extra_patch = merge_signal_extra(
        record,
        {
            "rejected_by": "manual",
            "rejected_at": now_iso_utc(now_provider or clock),
            "status_reason": "manual_rejected" if cancel_summary.get("ok") else "manual_rejected_with_cancel_failures",
            "cancel_order_failures": list(cancel_summary.get("failed_order_ids") or []),
            "cancelled_order_ids": list(cancel_summary.get("cancelled_order_ids") or []),
        },
    )
    updated_record = pb.update_record(
        "ibkr_signals",
        str(record.get("id")),
        {
            "status": "rejected",
            "note": rejection_note,
            "extra": extra_patch,
        },
    )
    updated_row = updated_record if isinstance(updated_record, dict) else {**dict(record), "status": "rejected", "note": rejection_note, "extra": extra_patch}
    try:
        _sync_signal_notification(
            pb,
            updated_row,
            action="rejected",
            message=(
                "信号已拒绝，暂不执行"
                if cancel_summary.get("ok")
                else f"信号已拒绝，但仍有 {failure_count} 条账户订单撤销失败"
            ),
            notify_signal_status=notify_signal_status,
            update_signal_card=update_signal_card,
            console_base_url=console_base_url,
        )
    except Exception:
        pass

    if not cancel_summary.get("ok"):
        return _fail_response(
            "信号已拒绝",
            f"账户撤单失败 {failure_count} 条",
            symbol,
            status_code=500,
            action="signal_cancel",
        )
    return _page_response(
        fail_page("信号已拒绝", "拒绝成功", symbol),
        status_code=200,
        title="信号已拒绝",
        detail="拒绝成功",
        symbol=symbol,
        page_kind="fail",
        action="signal_cancel",
    )
