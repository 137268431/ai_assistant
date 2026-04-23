from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
CHALLENGE_RESET_RECOMMEND_MS = 120 * 1000
RECOVERY_FIELDS = (
    "cycle_id",
    "recovery_phase",
    "recovery_class",
    "recovery_reason",
    "interruption_kind",
    "manual_takeover_active",
    "manual_takeover_started_at",
    "manual_takeover_until",
    "probe_started_at",
    "probe_last_checked_at",
    "probe_attempts",
    "probe_result",
    "auto_restart_scheduled",
    "last_runtime_authenticated_at",
    "last_gateway_status_code",
    "last_recovery_source",
    "lock_owner",
    "lock_expires_at",
)


AsDict = Callable[[Any], dict[str, Any]]
ParseEtTimeMs = Callable[[Any], int]


def normalize_two_factor_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "requested"
    if text in {"pending", "waiting_mobile_approval", "mobile_approval", "awaiting_mobile_approval"}:
        return "waiting_confirm"
    if text in {"complete", "completed", "authenticated"}:
        return "success"
    if text == "error":
        return "failed"
    return text


def is_server_boot_resume_recovery_state(state_data: dict[str, Any], *, as_dict: AsDict) -> bool:
    state = as_dict(state_data)
    interruption_kind = str(state.get("interruption_kind") or "").strip().lower()
    recovery_phase = str(state.get("recovery_phase") or "").strip().lower()
    recovery_reason = str(state.get("recovery_reason") or "").strip().lower()
    last_recovery_source = str(state.get("last_recovery_source") or "").strip().lower()
    return (
        interruption_kind == "server_boot_resume"
        or recovery_phase == "resume_waiting_manual"
        or (recovery_reason == "auto_restore" and last_recovery_source == "server_boot")
    )


def is_manual_auth_required_recovery_state(state_data: dict[str, Any], *, as_dict: AsDict) -> bool:
    state = as_dict(state_data)
    recovery_class = str(state.get("recovery_class") or "").strip().lower()
    recovery_phase = str(state.get("recovery_phase") or "").strip().lower()
    probe_result = str(state.get("probe_result") or "").strip().lower()
    return (
        recovery_class == "manual_auth_required"
        or recovery_phase == "requested"
        or probe_result == "manual_trigger_required"
        or probe_result == "timeout_after_self_heal"
    )


def is_silent_recovery_state(state_data: dict[str, Any], *, as_dict: AsDict) -> bool:
    state = as_dict(state_data)
    if is_manual_auth_required_recovery_state(state, as_dict=as_dict):
        return False
    if is_server_boot_resume_recovery_state(state, as_dict=as_dict):
        return True
    recovery_phase = str(state.get("recovery_phase") or "").strip().lower()
    recovery_class = str(state.get("recovery_class") or "").strip().lower()
    probe_result = str(state.get("probe_result") or "").strip().lower()
    return recovery_phase == "silent_probe" and (
        recovery_class == "scheduled_restart"
        or recovery_class == "stale_broker"
        or bool(state.get("auto_restart_scheduled"))
        or probe_result in {
            "pending",
            "self_heal",
            "self_heal_pending",
            "stale_broker_restart_scheduled",
            "stale_broker_restart_failed",
        }
    )


def build_silent_recovery_message(state_data: dict[str, Any], *, as_dict: AsDict) -> dict[str, str]:
    state = as_dict(state_data)
    recovery_class = str(state.get("recovery_class") or "").strip().lower()
    interruption_kind = str(state.get("interruption_kind") or "").strip().lower()
    probe_result = str(state.get("probe_result") or "").strip().lower()
    if recovery_class == "stale_broker" or probe_result.startswith("stale_broker"):
        if bool(state.get("auto_restart_scheduled")):
            return {
                "message": "检测到运行态内 broker 连接失配，系统已安排自动重启 Runtime 以恢复主连接；暂不需要立即重新 2FA。",
                "last_result": "已识别 stale in-process broker，正在等待自动重启恢复主连接。",
            }
        return {
            "message": "检测到运行态内 broker 连接失配，系统正在尝试本地恢复主连接；暂不需要立即重新 2FA。",
            "last_result": "已识别 stale in-process broker，正在继续静默恢复。",
        }
    if interruption_kind == "gateway_down":
        return {
            "message": "Gateway 刚经历中断或重启，系统正在静默探测并恢复当前会话；暂不需要立即重新 2FA。",
            "last_result": "已进入 Gateway 中断后的静默恢复窗口。",
        }
    return {
        "message": "检测到会话认证中断，系统正在静默探测与本地重连；暂不需要立即重新 2FA。",
        "last_result": "已进入静默恢复窗口，等待会话自动恢复。",
    }


def derive_2fa_action_state(
    state_data: dict[str, Any],
    *,
    as_dict: AsDict,
    parse_et_time_ms: ParseEtTimeMs,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current_ms = int(now_ms or 0) or int(datetime.now(tz=ET).timestamp() * 1000)
    state = as_dict(state_data)
    status = normalize_two_factor_status(state.get("status") or "")
    response_status = str(state.get("response_status") or "").strip().lower()
    challenge_code = str(state.get("challenge_code") or "").strip()
    feedback = str(state.get("challenge_feedback") or "").strip()
    recovery_phase = str(state.get("recovery_phase") or "").strip().lower()
    submitted_ms = parse_et_time_ms(state.get("response_submitted_at"))
    rejected_ms = parse_et_time_ms(state.get("response_rejected_at"))
    submitted_age_ms = max(0, current_ms - submitted_ms) if submitted_ms > 0 else 0
    rejected_age_ms = max(0, current_ms - rejected_ms) if rejected_ms > 0 else 0
    operator_action = "request_approval"
    reset_recommended = bool(state.get("reset_recommended"))
    reset_reason = str(state.get("reset_reason") or "").strip()

    if recovery_phase == "panic_resetting":
        operator_action = "panic_resetting"
    elif bool(state.get("manual_takeover_active")):
        operator_action = "manual_takeover"
    elif status == "waiting_response":
        if response_status == "received":
            operator_action = "wait_browser_submit"
        elif response_status == "submitted":
            operator_action = "wait_auth_restore"
            if not reset_recommended and submitted_age_ms >= CHALLENGE_RESET_RECOMMEND_MS:
                operator_action = "panic_reset"
                reset_recommended = True
                reset_reason = reset_reason or "submitted_no_recovery"
        elif response_status == "gateway_rejected":
            operator_action = "retry_response_same_challenge"
            if not reset_recommended and rejected_age_ms >= CHALLENGE_RESET_RECOMMEND_MS:
                operator_action = "panic_reset"
                reset_recommended = True
                reset_reason = reset_reason or "gateway_rejected_no_recovery"
        elif response_status == "submit_failed":
            operator_action = "retry_response_same_challenge"
        else:
            operator_action = "submit_response" if challenge_code else "wait_challenge"
    elif status == "waiting_confirm":
        operator_action = "confirm_push"
    elif status == "triggered":
        operator_action = "wait_for_mode"
    elif status in {"resume_pending", "recovering"}:
        operator_action = "check_runtime_status" if recovery_phase == "resume_waiting_manual" else "wait_auth_restore"
    elif status == "success":
        operator_action = "none"
    elif status in {"timeout", "failed"}:
        operator_action = "request_new_cycle"

    state["status"] = status
    state["response_status"] = response_status
    state["challenge_feedback"] = feedback
    state["operator_action"] = operator_action
    state["reset_recommended"] = reset_recommended
    state["reset_reason"] = reset_reason
    state["response_submitted_age_sec"] = round(submitted_age_ms / 1000) if submitted_age_ms > 0 else 0
    state["response_rejected_age_sec"] = round(rejected_age_ms / 1000) if rejected_age_ms > 0 else 0
    state.setdefault("business_deadline_at", "")
    state.setdefault("business_deadline_cn", "")
    state.setdefault("business_deadline_label", "")
    state.setdefault("business_deadline_overdue", False)
    state["confirm_window_seconds"] = int(state.get("confirm_window_seconds") or 180)
    state.setdefault("confirm_deadline_at", "")
    state.setdefault("confirm_deadline_cn", "")
    state.setdefault("confirm_deadline_overdue", False)
    return state


def normalize_two_factor_state_with_runtime(
    state_data: dict[str, Any],
    runtime_status: dict[str, Any],
    *,
    as_dict: AsDict,
    parse_et_time_ms: ParseEtTimeMs,
) -> dict[str, Any]:
    state = as_dict(state_data)
    runtime = as_dict(runtime_status)
    auth_recovery = as_dict(runtime.get("auth_recovery"))
    runtime_started = bool(
        runtime.get("starting")
        or as_dict(runtime.get("session")).get("running")
        or as_dict(runtime.get("websocket")).get("running")
        or as_dict(runtime.get("order_tracker")).get("running")
    )
    runtime_authenticated = bool(as_dict(runtime.get("session")).get("authenticated"))
    gateway = as_dict(runtime.get("gateway"))
    gateway_reachable = bool(gateway.get("running") or gateway.get("reachable"))
    gateway_status_code = int(gateway.get("status_code") or 0)

    state["runtime_started"] = runtime_started
    state["runtime_authenticated"] = runtime_authenticated
    state["gateway_status_code"] = gateway_status_code
    state["gateway_reachable"] = gateway_reachable
    for key in RECOVERY_FIELDS:
        if key in auth_recovery:
            state[key] = auth_recovery.get(key)
    if not str(state.get("recovery_phase") or "").strip():
        state["recovery_phase"] = "recovered" if runtime_authenticated else "idle"
    normalized_status = normalize_two_factor_status(state.get("status") or "")
    server_boot_resume_pending = is_server_boot_resume_recovery_state(state, as_dict=as_dict) and not runtime_authenticated

    if runtime_authenticated and gateway_reachable and gateway_status_code != 401:
        state.update(
            {
                "status": "success",
                "message": "Gateway 会话有效，无需再次确认。",
                "last_result": "运行态会话正常。",
                "last_error": "",
                "mode": "",
                "challenge_code": "",
                "challenge_detected_at": "",
                "response_code": "",
                "response_status": "",
                "response_received_at": "",
                "response_submitted_at": "",
                "response_rejected_at": "",
                "challenge_feedback": "",
                "page_title": "",
                "page_url": "",
                "gateway_trace": "",
                "browser_authenticated": True,
                "gateway_authenticated": True,
                "backend_authenticated": True,
                "recovery_phase": "recovered",
                "auto_restart_scheduled": False,
                "manual_takeover_active": False,
            }
        )
        return derive_2fa_action_state(state, as_dict=as_dict, parse_et_time_ms=parse_et_time_ms)

    if (not runtime_authenticated) or gateway_status_code == 401:
        state["gateway_authenticated"] = False
        state["backend_authenticated"] = False
        if not runtime_started:
            state["browser_authenticated"] = False
        active_cycle = normalized_status in {"triggered", "waiting_confirm", "waiting_response"}
        if server_boot_resume_pending and not active_cycle:
            state.update(
                {
                    "status": "resume_pending",
                    "message": (
                        "静默恢复尚未自动成功；当前不会自动补发新的 2FA，如需立即恢复请去 Runtime 页面人工处理。"
                        if str(state.get("probe_result") or "").strip().lower() == "resume_probe_timeout"
                        else "Compute 重启后正在静默复用现有 Gateway Session，本轮不会自动重开 2FA。"
                    ),
                    "last_result": (
                        "静默恢复未自动成功，当前保持被动等待，不会自动新开 2FA。"
                        if str(state.get("probe_result") or "").strip().lower() == "resume_probe_timeout"
                        else "已进入 server_boot 静默恢复窗口。"
                    ),
                    "last_error": "",
                    "mode": "",
                    "challenge_code": "",
                    "challenge_detected_at": "",
                    "response_code": "",
                    "response_status": "",
                    "response_received_at": "",
                    "response_submitted_at": "",
                    "response_rejected_at": "",
                    "challenge_feedback": "",
                    "page_title": "",
                    "page_url": "",
                    "gateway_trace": "",
                }
            )
        elif is_silent_recovery_state(state, as_dict=as_dict) and not active_cycle:
            silent_recovery = build_silent_recovery_message(state, as_dict=as_dict)
            state.update(
                {
                    "status": "recovering",
                    "message": silent_recovery.get("message") or "",
                    "last_result": silent_recovery.get("last_result") or "",
                    "last_error": "",
                    "mode": "",
                    "challenge_code": "",
                    "challenge_detected_at": "",
                    "response_code": "",
                    "response_status": "",
                    "response_received_at": "",
                    "response_submitted_at": "",
                    "response_rejected_at": "",
                    "challenge_feedback": "",
                    "page_title": "",
                    "page_url": "",
                    "gateway_trace": "",
                }
            )
        elif is_manual_auth_required_recovery_state(state, as_dict=as_dict) and not active_cycle:
            state["status"] = "requested"
            state["message"] = "静默恢复窗口已结束，当前需要手动触发 2FA。"
            state["last_result"] = "静默恢复未完成，等待手动触发新的 2FA 轮次。"
        elif normalized_status == "success":
            state["status"] = "requested"
            state["message"] = "旧 Gateway 认证已失效，请重新触发 2FA。"
            state["last_result"] = "旧 Gateway 认证已失效，等待重新触发 2FA。"

    return derive_2fa_action_state(state, as_dict=as_dict, parse_et_time_ms=parse_et_time_ms)


__all__ = [
    "CHALLENGE_RESET_RECOMMEND_MS",
    "RECOVERY_FIELDS",
    "build_silent_recovery_message",
    "derive_2fa_action_state",
    "is_manual_auth_required_recovery_state",
    "is_server_boot_resume_recovery_state",
    "is_silent_recovery_state",
    "normalize_two_factor_state_with_runtime",
    "normalize_two_factor_status",
]
