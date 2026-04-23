from __future__ import annotations

from typing import Any, Callable


NormalizeEnvironment = Callable[[Any, str], str]
EmitSystemEvent = Callable[..., dict[str, Any]]


def resolve_notify_level(notify_type: Any) -> str:
    normalized = str(notify_type or "status").strip().lower()
    if normalized in {"alert", "error"}:
        return "error"
    if normalized == "warning":
        return "warning"
    return "info"


def build_notify_response(
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    emit_system_event: EmitSystemEvent,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    detail = payload.get("data") if payload.get("data") is not None else payload.get("detail")
    try:
        result = emit_system_event(
            event_type="status_change",
            level=resolve_notify_level(payload.get("type")),
            source="ibkr_compute",
            title=str(payload.get("title") or ""),
            detail=detail if detail is not None else {},
            environment=environment,
            message_id=str(payload.get("message_id") or ""),
        )
    except Exception as exc:
        return {
            "ok": False,
            "environment": environment,
            "error": str(exc),
            "source": "ibkr-api",
        }, 500

    return {
        "ok": True,
        "environment": environment,
        "notified": bool(result.get("notified")),
        "persisted": bool(result.get("persisted")),
        "message_id": str(result.get("message_id") or ""),
        "updated": bool(result.get("updated")),
        "skipped": bool(result.get("skipped")),
        "suppressed": bool(result.get("suppressed")),
        "error": str(result.get("error") or ""),
        "source": "ibkr-api",
    }, 200
