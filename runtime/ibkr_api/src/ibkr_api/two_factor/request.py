from __future__ import annotations

import time
from typing import Any, Callable

from ibkr_api.two_factor.delivery import build_delivery_fingerprint, deliver_two_factor_card
from ibkr_api.two_factor.messages import build_request_response_message
from ibkr_api.two_factor.startup_sync import sync_startup_auth_progress
from ibkr_api.two_factor.state import (
    apply_runtime_state,
    ensure_requested_state,
    is_active_status,
    is_current_cycle_active_status,
    load_two_factor_state,
    save_two_factor_state,
    time_strings,
)


REQUEST_RENOTIFY_COOLDOWN_MS = 900000
NormalizeEnvironment = Callable[[Any, str], str]
AsDict = Callable[[Any], dict[str, Any]]
RequestJsonRequest = Callable[..., dict[str, Any]]
FetchRuntimeStatus = Callable[[str], dict[str, Any]]
InspectRuntimeEnvironment = Callable[[str], dict[str, Any]]
BuildMismatchPayload = Callable[[dict[str, Any], str], dict[str, Any]]
EmitSystemEvent = Callable[..., dict[str, Any]]
SendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
UpdateInteractive = Callable[[str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
MergeStartupSteps = Callable[[Any, Any, bool], dict[str, dict[str, Any]]]
DeliverStartupProgressCard = Callable[[dict[str, Any], str], dict[str, Any]]


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _request_payload(result: dict[str, Any], *, as_dict: AsDict) -> dict[str, Any]:
    payload = as_dict(result.get("payload"))
    if "ok" not in payload:
        payload["ok"] = bool(result.get("ok"))
    if result.get("target_url") and not payload.get("upstream"):
        payload["upstream"] = str(result.get("target_url") or "")
    if result.get("error") and not payload.get("error"):
        payload["error"] = str(result.get("error") or "")
    payload["status_code"] = int(result.get("status_code") or 0)
    return payload


def _with_runtime_context(
    state_data: dict[str, Any],
    *,
    runtime_payload: dict[str, Any],
    runtime_status_error: str,
    environment: str,
    as_dict: AsDict,
) -> dict[str, Any]:
    state = apply_runtime_state(state_data, runtime_payload, as_dict=as_dict)
    actual_runtime_environment = str((runtime_payload or {}).get("environment") or environment).strip().lower() or environment
    state["requested_environment"] = environment
    state["actual_runtime_environment"] = actual_runtime_environment
    state["runtime_environment_mismatch"] = actual_runtime_environment != environment
    if runtime_status_error:
        state["runtime_status_error"] = runtime_status_error
    return state


def _runtime_authenticated(state: dict[str, Any]) -> bool:
    return bool(state.get("runtime_authenticated")) and bool(state.get("gateway_reachable")) and int(state.get("gateway_status_code") or 0) != 401


def _sync_startup(
    pb: Any,
    environment: str,
    status: str,
    state_data: dict[str, Any],
    *,
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    merge_startup_steps: MergeStartupSteps,
    deliver_startup_progress_card: DeliverStartupProgressCard,
) -> None:
    try:
        sync_startup_auth_progress(
            pb,
            environment,
            status,
            state_data,
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=deliver_startup_progress_card,
        )
    except Exception:
        pass


def request_two_factor_approval(
    pb: Any,
    *,
    environment: str,
    reason: str,
    source: str,
    message: str,
    detail: dict[str, Any],
    force_reset: bool,
    force_new: bool,
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    console_base_url: str,
    config_value: ConfigValue,
    send_interactive: SendInteractive,
    update_interactive: UpdateInteractive,
    emit_system_event: EmitSystemEvent | None,
    merge_startup_steps: MergeStartupSteps,
    deliver_startup_progress_card: DeliverStartupProgressCard,
) -> dict[str, Any]:
    current = load_two_factor_state(pb, environment, normalize_environment=normalize_environment, as_dict=as_dict)
    current_data = as_dict(current.get("data"))
    current_status = str(current_data.get("status") or "requested")
    current_message_id = str(current_data.get("message_id") or "")
    last_request_push_ms = int(current_data.get("last_request_push_ms") or 0)
    already_active = is_current_cycle_active_status(current_status)
    active_card_exists = is_active_status(current_status) and bool(current_message_id)

    saved = ensure_requested_state(
        pb,
        environment,
        reason=reason,
        detail=detail,
        source=source,
        message=message,
        force_reset=force_reset,
        normalize_environment=normalize_environment,
        as_dict=as_dict,
    )
    saved_data = as_dict(saved.get("data"))
    should_refresh_existing_card = force_reset or (
        str(current_status).strip().lower() == "requested"
        and active_card_exists
        and build_delivery_fingerprint(current_data) != build_delivery_fingerprint(saved_data)
    )
    renotify_remaining_ms = 0
    if active_card_exists and last_request_push_ms > 0:
        renotify_remaining_ms = max(0, REQUEST_RENOTIFY_COOLDOWN_MS - (int(time.time() * 1000) - last_request_push_ms))
    should_renotify = active_card_exists and not should_refresh_existing_card and renotify_remaining_ms == 0

    if active_card_exists and not force_new and not should_renotify and not should_refresh_existing_card:
        delivered = {
            "ok": True,
            "skipped": True,
            "skipped_reason": "active_card_reused",
            "environment": saved["environment"],
            "date": saved["date"],
            "message_id": str(saved_data.get("message_id") or current_message_id),
            "data": saved_data,
            "result": {"success": True, "skipped": True, "reason": "active_card_reused"},
        }
    else:
        delivered = deliver_two_factor_card(
            saved,
            pb=pb,
            normalize_environment=normalize_environment,
            console_base_url=console_base_url,
            config_value=config_value,
            send_interactive=send_interactive,
            update_interactive=update_interactive,
            force_new=force_new,
            bypass_throttle=should_refresh_existing_card,
        )

    if bool(delivered.get("ok")) and not bool(delivered.get("skipped")):
        saved = save_two_factor_state(
            pb,
            environment,
            {
                "last_request_push_ms": int(time.time() * 1000),
                "last_request_push_at": time_strings()["us"],
                "message_id": delivered.get("message_id") or saved_data.get("message_id") or "",
            },
            normalize_environment=normalize_environment,
            as_dict=as_dict,
        )
        delivered["data"] = as_dict(saved.get("data"))

    if callable(emit_system_event) and not (already_active and delivered.get("skipped_reason") == "active_card_reused"):
        try:
            emit_system_event(
                event_type="status_change",
                level="info",
                source="ibkr-api",
                title="IBKR 2FA 请求已发送",
                detail={"reason": reason or "manual_reauth", "status": str((delivered.get('data') or {}).get('status') or 'requested')},
                environment=environment,
            )
        except Exception:
            pass

    _sync_startup(
        pb,
        environment,
        str((delivered.get("data") or {}).get("status") or "requested"),
        as_dict(delivered.get("data")),
        normalize_environment=normalize_environment,
        as_dict=as_dict,
        merge_startup_steps=merge_startup_steps,
        deliver_startup_progress_card=deliver_startup_progress_card,
    )

    return {
        "ok": bool(delivered.get("ok")),
        "environment": saved["environment"],
        "date": saved["date"],
        "status": str((delivered.get("data") or {}).get("status") or "requested"),
        "message_id": str(delivered.get("message_id") or ""),
        "state": as_dict(delivered.get("data") or saved.get("data")),
        "skipped": bool(delivered.get("skipped")),
        "skipped_reason": str(delivered.get("skipped_reason") or ""),
        "renotify_remaining_ms": int(renotify_remaining_ms or 0),
        "error": str(((delivered.get("result") or {}).get("error") or "") if bool(delivered.get("ok")) is False else ""),
        "already_active": already_active,
    }


def trigger_two_factor_flow(
    pb: Any,
    *,
    environment: str,
    reason: str,
    source: str,
    detail: dict[str, Any],
    force_restart: bool,
    force_new_card: bool,
    normalize_environment: NormalizeEnvironment,
    as_dict: AsDict,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    console_base_url: str,
    config_value: ConfigValue,
    send_interactive: SendInteractive,
    update_interactive: UpdateInteractive,
    emit_system_event: EmitSystemEvent | None,
    merge_startup_steps: MergeStartupSteps,
    deliver_startup_progress_card: DeliverStartupProgressCard,
) -> dict[str, Any]:
    current = load_two_factor_state(pb, environment, normalize_environment=normalize_environment, as_dict=as_dict)
    current_data = as_dict(current.get("data"))
    callback_driven = str(source or "").strip().lower() == "feishu_callback"
    restarted_from_active_cycle = bool(force_restart) and is_current_cycle_active_status(current_data.get("status"))
    trigger_time = time_strings()["us"]
    if not force_restart and is_current_cycle_active_status(current_data.get("status")):
        return {
            "ok": True,
            "environment": current["environment"],
            "already_active": True,
            "state": current_data,
        }

    previous_cycle = None
    if restarted_from_active_cycle:
        previous_cycle = {
            "status": str(current_data.get("status") or ""),
            "mode": str(current_data.get("mode") or ""),
            "requested_at": str(current_data.get("requested_at") or ""),
            "triggered_at": str(current_data.get("triggered_at") or ""),
            "challenge_code": str(current_data.get("challenge_code") or ""),
            "response_status": str(current_data.get("response_status") or ""),
            "source": str(current_data.get("source") or ""),
            "superseded_at": trigger_time,
            "superseded_reason": reason or str(current_data.get("reason") or "manual_reauth"),
            "superseded_source": source or str(current_data.get("source") or "ibkr-api"),
        }

    saved = save_two_factor_state(
        pb,
        environment,
        {
            "status": "triggered",
            "reason": reason or str(current_data.get("reason") or "manual_reauth"),
            "detail": detail or as_dict(current_data.get("detail")),
            "source": source or str(current_data.get("source") or "ibkr-api"),
            "message": "已放弃上一轮并开启新的一轮 2FA。请只跟当前这一轮。" if restarted_from_active_cycle else "",
            "requested_at": trigger_time,
            "last_request_at": trigger_time,
            "triggered_at": trigger_time,
            "result_at": "",
            "last_error": "",
            "last_result": "已放弃上一轮并开启新的一轮 2FA。请只跟当前这一轮。" if restarted_from_active_cycle else "已触发登录流程，等待网关提交 2FA。",
            "mode": "",
            "challenge_code": "",
            "challenge_detected_at": "",
            "response_code": "",
            "response_status": "",
            "response_received_at": "",
            "response_submitted_at": "",
            "response_rejected_at": "",
            "challenge_feedback": "",
            "next_retry_at": "",
            "recovery_phase": "triggered",
            "recovery_reason": reason or str(current_data.get("reason") or "manual_reauth"),
            "interruption_kind": "",
            "manual_takeover_active": False,
            "manual_takeover_started_at": "",
            "manual_takeover_until": "",
            "probe_started_at": "",
            "probe_last_checked_at": "",
            "probe_attempts": 0,
            "probe_result": "",
            "auto_restart_scheduled": False,
            "last_recovery_source": source or str(current_data.get("source") or "ibkr-api"),
            "lock_owner": "",
            "lock_expires_at": "",
            "restarted_from_active_cycle": restarted_from_active_cycle,
            "previous_cycle": previous_cycle,
        },
        normalize_environment=normalize_environment,
        as_dict=as_dict,
    )
    _sync_startup(
        pb,
        environment,
        "triggered",
        as_dict(saved.get("data")),
        normalize_environment=normalize_environment,
        as_dict=as_dict,
        merge_startup_steps=merge_startup_steps,
        deliver_startup_progress_card=deliver_startup_progress_card,
    )

    try:
        request_json_request("POST", runtime_base_url, "/ibkr/stop", json_body={"environment": environment}, timeout=30)
    except Exception:
        pass
    start_result = request_json_request(
        "POST",
        runtime_base_url,
        "/ibkr/start",
        json_body={
            "environment": environment,
            "trigger_login": True,
            "source": source or "ibkr-api",
            "reason": reason or str(current_data.get("reason") or "manual_reauth"),
        },
        timeout=30,
    )
    start_payload = _request_payload(start_result, as_dict=as_dict)
    if int(start_payload.get("status_code") or 0) >= 400 or start_payload.get("ok") is False:
        failed = save_two_factor_state(
            pb,
            environment,
            {
                "status": "failed",
                "result_at": time_strings()["us"],
                "last_error": str(start_payload.get("error") or start_payload.get("message") or f"http_{start_payload.get('status_code') or 500}"),
                "last_result": "触发失败，请稍后重试。",
            },
            normalize_environment=normalize_environment,
            as_dict=as_dict,
        )
        _sync_startup(
            pb,
            environment,
            "failed",
            as_dict(failed.get("data")),
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=deliver_startup_progress_card,
        )
        if not callback_driven:
            deliver_two_factor_card(
                failed,
                pb=pb,
                normalize_environment=normalize_environment,
                console_base_url=console_base_url,
                config_value=config_value,
                send_interactive=send_interactive,
                update_interactive=update_interactive,
                bypass_throttle=True,
            )
        if callable(emit_system_event):
            try:
                emit_system_event(
                    event_type="alert",
                    level="error",
                    source="ibkr-api",
                    title="IBKR 2FA 触发失败",
                    detail={"reason": reason or "manual_reauth", "error": str((failed.get('data') or {}).get('last_error') or "")},
                    environment=environment,
                )
            except Exception:
                pass
        return {
            "ok": False,
            "environment": environment,
            "error": str((failed.get("data") or {}).get("last_error") or ""),
            "state": as_dict(failed.get("data")),
            "payload": start_payload,
        }

    if callback_driven:
        return {
            "ok": True,
            "environment": environment,
            "status_code": int(start_payload.get("status_code") or 200),
            "upstream": str(start_payload.get("upstream") or ""),
            "state": as_dict(saved.get("data")),
            "skipped": True,
            "skipped_reason": "callback_card_response",
            "payload": start_payload,
        }

    delivered = deliver_two_factor_card(
        saved,
        pb=pb,
        normalize_environment=normalize_environment,
        console_base_url=console_base_url,
        config_value=config_value,
        send_interactive=send_interactive,
        update_interactive=update_interactive,
        force_new=force_new_card,
        bypass_throttle=True,
    )
    return {
        "ok": bool(delivered.get("ok")),
        "environment": environment,
        "status_code": int(start_payload.get("status_code") or 200),
        "upstream": str(start_payload.get("upstream") or ""),
        "state": as_dict(delivered.get("data") or saved.get("data")),
        "card": delivered.get("card") or {},
        "payload": start_payload,
    }


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
    force_reset = _parse_bool(payload.get("force_reset"))
    force_new = _parse_bool(payload.get("force_new"))
    trigger_now = _parse_bool(payload.get("trigger_now"))
    force_restart = _parse_bool(payload.get("force_restart")) or trigger_now

    environment_info = inspect_runtime_environment(environment)
    if bool(environment_info.get("runtime_environment_mismatch")):
        return build_runtime_environment_mismatch_payload(environment_info, "/api/custom/ibkr/2fa/request"), 409

    runtime_result = fetch_runtime_status(environment)
    runtime_payload = as_dict(runtime_result.get("payload"))
    runtime_status_error = str(runtime_result.get("error") or "")
    current = load_two_factor_state(pb, environment, normalize_environment=normalize_environment, as_dict=as_dict)
    current_state = _with_runtime_context(
        current.get("data") or {},
        runtime_payload=runtime_payload,
        runtime_status_error=runtime_status_error,
        environment=environment,
        as_dict=as_dict,
    )
    if _runtime_authenticated(current_state):
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
    state = _with_runtime_context(
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
