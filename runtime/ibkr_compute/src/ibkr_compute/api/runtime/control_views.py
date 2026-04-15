from __future__ import annotations

from ibkr_compute.api.monitor.views import _build_ibkr_monitor_snapshot
from ibkr_compute.api.runtime.common import (
    _api_app,
    _background_start_ibkr_service,
    _ibkr_service_environment,
    get_ibkr_runtime_control,
    get_ibkr_service,
    set_ibkr_runtime_control,
)
from ibkr_compute.api.runtime.restore import _maybe_restore_ibkr_service


def _build_ibkr_start_response(payload: dict | None = None) -> tuple[dict, int]:
    api_app = _api_app()
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503
    try:
        payload = payload if isinstance(payload, dict) else {}
        trigger_login = payload.get("trigger_login", False)
        reason = str(payload.get("reason") or "manual_start")
        source = str(payload.get("source") or "api_start")
        runtime_environment = _ibkr_service_environment(service)

        api_app._ibkr_restore_attempted = False
        set_ibkr_runtime_control(
            runtime_environment,
            True,
            source=source,
            reason=reason,
            extra={
                "last_restore_trigger_login": False,
            },
        )

        if getattr(service, "is_busy", False):
            return {
                "ok": True,
                "message": (
                    "IBKR service already starting"
                    if getattr(service, "is_starting", False)
                    else "IBKR service already running"
                ),
                "trigger_login": bool(trigger_login),
                "reason": reason,
                "source": source,
                "starting": bool(getattr(service, "is_starting", False)),
                "running": bool(getattr(service, "is_running", False)),
            }, 200

        _background_start_ibkr_service(
            service,
            trigger_login=bool(trigger_login),
            reason=reason,
            source=source,
        )
        return {
            "ok": True,
            "message": "IBKR service starting",
            "trigger_login": bool(trigger_login),
            "reason": reason,
            "source": source,
        }, 200
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 500


def _build_ibkr_stop_response() -> tuple[dict, int]:
    api_app = _api_app()
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503
    service.stop()
    api_app._ibkr_restore_attempted = False
    runtime_environment = _ibkr_service_environment(service)
    set_ibkr_runtime_control(
        runtime_environment,
        False,
        source="api_stop",
        reason="manual_stop",
        extra={
            "last_restore_trigger_login": False,
        },
    )
    return {"ok": True, "message": "IBKR service stopped"}, 200


def _build_ibkr_status_response() -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 200
    _maybe_restore_ibkr_service(service)
    status_payload = service.status()
    status_payload["runtime_control"] = get_ibkr_runtime_control(_ibkr_service_environment(service))
    return {"ok": True, **status_payload}, 200


def _build_ibkr_monitor_response(requested_environment: str) -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        return (
            _build_ibkr_monitor_snapshot(
                None,
                requested_environment=requested_environment,
                service_error="IBKR service not initialized",
            ),
            200,
        )
    _maybe_restore_ibkr_service(service)
    return _build_ibkr_monitor_snapshot(service, requested_environment=requested_environment), 200
