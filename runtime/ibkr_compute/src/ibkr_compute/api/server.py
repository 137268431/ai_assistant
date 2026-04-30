"""Compatibility HTTP API entrypoint.

Primary implementation now lives in ibkr_compute.api.app.
"""

from .app import *  # noqa: F401,F403
from .app import _run_internal_compute, _run_internal_scan  # noqa: F401

from ibkr_compute.api.monitor.views import (  # noqa: F401
    _build_cpu_usage_snapshot,
    _build_monitor_flags,
    _collect_host_snapshot,
    _parse_meminfo_text,
)
from ibkr_compute.api.monitor.runtime import snapshot as _monitor_snapshot_mod
from ibkr_compute.api.runtime.common import get_ibkr_runtime_control, get_ibkr_service  # noqa: F401
from ibkr_compute.api.runtime.restore import _maybe_restore_ibkr_service  # noqa: F401
from ibkr_compute.api.service_topology import build_service_topology, get_runtime_mode, get_service_profile


def _build_ibkr_monitor_snapshot(service, requested_environment: str | None = None, service_error: str | None = None) -> dict:
    """Compatibility wrapper that keeps legacy server-level test patching working."""
    from . import app as app_mod

    original_collect = _monitor_snapshot_mod._collect_host_snapshot
    original_runtime_control = getattr(app_mod, "get_ibkr_runtime_control", None)
    _monitor_snapshot_mod._collect_host_snapshot = _collect_host_snapshot
    app_mod.get_ibkr_runtime_control = get_ibkr_runtime_control
    try:
        return _monitor_snapshot_mod._build_ibkr_monitor_snapshot(
            service,
            requested_environment=requested_environment,
            service_error=service_error,
        )
    finally:
        _monitor_snapshot_mod._collect_host_snapshot = original_collect
        if original_runtime_control is not None:
            app_mod.get_ibkr_runtime_control = original_runtime_control


def _build_ibkr_monitor_response(requested_environment: str = "live") -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        payload = _build_ibkr_monitor_snapshot(
            None,
            requested_environment=requested_environment,
            service_error="IBKR service not initialized",
        )
        payload["service_profile"] = get_service_profile()
        payload["runtime_mode"] = get_runtime_mode()
        payload["service_topology"] = build_service_topology()
        return payload, 200
    _maybe_restore_ibkr_service(service, refresh_auth=False, block=False)
    payload = _build_ibkr_monitor_snapshot(service, requested_environment=requested_environment)
    payload["service_profile"] = get_service_profile()
    payload["runtime_mode"] = get_runtime_mode()
    payload["service_topology"] = build_service_topology(service=service, service_status=payload.get("runtime") or {})
    return payload, 200


def ibkr_monitor():
    payload, _status_code = _build_ibkr_monitor_response("live")
    return payload


if __name__ == "__main__":
    import os

    from .startup_preload import schedule_compute_startup_preload

    port = int(os.environ.get("PORT", "5100"))
    schedule_compute_startup_preload()
    print(f"[IBKR Compute] Starting on port {port}, PB={PB_BASE_URL}")
    app.run(host="0.0.0.0", port=port, debug=False)
