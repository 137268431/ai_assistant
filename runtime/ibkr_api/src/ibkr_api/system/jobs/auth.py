from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo


AUTH_EDGE_ALERT_COOLDOWN_MS = 30 * 60 * 1000
AUTH_EDGE_MONITOR_STATE_KEY = "system_auth_edge_monitor"
AUTH_PENDING_ALERT_TRIGGER_MS = 15 * 60 * 1000
AUTH_PENDING_ALERT_COOLDOWN_MS = 30 * 60 * 1000
AUTH_MONITOR_STATE_KEY = "system_auth_monitor"
ET = ZoneInfo("America/New_York")

NormalizeEnvironment = Callable[[Any, str], str]
GetStatePayload = Callable[[str, str], dict[str, Any]]
NormalizeTwoFactorStateWithRuntime = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
FetchRuntimeStatus = Callable[[str], dict[str, Any]]
TimeStrings = Callable[[], dict[str, str]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]
EmitSystemEvent = Callable[..., dict[str, Any]]
RequestTwoFactorApproval = Callable[..., dict[str, Any]]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_bool(value: Any) -> bool:
    return bool(value)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _parse_et_time_ms(value: Any) -> int:
    text = _to_text(value)
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return int(parsed.timestamp() * 1000)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=ET).timestamp() * 1000)
        except Exception:
            continue
    return 0


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def load_auth_attention_summary(
    *,
    environment: str,
    get_state_payload: GetStatePayload,
    normalize_two_factor_state_with_runtime: NormalizeTwoFactorStateWithRuntime,
    runtime_status: dict[str, Any] | None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    runtime = _as_dict(runtime_status)
    state_payload = get_state_payload("ibkr_2fa", environment)
    state = normalize_two_factor_state_with_runtime(_as_dict(state_payload.get("data")), runtime)
    status = _to_text(state.get("status")).lower() or "requested"
    has_request = bool(
        _to_int(state.get("request_count"), 0) > 0
        or state.get("requested_at")
        or state.get("triggered_at")
        or state.get("message_id")
    )
    runtime_started = bool(
        runtime.get("starting")
        or _as_dict(runtime.get("session")).get("running")
        or _as_dict(runtime.get("websocket")).get("running")
        or _as_dict(runtime.get("order_tracker")).get("running")
    )
    runtime_authenticated = bool(_as_dict(runtime.get("session")).get("authenticated"))
    gateway = _as_dict(runtime.get("gateway"))
    gateway_reachable = bool(gateway.get("running") or gateway.get("reachable"))
    gateway_status_code = _to_int(gateway.get("status_code"), 0)
    gateway_pid = _to_int(gateway.get("pid"), 0)
    gateway_uptime_s = _to_int(gateway.get("uptime_s"), 0)
    started_ms = max(_parse_et_time_ms(state.get("triggered_at")), _parse_et_time_ms(state.get("requested_at")))
    current_ms = int(now_ms or 0) or int(datetime.now(tz=ET).timestamp() * 1000)
    age_min = max(0, round((current_ms - started_ms) / 60000)) if started_ms > 0 else 0
    active = has_request and status in {"requested", "triggered", "waiting_confirm", "waiting_response"}
    needs_attention = gateway_reachable and (not runtime_authenticated or gateway_status_code == 401 or not runtime_started)
    pending_too_long = active and needs_attention and started_ms > 0 and (current_ms - started_ms) >= AUTH_PENDING_ALERT_TRIGGER_MS
    return {
        "status": status,
        "has_request": has_request,
        "active": active,
        "pending_too_long": pending_too_long,
        "cycle_id": _to_text(state.get("cycle_id")),
        "recovery_phase": _to_text(state.get("recovery_phase")),
        "recovery_class": _to_text(state.get("recovery_class")),
        "recovery_reason": _to_text(state.get("recovery_reason")),
        "interruption_kind": _to_text(state.get("interruption_kind")),
        "probe_result": _to_text(state.get("probe_result")),
        "auto_restart_scheduled": _to_bool(state.get("auto_restart_scheduled")),
        "last_runtime_authenticated_at": _to_text(state.get("last_runtime_authenticated_at")),
        "last_gateway_status_code": _to_int(state.get("last_gateway_status_code"), 0),
        "last_recovery_source": _to_text(state.get("last_recovery_source")),
        "age_min": age_min,
        "requested_at": _to_text(state.get("requested_at")),
        "triggered_at": _to_text(state.get("triggered_at")),
        "mode": _to_text(state.get("mode")),
        "challenge_code": _to_text(state.get("challenge_code")),
        "response_status": _to_text(state.get("response_status")).lower(),
        "response_received_at": _to_text(state.get("response_received_at")),
        "response_submitted_at": _to_text(state.get("response_submitted_at")),
        "response_rejected_at": _to_text(state.get("response_rejected_at")),
        "challenge_feedback": _to_text(state.get("challenge_feedback")),
        "operator_action": _to_text(state.get("operator_action")),
        "reset_recommended": _to_bool(state.get("reset_recommended")),
        "reset_reason": _to_text(state.get("reset_reason")),
        "last_result": _to_text(state.get("last_result")),
        "last_error": _to_text(state.get("last_error")),
        "reason": _to_text(state.get("reason")),
        "message": _to_text(state.get("message")),
        "page_url": _to_text(state.get("page_url")),
        "runtime_started": runtime_started,
        "runtime_authenticated": runtime_authenticated,
        "gateway_reachable": gateway_reachable,
        "gateway_status_code": gateway_status_code,
        "gateway_pid": gateway_pid,
        "gateway_uptime_s": gateway_uptime_s,
        "state": state,
    }


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


def _build_waiting_response_advice(auth: dict[str, Any]) -> str:
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


def _build_waiting_response_issue(auth: dict[str, Any]) -> dict[str, str]:
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
        return _build_waiting_response_issue(auth)
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
            _build_waiting_response_advice(auth)
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

    fingerprint = str(
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
    current_state = _as_dict(get_state_payload(AUTH_MONITOR_STATE_KEY, environment).get("data"))
    last_alert_hash = _to_text(current_state.get("last_auth_alert_hash"))
    last_alert_ms = _to_int(current_state.get("last_auth_alert_ms"), 0)
    should_notify = fingerprint != last_alert_hash or last_alert_ms <= 0 or (now_ms - last_alert_ms) >= AUTH_PENDING_ALERT_COOLDOWN_MS
    event_result: dict[str, Any] = {}
    title = "IBKR Session 长时间未恢复认证"
    if auth.get("status") == "waiting_confirm":
        title = "IBKR 2FA 长时间未确认"
    elif auth.get("status") == "waiting_response":
        title = _build_waiting_response_issue(auth).get("title") or title
    elif auth.get("status") in {"requested", "triggered"}:
        title = "IBKR 2FA 长时间未完成"

    if should_notify:
        detail = {
            "检查时间": now["us"],
            "2FA状态": auth.get("status") or "requested",
            "持续时间": f"{auth.get('age_min') or 0} 分钟",
            "恢复阶段": auth.get("recovery_phase") or "idle",
            "轮次ID": auth.get("cycle_id") or "-",
            "Runtime已启动": "yes" if auth.get("runtime_started") else "no",
            "Session认证": "yes" if auth.get("runtime_authenticated") else "no",
            "Gateway状态码": str(auth.get("gateway_status_code")) if auth.get("gateway_status_code") else "n/a",
            "Gateway运行时长(s)": str(auth.get("gateway_uptime_s")) if auth.get("gateway_uptime_s") else "n/a",
            "处理建议": _build_waiting_response_advice(auth) if auth.get("status") == "waiting_response" else "优先去 Runtime 页面确认当前轮次；如果仍是手机确认，只在 IBKR App 点一次确认。",
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
        event_result = emit_system_event(
            event_type="alert",
            level="warning",
            source="ibkr_compute",
            title=title,
            detail=detail,
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


def _needs_auth_attention(auth: dict[str, Any]) -> bool:
    return bool(auth.get("gateway_reachable")) and (
        not bool(auth.get("runtime_authenticated"))
        or _to_int(auth.get("gateway_status_code"), 0) == 401
        or not bool(auth.get("runtime_started"))
    )


def build_two_factor_hourly_check_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    get_state_payload: GetStatePayload,
    normalize_two_factor_state_with_runtime: NormalizeTwoFactorStateWithRuntime,
    fetch_runtime_status: FetchRuntimeStatus,
    request_two_factor_approval: RequestTwoFactorApproval,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    runtime_result = fetch_runtime_status(environment)
    runtime_payload = _as_dict(runtime_result.get("payload"))
    now_ms = int(datetime.now(tz=ET).timestamp() * 1000)
    auth = load_auth_attention_summary(
        environment=environment,
        get_state_payload=get_state_payload,
        normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
        runtime_status=runtime_payload,
        now_ms=now_ms,
    )
    if not _needs_auth_attention(auth):
        return {"ok": True, "environment": environment, "skipped": True, "reason": "auth_not_required", "source": "ibkr-api", "job_id": "ibkr_2fa_hourly_check"}, 200

    state_payload = get_state_payload("ibkr_2fa", environment)
    state = normalize_two_factor_state_with_runtime(_as_dict(state_payload.get("data")), runtime_payload)
    status = _to_text(state.get("status")).lower()
    reason = _to_text(state.get("reason")).lower()
    last_push_ms = _to_int(state.get("last_request_push_ms"), 0)
    reminder_eligible = status == "requested" and reason in {"manual_start", "startup", "weekly_reauth", "manual_gateway_restart"}
    if not reminder_eligible:
        return {"ok": True, "environment": environment, "skipped": True, "reason": "state_not_eligible", "source": "ibkr-api", "job_id": "ibkr_2fa_hourly_check"}, 200
    if last_push_ms > 0 and (now_ms - last_push_ms) < 55 * 60 * 1000:
        return {"ok": True, "environment": environment, "skipped": True, "reason": "recent_push_exists", "source": "ibkr-api", "job_id": "ibkr_2fa_hourly_check"}, 200

    result = request_two_factor_approval(
        environment=environment,
        reason=_to_text(state.get("reason")) or "scheduled_2fa_check",
        source="ibkr_scheduler",
        message=(
            "本周重登已晚于美股周一盘前建议完成时间，请尽快只去当前飞书卡片点击开始验证。"
            if reason == "weekly_reauth" and bool(state.get("business_deadline_overdue"))
            else (
                "本周重登仍停在待手动触发阶段，请只去当前飞书卡片点击开始验证；最晚请于美股周一盘前前完成。"
                if reason == "weekly_reauth"
                else (
                    "网关重启后的新轮次仍停在待手动触发阶段，请只去当前启动卡片点击开始验证。"
                    if reason == "manual_gateway_restart"
                    else "启动验证仍停在待手动触发阶段，请只去当前飞书卡片点击开始验证。"
                )
            )
        ),
        detail={
            "当前状态": status or "requested",
            "周验证截止": (
                f"{_to_text(state.get('business_deadline_cn'))} 北京时间 / {_to_text(state.get('business_deadline_at'))} 美东"
                if _to_text(state.get("business_deadline_cn"))
                else "n/a"
            ),
            "Runtime已启动": "yes" if auth.get("runtime_started") else "no",
            "Session认证": "yes" if auth.get("runtime_authenticated") else "no",
            "Gateway状态码": str(auth.get("gateway_status_code")) if auth.get("gateway_status_code") else "n/a",
            "最近结果": _to_text(state.get("last_result")),
            "最近错误": _to_text(state.get("last_error")),
        },
        force_reset=False,
        force_new=False,
    )
    return {"ok": True, "environment": environment, "requested": True, "result": result, "source": "ibkr-api", "job_id": "ibkr_2fa_hourly_check"}, 200


def build_weekly_reauth_followup_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    get_state_payload: GetStatePayload,
    normalize_two_factor_state_with_runtime: NormalizeTwoFactorStateWithRuntime,
    fetch_runtime_status: FetchRuntimeStatus,
    request_two_factor_approval: RequestTwoFactorApproval,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    runtime_result = fetch_runtime_status(environment)
    runtime_payload = _as_dict(runtime_result.get("payload"))
    auth = load_auth_attention_summary(
        environment=environment,
        get_state_payload=get_state_payload,
        normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
        runtime_status=runtime_payload,
    )
    if not _needs_auth_attention(auth):
        return {"ok": True, "environment": environment, "skipped": True, "reason": "auth_not_required", "source": "ibkr-api", "job_id": "ibkr_weekly_reauth_followup"}, 200
    state_payload = get_state_payload("ibkr_2fa", environment)
    state = normalize_two_factor_state_with_runtime(_as_dict(state_payload.get("data")), runtime_payload)
    if _to_text(state.get("status")).lower() != "requested" or _to_text(state.get("reason")).lower() != "weekly_reauth":
        return {"ok": True, "environment": environment, "skipped": True, "reason": "weekly_reauth_not_pending", "source": "ibkr-api", "job_id": "ibkr_weekly_reauth_followup"}, 200
    result = request_two_factor_approval(
        environment=environment,
        reason="weekly_reauth",
        source="ibkr_scheduler",
        message=(
            "本周重登已晚于美股周一盘前建议完成时间，请尽快只去当前飞书卡片点击开始验证。"
            if bool(state.get("business_deadline_overdue"))
            else "美国周一已进入盘前准备窗口。本周重登仍待手动开始，请只去当前飞书卡片点击开始验证；最晚请于美股周一盘前前完成。"
        ),
        detail={
            "提醒类型": "weekly_reauth_followup",
            "最晚完成": (
                f"{_to_text(state.get('business_deadline_cn'))} 北京时间 / {_to_text(state.get('business_deadline_at'))} 美东"
                if _to_text(state.get("business_deadline_cn"))
                else "美股周一盘前前"
            ),
            "点击后时限": "180 秒",
            "当前状态": _to_text(state.get("status")) or "requested",
            "Runtime已启动": "yes" if auth.get("runtime_started") else "no",
            "Session认证": "yes" if auth.get("runtime_authenticated") else "no",
            "Gateway状态码": str(auth.get("gateway_status_code")) if auth.get("gateway_status_code") else "n/a",
            "最近结果": _to_text(state.get("last_result")),
            "最近错误": _to_text(state.get("last_error")),
        },
        force_reset=False,
        force_new=False,
    )
    return {"ok": True, "environment": environment, "requested": True, "result": result, "source": "ibkr-api", "job_id": "ibkr_weekly_reauth_followup"}, 200


def build_weekly_reauth_reminder_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    get_state_payload: GetStatePayload,
    normalize_two_factor_state_with_runtime: NormalizeTwoFactorStateWithRuntime,
    fetch_runtime_status: FetchRuntimeStatus,
    request_two_factor_approval: RequestTwoFactorApproval,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    runtime_result = fetch_runtime_status(environment)
    runtime_payload = _as_dict(runtime_result.get("payload"))
    auth = load_auth_attention_summary(
        environment=environment,
        get_state_payload=get_state_payload,
        normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
        runtime_status=runtime_payload,
    )
    if auth.get("runtime_authenticated") and auth.get("gateway_reachable") and _to_int(auth.get("gateway_status_code"), 0) != 401:
        return {"ok": True, "environment": environment, "skipped": True, "reason": "already_authenticated", "source": "ibkr-api", "job_id": "ibkr_weekly_reauth_reminder"}, 200
    state_payload = get_state_payload("ibkr_2fa", environment)
    state = normalize_two_factor_state_with_runtime(_as_dict(state_payload.get("data")), runtime_payload)
    result = request_two_factor_approval(
        environment=environment,
        reason="weekly_reauth",
        source="ibkr_scheduler",
        message="美国周一已开始，本周重登提醒已发出。你有空时再去当前飞书卡片点击开始验证；最晚请于美股周一盘前前完成。点击开始后需在 180 秒内完成当前 2FA。",
        detail={
            "提醒类型": "weekly_reauth",
            "最晚完成": (
                f"{_to_text(state.get('business_deadline_cn'))} 北京时间 / {_to_text(state.get('business_deadline_at'))} 美东"
                if _to_text(state.get("business_deadline_cn"))
                else "美股周一盘前前"
            ),
            "点击后时限": "180 秒",
            "当前状态": _to_text(state.get("status")) or "requested",
            "Runtime已启动": "yes" if auth.get("runtime_started") else "no",
            "Session认证": "yes" if auth.get("runtime_authenticated") else "no",
            "Gateway状态码": str(auth.get("gateway_status_code")) if auth.get("gateway_status_code") else "n/a",
        },
        force_reset=False,
        force_new=False,
    )
    return {"ok": True, "environment": environment, "requested": True, "result": result, "source": "ibkr-api", "job_id": "ibkr_weekly_reauth_reminder"}, 200
