from __future__ import annotations

from typing import Any, Callable

from ibkr_api.two_factor.delivery import deliver_two_factor_card
from ibkr_api.two_factor.startup_sync import sync_startup_auth_progress
from ibkr_api.two_factor.state import is_terminal_status, load_two_factor_state, save_two_factor_state, time_strings


NormalizeEnvironment = Callable[[Any, str], str]
AsDict = Callable[[Any], dict[str, Any]]
BuildMismatchPayload = Callable[[dict[str, Any], str], dict[str, Any]]
InspectRuntimeEnvironment = Callable[[str], dict[str, Any]]
SendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
UpdateInteractive = Callable[[str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
MergeStartupSteps = Callable[[Any, Any, bool], dict[str, dict[str, Any]]]
DeliverStartupProgressCard = Callable[[dict[str, Any], str], dict[str, Any]]


def _sanitize_response_code(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isalnum()).upper()


def build_two_factor_respond_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    inspect_runtime_environment: InspectRuntimeEnvironment,
    build_runtime_environment_mismatch_payload: BuildMismatchPayload,
    console_base_url: str,
    config_value: ConfigValue,
    send_interactive: SendInteractive,
    update_interactive: UpdateInteractive,
    merge_startup_steps: MergeStartupSteps,
    deliver_startup_progress_card: DeliverStartupProgressCard,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    environment_info = inspect_runtime_environment(environment)
    if bool(environment_info.get("runtime_environment_mismatch")):
        return build_runtime_environment_mismatch_payload(environment_info, "/api/custom/ibkr/2fa/respond"), 409

    current = load_two_factor_state(pb, environment, normalize_environment=normalize_environment, as_dict=as_dict)
    current_data = as_dict(current.get("data"))
    response_code = _sanitize_response_code(payload.get("response_code"))
    expected_challenge = str(current_data.get("challenge_code") or "").strip()
    submitted_challenge = str(payload.get("challenge_code") or "").strip()
    if not response_code:
        return {"ok": False, "environment": environment, "error": "response_code_required", "state": current_data, "source": "ibkr-api"}, 400
    if not expected_challenge:
        return {"ok": False, "environment": environment, "error": "challenge_not_ready", "state": current_data, "source": "ibkr-api"}, 400
    if is_terminal_status(current_data.get("status")):
        return {"ok": False, "environment": environment, "error": "challenge_expired_retrigger_required", "state": current_data, "source": "ibkr-api"}, 400
    if submitted_challenge and expected_challenge and submitted_challenge != expected_challenge:
        return {"ok": False, "environment": environment, "error": "challenge_mismatch", "state": current_data, "source": "ibkr-api"}, 400

    saved = save_two_factor_state(
        pb,
        environment,
        {
            "status": "success" if str(current_data.get("status") or "").strip().lower() == "success" else "waiting_response",
            "response_code": response_code,
            "response_status": "received",
            "response_received_at": time_strings()["us"],
            "response_submitted_at": "",
            "response_rejected_at": "",
            "challenge_feedback": "",
            "source": str(payload.get("source") or current_data.get("source") or "runtime_page").strip() or "runtime_page",
            "last_result": "已收到 Response Code，等待浏览器提交流程。",
            "last_error": "",
        },
        normalize_environment=normalize_environment,
        as_dict=as_dict,
    )
    try:
        sync_startup_auth_progress(
            pb,
            environment,
            str((saved.get("data") or {}).get("status") or "waiting_response"),
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
        bypass_throttle=True,
    )
    return {
        "ok": bool(delivered.get("ok")),
        "environment": environment,
        "status": str(((delivered.get("data") or {}).get("status") or "waiting_response")),
        "message_id": str(delivered.get("message_id") or ""),
        "state": as_dict(delivered.get("data") or saved.get("data")),
        "error": "" if bool(delivered.get("ok")) else str(((delivered.get("result") or {}).get("error") or "send_failed")),
        "source": "ibkr-api",
    }, 200 if bool(delivered.get("ok")) else 400


__all__ = ["build_two_factor_respond_response"]
