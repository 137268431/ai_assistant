from __future__ import annotations

from typing import Any

from .auth_shared import _to_int, _to_text


def is_waiting_response_issue_kind(kind: str) -> bool:
    return _to_text(kind).lower().startswith("waiting_response")


def _is_gateway_down_auth_issue(auth: dict[str, Any]) -> bool:
    markers = [auth.get("reason"), auth.get("recovery_reason"), auth.get("interruption_kind")]
    return any(_to_text(item).lower() == "gateway_down" for item in markers)


def _is_server_boot_resume_recovery(auth: dict[str, Any]) -> bool:
    interruption_kind = _to_text(auth.get("interruption_kind")).lower()
    recovery_phase = _to_text(auth.get("recovery_phase")).lower()
    recovery_reason = _to_text(auth.get("recovery_reason")).lower()
    last_recovery_source = _to_text(auth.get("last_recovery_source")).lower()
    return (
        interruption_kind == "server_boot_resume"
        or recovery_phase == "resume_waiting_manual"
        or (recovery_reason == "auto_restore" and last_recovery_source == "server_boot")
    )


def _is_silent_recovery_issue(auth: dict[str, Any]) -> bool:
    status = _to_text(auth.get("status")).lower()
    recovery_phase = _to_text(auth.get("recovery_phase")).lower()
    recovery_class = _to_text(auth.get("recovery_class")).lower()
    probe_result = _to_text(auth.get("probe_result")).lower()
    if _is_server_boot_resume_recovery(auth):
        return True
    if status == "recovering":
        return True
    if recovery_phase != "silent_probe":
        return False
    return (
        recovery_class == "scheduled_restart"
        or recovery_class == "stale_broker"
        or bool(auth.get("auto_restart_scheduled"))
        or probe_result in {
            "pending",
            "self_heal",
            "self_heal_pending",
            "stale_broker_restart_scheduled",
            "stale_broker_restart_failed",
        }
    )


def _build_silent_recovery_issue(auth: dict[str, Any]) -> dict[str, str]:
    recovery_class = _to_text(auth.get("recovery_class")).lower()
    probe_result = _to_text(auth.get("probe_result")).lower()
    interruption_kind = _to_text(auth.get("interruption_kind")).lower()
    if recovery_class == "stale_broker" or probe_result.startswith("stale_broker"):
        return {
            "kind": "stale_broker_recovering",
            "title": "IBKR 主连接恢复中，暂不需要重新 2FA",
            "summary": (
                "检测到 fresh probe 已认证、但主运行时 broker 连接仍失配；系统已安排自动重启 Runtime 恢复主连接，暂不需要立即重新触发 2FA。"
                if bool(auth.get("auto_restart_scheduled"))
                else "检测到主运行时 broker 连接仍失配；系统正在继续静默恢复主连接，暂不需要立即重新触发 2FA。"
            ),
        }
    if interruption_kind == "gateway_down":
        return {
            "kind": "session_recovering",
            "title": "IBKR Gateway 恢复中，暂不需要重新 2FA",
            "summary": "Gateway 刚经历中断或重启，系统正在静默探测并恢复当前会话；只有静默恢复窗口耗尽后，才会升级为手动 2FA。",
        }
    return {
        "kind": "session_recovering",
        "title": "IBKR 会话静默恢复中，暂不需要重新 2FA",
        "summary": "检测到会话认证中断，系统正在静默探测与本地重连；只有静默恢复窗口耗尽后，才会升级为手动 2FA。",
    }


def build_waiting_response_advice(auth: dict[str, Any]) -> str:
    if auth.get("reset_recommended"):
        return "当前旧 2FA / Session 状态很可能已失配。请直接去 Runtime 页面点“全量清空并重新验证”，不要继续围绕旧 Challenge / Response 重试。"
    response_status = _to_text(auth.get("response_status")).lower()
    if response_status == "gateway_rejected":
        return "Gateway 已拒绝当前 Response Code。请在 Runtime 页面核对当前 Challenge，用 IBKR App 重新生成 Response Code 后重提。"
    if response_status == "submitted":
        return "Response Code 已提交。先不要重新触发或重复提交；继续观察 Runtime 是否恢复认证。"
    if response_status == "received":
        return "Runtime 已收到 Response Code。先不要重复输入，优先观察是否自动推进到 submitted。"
    if response_status == "submit_failed":
        return "浏览器提交动作失败。请在 Runtime 页面重新提交当前 Challenge 对应的 Response Code，不要重新触发。"
    return "不要再点旧确认消息。若接受 Challenge/Response，请按当前 Challenge 提交 Response Code；若不想继续旧轮次，请去 Runtime 页面点“全量清空并重新验证”。"


def build_waiting_response_issue(auth: dict[str, Any]) -> dict[str, str]:
    feedback = _to_text(auth.get("challenge_feedback"))
    response_status = _to_text(auth.get("response_status")).lower()
    if auth.get("reset_recommended"):
        return {
            "kind": "waiting_response_desynced",
            "title": "IBKR 2FA 会话已失配，建议干净重开",
            "summary": (
                "Gateway 已拒绝当前 Response Code，且旧轮次长时间未恢复。当前旧 2FA / Session 状态很可能已失配，请直接去 Runtime 页面执行“全量清空并重新验证”。"
                if response_status == "gateway_rejected"
                else "Response Code 已提交较久但 Gateway 仍未恢复认证。当前旧 2FA / Session 状态很可能已失配，请直接去 Runtime 页面执行“全量清空并重新验证”。"
            ),
        }
    if response_status == "gateway_rejected":
        return {
            "kind": "waiting_response_rejected",
            "title": "IBKR Gateway 已拒绝当前 Response Code",
            "summary": (
                f"Gateway 已返回失败反馈（{feedback}）。请核对当前 Challenge 后重新生成并提交。"
                if feedback
                else "Gateway 已明确拒绝当前 Response Code。请核对当前 Challenge 后重新生成并提交。"
            ),
        }
    if response_status == "submitted":
        return {
            "kind": "waiting_response_submitted",
            "title": "IBKR Response Code 已提交，等待认证恢复",
            "summary": "Runtime 已经把当前 Response Code 提交给 Gateway。先不要重复提交或重开，继续观察会话是否恢复认证。",
        }
    if response_status == "received":
        return {
            "kind": "waiting_response_received",
            "title": "IBKR Response Code 已收到，等待浏览器提交",
            "summary": "Runtime 已收到 Response Code，但浏览器提交流程尚未完成。先不要重复提交，继续观察当前轮次。",
        }
    if response_status == "submit_failed":
        return {
            "kind": "waiting_response_submit_failed",
            "title": "IBKR Response Code 浏览器提交失败",
            "summary": "Runtime 已收到 Response Code，但浏览器提交动作失败。请打开 Runtime 页面重新提交当前 Challenge 的 Response Code。",
        }
    return {
        "kind": "waiting_response",
        "title": "IBKR 2FA 已切到 Challenge/Response",
        "summary": "本轮 2FA 已不再是手机确认。不要再点旧的确认消息；如不想提交 Response Code，请去 Runtime 页面执行“全量清空并重新验证”。",
    }


def build_auth_immediate_issue(auth: dict[str, Any]) -> dict[str, str] | None:
    if not auth:
        return None
    status = _to_text(auth.get("status")).lower()
    gateway_status_code = _to_int(auth.get("gateway_status_code"), 0)
    gateway_reachable = bool(auth.get("gateway_reachable"))
    runtime_authenticated = bool(auth.get("runtime_authenticated"))
    runtime_started = bool(auth.get("runtime_started"))
    has_request = bool(auth.get("has_request") or auth.get("active"))
    active = bool(auth.get("active"))

    if not gateway_reachable and not active and not has_request:
        return None
    if has_request and status == "waiting_response":
        return build_waiting_response_issue(auth)
    if has_request and status == "waiting_confirm":
        return {
            "kind": "waiting_confirm",
            "title": "IBKR 2FA 已触发，等待确认",
            "summary": "本轮 2FA 当前仍是手机确认。只需要在 IBKR App 点一次确认；如果手机没有反应，不要反复点旧消息，先去 Runtime 页面确认当前状态是否已变成 Challenge/Response。",
        }
    if active and status in {"requested", "triggered"}:
        return {
            "kind": "requested",
            "title": "IBKR 2FA 已请求，待处理",
            "summary": "检测到系统已请求 2FA，当前会话尚未恢复认证，请立即处理飞书 2FA 卡片。",
        }
    if _is_server_boot_resume_recovery(auth) and not runtime_authenticated:
        return {
            "kind": "server_boot_resume_pending",
            "title": "IBKR 会话静默恢复中，暂不需要重新 2FA",
            "summary": (
                "检测到 compute 重启后的静默恢复尚未自动成功；当前不会自动补发新的 2FA，如需立即恢复请去 Runtime 页面人工接管或手动重开。"
                if _to_text(auth.get("recovery_phase")).lower() == "resume_waiting_manual"
                else "检测到 compute 重启后正在静默复用现有 Gateway Session；当前不会自动触发新的 2FA，请先等待恢复窗口结束。"
            ),
        }
    if not runtime_authenticated and _is_silent_recovery_issue(auth):
        return _build_silent_recovery_issue(auth)
    if gateway_status_code == 401 and not runtime_authenticated:
        if _is_gateway_down_auth_issue(auth):
            return {
                "kind": "gateway_restart_reauth_required",
                "title": "IBKR Gateway 已重启，需重新完成 2FA",
                "summary": "检测到 Gateway 重启后会话尚未恢复认证（401），当前需要重新完成这一轮 2FA。",
            }
        return {
            "kind": "session_expired",
            "title": "IBKR Session 已失效，需重新触发 2FA",
            "summary": "检测到 Gateway Session 已失效（401），运行态未认证，需要立即重新触发 2FA。",
        }
    if runtime_started and not runtime_authenticated:
        return {
            "kind": "runtime_unauthenticated",
            "title": "IBKR Runtime 未认证",
            "summary": "检测到运行态未认证，实时链路可能不可用，请立即检查 Gateway 与 2FA 状态。",
        }
    return None


def is_operational_2fa_issue(issue: dict[str, Any] | None) -> bool:
    kind = _to_text((issue or {}).get("kind"))
    return kind in {
        "requested",
        "waiting_confirm",
        "server_boot_resume_pending",
        "session_recovering",
        "stale_broker_recovering",
    } or is_waiting_response_issue_kind(kind)


__all__ = [
    "build_auth_immediate_issue",
    "build_waiting_response_advice",
    "build_waiting_response_issue",
    "is_operational_2fa_issue",
    "is_waiting_response_issue_kind",
]
