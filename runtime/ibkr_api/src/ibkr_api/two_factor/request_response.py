from __future__ import annotations

from typing import Any

from ibkr_api.two_factor.messages import build_request_response_message
from ibkr_api.two_factor.state import load_two_factor_state

from .request_approval import request_two_factor_approval
from .request_shared import (
    AsDict,
    BuildMismatchPayload,
    ConfigValue,
    DeliverStartupProgressCard,
    EmitSystemEvent,
    FetchRuntimeStatus,
    InspectRuntimeEnvironment,
    MergeStartupSteps,
    NormalizeEnvironment,
    RequestJsonRequest,
    SendInteractive,
    UpdateInteractive,
    parse_bool,
    runtime_authenticated,
    with_runtime_context,
)
from .request_trigger import trigger_two_factor_flow


def build_two_factor_request_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    fetch_runtime_status: FetchRuntimeStatus,
    inspect_runtime_environment: InspectRuntimeEnvironment,
    build_runtime_environment_mismatch_payload: BuildMismatchPayload,
    console_base_url: str,
    config_value: ConfigValue,
    send_interactive: SendInteractive,
    update_interactive: UpdateInteractive,
    emit_system_event: EmitSystemEvent | None,
    merge_startup_steps: MergeStartupSteps,
    deliver_startup_progress_card: DeliverStartupProgressCard,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    force_reset = parse_bool(payload.get("force_reset"))
    force_new = parse_bool(payload.get("force_new"))
    trigger_now = parse_bool(payload.get("trigger_now"))
    force_restart = parse_bool(payload.get("force_restart")) or trigger_now

    environment_info = inspect_runtime_environment(environment)
    if bool(environment_info.get("runtime_environment_mismatch")):
        return build_runtime_environment_mismatch_payload(environment_info, "/api/custom/ibkr/2fa/request"), 409

    runtime_result = fetch_runtime_status(environment)
    runtime_payload = as_dict(runtime_result.get("payload"))
    runtime_status_error = str(runtime_result.get("error") or "")
    current = load_two_factor_state(pb, environment, normalize_environment=normalize_environment, as_dict=as_dict)
    current_state = with_runtime_context(
        current.get("data") or {},
        runtime_payload=runtime_payload,
        runtime_status_error=runtime_status_error,
        environment=environment,
        as_dict=as_dict,
    )
    if runtime_authenticated(current_state):
        return {
            "ok": True,
            "environment": environment,
            "date": current.get("date") or "global",
            "status": str(current_state.get("status") or "success"),
            "message": "当前 Gateway 会话已认证，无需再次确认。",
            "message_id": str(current_state.get("message_id") or ""),
            "state": current_state,
            "skipped": True,
            "skipped_reason": "runtime_already_authenticated",
            "renotify_remaining_ms": 0,
            "error": "",
            "source": "ibkr-api",
        }, 200

    reason = str(payload.get("reason") or "").strip() or "manual_reauth"
    source = str(payload.get("source") or "").strip() or "ibkr-api"
    message_text = str(payload.get("message") or "").strip()
    detail = as_dict(payload.get("detail"))
    weekly_reminder_requested = (not trigger_now) and reason == "weekly_reauth"

    result = (
        trigger_two_factor_flow(
            pb,
            environment=environment,
            reason=reason,
            source=source,
            detail=detail,
            force_restart=force_restart,
            force_new_card=force_new,
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            console_base_url=console_base_url,
            config_value=config_value,
            send_interactive=send_interactive,
            update_interactive=update_interactive,
            emit_system_event=emit_system_event,
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=deliver_startup_progress_card,
        )
        if trigger_now
        else request_two_factor_approval(
            pb,
            environment=environment,
            reason=reason,
            source=source,
            message=message_text,
            detail=detail,
            force_reset=force_reset,
            force_new=force_new,
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            console_base_url=console_base_url,
            config_value=config_value,
            send_interactive=send_interactive,
            update_interactive=update_interactive,
            emit_system_event=emit_system_event,
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=deliver_startup_progress_card,
        )
    )
    state = with_runtime_context(
        as_dict(result.get("state")),
        runtime_payload=runtime_payload,
        runtime_status_error=runtime_status_error,
        environment=environment,
        as_dict=as_dict,
    )
    message = build_request_response_message(
        state_data=state,
        result=result,
        trigger_now=trigger_now,
        force_new=force_new,
        weekly_reminder_requested=weekly_reminder_requested,
    )
    return {
        "ok": bool(result.get("ok")),
        "environment": str(result.get("environment") or environment),
        "date": str(result.get("date") or current.get("date") or "global"),
        "status": str(state.get("status") or result.get("status") or ""),
        "message": message,
        "message_id": str(result.get("message_id") or state.get("message_id") or ""),
        "state": state,
        "skipped": bool(result.get("skipped")),
        "skipped_reason": str(result.get("skipped_reason") or ""),
        "renotify_remaining_ms": int(result.get("renotify_remaining_ms") or 0),
        "error": str(result.get("error") or ""),
        "already_active": bool(result.get("already_active")),
        "source": "ibkr-api",
    }, 200 if bool(result.get("ok")) else 500


__all__ = ["build_two_factor_request_response"]
