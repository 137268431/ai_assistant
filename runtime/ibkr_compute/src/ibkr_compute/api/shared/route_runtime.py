from __future__ import annotations

from flask import jsonify

from ibkr_compute.api.shared.route_request import get_query_arg_text


def _app_mod():
    from .. import app as app_mod

    return app_mod


def get_app_module():
    return _app_mod()


def get_requested_environment(default: str = "live") -> str:
    app_mod = _app_mod()
    return app_mod._normalize_runtime_environment_name(get_query_arg_text("environment"), default)


def get_service_status(service) -> dict:
    if service and hasattr(service, "status"):
        return service.status() or {}
    return {}


def require_ibkr_service(*, restore: bool = False):
    app_mod = _app_mod()
    service = app_mod.get_ibkr_service()
    if not service:
        return app_mod, None, (jsonify({"ok": False, "error": "IBKR service not initialized"}), 503)
    if restore:
        app_mod._maybe_restore_ibkr_service(service)
    return app_mod, service, None


def build_runtime_environment_payload(app_mod, service, requested_environment: str | None = None) -> dict:
    runtime_environment = app_mod._ibkr_service_environment(service)
    payload = {"environment": runtime_environment}
    if requested_environment is not None:
        payload["requested_environment"] = requested_environment
        payload["runtime_environment_mismatch"] = runtime_environment != requested_environment
    return payload
