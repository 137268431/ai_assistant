from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SERVICE_NAMES = (
    "ibkr-console",
    "ibkr-api",
    "ibkr-scheduler",
    "ibkr-compute",
    "ibkr-backtest",
    "ibkr-runtime",
    "ibkr-gateway",
    "pocketbase",
)

NOMINAL_STATUSES = {"ok", "running", "healthy", "ready", "external"}
STARTING_STATUSES = {"starting", "pending", "initializing", "scheduled"}
DEGRADED_STATUSES = {"warning", "warn", "degraded", "partial", "error"}
OFFLINE_STATUSES = {"offline", "down", "stopped", "failed", "unavailable"}


def as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_service_status(value: Any, *, fallback_running: bool = False) -> str:
    text = str(value or "").strip().lower()
    if text in NOMINAL_STATUSES:
        return "running"
    if text in STARTING_STATUSES:
        return "starting"
    if text in DEGRADED_STATUSES:
        return "degraded"
    if text in OFFLINE_STATUSES:
        return "offline"
    if text in {"peer", "expected_remote", "unknown"}:
        return "unknown"
    return "running" if fallback_running else "unknown"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return int(default)


def _phase_for_status(status: str) -> str:
    if status == "running":
        return "ready"
    if status == "starting":
        return "starting"
    if status == "degraded":
        return "degraded"
    if status == "offline":
        return "offline"
    return "unknown"


def _base_state(
    name: str,
    raw_item: dict[str, Any],
    *,
    observed_at: str,
    default_status: str = "unknown",
    status_source: str = "topology",
) -> dict[str, Any]:
    status = normalize_service_status(raw_item.get("status") or default_status)
    if status == "unknown" and default_status != "unknown":
        status = normalize_service_status(default_status)
    return {
        **raw_item,
        "service_name": raw_item.get("service_name") or name,
        "status": status,
        "ready": status == "running",
        "readiness_phase": str(raw_item.get("readiness_phase") or _phase_for_status(status)),
        "status_source": str(raw_item.get("status_source") or status_source),
        "last_observed_at": str(raw_item.get("last_observed_at") or observed_at),
        "stale": bool(raw_item.get("stale", False)),
        "detail": str(raw_item.get("detail") or "").strip(),
    }


def _preload_active(payload: dict[str, Any]) -> bool:
    preload = as_dict(payload.get("compute_startup_preload"))
    if not preload:
        preload = as_dict(as_dict(payload.get("compute")).get("compute_startup_preload"))
    status = str(preload.get("status") or "").strip().lower()
    return bool(preload.get("running")) or status in {"running", "scheduled"}


def _auth_recovery_active(runtime: dict[str, Any]) -> bool:
    auth = as_dict(runtime.get("auth_recovery"))
    phase = str(auth.get("recovery_phase") or "").strip().lower()
    recovery_class = str(auth.get("recovery_class") or "").strip().lower()
    probe_result = str(auth.get("probe_result") or "").strip().lower()
    if bool(auth.get("manual_takeover_active")) or bool(auth.get("auto_restart_scheduled")):
        return True
    if phase and phase not in {"idle", "recovered", "authenticated", "complete", "completed"}:
        return True
    if recovery_class and recovery_class not in {"", "recovered"}:
        return True
    return probe_result in {"pending", "resume_probe_timeout", "silent_probe", "manual_required"}


def _warmup_active(runtime: dict[str, Any]) -> bool:
    warmup = as_dict(runtime.get("warmup"))
    phase = str(warmup.get("phase") or "").strip().lower()
    if phase in {"pending", "running", "repairing", "preflight", "warmup"}:
        return True
    if warmup and phase not in {"", "ready", "completed", "complete", "skipped"}:
        return True
    return warmup and warmup.get("trading_gate_open") is False and not warmup.get("finished_at")


def derive_compute_state(compute: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
    payload = as_dict(compute)
    status_text = normalize_service_status(payload.get("status") or ("running" if payload else "offline"))
    ready_engines = int(payload.get("ready_engines") or 0)
    total_engines = int(payload.get("total_engines") or 0)
    client_id = _safe_int(payload.get("ib_gateway_client_id") or payload.get("broker_client_id") or payload.get("client_id"))
    if _preload_active(payload):
        status = "starting"
        phase = "preload"
        ready = False
        detail = "compute startup preload active"
    elif status_text in {"offline", "degraded"}:
        status = status_text
        phase = status_text
        ready = False
        detail = str(payload.get("error") or "").strip()
    elif total_engines > 0 and ready_engines <= 0:
        status = "degraded"
        phase = "engine_unavailable"
        ready = False
        detail = f"engines {ready_engines}/{total_engines}"
    else:
        status = "running"
        phase = "ready"
        ready = True
        detail = f"engines {ready_engines}/{total_engines}" if total_engines else "compute endpoint reachable"
    if client_id:
        detail = f"{detail} · client {client_id}" if detail else f"client {client_id}"
    state = {
        "service_name": "ibkr-compute",
        "status": status,
        "ready": ready,
        "readiness_phase": phase,
        "status_source": "compute",
        "last_observed_at": observed_at,
        "stale": bool(payload.get("stale", False)),
        "detail": detail,
    }
    if client_id:
        state["ib_gateway_client_id"] = client_id
    return state


def derive_runtime_state(runtime: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
    payload = as_dict(runtime)
    gateway = as_dict(payload.get("gateway"))
    session = as_dict(payload.get("session"))
    websocket = as_dict(payload.get("websocket"))
    runtime_phase = str(payload.get("runtime_phase") or "").strip().lower()
    broker = as_dict(payload.get("broker"))
    client_id = _safe_int(
        payload.get("ib_gateway_client_id")
        or payload.get("broker_client_id")
        or payload.get("client_id")
        or broker.get("client_id")
        or gateway.get("client_id")
    )
    gateway_ready = bool(gateway.get("running") or gateway.get("reachable"))
    session_ready = bool(session.get("authenticated"))
    websocket_ready = bool(websocket.get("connected") or websocket.get("ready"))
    startup_incomplete = bool(payload.get("starting")) or (
        "startup_complete" in payload and payload.get("startup_complete") is False
    )
    auth_active = _auth_recovery_active(payload)
    warmup_active = _warmup_active(payload)

    if not payload:
        status = "offline"
        phase = "offline"
        ready = False
        detail = "runtime payload unavailable"
    elif runtime_phase in {"stopped", "stop_requested", "stopping"}:
        status = "degraded" if (gateway_ready or session_ready or websocket_ready) else "offline"
        phase = runtime_phase
        ready = False
        detail = f"phase {runtime_phase}"
    elif startup_incomplete or auth_active or warmup_active:
        status = "starting"
        ready = False
        if auth_active or not session_ready:
            phase = "auth_pending"
        elif warmup_active:
            phase = "warmup"
        elif not gateway_ready:
            phase = "gateway_pending"
        elif not websocket_ready:
            phase = "websocket_pending"
        else:
            phase = "starting"
        detail = f"phase {runtime_phase or '--'}"
    elif not gateway_ready:
        status = "degraded"
        phase = "gateway_pending"
        ready = False
        detail = "gateway not reachable"
    elif not session_ready:
        status = "degraded"
        phase = "auth_pending"
        ready = False
        detail = "session not authenticated"
    elif not websocket_ready:
        status = "degraded"
        phase = "websocket_pending"
        ready = False
        detail = "websocket not ready"
    else:
        status = "running"
        phase = "ready"
        ready = True
        detail = f"phase {runtime_phase or 'running'}"
    if client_id:
        detail = f"{detail} · client {client_id}" if detail else f"client {client_id}"

    state = {
        "service_name": "ibkr-runtime",
        "status": status,
        "ready": ready,
        "readiness_phase": phase,
        "status_source": "runtime",
        "last_observed_at": observed_at,
        "stale": bool(payload.get("stale", False)),
        "detail": detail,
    }
    if client_id:
        state["ib_gateway_client_id"] = client_id
    return state


def derive_gateway_state(runtime: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
    payload = as_dict(runtime)
    gateway = as_dict(payload.get("gateway"))
    runtime_state = derive_runtime_state(payload, observed_at=observed_at)
    gateway_ready = bool(gateway.get("running") or gateway.get("reachable"))
    if gateway_ready:
        status = "running"
        phase = "ready"
        ready = True
        detail = "reachable" if gateway.get("reachable") else "running"
    elif runtime_state["status"] == "starting":
        status = "starting"
        phase = "gateway_pending"
        ready = False
        detail = "gateway starting"
    else:
        status = "offline"
        phase = "offline"
        ready = False
        detail = "not reachable"
    return {
        "service_name": "ibkr-gateway",
        "status": status,
        "ready": ready,
        "readiness_phase": phase,
        "status_source": "runtime",
        "last_observed_at": observed_at,
        "stale": bool(payload.get("stale", False)),
        "detail": detail,
        "managed_by": str(gateway.get("managed_by") or "ibkr-runtime"),
        "pid": int(gateway.get("pid") or 0),
    }


def derive_backtest_state(backtest_health: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
    payload = as_dict(backtest_health)
    body = as_dict(payload.get("payload"))
    is_envelope = "payload" in payload or "status_code" in payload
    nested = body if is_envelope else payload
    worker = as_dict(nested.get("backtest"))
    service_ok = bool(payload.get("ok", nested.get("ok", False)))
    http_status = int(payload.get("status_code") or 0)
    worker_status = str(worker.get("status") or nested.get("worker_status") or nested.get("status") or "").strip().lower()
    client_id = _safe_int(
        nested.get("ib_gateway_client_id")
        or nested.get("broker_client_id")
        or worker.get("ib_gateway_client_id")
        or worker.get("broker_client_id")
    )
    if service_ok:
        status = "running"
        ready = True
        phase = "ready" if worker_status in {"", "idle", "running"} else worker_status
        detail = "backtest worker " + (worker_status or "ready")
    elif nested or http_status:
        status = "degraded"
        ready = False
        phase = worker_status or "degraded"
        detail = str(nested.get("error") or payload.get("error") or f"http {http_status}").strip()
    else:
        status = "offline"
        ready = False
        phase = "offline"
        detail = str(payload.get("error") or "backtest health unavailable").strip()
    if client_id:
        detail = f"{detail} · client {client_id}" if detail else f"client {client_id}"
    if http_status:
        detail = f"{detail} · http {http_status}" if detail else f"http {http_status}"
    return {
        "service_name": "ibkr-backtest",
        "status": status,
        "ready": ready,
        "readiness_phase": phase,
        "status_source": "backtest",
        "last_observed_at": observed_at,
        "stale": bool(payload.get("stale", False) or nested.get("stale", False)),
        "detail": detail,
        "worker_status": worker_status or "",
        "active_runs": int(worker.get("active_runs") or worker.get("running_count") or 0),
        "queue_depth": int(worker.get("queue_depth") or worker.get("pending") or 0),
        "ib_gateway_client_id": client_id,
    }


def _extract_compute_payload(payloads: tuple[Any, ...]) -> dict[str, Any]:
    for payload in payloads:
        item = as_dict(payload)
        compute = as_dict(item.get("compute"))
        if compute:
            return compute
        if any(key in item for key in ("ready_engines", "total_engines", "compute_startup_preload", "compute_enabled")):
            return item
    return {}


def _extract_runtime_payload(payloads: tuple[Any, ...]) -> dict[str, Any]:
    for payload in payloads:
        item = as_dict(payload)
        runtime = as_dict(item.get("runtime"))
        if runtime:
            return runtime
        if any(key in item for key in ("session", "gateway", "websocket", "auth_recovery", "runtime_phase")):
            return item
    return {}


def _extract_backtest_payload(payloads: tuple[Any, ...]) -> dict[str, Any]:
    for payload in payloads:
        item = as_dict(payload)
        if str(item.get("service") or "").strip().lower() == "ibkr-backtest":
            return item
        backtest = as_dict(item.get("backtest_service"))
        if backtest:
            return backtest
        if str(item.get("status_source") or "").strip().lower() == "backtest":
            return item
    return {}


def build_service_monitor_from_topology(
    environment: str,
    topology: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    timestamp = observed_at or utc_timestamp()
    raw_services = as_dict(topology.get("services"))
    services: dict[str, dict[str, Any]] = {}
    for name in SERVICE_NAMES:
        raw_item = as_dict(raw_services.get(name))
        default_status = "running" if name in {"ibkr-api", "ibkr-scheduler", "ibkr-console", "pocketbase"} else "unknown"
        services[name] = _base_state(
            name,
            raw_item,
            observed_at=timestamp,
            default_status=default_status,
            status_source="assumed" if default_status == "running" else "topology",
        )
    for name, raw_item in raw_services.items():
        if name in services:
            continue
        services[str(name)] = _base_state(str(name), as_dict(raw_item), observed_at=timestamp)
    return rebuild_service_monitor(environment, services)


def rebuild_service_monitor(environment: str, services: dict[str, dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for service in services.values():
        status = normalize_service_status(service.get("status"))
        service["status"] = status
        service["ready"] = bool(service.get("ready", True)) if status == "running" else False
        service["readiness_phase"] = str(service.get("readiness_phase") or _phase_for_status(status))
        counts[status] = counts.get(status, 0) + 1
    return {
        "environment": environment,
        "services": services,
        "status_counts": counts,
    }


def apply_service_monitor_to_topology(
    topology: dict[str, Any],
    service_monitor: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(topology) if isinstance(topology, dict) else {}
    raw_services = as_dict(updated.get("services"))
    monitor_services = as_dict(as_dict(service_monitor).get("services"))
    services = {str(name): as_dict(item) for name, item in raw_services.items()}
    for name, state in monitor_services.items():
        current = services.get(str(name), {})
        services[str(name)] = {**current, **as_dict(state)}
    updated["services"] = services
    return updated


def canonicalize_topology(
    environment: str,
    topology: dict[str, Any],
    *payloads: Any,
    observed_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    timestamp = observed_at or utc_timestamp()
    monitor = build_service_monitor_from_topology(environment, topology, observed_at=timestamp)
    services = {name: dict(item) for name, item in as_dict(monitor.get("services")).items()}
    compute_payload = _extract_compute_payload(payloads)
    runtime_payload = _extract_runtime_payload(payloads)
    backtest_payload = _extract_backtest_payload(payloads)

    services["ibkr-api"] = {
        **services.get("ibkr-api", {}),
        "status": "running",
        "ready": True,
        "readiness_phase": "ready",
        "status_source": "api",
        "last_observed_at": timestamp,
        "stale": False,
        "detail": "api route responding",
    }
    if compute_payload:
        services["ibkr-compute"] = {**services.get("ibkr-compute", {}), **derive_compute_state(compute_payload, observed_at=timestamp)}
    if runtime_payload:
        services["ibkr-runtime"] = {**services.get("ibkr-runtime", {}), **derive_runtime_state(runtime_payload, observed_at=timestamp)}
        services["ibkr-gateway"] = {**services.get("ibkr-gateway", {}), **derive_gateway_state(runtime_payload, observed_at=timestamp)}
    if backtest_payload:
        services["ibkr-backtest"] = {
            **services.get("ibkr-backtest", {}),
            **derive_backtest_state(backtest_payload, observed_at=timestamp),
        }

    monitor = rebuild_service_monitor(environment, services)
    topology = apply_service_monitor_to_topology(topology, monitor)
    return topology, monitor


__all__ = [
    "SERVICE_NAMES",
    "apply_service_monitor_to_topology",
    "as_dict",
    "build_service_monitor_from_topology",
    "canonicalize_topology",
    "derive_backtest_state",
    "derive_compute_state",
    "derive_gateway_state",
    "derive_runtime_state",
    "normalize_service_status",
    "rebuild_service_monitor",
    "utc_timestamp",
]
