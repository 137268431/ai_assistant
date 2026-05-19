from __future__ import annotations

from typing import Any

from ibkr_api.two_factor.delivery import deliver_two_factor_card
from ibkr_api.two_factor.state import (
    is_current_cycle_active_status,
    load_two_factor_state,
    save_two_factor_state,
    time_strings,
)

from .request_shared import (
    AsDict,
    ConfigValue,
    DeliverStartupProgressCard,
    EmitSystemEvent,
    MergeStartupSteps,
    NormalizeEnvironment,
    RequestJsonRequest,
    SendInteractive,
    UpdateInteractive,
    request_payload,
    sync_startup,
)


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
    sync_startup(
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
        request_json_request(
            "POST",
            runtime_base_url,
            "/ibkr/stop",
            json_body={"broker_mode": environment, "environment": environment},
            timeout=30,
        )
    except Exception:
        pass
    start_result = request_json_request(
        "POST",
        runtime_base_url,
        "/ibkr/start",
        json_body={
            "broker_mode": environment,
            "environment": environment,
            "trigger_login": True,
            "source": source or "ibkr-api",
            "reason": reason or str(current_data.get("reason") or "manual_reauth"),
        },
        timeout=30,
    )
    start_payload = request_payload(start_result, as_dict=as_dict)
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
        sync_startup(
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


__all__ = ["trigger_two_factor_flow"]
