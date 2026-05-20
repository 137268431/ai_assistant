from __future__ import annotations

import logging
import os
import time
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


logger = logging.getLogger(__name__)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)) or default)
    except Exception:
        value = float(default)
    return max(float(minimum), value)


def _service_host_resources_snapshot(service) -> dict:
    method = getattr(service, "_host_resources_snapshot", None)
    if not callable(method):
        return {}
    try:
        payload = method()
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 1)


def _log_monitor_build_if_slow(*, environment: str, total_ms: float, stages_ms: dict[str, float]) -> None:
    total_threshold_s = _env_float("IBKR_COMPUTE_MONITOR_BUILD_SLOW_LOG_SEC", 5.0)
    stage_threshold_s = _env_float("IBKR_COMPUTE_MONITOR_STAGE_SLOW_LOG_SEC", 2.0)
    slow_stages = {
        name: elapsed
        for name, elapsed in stages_ms.items()
        if name != "total" and stage_threshold_s > 0 and elapsed >= stage_threshold_s * 1000.0
    }
    if not slow_stages and not (total_threshold_s > 0 and total_ms >= total_threshold_s * 1000.0):
        return
    logger.warning(
        "IBKR monitor snapshot build slow: environment=%s total_ms=%.1f slow_stages=%s",
        environment,
        total_ms,
        slow_stages,
    )


def _build_ibkr_monitor_snapshot(service, requested_environment: str | None = None, service_error: str | None = None) -> dict:
    build_started = time.monotonic()
    stages_ms: dict[str, float] = {}
    api_app = _api_app()
    runtime_environment = api_app._normalize_runtime_environment_name(
        requested_environment or api_app._ibkr_service_environment(service),
        "live",
    )
    service_available = service is not None
    config_source = getattr(service, "config", None) or api_app.cfg
    stage_started = time.monotonic()
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
    stages_ms["status"] = _elapsed_ms(stage_started)
    stage_started = time.monotonic()
    compute_summary = _build_compute_summary()
    stages_ms["compute_summary"] = _elapsed_ms(stage_started)
    stage_started = time.monotonic()
    sample_payload = _build_monitor_samples(service, runtime_status) if service_available else _build_empty_monitor_samples()
    stages_ms["samples"] = _elapsed_ms(stage_started)
    stage_started = time.monotonic()
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
    stages_ms["api_utilization"] = _elapsed_ms(stage_started)
    stage_started = time.monotonic()
    host_snapshot = _service_host_resources_snapshot(service) if service_available else {}
    if not host_snapshot:
        host_snapshot = _collect_host_snapshot()
    stages_ms["host"] = _elapsed_ms(stage_started)
    stage_started = time.monotonic()
    flags = _build_monitor_flags(runtime_status, api_utilization, host_snapshot, sample_payload)
    stages_ms["flags"] = _elapsed_ms(stage_started)
    stage_started = time.monotonic()
    service_topology = build_service_topology(service=service, service_status=runtime_status)
    stages_ms["topology"] = _elapsed_ms(stage_started)
    total_ms = _elapsed_ms(build_started)
    stages_ms["total"] = total_ms
    _log_monitor_build_if_slow(environment=runtime_environment, total_ms=total_ms, stages_ms=stages_ms)
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
        "service_topology": service_topology,
        "diagnostics": {
            "monitor_build": {
                "total_ms": total_ms,
                "stages_ms": stages_ms,
                "slow_total_threshold_s": _env_float("IBKR_COMPUTE_MONITOR_BUILD_SLOW_LOG_SEC", 5.0),
                "slow_stage_threshold_s": _env_float("IBKR_COMPUTE_MONITOR_STAGE_SLOW_LOG_SEC", 2.0),
            }
        },
    }
    resource_governor = runtime_status.get("resource_governor")
    if isinstance(resource_governor, dict):
        payload["resource_governor"] = resource_governor
    if not service_available and service_error:
        payload["error"] = str(service_error)
    return payload


__all__ = ["_build_ibkr_monitor_snapshot"]
