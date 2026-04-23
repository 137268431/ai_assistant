from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ibkr_api.runtime.two_factor import derive_2fa_action_state, normalize_two_factor_status, normalize_two_factor_state_with_runtime
from ibkr_api.two_factor.deadlines import derive_two_factor_deadlines, parse_et_time_ms


ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")
IBKR_2FA_STATE_KEY = "ibkr_2fa"
IBKR_2FA_STATE_DATE = "global"
ACTIVE_STATUSES = {"requested", "triggered", "waiting_confirm", "waiting_response"}
CURRENT_CYCLE_ACTIVE_STATUSES = {"triggered", "waiting_confirm", "waiting_response"}
TERMINAL_STATUSES = {"success", "timeout", "failed"}
CLEARABLE_FIELDS = (
    "mode",
    "mode_changed_at",
    "mode_timeline",
    "challenge_code",
    "challenge_detected_at",
    "response_code",
    "response_status",
    "response_received_at",
    "response_submitted_at",
    "response_rejected_at",
    "challenge_feedback",
    "page_title",
    "page_url",
    "gateway_trace",
    "passive_network_summary",
    "passive_network_history",
    "cookie_bridge_timeline",
    "message",
    "last_error",
    "last_result",
    "result_at",
    "triggered_at",
    "next_retry_at",
    "recovery_phase",
    "recovery_reason",
    "interruption_kind",
    "manual_takeover_started_at",
    "manual_takeover_until",
    "probe_started_at",
    "probe_last_checked_at",
    "probe_result",
    "last_recovery_source",
    "lock_owner",
    "lock_expires_at",
)
RESET_BOOLEAN_FIELDS = (
    "manual_takeover_active",
    "auto_restart_scheduled",
    "browser_authenticated",
    "backend_authenticated",
    "gateway_authenticated",
    "runtime_authenticated",
    "runtime_started",
    "reset_recommended",
)


NormalizeEnvironment = Callable[[Any, str], str]
AsDict = Callable[[Any], dict[str, Any]]


def time_strings(now_ts: float | None = None) -> dict[str, str]:
    current = float(now_ts) if now_ts is not None else datetime.now(tz=ET).timestamp()
    now_et = datetime.fromtimestamp(current, tz=ET)
    now_cn = datetime.fromtimestamp(current, tz=CN)
    return {
        "us": now_et.strftime("%Y-%m-%d %H:%M:%S"),
        "cn": now_cn.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now_et.strftime("%Y-%m-%d"),
    }


def is_active_status(status: Any) -> bool:
    return normalize_two_factor_status(status) in ACTIVE_STATUSES


def is_current_cycle_active_status(status: Any) -> bool:
    return normalize_two_factor_status(status) in CURRENT_CYCLE_ACTIVE_STATUSES


def is_terminal_status(status: Any) -> bool:
    return normalize_two_factor_status(status) in TERMINAL_STATUSES


def finalize_two_factor_state(state_data: dict[str, Any] | None, *, as_dict: AsDict) -> dict[str, Any]:
    state = as_dict(state_data)
    state["status"] = normalize_two_factor_status(state.get("status"))
    state.update(derive_two_factor_deadlines(state))
    return derive_2fa_action_state(state, as_dict=as_dict, parse_et_time_ms=parse_et_time_ms)


def apply_runtime_state(
    state_data: dict[str, Any] | None,
    runtime_status: dict[str, Any] | None,
    *,
    as_dict: AsDict,
) -> dict[str, Any]:
    merged = normalize_two_factor_state_with_runtime(
        as_dict(state_data),
        as_dict(runtime_status),
        as_dict=as_dict,
        parse_et_time_ms=parse_et_time_ms,
    )
    merged.update(derive_two_factor_deadlines(merged))
    return derive_2fa_action_state(merged, as_dict=as_dict, parse_et_time_ms=parse_et_time_ms)


def load_two_factor_state(
    pb: Any,
    environment: Any,
    *,
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    state_key: str = IBKR_2FA_STATE_KEY,
    date: str = IBKR_2FA_STATE_DATE,
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    try:
        record = pb.get_state(state_key, runtime_environment, date=date)
    except Exception:
        record = None
    payload = as_dict((record or {}).get("data") if isinstance(record, dict) else {})
    if not str(payload.get("status") or "").strip():
        payload["status"] = "requested"
    if not str(payload.get("recovery_phase") or "").strip():
        payload["recovery_phase"] = "idle"
    return {
        "environment": runtime_environment,
        "date": str(((record or {}).get("date") if isinstance(record, dict) else "") or date).strip() or date,
        "record": record if isinstance(record, dict) else {},
        "data": finalize_two_factor_state(payload, as_dict=as_dict),
    }


def save_two_factor_state(
    pb: Any,
    environment: Any,
    patch: dict[str, Any] | None,
    *,
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    state_key: str = IBKR_2FA_STATE_KEY,
    date: str = IBKR_2FA_STATE_DATE,
) -> dict[str, Any]:
    current = load_two_factor_state(
        pb,
        environment,
        normalize_environment=normalize_environment,
        as_dict=as_dict,
        state_key=state_key,
        date=date,
    )
    next_state = {
        **as_dict(current.get("data")),
        **as_dict(patch),
        "updated_at": str(as_dict(patch).get("updated_at") or time_strings()["us"]),
    }
    next_state = finalize_two_factor_state(next_state, as_dict=as_dict)
    try:
        record = pb.upsert_state(state_key, current["environment"], next_state, date=date)
    except Exception:
        record = current.get("record") or {}
    return {
        "environment": current["environment"],
        "date": date,
        "record": record if isinstance(record, dict) else {},
        "data": next_state,
    }


def ensure_requested_state(
    pb: Any,
    environment: Any,
    *,
    reason: str,
    detail: dict[str, Any],
    source: str,
    message: str,
    force_reset: bool,
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
) -> dict[str, Any]:
    current = load_two_factor_state(pb, environment, normalize_environment=normalize_environment, as_dict=as_dict)
    current_data = as_dict(current.get("data"))
    current_status = normalize_two_factor_status(current_data.get("status"))
    current_times = time_strings()
    active_flow = is_current_cycle_active_status(current_status)
    active_status = is_active_status(current_status)
    preserve_current_display = active_flow and bool(current_data.get("message_id")) and not force_reset
    next_status = current_status if (active_status and not force_reset) else "requested"
    next_detail = current_data.get("detail") if preserve_current_display else (detail or ({} if force_reset else as_dict(current_data.get("detail"))))
    patch: dict[str, Any] = {
        "status": next_status,
        "reason": str(current_data.get("reason") or reason or "manual_reauth") if preserve_current_display else str(reason or current_data.get("reason") or "manual_reauth"),
        "detail": next_detail,
        "source": str(current_data.get("source") or source or "ibkr_api") if preserve_current_display else str(source or current_data.get("source") or "ibkr_api"),
        "requested_at": str(current_data.get("requested_at") or current_times["us"]) if preserve_current_display else current_times["us"],
        "last_request_at": current_times["us"],
        "request_count": int(current_data.get("request_count") or 0) + 1,
    }
    if not active_flow and (force_reset or not active_status):
        for key in CLEARABLE_FIELDS:
            patch[key] = ""
        for key in RESET_BOOLEAN_FIELDS:
            patch[key] = False
        patch.update(
            {
                "probe_attempts": 0,
                "push_body_sample_count": 0,
                "gateway_status_code": 0,
                "gateway_sso_expires_ms": 0,
                "previous_cycle": None,
            }
        )
    if preserve_current_display and str(current_data.get("message") or "").strip():
        patch["message"] = str(current_data.get("message") or "")
    elif next_status == "requested":
        patch["message"] = str(message or ("" if force_reset else current_data.get("message") or ""))
    elif message and not str(current_data.get("message") or "").strip():
        patch["message"] = message
    return save_two_factor_state(pb, environment, patch, normalize_environment=normalize_environment, as_dict=as_dict)


__all__ = [
    "ACTIVE_STATUSES",
    "CURRENT_CYCLE_ACTIVE_STATUSES",
    "IBKR_2FA_STATE_DATE",
    "IBKR_2FA_STATE_KEY",
    "TERMINAL_STATUSES",
    "apply_runtime_state",
    "ensure_requested_state",
    "finalize_two_factor_state",
    "is_active_status",
    "is_current_cycle_active_status",
    "is_terminal_status",
    "load_two_factor_state",
    "save_two_factor_state",
    "time_strings",
]
