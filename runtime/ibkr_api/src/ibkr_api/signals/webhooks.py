from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.orders.group_cancel import CancelBrokerOrder
from ibkr_api.orders.values import to_text
from ibkr_api.signals.notifications import SignalStatusNotifier, apply_signal_status_notification, sync_signal_status_notification
from ibkr_api.signals.order_cancel import OrderStatusNotifier, build_signal_cancel_order_summary, cancel_signal_related_orders
from ibkr_api.signals.values import get_signal_extra, load_signal_record, merge_signal_extra, now_iso_utc, signal_status, signal_symbol
from ibkr_api.webhooks.pages import fail_page, ok_page, warn_page


HTML_CONTENT_TYPE = "text/html; charset=utf-8"

NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]
UpdateSignalCard = Callable[[str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]

_CONFIRM_STATUS_HINTS = {
    "expired": ("fail", "信号已过期", "该信号超时自动失效，无法操作"),
    "rejected": ("fail", "信号已拒绝", "该信号已被拒绝，无法重复操作"),
    "executed": ("ok", "信号已执行", "该信号已执行，无需重复操作"),
    "submitted": ("ok", "订单已提交", "该信号已提交到券商，等待成交或后续订单回报"),
    "submitted_waiting_fill": ("ok", "订单已提交", "该信号已提交到券商，等待成交"),
    "protected_active": ("ok", "保护单已生效", "入场已成交且止盈/止损保护单已提交，无需重复确认"),
    "filled_repricing_protection": ("ok", "保护单重定价中", "入场已成交，止盈/止损保护单正在按实际成交价重定价"),
    "filled_position": ("ok", "持仓已建立", "入场已成交且持仓已建立，无需重复确认"),
    "protection_incomplete": ("warn", "保护单不完整", "入场或下单链路已推进，但保护单未完整生效；请到 Orders / Account 核查"),
    "protection_reprice_failed": ("warn", "保护单重定价失败", "入场已成交但保护单重定价失败；请立即核查 Orders / Account"),
    "entry_missed_limit_cap": ("fail", "入场未成交", "该信号因限价上限或未成交错过，无法重复操作"),
    "pending": ("ok", "信号已确认", "该信号已确认，无需重复操作"),
}

_CANCEL_STATUS_HINTS = {
    "expired": ("fail", "信号已过期", "该信号超时自动失效，无法操作"),
    "rejected": ("fail", "信号已拒绝", "该信号已被拒绝，无法重复操作"),
    "executed": ("warn", "信号已执行", "信号已执行，无法取消"),
    "submitted": ("warn", "订单已提交", "订单已提交到券商，不能再按信号拒绝取消"),
    "submitted_waiting_fill": ("warn", "订单已提交", "订单已提交到券商并等待成交，不能再按信号拒绝取消"),
    "protected_active": ("warn", "保护单已生效", "入场已成交且保护单已提交，不能再按信号拒绝取消"),
    "filled_repricing_protection": ("warn", "保护单重定价中", "入场已成交且保护单正在重定价，不能再按信号拒绝取消"),
    "filled_position": ("warn", "持仓已建立", "入场已成交且持仓已建立，不能再按信号拒绝取消"),
    "protection_incomplete": ("warn", "保护单不完整", "保护单未完整生效，不能再按信号拒绝取消；请先核查券商订单"),
    "protection_reprice_failed": ("warn", "保护单重定价失败", "入场已成交但保护单重定价失败，不能再按信号拒绝取消；请先核查券商订单"),
    "entry_missed_limit_cap": ("fail", "入场未成交", "该信号因限价上限或未成交错过，无法取消"),
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


def _request_modes(payload: dict[str, Any] | None) -> tuple[str, str]:
    request_payload = payload if isinstance(payload, dict) else {}
    broker_mode = request_broker_mode(
        {"broker_mode": request_payload.get("broker_mode") or request_payload.get("environment")}
    )
    data_environment = request_market_data_mode(
        {
            "market_data_mode": request_payload.get("market_data_mode"),
            "data_environment": request_payload.get("data_environment"),
        }
    )
    return broker_mode, data_environment


def _effective_signal_status(record: Any, broker_mode: str, data_environment: str) -> str:
    extra = get_signal_extra(record)
    execution_by_mode = extra.get("execution_by_mode") if isinstance(extra, dict) else {}
    broker_execution = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
    broker_status = to_text(broker_execution.get("status") if isinstance(broker_execution, dict) else "").lower()
    if broker_status:
        return broker_status
    top_level_status = signal_status(record).lower()
    if broker_mode == data_environment == "live":
        return top_level_status
    if top_level_status in {"pending", "awaiting_confirm"}:
        return top_level_status
    return "pending"


def _broker_scoped_execution_patch(
    record: dict[str, Any],
    *,
    broker_mode: str,
    data_environment: str,
    status: str,
    note: str,
    extra_update: dict[str, Any],
) -> dict[str, Any]:
    extra_patch = merge_signal_extra(record, extra_update)
    execution_by_mode = extra_patch.get("execution_by_mode") if isinstance(extra_patch.get("execution_by_mode"), dict) else {}
    broker_payload = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
    if not isinstance(broker_payload, dict):
        broker_payload = {}
    execution_by_mode = dict(execution_by_mode)
    execution_by_mode[broker_mode] = {
        **broker_payload,
        "status": status,
        "note": note,
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "status_reason": to_text(extra_update.get("status_reason") or note),
        "updated_at": extra_update.get("confirmed_at")
        or extra_update.get("rejected_at")
        or extra_update.get("expired_at")
        or now_iso_utc(),
        "source": "signal_webhook",
    }
    extra_patch["execution_by_mode"] = execution_by_mode
    extra_patch["last_runtime_broker_mode"] = broker_mode
    extra_patch["last_runtime_data_environment"] = data_environment
    return extra_patch


def _signal_status_update_payload(
    record: dict[str, Any],
    *,
    broker_mode: str,
    data_environment: str,
    status: str,
    note: str,
    extra_update: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    extra_patch = _broker_scoped_execution_patch(
        record,
        broker_mode=broker_mode,
        data_environment=data_environment,
        status=status,
        note=note,
        extra_update=extra_update,
    )
    update_payload: dict[str, Any] = {"extra": extra_patch}
    existing_note = to_text(record.get("note")).lower()
    if broker_mode != data_environment or broker_mode != "live":
        if existing_note.startswith(f"{broker_mode}:") or "history_repair_pending" in existing_note:
            update_payload["note"] = ""
    notification_row = {
        **dict(record),
        "status": status,
        "note": note,
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "extra": extra_patch,
    }
    if broker_mode == data_environment == "live":
        update_payload.update({"status": status, "note": note})
    return update_payload, notification_row


def _parse_timestamp_ms(value: Any) -> int:
    text = to_text(value)
    if not text:
        return 0
    try:
        if text.endswith("Z"):
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except Exception:
        return 0


def _clock_ms(clock: Callable[[], Any] | None) -> int:
    if callable(clock):
        value = clock()
        if isinstance(value, datetime):
            return int(value.astimezone(timezone.utc).timestamp() * 1000)
        parsed = _parse_timestamp_ms(value)
        if parsed > 0:
            return parsed
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _signal_reference_ms(record: dict[str, Any]) -> int:
    extra = merge_signal_extra(record, {})
    try:
        bar_time_ms = int(record.get("bar_time_ms") or extra.get("bar_time_ms") or 0)
    except Exception:
        bar_time_ms = 0
    if bar_time_ms > 0:
        return bar_time_ms
    return _parse_timestamp_ms(record.get("created")) or _parse_timestamp_ms(record.get("updated"))


def _signal_validity_minutes(config_value: ConfigValue | None, environment: str) -> int:
    if not callable(config_value):
        return 30
    try:
        raw = int(str(config_value("signal_validity_minutes", "30", environment) or "30").strip())
    except Exception:
        raw = 30
    return raw if raw > 0 else 30


def _signal_is_expired(record: dict[str, Any], *, validity_minutes: int, clock: Callable[[], Any] | None) -> bool:
    reference_ms = _signal_reference_ms(record)
    if reference_ms <= 0:
        return False
    return _clock_ms(clock) - reference_ms > int(validity_minutes or 30) * 60 * 1000


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
    config_value: ConfigValue | None = None,
    now_provider: Callable[[], Any] | None = None,
    clock: Callable[[], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    signal_id = str((payload or {}).get("id") or "").strip()
    if not signal_id:
        return _fail_response("参数错误", "缺少信号ID", status_code=400, action="signal_confirm")

    environment, data_environment = _request_modes(payload)
    record = load_signal_record(pb, signal_id, data_environment, escape_filter=escape_filter_string)
    if not record or not record.get("id"):
        return _fail_response("信号不存在", "找不到信号", signal_id, status_code=404, action="signal_confirm")

    current_status = _effective_signal_status(record, environment, data_environment)
    symbol = signal_symbol(record, signal_id)
    if current_status not in {"pending", "awaiting_confirm"}:
        page_kind, title, detail = _CONFIRM_STATUS_HINTS.get(
            current_status,
            ("warn", "无法操作", f"状态: {current_status or '--'}"),
        )
        return _status_hint_response("signal_confirm", page_kind, title, detail, symbol)

    validity_minutes = _signal_validity_minutes(config_value, environment)
    if _signal_is_expired(record, validity_minutes=validity_minutes, clock=now_provider or clock):
        extra_update = {
            "expired_by": "signal_confirm_webhook",
            "expired_at": now_iso_utc(now_provider or clock),
            "status_reason": "confirm_too_late",
            "signal_validity_minutes": validity_minutes,
            "broker_mode": environment,
            "data_environment": data_environment,
        }
        update_payload, updated_row = _signal_status_update_payload(
            record,
            broker_mode=environment,
            data_environment=data_environment,
            status="expired",
            note="confirm_too_late",
            extra_update=extra_update,
        )
        updated_record = pb.update_record(
            "ibkr_signals",
            str(record.get("id")),
            update_payload,
        )
        updated_row = updated_record if isinstance(updated_record, dict) else updated_row
        try:
            _sync_signal_notification(
                pb,
                updated_row,
                action="expired",
                message=f"确认超时，信号已失效（有效期 {validity_minutes} 分钟）",
                notify_signal_status=notify_signal_status,
                update_signal_card=update_signal_card,
                console_base_url=console_base_url,
            )
        except Exception:
            pass
        return _status_hint_response(
            "signal_confirm",
            "fail",
            "信号已过期",
            f"确认超时，信号已失效（有效期 {validity_minutes} 分钟）",
            symbol,
        )

    existing_extra = get_signal_extra(record)
    confirm_time = now_iso_utc(now_provider or clock)
    extra_update = {
        "confirmed_by": "manual",
        "confirmed_at": confirm_time,
        "status_reason": "confirmed_by_user",
        "broker_mode": environment,
        "data_environment": data_environment,
    }
    if existing_extra.get("followup_requires_reconfirm") or existing_extra.get("confirmation_stale"):
        extra_update.update(
            {
                "followup_requires_reconfirm": False,
                "confirmation_stale": False,
                "reconfirmed_by": "manual",
                "reconfirmed_at": confirm_time,
                "reconfirm_resolution": "confirmed_by_user",
            }
        )
    update_payload, updated_row = _signal_status_update_payload(
        record,
        broker_mode=environment,
        data_environment=data_environment,
        status="pending",
        note="",
        extra_update=extra_update,
    )
    updated_record = pb.update_record(
        "ibkr_signals",
        str(record.get("id")),
        update_payload,
    )
    updated_row = updated_record if isinstance(updated_record, dict) else updated_row
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

    environment, data_environment = _request_modes(payload)
    record = load_signal_record(pb, signal_id, data_environment, escape_filter=escape_filter_string)
    if not record or not record.get("id"):
        return _fail_response("信号不存在", "找不到信号", signal_id, status_code=404, action="signal_cancel")

    current_status = _effective_signal_status(record, environment, data_environment)
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
    extra_update = {
        "rejected_by": "manual",
        "rejected_at": now_iso_utc(now_provider or clock),
        "status_reason": "manual_rejected" if cancel_summary.get("ok") else "manual_rejected_with_cancel_failures",
        "cancel_order_failures": list(cancel_summary.get("failed_order_ids") or []),
        "cancelled_order_ids": list(cancel_summary.get("cancelled_order_ids") or []),
        "broker_mode": environment,
        "data_environment": data_environment,
    }
    update_payload, updated_row = _signal_status_update_payload(
        record,
        broker_mode=environment,
        data_environment=data_environment,
        status="rejected",
        note=rejection_note,
        extra_update=extra_update,
    )
    updated_record = pb.update_record(
        "ibkr_signals",
        str(record.get("id")),
        update_payload,
    )
    updated_row = updated_record if isinstance(updated_record, dict) else updated_row
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
