from __future__ import annotations

from typing import Any

from ibkr_api.orders.values import ensure_object, to_text
from ibkr_api.signals.values import get_signal_extra


FINAL_SIGNAL_STATUSES = {"submitted", "protected_active", "protection_incomplete", "executed", "rejected", "expired", "closed"}


def prepare_signal_lifecycle(
    signal_payload: dict[str, Any],
    existing_row: dict[str, Any] | None,
    *,
    manual_confirm_enabled: bool,
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

    signal_payload["status"] = resolved_status
    signal_payload["note"] = resolved_note
    signal_payload["extra"] = {
        **existing_extra,
        **ensure_object(signal_payload.get("extra")),
        "signal_confirmation_required": bool(manual_confirm_enabled),
        "signal_confirmation_mode": "manual" if manual_confirm_enabled else "auto",
    }
    if resolved_note:
        signal_payload["extra"]["status_reason"] = resolved_note

    return {"previous_status": existing_status, "next_status": resolved_status}


__all__ = ["FINAL_SIGNAL_STATUSES", "prepare_signal_lifecycle"]
