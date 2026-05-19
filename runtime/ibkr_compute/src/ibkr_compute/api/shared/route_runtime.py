from __future__ import annotations

import importlib
import sys

from flask import jsonify

try:
    from flask import current_app, has_app_context
except ImportError:  # pragma: no cover - compatibility for lightweight test stubs.
    current_app = None

    def has_app_context() -> bool:
        return False

from ibkr_compute.api.shared.route_request import get_query_arg_text
from ibkr_compute.api.shared.service_status import get_service_status_snapshot
from ibkr_compute.core.broker_mode import (
    configured_broker_mode,
    configured_market_data_mode,
    normalize_broker_mode,
    resolve_data_environment,
)

APP_MODULE_CONFIG_KEY = "IBKR_COMPUTE_APP_MODULE"
APP_MODULE_EXTENSION_KEY = "ibkr_compute_app_module"


def _app_mod():
    from .. import app as app_mod

    return app_mod


def _resolve_app_marker(marker):
    if not marker:
        return None
    if isinstance(marker, str):
        return importlib.import_module(marker)
    return marker


def _current_flask_app_module():
    if current_app is None or not has_app_context():
        return None

    flask_app = current_app._get_current_object()
    marker = flask_app.config.get(APP_MODULE_CONFIG_KEY)
    if marker is None:
        marker = flask_app.extensions.get(APP_MODULE_EXTENSION_KEY)
    app_mod = _resolve_app_marker(marker)
    if app_mod is not None:
        return app_mod

    module = sys.modules.get(str(getattr(flask_app, "import_name", "") or ""))
    if module is not None and getattr(module, "app", None) is flask_app:
        return module
    return None


def register_app_module_context(flask_app, app_mod) -> None:
    flask_app.config[APP_MODULE_CONFIG_KEY] = app_mod
    flask_app.extensions[APP_MODULE_EXTENSION_KEY] = app_mod


def get_app_module():
    return _current_flask_app_module() or _app_mod()


def get_requested_environment(default: str = "live") -> str:
    app_mod = get_app_module()
    raw_value = (
        get_query_arg_text("market_data_mode")
        or get_query_arg_text("data_environment")
        or get_query_arg_text("environment")
    )
    data_environment = resolve_data_environment(raw_value or default)

    supported = getattr(app_mod, "SUPPORTED_COMPUTE_ENVIRONMENTS", ("live", "backtest"))
    if data_environment in supported:
        return data_environment
    fallback = resolve_data_environment(default or configured_market_data_mode())
    return fallback if fallback in supported else "live"


def get_requested_broker_mode(default: str | None = None) -> str:
    return normalize_broker_mode(
        get_query_arg_text("broker_mode") or get_query_arg_text("environment") or default,
        configured_broker_mode(),
    )


def get_service_status(service) -> dict:
    return get_service_status_snapshot(service)


def require_ibkr_service(*, restore: bool = False):
    app_mod = get_app_module()
    service_getter = getattr(app_mod, "get_ibkr_service", None)
    service = service_getter() if callable(service_getter) else None
    if not service:
        return app_mod, None, (jsonify({"ok": False, "error": "IBKR service not initialized"}), 503)
    if restore:
        restorer = getattr(app_mod, "_maybe_restore_ibkr_service", None)
        if callable(restorer):
            restorer(service)
    return app_mod, service, None


def build_runtime_environment_payload(app_mod, service, requested_environment: str | None = None) -> dict:
    environment_resolver = getattr(app_mod, "_ibkr_service_environment", None)
    runtime_environment = (
        environment_resolver(service)
        if callable(environment_resolver)
        else str(requested_environment or "live").strip().lower() or "live"
    )
    data_environment = resolve_data_environment(runtime_environment)
    payload = {
        "environment": runtime_environment,
        "broker_mode": runtime_environment,
        "data_environment": data_environment,
        "market_data_environment": data_environment,
        "shared_market_data": data_environment == "live",
    }
    if requested_environment is not None:
        payload["requested_environment"] = requested_environment
        requested_data_environment = resolve_data_environment(requested_environment)
        payload["requested_data_environment"] = requested_data_environment
        payload["runtime_environment_mismatch"] = False
        payload["data_environment_mismatch"] = data_environment != requested_data_environment
    return payload
