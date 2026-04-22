from __future__ import annotations

from typing import Any, Callable


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


def dispatch_feishu_signal_callback(
    action: str,
    signal_id: str,
    environment: str,
    *,
    pb: Any,
    escape_filter_string: Callable[[Any], str],
    callback_toast_fn: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    record = pb.get_first_record(
        "ibkr_signals",
        filter=(
            f'(id = "{escape_filter_string(signal_id)}" || signal_id = "{escape_filter_string(signal_id)}") && '
            f'environment = "{escape_filter_string(environment)}"'
        ),
    )
    if not record or not record.get("id"):
        return callback_toast_fn("error", "信号不存在", card=None), 404

    current_status = str(record.get("status") or "").strip()
    if current_status in {"expired", "rejected", "executed", "closed"}:
        return callback_toast_fn("warning", f"该信号已是 {current_status}，无法继续操作"), 200

    if action == "confirm":
        if current_status == "pending":
            return callback_toast_fn("info", "⏳ 信号已确认，请勿重复操作"), 200
        if current_status != "awaiting_confirm":
            return callback_toast_fn("warning", f"该信号当前状态为 {current_status or '--'}，无法确认"), 200
        pb.update_record("ibkr_signals", str(record.get("id")), {"status": "pending"})
        return callback_toast_fn("success", "确认成功"), 200

    if action == "reject":
        if current_status == "rejected":
            return callback_toast_fn("info", "❌ 信号已拒绝，请勿重复操作"), 200
        if current_status == "pending":
            return callback_toast_fn("warning", "⏳ 信号正在等待执行，无法拒绝"), 200
        if current_status != "awaiting_confirm":
            return callback_toast_fn("warning", f"该信号当前状态为 {current_status or '--'}，无法拒绝"), 200
        pb.update_record("ibkr_signals", str(record.get("id")), {"status": "rejected"})
        return callback_toast_fn("success", "拒绝成功"), 200

    return callback_toast_fn("error", f"未知操作: {action}"), 400


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
