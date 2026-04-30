from __future__ import annotations

from datetime import datetime, timezone

from ibkr_compute.api.monitor.flags import (
    _build_monitor_flags,
    _build_ws_silence_policy,
    _derive_monitor_status,
)
from ibkr_compute.api.monitor.host import _api_app, _collect_host_snapshot
from ibkr_compute.api.monitor.runtime.compute import _build_compute_summary
from ibkr_compute.api.monitor.runtime.empty import (
    _build_empty_api_utilization_snapshot,
    _build_empty_monitor_samples,
)
from ibkr_compute.api.monitor.runtime.uninitialized import _build_uninitialized_runtime_status
from ibkr_compute.api.monitor.samples import _build_api_utilization_snapshot, _build_monitor_samples
from ibkr_compute.api.shared.service_status import get_service_status_snapshot
from ibkr_compute.api.service_topology import build_service_topology


def _service_host_resources_snapshot(service) -> dict:
    method = getattr(service, "_host_resources_snapshot", None)
    if not callable(method):
        return {}
    try:
        payload = method()
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _build_ibkr_monitor_snapshot(service, requested_environment: str | None = None, service_error: str | None = None) -> dict:
    api_app = _api_app()
    runtime_environment = api_app._normalize_runtime_environment_name(
        requested_environment or api_app._ibkr_service_environment(service),
        "live",
    )
    service_available = service is not None
    config_source = getattr(service, "config", None) or api_app.cfg
    if not service_available and hasattr(config_source, "refresh"):
        try:
            config_source.refresh()
        except Exception:
            pass
    runtime_status = (
        get_service_status_snapshot(service)
        if service_available and hasattr(service, "status")
        else _build_uninitialized_runtime_status(runtime_environment, service_error)
    )
    compute_summary = _build_compute_summary()
    sample_payload = _build_monitor_samples(service, runtime_status) if service_available else _build_empty_monitor_samples()
    api_utilization = (
        _build_api_utilization_snapshot(service, runtime_environment, runtime_status, sample_payload)
        if service_available
        else _build_empty_api_utilization_snapshot(runtime_environment)
    )
    api_utilization = {
        **api_utilization,
        **_build_ws_silence_policy(
            runtime_status,
            config_source=config_source,
            runtime_environment=runtime_environment,
        ),
    }
    host_snapshot = _service_host_resources_snapshot(service) if service_available else {}
    if not host_snapshot:
        host_snapshot = _collect_host_snapshot()
    flags = _build_monitor_flags(runtime_status, api_utilization, host_snapshot, sample_payload)
    payload = {
        "ok": service_available,
        "status": _derive_monitor_status(flags),
        "environment": runtime_environment,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "compute": compute_summary,
        "runtime": runtime_status,
        "runtime_control": api_app.get_ibkr_runtime_control(runtime_environment),
        "api_utilization": api_utilization,
        "samples": sample_payload,
        "host": host_snapshot,
        "flags": flags,
        "service_topology": build_service_topology(service=service, service_status=runtime_status),
    }
    resource_governor = runtime_status.get("resource_governor")
    if isinstance(resource_governor, dict):
        payload["resource_governor"] = resource_governor
    if not service_available and service_error:
        payload["error"] = str(service_error)
    return payload


__all__ = ["_build_ibkr_monitor_snapshot"]
