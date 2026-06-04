from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ibkr_api.orders.values import ensure_object, to_text
from ibkr_api.signals.values import get_signal_extra


FINAL_SIGNAL_STATUSES = {
    "submitted",
    "submitted_waiting_fill",
    "filled_repricing_protection",
    "filled_position",
    "protected_active",
    "protection_incomplete",
    "protection_reprice_failed",
    "entry_missed_limit_cap",
    "ignored_no_broker_position",
    "stale_signal",
    "signal_clock_skew",
    "executed",
    "rejected",
    "expired",
    "closed",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _broker_scoped(broker_mode: str, data_environment: str) -> bool:
    return bool(broker_mode and data_environment and not (broker_mode == data_environment == "live"))


def _with_broker_lifecycle(
    extra: dict[str, Any],
    *,
    broker_mode: str,
    data_environment: str,
    status: str,
    note: str,
    manual_confirm_enabled: bool,
) -> dict[str, Any]:
    merged = dict(extra if isinstance(extra, dict) else {})
    execution_by_mode = merged.get("execution_by_mode") if isinstance(merged.get("execution_by_mode"), dict) else {}
    broker_payload = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
    if not isinstance(broker_payload, dict):
        broker_payload = {}
    execution_by_mode = dict(execution_by_mode)
    execution_by_mode[broker_mode] = {
        **broker_payload,
        "status": status,
        "note": note,
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "signal_confirmation_required": bool(manual_confirm_enabled),
        "signal_confirmation_mode": "manual" if manual_confirm_enabled else "auto",
        "status_reason": note,
        "updated_at": _utc_now_iso(),
        "source": "ingest_lifecycle",
    }
    merged["execution_by_mode"] = execution_by_mode
    merged["last_runtime_broker_mode"] = broker_mode
    merged["last_runtime_data_environment"] = data_environment
    return merged


def prepare_signal_lifecycle(
    signal_payload: dict[str, Any],
    existing_row: dict[str, Any] | None,
    *,
    manual_confirm_enabled: bool,
    broker_mode: str = "",
    data_environment: str = "",
) -> dict[str, str]:
    existing = dict(existing_row or {})
    existing_status = to_text(existing.get("status")).lower()
    existing_note = to_text(existing.get("note"))
    existing_extra = get_signal_extra(existing)
    incoming_status = to_text(signal_payload.get("status") or "pending").lower() or "pending"
    incoming_note = to_text(signal_payload.get("note"))

    resolved_status = incoming_status
    resolved_note = incoming_note
    if existing_status in FINAL_SIGNAL_STATUSES:
        resolved_status = existing_status
        resolved_note = existing_note or incoming_note
    elif manual_confirm_enabled:
        if existing_status == "pending":
            resolved_status = "pending"
            resolved_note = existing_note or incoming_note
        elif incoming_status in {"pending", "awaiting_confirm"}:
            resolved_status = "awaiting_confirm"
            resolved_note = incoming_note or "manual_confirmation_required"
    elif incoming_status in {"pending", "awaiting_confirm"}:
        resolved_status = "pending"
        resolved_note = "" if incoming_note == "manual_confirmation_required" else incoming_note

    extra_payload = {
        **existing_extra,
        **ensure_object(signal_payload.get("extra")),
        "signal_confirmation_required": bool(manual_confirm_enabled),
        "signal_confirmation_mode": "manual" if manual_confirm_enabled else "auto",
    }
    if _broker_scoped(to_text(broker_mode), to_text(data_environment)):
        top_level_status = existing_status or ("pending" if incoming_status in {"pending", "awaiting_confirm"} else incoming_status)
        top_level_note = existing_note if existing_status and existing_status not in {"pending", "awaiting_confirm"} else ""
        signal_payload["status"] = top_level_status
        signal_payload["note"] = top_level_note
        signal_payload["extra"] = _with_broker_lifecycle(
            extra_payload,
            broker_mode=to_text(broker_mode),
            data_environment=to_text(data_environment),
            status=resolved_status,
            note=resolved_note,
            manual_confirm_enabled=manual_confirm_enabled,
        )
        if resolved_note:
            signal_payload["extra"]["status_reason"] = resolved_note
        return {"previous_status": existing_status, "next_status": resolved_status}

    signal_payload["status"] = resolved_status
    signal_payload["note"] = resolved_note
    signal_payload["extra"] = {
        **extra_payload,
    }
    if resolved_note:
        signal_payload["extra"]["status_reason"] = resolved_note

    return {"previous_status": existing_status, "next_status": resolved_status}


__all__ = ["FINAL_SIGNAL_STATUSES", "prepare_signal_lifecycle"]
