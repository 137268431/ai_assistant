from __future__ import annotations

import time
from typing import Any

from ibkr_api.two_factor.delivery import build_delivery_fingerprint, deliver_two_factor_card
from ibkr_api.two_factor.state import (
    ensure_requested_state,
    is_active_status,
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
    SendInteractive,
    UpdateInteractive,
    sync_startup,
)


REQUEST_RENOTIFY_COOLDOWN_MS = 900000


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

    sync_startup(
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


__all__ = ["REQUEST_RENOTIFY_COOLDOWN_MS", "request_two_factor_approval"]
