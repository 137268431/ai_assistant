from __future__ import annotations

from typing import Any, Callable

from ibkr_api.modes import request_broker_mode

from ibkr_api.runtime.two_factor import normalize_two_factor_status
from ibkr_api.two_factor.deadlines import parse_et_time_ms
from ibkr_api.two_factor.delivery import deliver_two_factor_card
from ibkr_api.two_factor.startup_sync import sync_startup_auth_progress
from ibkr_api.two_factor.state import is_terminal_status, load_two_factor_state, save_two_factor_state, time_strings


NormalizeEnvironment = Callable[[Any, str], str]
AsDict = Callable[[Any], dict[str, Any]]
EmitSystemEvent = Callable[..., dict[str, Any]]
SendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
UpdateInteractive = Callable[[str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
MergeStartupSteps = Callable[[Any, Any, bool], dict[str, dict[str, Any]]]
DeliverStartupProgressCard = Callable[[dict[str, Any], str], dict[str, Any]]

SUPPORTED_STATUSES = {"requested", "triggered", "waiting_confirm", "waiting_response", "success", "timeout", "failed"}
AUTO_RESTORE_SUCCESS_EVENT_DEDUPE_MS = 2 * 60 * 1000


def _terminal_success_reason(state: dict[str, Any]) -> str:
    return str(state.get("reason") or state.get("recovery_reason") or "").strip().lower()


def _should_emit_terminal_system_event(
    status: str,
    current_data: dict[str, Any],
    next_data: dict[str, Any],
    current_ms: int,
) -> bool:
    if status != "success":
        return True
    reason = _terminal_success_reason(next_data)
    if reason != "auto_restore":
        return True
    if normalize_two_factor_status(current_data.get("status")) != "success":
        return True
    if _terminal_success_reason(current_data) != reason:
        return True
    previous_result_ms = parse_et_time_ms(current_data.get("result_at"))
    if previous_result_ms <= 0:
        return True
    return (int(current_ms or 0) - previous_result_ms) >= AUTO_RESTORE_SUCCESS_EVENT_DEDUPE_MS


def build_two_factor_result_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    console_base_url: str,
    config_value: ConfigValue,
    send_interactive: SendInteractive,
    update_interactive: UpdateInteractive,
    emit_system_event: EmitSystemEvent | None,
    merge_startup_steps: MergeStartupSteps,
    deliver_startup_progress_card: DeliverStartupProgressCard,
) -> tuple[dict[str, Any], int]:
    environment = request_broker_mode(payload)
    status = normalize_two_factor_status(payload.get("status") or "requested")
    if status not in SUPPORTED_STATUSES:
        status = "failed"
    current = load_two_factor_state(pb, environment, normalize_environment=normalize_environment, as_dict=as_dict)
    current_data = as_dict(current.get("data"))
    current_times = time_strings()
    current_ms = parse_et_time_ms(current_times["us"])
    state_patch = as_dict(payload.get("state_patch"))
    patch: dict[str, Any] = {
        "status": status,
        "detail": as_dict(payload.get("detail")),
        "source": str(payload.get("source") or "ibkr_compute").strip() or "ibkr_compute",
        "message": str(payload.get("message") or ""),
        "last_result": str(payload.get("last_result") or payload.get("message") or ""),
        **state_patch,
    }
    if status in {"triggered", "waiting_confirm", "waiting_response"}:
        patch["requested_at"] = str(state_patch.get("requested_at") or current_data.get("requested_at") or current_times["us"])
        patch["triggered_at"] = str(state_patch.get("triggered_at") or current_data.get("triggered_at") or patch["requested_at"])
    if is_terminal_status(status):
        patch["result_at"] = current_times["us"]
    error = str(payload.get("error") or "")
    if error:
        patch["last_error"] = error
    elif status == "success":
        patch.update(
            {
                "last_error": "",
                "recovery_phase": "recovered",
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
                "runtime_authenticated": True,
                "runtime_started": True,
                "next_retry_at": "",
                "manual_takeover_active": False,
                "manual_takeover_started_at": "",
                "manual_takeover_until": "",
                "probe_result": "authenticated",
                "auto_restart_scheduled": False,
                "lock_owner": "",
                "lock_expires_at": "",
            }
        )
    saved = save_two_factor_state(pb, environment, patch, normalize_environment=normalize_environment, as_dict=as_dict)
    try:
        sync_startup_auth_progress(
            pb,
            environment,
            status,
            as_dict(saved.get("data")),
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=deliver_startup_progress_card,
        )
    except Exception:
        pass
    delivered = deliver_two_factor_card(
        saved,
        pb=pb,
        normalize_environment=normalize_environment,
        console_base_url=console_base_url,
        config_value=config_value,
        send_interactive=send_interactive,
        update_interactive=update_interactive,
        bypass_throttle=is_terminal_status(status),
    )
    should_emit_system_event = _should_emit_terminal_system_event(
        status,
        current_data,
        as_dict(saved.get("data")),
        current_ms,
    )
    if callable(emit_system_event) and is_terminal_status(status) and should_emit_system_event:
        try:
            emit_system_event(
                event_type="status_change" if status == "success" else "alert",
                level="info" if status == "success" else ("warning" if status == "timeout" else "error"),
                source="ibkr_compute",
                title="IBKR 2FA 完成" if status == "success" else ("IBKR 2FA 超时" if status == "timeout" else "IBKR 2FA 失败"),
                detail={
                    "status": status,
                    **as_dict((saved.get("data") or {}).get("detail")),
                    "result": str((saved.get("data") or {}).get("last_result") or ""),
                    "error": str((saved.get("data") or {}).get("last_error") or ""),
                },
                environment=environment,
            )
        except Exception:
            pass
    return {
        "ok": bool(delivered.get("ok")),
        "environment": environment,
        "status": status,
        "message_id": str(delivered.get("message_id") or ""),
        "state": as_dict(delivered.get("data") or saved.get("data")),
        "error": "" if bool(delivered.get("ok")) else str(((delivered.get("result") or {}).get("error") or "send_failed")),
        "source": "ibkr-api",
    }, 200 if bool(delivered.get("ok")) else 500


__all__ = ["build_two_factor_result_response"]
