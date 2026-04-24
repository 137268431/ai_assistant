from __future__ import annotations

from datetime import datetime
from typing import Any

from .auth_issue import (
    build_auth_immediate_issue,
    build_waiting_response_advice,
    is_operational_2fa_issue,
    is_waiting_response_issue_kind,
)
from .auth_shared import (
    AUTH_EDGE_ALERT_COOLDOWN_MS,
    AUTH_EDGE_MONITOR_STATE_KEY,
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


def _build_auth_immediate_fingerprint(auth: dict[str, Any], issue: dict[str, Any]) -> str:
    return str(
        {
            "issue_kind": issue.get("kind") or "",
            "cycle_id": auth.get("cycle_id") or "",
            "status": auth.get("status") or "",
            "requested_at": auth.get("requested_at") or "",
            "triggered_at": auth.get("triggered_at") or "",
            "gateway_status_code": auth.get("gateway_status_code") or 0,
            "recovery_class": auth.get("recovery_class") or "",
            "recovery_reason": auth.get("recovery_reason") or "",
            "interruption_kind": auth.get("interruption_kind") or "",
            "probe_result": auth.get("probe_result") or "",
            "auto_restart_scheduled": "yes" if auth.get("auto_restart_scheduled") else "no",
            "runtime_started": "yes" if auth.get("runtime_started") else "no",
            "runtime_authenticated": "yes" if auth.get("runtime_authenticated") else "no",
            "challenge_code": auth.get("challenge_code") or "",
            "response_status": auth.get("response_status") or "",
            "response_rejected_at": auth.get("response_rejected_at") or "",
            "challenge_feedback": auth.get("challenge_feedback") or "",
            "operator_action": auth.get("operator_action") or "",
            "reset_recommended": "yes" if auth.get("reset_recommended") else "no",
        }
    )


def _should_notify_auth_immediate_alert(
    previous: dict[str, Any],
    auth: dict[str, Any],
    issue: dict[str, Any],
    fingerprint: str,
    now_ms: int,
) -> bool:
    prev_gateway_status_code = _to_int(previous.get("last_gateway_status_code"), 0)
    prev_runtime_authenticated = _to_text(previous.get("last_runtime_authenticated")) == "yes"
    prev_active = _to_text(previous.get("last_auth_active")) == "yes"
    prev_status = _to_text(previous.get("last_auth_status")).lower()
    prev_requested_at = _to_text(previous.get("last_requested_at"))
    prev_triggered_at = _to_text(previous.get("last_triggered_at"))
    prev_issue_kind = _to_text(previous.get("last_auth_issue_kind"))
    prev_cycle_id = _to_text(previous.get("last_auth_cycle_id"))
    last_alert_hash = _to_text(previous.get("last_auth_edge_alert_hash"))
    last_alert_ms = _to_int(previous.get("last_auth_edge_alert_ms"), 0)
    current_gateway_status_code = _to_int(auth.get("gateway_status_code"), 0)
    current_runtime_authenticated = bool(auth.get("runtime_authenticated"))
    current_active = bool(auth.get("active"))
    current_status = _to_text(auth.get("status")).lower()
    current_requested_at = _to_text(auth.get("requested_at"))
    current_triggered_at = _to_text(auth.get("triggered_at"))
    issue_kind = _to_text(issue.get("kind"))
    current_cycle_id = _to_text(auth.get("cycle_id"))

    if issue_kind in {"waiting_confirm"} or is_waiting_response_issue_kind(issue_kind):
        if current_cycle_id and issue_kind == prev_issue_kind and current_cycle_id == prev_cycle_id:
            return False

    edge_detected = (
        not _to_text(previous.get("last_auth_scan_at"))
        or (current_gateway_status_code == 401 and prev_gateway_status_code != 401)
        or (not current_runtime_authenticated and prev_runtime_authenticated)
        or (current_active and not prev_active)
        or (current_active and current_requested_at and current_requested_at != prev_requested_at)
        or (current_active and current_triggered_at and current_triggered_at != prev_triggered_at)
        or (issue_kind and issue_kind != prev_issue_kind)
        or (current_status and current_status != prev_status and current_active)
    )
    return edge_detected or fingerprint != last_alert_hash or last_alert_ms <= 0 or (now_ms - last_alert_ms) >= AUTH_EDGE_ALERT_COOLDOWN_MS


def _auth_issue_detail(auth: dict[str, Any], issue: dict[str, Any], *, now_us: str) -> dict[str, Any]:
    issue_kind = _to_text(issue.get("kind"))
    detail = {
        "异常结论": _to_text(issue.get("summary")),
        "检查时间": now_us,
        "2FA状态": _to_text(auth.get("status")) or "requested",
        "恢复阶段": _to_text(auth.get("recovery_phase")) or "idle",
        "轮次ID": _to_text(auth.get("cycle_id")) or "-",
        "Session认证": "yes" if auth.get("runtime_authenticated") else "no",
        "Runtime已启动": "yes" if auth.get("runtime_started") else "no",
        "Gateway状态码": str(auth.get("gateway_status_code")) if auth.get("gateway_status_code") else "n/a",
        "Gateway运行时长(s)": str(auth.get("gateway_uptime_s")) if auth.get("gateway_uptime_s") else "n/a",
        "处理建议": (
            build_waiting_response_advice(auth)
            if is_waiting_response_issue_kind(issue_kind)
            else (
                "先观察当前静默恢复窗口，不要重复触发 2FA；若长时间未恢复，再去 Runtime 页面人工接管或手动重开。"
                if issue_kind in {"session_recovering", "stale_broker_recovering"}
                else (
                    "本次是 Gateway 重启后的新轮次。优先处理当前 2FA，不要把它当成旧 Session 自然失效后反复重触发。"
                    if issue_kind == "gateway_restart_reauth_required"
                    else "优先打开 Runtime 页面确认当前状态；如果仍是 waiting_confirm，只在 IBKR App 点一次确认。"
                )
            )
        ),
    }
    optional_map = {
        "触发原因": auth.get("reason"),
        "恢复原因": auth.get("recovery_reason"),
        "中断类型": auth.get("interruption_kind"),
        "最近反馈": auth.get("message"),
        "验证模式": auth.get("mode"),
        "Challenge": auth.get("challenge_code"),
        "响应状态": auth.get("response_status"),
        "响应码收到": auth.get("response_received_at"),
        "响应码提交": auth.get("response_submitted_at"),
        "响应码拒绝": auth.get("response_rejected_at"),
        "Gateway反馈": auth.get("challenge_feedback"),
        "建议动作": auth.get("operator_action"),
        "重开原因": auth.get("reset_reason"),
        "请求时间": auth.get("requested_at"),
        "触发时间": auth.get("triggered_at"),
        "上次认证成功": auth.get("last_runtime_authenticated_at"),
        "页面": auth.get("page_url"),
        "最近错误": auth.get("last_error"),
        "最近结果": auth.get("last_result"),
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


def build_auth_edge_guard_response(
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
    current_state = _as_dict(get_state_payload(AUTH_EDGE_MONITOR_STATE_KEY, environment).get("data"))
    next_state = {
        "last_auth_scan_at": now["us"],
        "last_auth_status": auth.get("status") or "",
        "last_auth_age_min": auth.get("age_min") or 0,
        "last_runtime_authenticated": "yes" if auth.get("runtime_authenticated") else "no",
        "last_gateway_status_code": auth.get("gateway_status_code") or 0,
        "last_auth_active": "yes" if auth.get("active") else "no",
        "last_requested_at": auth.get("requested_at") or "",
        "last_triggered_at": auth.get("triggered_at") or "",
        "last_auth_cycle_id": auth.get("cycle_id") or "",
    }
    issue = build_auth_immediate_issue(auth)
    event_result: dict[str, Any] = {}

    if not issue:
        next_state.update(
            {
                "last_auth_issue_at": "",
                "last_auth_issue_kind": "",
                "last_auth_issue_title": "",
                "last_auth_issue_summary": "",
                "last_auth_edge_alert_ms": 0,
                "last_auth_edge_alert_hash": "",
            }
        )
        upsert_state(AUTH_EDGE_MONITOR_STATE_KEY, environment, next_state, now["date"])
        return {
            "ok": True,
            "environment": environment,
            "issue": None,
            "state": next_state,
            "runtime_status_error": _to_text(runtime_result.get("error")),
            "source": "ibkr-api",
            "job_id": "ibkr_auth_edge_guard",
        }, 200

    fingerprint = _build_auth_immediate_fingerprint(auth, issue)
    if _should_notify_auth_immediate_alert(current_state, auth, issue, fingerprint, now_ms):
        event_type = "status_change" if is_operational_2fa_issue(issue) else "alert"
        level = "info" if is_operational_2fa_issue(issue) else "warning"
        event_result = emit_system_event(
            event_type=event_type,
            level=level,
            source="ibkr_compute",
            title=_to_text(issue.get("title")),
            detail=_auth_issue_detail(auth, issue, now_us=now["us"]),
            environment=environment,
        )
        next_state.update(
            {
                "last_auth_issue_at": now["us"],
                "last_auth_issue_kind": issue.get("kind") or "",
                "last_auth_issue_title": issue.get("title") or "",
                "last_auth_issue_summary": issue.get("summary") or "",
                "last_auth_edge_alert_ms": now_ms,
                "last_auth_edge_alert_hash": fingerprint,
            }
        )
    else:
        next_state.update(
            {
                "last_auth_issue_kind": issue.get("kind") or "",
                "last_auth_issue_title": issue.get("title") or "",
                "last_auth_issue_summary": issue.get("summary") or "",
            }
        )
    upsert_state(AUTH_EDGE_MONITOR_STATE_KEY, environment, next_state, now["date"])
    return {
        "ok": True,
        "environment": environment,
        "issue": issue,
        "state": next_state,
        "notified": bool(event_result.get("notified")),
        "runtime_status_error": _to_text(runtime_result.get("error")),
        "source": "ibkr-api",
        "job_id": "ibkr_auth_edge_guard",
    }, 200


__all__ = ["build_auth_edge_guard_response"]
