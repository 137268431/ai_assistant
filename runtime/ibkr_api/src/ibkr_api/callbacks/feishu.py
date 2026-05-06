from __future__ import annotations

from typing import Any, Callable

from ibkr_api.signals.notifications import build_signal_status_card
from ibkr_api.signals.values import load_signal_record, signal_status


def callback_toast(toast_type: str, content: str, *, card: Any = None) -> dict[str, Any]:
    payload = {"toast": {"type": str(toast_type or "info"), "content": str(content or "")}}
    if card is not None:
        payload["card"] = {"type": "raw", "data": card}
    return payload


def dispatch_feishu_2fa_callback(
    action: str,
    environment: str,
    *,
    request_json_request: Callable[..., dict[str, Any]],
    pb_base_url: str,
    as_dict: Callable[[Any], dict[str, Any]],
    callback_toast_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    if action != "ibkr_2fa_start":
        return callback_toast_fn("error", f"未知操作: {action}")
    result = request_json_request(
        "POST",
        pb_base_url,
        "/api/custom/ibkr/2fa/request",
        json_body={
            "environment": environment,
            "source": "feishu_callback",
            "reason": "manual_reauth",
            "trigger_now": True,
        },
        timeout=15,
    )
    payload = as_dict(result.get("payload"))
    if result.get("ok"):
        return callback_toast_fn("success", str(payload.get("message") or "2FA 已触发，请在 IBKR Mobile 确认"))
    return callback_toast_fn("error", str(payload.get("error") or payload.get("message") or "2FA 触发失败"))


def _signal_callback_toast_type(action: str, payload: dict[str, Any], status_code: int) -> str:
    if int(status_code or 0) >= 400:
        return "error"
    page_kind = str((payload or {}).get("page_kind") or "").strip().lower()
    title = str((payload or {}).get("title") or "").strip()
    detail = str((payload or {}).get("detail") or "").strip()
    if page_kind == "warn":
        return "warning"
    if page_kind == "fail":
        return "success" if action == "reject" and title == "信号已拒绝" and "成功" in detail else "warning"
    if "无需" in detail or "请勿重复" in detail or "成功" not in detail:
        return "info"
    return "success"


def _signal_callback_message(action: str, payload: dict[str, Any], record: dict[str, Any] | None) -> str:
    current_status = signal_status(record or {})
    detail = str((payload or {}).get("detail") or (payload or {}).get("title") or "").strip()
    if action == "confirm" and current_status == "pending":
        return "信号已确认，等待执行"
    if action == "reject" and current_status == "rejected":
        return "信号已拒绝，暂不执行" if detail in {"", "拒绝成功"} else detail
    return detail


def _signal_callback_card(
    record: dict[str, Any] | None,
    *,
    message: str,
    console_base_url: str,
) -> dict[str, Any] | None:
    if not isinstance(record, dict) or not record.get("id"):
        return None
    return build_signal_status_card(record, message=message, console_base_url=console_base_url)


def dispatch_feishu_signal_callback(
    action: str,
    signal_id: str,
    environment: str,
    *,
    pb: Any,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    cancel_broker_order: Callable[[str, str, dict[str, Any] | None], dict[str, Any]],
    build_signal_confirm_webhook_response_fn: Callable[..., tuple[dict[str, Any], int]],
    build_signal_cancel_webhook_response_fn: Callable[..., tuple[dict[str, Any], int]],
    callback_toast_fn: Callable[..., dict[str, Any]],
    update_signal_card: Callable[[str, dict[str, Any], str], dict[str, Any]] | None = None,
    console_base_url: str = "",
    config_value: Callable[[str, str, str], str] | None = None,
) -> tuple[dict[str, Any], int]:
    runtime_environment = normalize_environment(environment, "live")
    action_payload = {"id": signal_id, "environment": runtime_environment}
    if action == "confirm":
        payload, status_code = build_signal_confirm_webhook_response_fn(
            pb,
            payload=action_payload,
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            update_signal_card=update_signal_card,
            console_base_url=console_base_url,
            config_value=config_value,
        )
    elif action == "reject":
        payload, status_code = build_signal_cancel_webhook_response_fn(
            pb,
            payload=action_payload,
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=cancel_broker_order,
            update_signal_card=update_signal_card,
            console_base_url=console_base_url,
        )
    else:
        return callback_toast_fn("error", f"未知操作: {action}"), 400

    latest_record = load_signal_record(pb, signal_id, runtime_environment, escape_filter=escape_filter_string)
    latest_record = latest_record if isinstance(latest_record, dict) else None
    message = _signal_callback_message(action, payload, latest_record)
    card = _signal_callback_card(latest_record, message=message, console_base_url=console_base_url)
    toast_type = _signal_callback_toast_type(action, payload, int(status_code or 200))
    content = message or str(payload.get("title") or "操作完成")
    return callback_toast_fn(toast_type, content, card=card), int(status_code or 200)


def dispatch_feishu_order_callback(
    action: str,
    order_id: str,
    environment: str,
    *,
    pb: Any,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    cancel_broker_order: Callable[[str, str, dict[str, Any] | None], dict[str, Any]],
    build_order_cancel_group_response_fn: Callable[..., tuple[dict[str, Any], int]],
    build_order_close_group_response_fn: Callable[..., tuple[dict[str, Any], int]],
    callback_toast_fn: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    payload: dict[str, Any]
    status_code: int
    action_payload = {"id": order_id, "environment": environment}
    if action == "cancel":
        payload, status_code = build_order_cancel_group_response_fn(
            pb,
            payload=action_payload,
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=cancel_broker_order,
        )
        success_message = "取消指令已发送"
    elif action == "close":
        payload, status_code = build_order_close_group_response_fn(
            pb,
            payload=action_payload,
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
        )
        success_message = "交易组平仓指令已发送"
    else:
        return callback_toast_fn("error", f"未知操作: {action}"), 400

    if status_code < 400 and not bool(payload.get("warning")):
        return callback_toast_fn("success", str(payload.get("message") or success_message)), 200
    level = "warning" if bool(payload.get("warning")) and status_code < 400 else "error"
    return callback_toast_fn(level, str(payload.get("error") or payload.get("message") or "订单操作失败")), int(
        status_code or 500
    )


def handle_feishu_callback(
    body: dict[str, Any],
    *,
    as_dict: Callable[[Any], dict[str, Any]],
    normalize_environment: Callable[[Any, str], str],
    dispatch_feishu_2fa_callback_fn: Callable[[str, str], dict[str, Any]],
    dispatch_feishu_order_callback_fn: Callable[[str, str, str], tuple[dict[str, Any], int]],
    dispatch_feishu_signal_callback_fn: Callable[[str, str, str], tuple[dict[str, Any], int]],
    callback_toast_fn: Callable[..., dict[str, Any]],
    callback_response_fn: Callable[..., Any],
):
    if body.get("type") == "url_verification" and body.get("challenge"):
        return callback_response_fn({"challenge": body.get("challenge") or ""})

    event = as_dict(body.get("event"))
    action_obj = as_dict(event.get("action"))
    value = as_dict(action_obj.get("value") or body.get("value"))
    update_token = str(event.get("token") or "").strip()
    action = str(value.get("action") or body.get("action") or "").strip()
    signal_id = str(value.get("signal_id") or body.get("signal_id") or "").strip()
    order_id = str(value.get("order_id") or body.get("order_id") or "").strip()
    environment = normalize_environment(value.get("environment") or body.get("environment"), "live")

    try:
        if action.startswith("ibkr_2fa_"):
            return callback_response_fn(
                dispatch_feishu_2fa_callback_fn(action, environment),
                update_token=update_token,
            )

        if order_id:
            payload, status_code = dispatch_feishu_order_callback_fn(action, order_id, environment)
            return callback_response_fn(payload, update_token=update_token, status_code=status_code)

        if signal_id:
            payload, status_code = dispatch_feishu_signal_callback_fn(action, signal_id, environment)
            return callback_response_fn(payload, update_token=update_token, status_code=status_code)

        return callback_response_fn(
            callback_toast_fn("error", "缺少 signal_id 或 order_id", card=None),
            update_token=update_token,
            status_code=400,
        )
    except Exception as exc:
        return callback_response_fn(
            callback_toast_fn("error", f"处理失败: {exc}", card=None),
            update_token=update_token,
            status_code=500,
        )
