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


__all__ = [
    "AUTH_EDGE_ALERT_COOLDOWN_MS",
    "AUTH_EDGE_MONITOR_STATE_KEY",
    "AUTH_MONITOR_STATE_KEY",
    "AUTH_PENDING_ALERT_COOLDOWN_MS",
    "AUTH_PENDING_ALERT_TRIGGER_MS",
    "ET",
    "EmitSystemEvent",
    "FetchRuntimeStatus",
    "GetStatePayload",
    "NormalizeEnvironment",
    "NormalizeTwoFactorStateWithRuntime",
    "RequestTwoFactorApproval",
    "TimeStrings",
    "UpsertState",
    "_as_dict",
    "_parse_et_time_ms",
    "_to_bool",
    "_to_int",
    "_to_text",
    "load_auth_attention_summary",
]
