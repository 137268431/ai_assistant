from __future__ import annotations

from typing import Any

from ibkr_api.runtime.two_factor import derive_2fa_action_state
from ibkr_api.two_factor.deadlines import DEFAULT_CONFIRM_TIMEOUT_SECONDS, parse_et_time_ms


MANUAL_AUTH_REASON_LABELS = {
    "auto_restore": "静默恢复",
    "weekly_reauth": "每周重登提醒",
    "manual_start": "启动验证",
    "startup": "启动验证",
    "manual_reauth": "手动重登验证",
    "manual_gateway_restart": "网关重启验证",
    "panic_reset_2fa": "重开验证",
}

STATUS_CARD_META = {
    "requested": {
        "emoji": "🔐",
        "title": "IBKR 2FA 待触发",
        "template": "yellow",
        "button": "开始 2FA 验证",
        "summary": "点击按钮后才会开始登录与 2FA 推送。",
    },
    "triggered": {
        "emoji": "🚀",
        "title": "IBKR 2FA 已触发",
        "template": "blue",
        "button": "查看当前轮次",
        "summary": "控制请求已发送，正在等待 Gateway 真正进入 2FA；手机 Push 尚未确认发出，请打开 Runtime 跟当前轮次。",
    },
    "waiting_confirm": {
        "emoji": "📲",
        "title": "IBKR 2FA 待确认",
        "template": "yellow",
        "button": "继续当前轮次",
        "summary": "请在 IBKR Mobile 上确认推送。当前已有一轮 2FA 在进行中，不要重复触发；如果后续切到 Challenge/Response，再去 Runtime 页面提交 Response Code。",
    },
    "waiting_response": {
        "emoji": "🔢",
        "title": "IBKR 2FA 待输入响应码",
        "template": "orange",
        "button": "打开 Runtime 提交响应码",
        "summary": "当前已进入 Challenge/Response。请在 App 输入 Challenge 生成 Response Code，并去 Runtime 页面提交；不要重复触发新一轮。",
    },
    "resume_pending": {
        "emoji": "♻️",
        "title": "IBKR Session 静默恢复中",
        "template": "blue",
        "button": "查看恢复状态",
        "summary": "当前正在尝试复用已有 Gateway Session，不会自动触发新的 2FA。",
    },
    "recovering": {
        "emoji": "♻️",
        "title": "IBKR 会话静默恢复中",
        "template": "blue",
        "button": "查看恢复状态",
        "summary": "系统正在尝试自动恢复当前会话或重启运行态，暂不需要立即重新 2FA。",
    },
    "success": {
        "emoji": "✅",
        "title": "IBKR 2FA 验证成功",
        "template": "green",
        "button": "再次验证",
        "summary": "Gateway 已恢复认证。",
    },
    "timeout": {
        "emoji": "⏰",
        "title": "IBKR 2FA 等待超时",
        "template": "red",
        "button": "重新触发",
        "summary": "未在等待窗口内完成确认，可点击按钮重试。",
    },
    "failed": {
        "emoji": "🚨",
        "title": "IBKR 2FA 触发失败",
        "template": "red",
        "button": "重新触发",
        "summary": "登录流程未成功完成，可点击按钮重试。",
    },
}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def normalize_manual_auth_reason(reason: Any) -> str:
    text = str(reason or "").strip().lower()
    if not text:
        return "manual_reauth"
    if text == "startup":
        return "manual_start"
    return text


def manual_auth_reason_label(reason: Any) -> str:
    normalized = normalize_manual_auth_reason(reason)
    return MANUAL_AUTH_REASON_LABELS.get(normalized, "手动验证")


def card_meta_for_status(status: Any) -> dict[str, str]:
    normalized = str(status or "requested").strip().lower() or "requested"
    return dict(STATUS_CARD_META.get(normalized, STATUS_CARD_META["requested"]))


def _derive_state(state_data: dict[str, Any]) -> dict[str, Any]:
    return derive_2fa_action_state(
        state_data,
        as_dict=_as_dict,
        parse_et_time_ms=parse_et_time_ms,
    )


def build_waiting_response_summary(state_data: dict[str, Any]) -> str:
    state = _derive_state(state_data)
    feedback = str(state.get("challenge_feedback") or "Authentication failed")
    if bool(state.get("reset_recommended")):
        if state.get("response_status") == "submitted":
            return "Response Code 已提交较久但 Gateway 仍未恢复认证。当前旧 2FA / Session 状态很可能已失配，请在 Runtime 页面执行“全量清空并重新验证”。"
        if state.get("response_status") == "gateway_rejected":
            return "Gateway 已拒绝当前 Response Code，且旧轮次长时间未恢复。请在 Runtime 页面执行“全量清空并重新验证”。"
    if state.get("response_status") == "received":
        return "已收到 Response Code，等待浏览器提交流程。当前已有 active 轮次，请不要重复触发。"
    if state.get("response_status") == "submitted":
        return "Response Code 已提交，等待 Gateway 会话恢复认证。当前已有 active 轮次，请不要重复触发。"
    if state.get("response_status") == "gateway_rejected":
        return f"Gateway 已拒绝当前 Response Code（{feedback}）。请核对当前 Challenge 后重新生成并提交。"
    if state.get("response_status") == "submit_failed":
        return "浏览器提交 Response Code 失败。请在 Runtime 页面重试，不要重复触发新一轮。"
    return "当前已进入 Challenge/Response。请在 App 输入 Challenge 生成 Response Code，并去 Runtime 页面提交；不要重复触发新一轮。"


def build_waiting_response_prompt(state_data: dict[str, Any]) -> str:
    state = _derive_state(state_data)
    feedback = str(state.get("challenge_feedback") or "Authentication failed")
    if bool(state.get("reset_recommended")):
        return "**操作提示**: 当前旧 2FA / Session 状态很可能已失配。不要继续围绕旧 Challenge 反复尝试；请打开 Runtime 页面执行“全量清空并重新验证”。"
    if state.get("response_status") == "received":
        return "**操作提示**: Runtime 已收到你的 Response Code，正在等待 compute 浏览器提交流程。先不要重复提交，也不要再触发新一轮。"
    if state.get("response_status") == "submitted":
        return "**操作提示**: 浏览器已提交 Response Code，正在等待 Gateway 恢复认证。此时不要再提交旧 Response，也不要重复触发新一轮。"
    if state.get("response_status") == "gateway_rejected":
        return f"**操作提示**: Gateway 已返回失败反馈（{feedback}）。请核对当前 Challenge，用 App 重新生成新的 Response Code 后去 Runtime 页面重提。"
    if state.get("response_status") == "submit_failed":
        return "**操作提示**: 这一步不是点手机推送；但浏览器提交动作失败了。请打开 Runtime 页面重新提交当前 Challenge 对应的 Response Code。"
    return "**操作提示**: 这一步不是点手机推送。请在 IBKR App 的 Two-Factor Authentication 输入当前 Challenge，拿到 Response Code 后打开 Runtime 页面提交。当前已有 active 轮次，请不要重复触发。"


def build_requested_summary(state_data: dict[str, Any], fallback_summary: str) -> str:
    state = _derive_state(state_data)
    if normalize_manual_auth_reason(state.get("reason")) != "weekly_reauth":
        return str(state.get("message") or fallback_summary)
    confirm_seconds = int(state.get("confirm_window_seconds") or DEFAULT_CONFIRM_TIMEOUT_SECONDS)
    if bool(state.get("business_deadline_overdue")):
        return (
            "本周重登提醒仍待手动开始，当前已晚于美股周一盘前建议完成时间。"
            f"你仍可从当前卡片开始验证；点击开始后需在 {confirm_seconds} 秒内完成当前 2FA。"
        )
    return (
        "本周重登提醒已发出。你有空时可直接在当前卡片点击“开始 2FA 验证”；"
        f"最晚请于美股周一盘前前完成。点击开始后，本轮 2FA 需在 {confirm_seconds} 秒内完成。"
    )


def build_card_summary(state_data: dict[str, Any], fallback_summary: str) -> str:
    state = _derive_state(state_data)
    if state.get("status") == "waiting_response":
        return build_waiting_response_summary(state)
    if state.get("status") == "requested":
        return build_requested_summary(state, fallback_summary)
    return str(state.get("message") or fallback_summary)


def build_weekly_reminder_deadline_note(state_data: dict[str, Any]) -> str:
    state = _derive_state(state_data)
    if normalize_manual_auth_reason(state.get("reason")) != "weekly_reauth" or not state.get("business_deadline_cn"):
        return ""
    confirm_seconds = int(state.get("confirm_window_seconds") or DEFAULT_CONFIRM_TIMEOUT_SECONDS)
    if bool(state.get("business_deadline_overdue")):
        return (
            "**周验证提醒**: 已晚于美股周一盘前建议完成时间"
            f"（北京时间 {state.get('business_deadline_cn')} / 美东 {state.get('business_deadline_at') or '-'}）。"
            "你仍可从当前卡片开始验证，但请尽快完成恢复。"
        )
    return (
        "**周验证提醒**: 你有空时可从当前卡片开始验证；"
        f"最晚请于美股周一盘前前完成（北京时间 {state.get('business_deadline_cn')} / 美东 {state.get('business_deadline_at') or '-'}）。"
        f"点击开始后，本轮 2FA 需在 {confirm_seconds} 秒内完成。"
    )


def build_confirm_deadline_note(state_data: dict[str, Any]) -> str:
    state = _derive_state(state_data)
    if not state.get("confirm_deadline_cn"):
        return ""
    confirm_seconds = int(state.get("confirm_window_seconds") or DEFAULT_CONFIRM_TIMEOUT_SECONDS)
    if bool(state.get("confirm_deadline_overdue")):
        return (
            f"**本轮时限**: 当前轮次已超过 {confirm_seconds} 秒等待窗口"
            f"（北京时间 {state.get('confirm_deadline_cn')} / 美东 {state.get('confirm_deadline_at') or '-'}）。"
            "若 Gateway 仍未恢复，请准备重新开始本轮。"
        )
    return (
        f"**本轮时限**: 点击开始后，本轮 2FA 需在 {confirm_seconds} 秒内完成；"
        f"当前预计截止为北京时间 {state.get('confirm_deadline_cn')} / 美东 {state.get('confirm_deadline_at') or '-'}。"
    )


def get_active_cycle_primary_label(state_data: dict[str, Any]) -> str:
    state = _derive_state(state_data)
    if state.get("status") == "waiting_response":
        if bool(state.get("reset_recommended")):
            return "打开 Runtime 干净重开"
        if state.get("response_status") in {"gateway_rejected", "submit_failed"}:
            return "打开 Runtime 重新提交响应码"
        if state.get("response_status") == "submitted":
            return "打开 Runtime 查看提交状态"
        if state.get("response_status") == "received":
            return "打开 Runtime 查看提交流程"
        return "打开 Runtime 提交响应码"
    return "打开 Runtime 查看当前轮次"


def build_request_response_message(
    *,
    state_data: dict[str, Any],
    result: dict[str, Any],
    trigger_now: bool,
    force_new: bool,
    weekly_reminder_requested: bool,
) -> str:
    state = _derive_state(state_data)
    if bool(state.get("runtime_authenticated")) and bool(state.get("gateway_reachable")) and int(state.get("gateway_status_code") or 0) != 401:
        return "当前 Gateway 会话已认证，无需再次确认。"

    active_status = str(state.get("status") or "").strip().lower()
    current_cycle_active = active_status in {"triggered", "waiting_confirm", "waiting_response"}
    if current_cycle_active or bool(result.get("already_active")):
        if active_status == "waiting_response":
            if bool(state.get("reset_recommended")):
                return "当前旧 2FA / Session 状态很可能已失配，请打开 Runtime 页面执行“全量清空并重新验证”，不要重复触发。"
            if state.get("response_status") == "submitted":
                return "当前 Response Code 已提交，正在等待 Gateway 恢复认证；请继续当前轮次，不要重复触发。"
            if state.get("response_status") == "gateway_rejected":
                return "Gateway 已拒绝当前 Response Code，请打开 Runtime 页面核对当前 Challenge 后重新提交，不要重复触发。"
            if state.get("response_status") == "submit_failed":
                return "浏览器提交 Response Code 失败，请打开 Runtime 页面重试当前 Challenge，不要重复触发。"
            if state.get("response_status") == "received":
                return "Runtime 已收到 Response Code，正在等待浏览器提交流程；请继续当前轮次，不要重复触发。"
            return "当前已进入 Challenge/Response，请继续当前轮次并在 Runtime 页面提交 Response Code，不要重复触发。"
        if active_status == "triggered":
            if bool(state.get("gateway_2fa_not_reached")) or str(state.get("last_result") or "").startswith("gateway_not_ready"):
                return "当前只确认控制请求已发送，Gateway 尚未进入 2FA；手机 Push 未确认发出，请打开 Runtime 检查 Gateway，必要时重启 Gateway。"
            return "当前控制请求已发送，正在等待 Gateway 进入手机 Push 或 Challenge/Response；不要重复触发。"
        return "当前已有一轮 2FA 正在进行，请继续当前轮次，不要重复触发。"

    if trigger_now and bool(result.get("ok")):
        return (
            "已强制开启新一轮 2FA，并刷新卡片。请等待 Gateway 进入手机 Push 或 Challenge/Response；"
            "若 Gateway 已进入 Second Factor 后仍无手机通知，再去 Runtime 查看是否切到 Challenge/Response。"
            if force_new
            else "已重新触发 2FA。请先等待 Gateway 进入手机 Push 或 Challenge/Response；不要把飞书请求成功当作手机 Push 已发出。"
        )

    skipped_reason = str(result.get("skipped_reason") or "")
    if skipped_reason == "active_card_reused":
        if weekly_reminder_requested:
            if bool(state.get("business_deadline_overdue")):
                return "已复用现有本周重登提醒卡片；当前已晚于美股周一盘前建议完成时间，请尽快在飞书点击开始验证。点击开始后需在 180 秒内完成当前 2FA。"
            return "已复用现有本周重登提醒卡片；你有空时直接去飞书点击开始验证，最晚请于美股周一盘前前完成。点击开始后需在 180 秒内完成当前 2FA。"
        remaining_ms = int(result.get("renotify_remaining_ms") or 0)
        remaining_min = (remaining_ms + 59999) // 60000 if remaining_ms > 0 else 0
        if remaining_min > 0:
            return f"已复用现有飞书 2FA 卡片；这一步不会直接触发手机 Push，请去飞书点击开始验证（约 {remaining_min} 分钟内不会再新发提醒）。"
        return "已复用现有飞书 2FA 卡片；这一步不会直接触发手机 Push，请直接去飞书点击开始验证。"
    if skipped_reason == "cooldown":
        return "2FA 卡片刚更新过；这一步不会直接触发手机 Push，请直接使用飞书中的当前卡片。"
    if skipped_reason == "delivery_locked":
        return "2FA 卡片发送仍在处理中；这一步不会直接触发手机 Push，请直接查看飞书中的当前卡片。"

    if bool(result.get("ok")):
        if weekly_reminder_requested:
            if bool(state.get("business_deadline_overdue")):
                return "已发送本周重登提醒卡片；当前已晚于美股周一盘前建议完成时间，请尽快在飞书点击开始验证。点击开始后需在 180 秒内完成当前 2FA。"
            return "已发送本周重登提醒卡片；你有空时可在飞书点击开始验证，最晚请于美股周一盘前前完成。点击开始后需在 180 秒内完成当前 2FA。"
        return (
            "已强制发送新的 2FA 卡片；这一步不会直接触发手机 Push，请在飞书点击按钮触发验证。"
            if force_new
            else "已请求 2FA 卡片；这一步不会直接触发手机 Push，请在飞书点击按钮触发验证。"
        )

    error = str(result.get("error") or "").strip()
    return f"2FA 请求失败：{error}" if error else "2FA 请求失败。"


__all__ = [
    "STATUS_CARD_META",
    "build_card_summary",
    "build_confirm_deadline_note",
    "build_request_response_message",
    "build_waiting_response_prompt",
    "build_waiting_response_summary",
    "build_weekly_reminder_deadline_note",
    "card_meta_for_status",
    "get_active_cycle_primary_label",
    "manual_auth_reason_label",
    "normalize_manual_auth_reason",
]
