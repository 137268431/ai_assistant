from __future__ import annotations

import inspect


def get_service_status_snapshot(
    service,
    default: dict | None = None,
    *,
    refresh_auth: bool = False,
) -> dict:
    fallback = dict(default) if isinstance(default, dict) else {}
    if not service:
        return fallback

    status_fn = getattr(service, "status", None)
    if not callable(status_fn):
        return fallback

    try:
        signature = inspect.signature(status_fn)
    except (TypeError, ValueError):
        signature = None

    try:
        if signature and "refresh_auth" in signature.parameters:
            payload = status_fn(refresh_auth=refresh_auth)
        else:
            payload = status_fn()
    except Exception:
        return fallback

    return dict(payload) if isinstance(payload, dict) else fallback


__all__ = ["get_service_status_snapshot"]
