from __future__ import annotations

from datetime import datetime
from typing import Any

from .auth_issue import build_waiting_response_advice, build_waiting_response_issue
from .auth_shared import (
    AUTH_MONITOR_STATE_KEY,
    AUTH_PENDING_ALERT_COOLDOWN_MS,
    ET,
    EmitSystemEvent,
    FetchRuntimeStatus,
    GetStatePayload,
    NormalizeEnvironment,
    NormalizeTwoFactorStateWithRuntime,
    TimeStrings,
    UpsertState,
    _as_dict,
    _to_int,
    _to_text,
    load_auth_attention_summary,
)


def _build_auth_pending_fingerprint(auth: dict[str, Any]) -> str:
    return str(
        {
            "status": auth.get("status") or "",
            "mode": auth.get("mode") or "",
            "age_bucket": int((_to_int(auth.get("age_min"), 0)) / 5),
            "challenge": "yes" if auth.get("challenge_code") else "no",
            "response_status": auth.get("response_status") or "",
            "response_rejected_at": auth.get("response_rejected_at") or "",
            "challenge_feedback": auth.get("challenge_feedback") or "",
            "operator_action": auth.get("operator_action") or "",
            "reset_recommended": "yes" if auth.get("reset_recommended") else "no",
            "gateway_status_code": auth.get("gateway_status_code") or 0,
            "runtime_started": "yes" if auth.get("runtime_started") else "no",
            "runtime_authenticated": "yes" if auth.get("runtime_authenticated") else "no",
        }
    )


def _build_pending_title(auth: dict[str, Any]) -> str:
    title = "IBKR Session 长时间未恢复认证"
    if auth.get("status") == "waiting_confirm":
        return "IBKR 2FA 长时间未确认"
    if auth.get("status") == "waiting_response":
        return build_waiting_response_issue(auth).get("title") or title
    if auth.get("status") in {"requested", "triggered"}:
        return "IBKR 2FA 长时间未完成"
    return title


def _build_pending_detail(auth: dict[str, Any], *, now_us: str) -> dict[str, Any]:
    detail = {
        "检查时间": now_us,
        "2FA状态": auth.get("status") or "requested",
        "持续时间": f"{auth.get('age_min') or 0} 分钟",
        "恢复阶段": auth.get("recovery_phase") or "idle",
        "轮次ID": auth.get("cycle_id") or "-",
        "Runtime已启动": "yes" if auth.get("runtime_started") else "no",
        "Session认证": "yes" if auth.get("runtime_authenticated") else "no",
        "Gateway状态码": str(auth.get("gateway_status_code")) if auth.get("gateway_status_code") else "n/a",
        "Gateway运行时长(s)": str(auth.get("gateway_uptime_s")) if auth.get("gateway_uptime_s") else "n/a",
        "处理建议": build_waiting_response_advice(auth)
        if auth.get("status") == "waiting_response"
        else "优先去 Runtime 页面确认当前轮次；如果仍是手机确认，只在 IBKR App 点一次确认。",
    }
    optional_map = {
        "恢复原因": auth.get("recovery_reason"),
        "中断类型": auth.get("interruption_kind"),
        "验证模式": auth.get("mode"),
        "Challenge": auth.get("challenge_code"),
        "响应状态": auth.get("response_status"),
        "响应码收到": auth.get("response_received_at"),
        "响应码提交": auth.get("response_submitted_at"),
        "响应码拒绝": auth.get("response_rejected_at"),
        "Gateway反馈": auth.get("challenge_feedback"),
        "建议动作": auth.get("operator_action"),
        "重开原因": auth.get("reset_reason"),
        "触发时间": auth.get("triggered_at"),
        "上次认证成功": auth.get("last_runtime_authenticated_at"),
        "最近反馈": auth.get("last_result"),
        "最近错误": auth.get("last_error"),
    }
    if auth.get("reset_recommended"):
        optional_map["建议重开"] = "yes"
    if auth.get("gateway_pid"):
        optional_map["GatewayPID"] = str(auth.get("gateway_pid"))
    for key, value in optional_map.items():
        text = _to_text(value)
        if text:
            detail[key] = text
    return detail


def build_auth_pending_guard_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    get_state_payload: GetStatePayload,
    normalize_two_factor_state_with_runtime: NormalizeTwoFactorStateWithRuntime,
    fetch_runtime_status: FetchRuntimeStatus,
    time_strings: TimeStrings,
    upsert_state: UpsertState,
    emit_system_event: EmitSystemEvent,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    runtime_result = fetch_runtime_status(environment)
    runtime_payload = _as_dict(runtime_result.get("payload"))
    now = time_strings()
    now_ms = int(datetime.now(tz=ET).timestamp() * 1000)
    auth = load_auth_attention_summary(
        environment=environment,
        get_state_payload=get_state_payload,
        normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
        runtime_status=runtime_payload,
        now_ms=now_ms,
    )
    next_state = {
        "last_auth_scan_at": now["us"],
        "last_auth_status": auth.get("status") or "",
        "last_auth_age_min": auth.get("age_min") or 0,
    }
    if not auth.get("pending_too_long"):
        next_state["last_auth_issue_at"] = ""
        upsert_state(AUTH_MONITOR_STATE_KEY, environment, next_state, now["date"])
        return {
            "ok": True,
            "environment": environment,
            "pending_too_long": False,
            "state": next_state,
            "runtime_status_error": _to_text(runtime_result.get("error")),
            "source": "ibkr-api",
            "job_id": "ibkr_auth_pending_guard",
        }, 200

    fingerprint = _build_auth_pending_fingerprint(auth)
    current_state = _as_dict(get_state_payload(AUTH_MONITOR_STATE_KEY, environment).get("data"))
    last_alert_hash = _to_text(current_state.get("last_auth_alert_hash"))
    last_alert_ms = _to_int(current_state.get("last_auth_alert_ms"), 0)
    should_notify = fingerprint != last_alert_hash or last_alert_ms <= 0 or (now_ms - last_alert_ms) >= AUTH_PENDING_ALERT_COOLDOWN_MS
    event_result: dict[str, Any] = {}
    title = _build_pending_title(auth)

    if should_notify:
        event_result = emit_system_event(
            event_type="alert",
            level="warning",
            source="ibkr_compute",
            title=title,
            detail=_build_pending_detail(auth, now_us=now["us"]),
            environment=environment,
        )
        next_state.update(
            {
                "last_auth_issue_at": now["us"],
                "last_auth_alert_ms": now_ms,
                "last_auth_alert_hash": fingerprint,
            }
        )
    upsert_state(AUTH_MONITOR_STATE_KEY, environment, next_state, now["date"])
    return {
        "ok": True,
        "environment": environment,
        "pending_too_long": True,
        "title": title,
        "state": next_state,
        "notified": bool(event_result.get("notified")),
        "runtime_status_error": _to_text(runtime_result.get("error")),
        "source": "ibkr-api",
        "job_id": "ibkr_auth_pending_guard",
    }, 200


__all__ = ["build_auth_pending_guard_response"]
