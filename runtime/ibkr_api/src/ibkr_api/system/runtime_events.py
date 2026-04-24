from __future__ import annotations

from typing import Any, Callable


def emit_system_event(
    *,
    deliver_system_event_notification: Callable[..., dict[str, Any]],
    write_system_event_record: Callable[..., Any],
    event_type: str,
    level: str,
    source: str,
    title: str,
    detail: Any,
    environment: str,
    message_id: str = "",
) -> dict[str, Any]:
    delivery = deliver_system_event_notification(
        event_type,
        level,
        source,
        title,
        detail,
        environment,
        message_id=message_id,
    )
    notified = bool(delivery.get("success")) and not bool(delivery.get("suppressed"))
    persisted = bool(
        write_system_event_record(
            event_type,
            level,
            source,
            title,
            detail,
            environment,
            notified,
        )
    )
    return {
        "ok": True,
        "notified": notified,
        "persisted": persisted,
        "message_id": str(delivery.get("message_id") or message_id),
        "updated": bool(delivery.get("updated")),
        "skipped": bool(delivery.get("skipped")),
        "suppressed": bool(delivery.get("suppressed")),
        "error": str(delivery.get("error") or ""),
    }


def build_emit_system_event(
    *,
    deliver_system_event_notification: Callable[..., dict[str, Any]],
    write_system_event_record: Callable[..., Any],
):
    def _emit_system_event(
        *,
        event_type: str,
        level: str,
        source: str,
        title: str,
        detail: Any,
        environment: str,
        message_id: str = "",
    ) -> dict[str, Any]:
        return emit_system_event(
            deliver_system_event_notification=deliver_system_event_notification,
            write_system_event_record=write_system_event_record,
            event_type=event_type,
            level=level,
            source=source,
            title=title,
            detail=detail,
            environment=environment,
            message_id=message_id,
        )

    return _emit_system_event


def build_request_two_factor_approval(*, pb: Any):
    def _request_two_factor_approval(
        *,
        environment: str,
        reason: str,
        source: str,
        message: str,
        detail: dict[str, Any] | None = None,
        force_reset: bool = False,
        force_new: bool = False,
    ) -> dict[str, Any]:
        return pb.request_ibkr_2fa(
            reason=reason,
            detail=detail or {},
            source=source,
            environment=environment,
            message=message,
            force_reset=force_reset,
            force_new=force_new,
        )

    return _request_two_factor_approval


__all__ = [
    "emit_system_event",
    "build_emit_system_event",
    "build_request_two_factor_approval",
]
