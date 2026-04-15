from __future__ import annotations

from ibkr_compute.api.monitor.host import _api_app


def _build_gateway_action_payload(
    service,
    action: str,
    *,
    ok: bool,
    message: str,
    reason: str = "",
    source: str = "",
    extra: dict | None = None,
) -> dict:
    api_app = _api_app()
    payload = {
        "ok": bool(ok),
        "action": str(action or "").strip() or "gateway",
        "message": str(message or "").strip(),
        "environment": api_app._ibkr_service_environment(service),
        "reason": str(reason or "").strip(),
        "source": str(source or "").strip(),
        "gateway": service.gateway_manager.status(),
        "runtime_running": bool(getattr(service, "is_running", False)),
        "runtime_starting": bool(getattr(service, "is_starting", False)),
        "startup": service.startup_progress_snapshot() if hasattr(service, "startup_progress_snapshot") else {},
        "startup_strategy": service.startup_strategy() if hasattr(service, "startup_strategy") else {},
        "auto_restore_guard": service.auto_restore_guard() if hasattr(service, "auto_restore_guard") else {},
    }
    if extra:
        payload.update(extra)
    return payload


__all__ = ["_build_gateway_action_payload"]
