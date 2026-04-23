from __future__ import annotations

from typing import Any, Callable

import requests

NormalizeEnvironment = Callable[[Any, str], str]
FetchPayload = Callable[[str], dict[str, Any]]
AsDict = Callable[[Any], dict[str, Any]]
ConfigRefresh = Callable[[], None]
SchedulerStatus = Callable[[str], dict[str, Any]]
BuildCronPayload = Callable[[Any, str, dict[str, Any]], list[dict[str, Any]]]
BuildSchedulerSummary = Callable[[str, dict[str, Any]], dict[str, Any]]
AugmentSchedulerSummary = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]
RequestJson = Callable[..., dict[str, Any]]
LoadEffectiveConfigMap = Callable[[str, tuple[str, ...] | list[str] | set[str] | None], dict[str, str]]
LoadRecentSystemEvents = Callable[[str, int], list[dict[str, Any]]]
EnrichMonitorPayload = Callable[[dict[str, Any]], dict[str, Any]]
DeriveMonitorServiceMap = Callable[..., dict[str, Any]]
MergeServiceTopology = Callable[..., dict[str, Any]]
BuildServiceTopology = Callable[[], dict[str, Any]]
ProbeConsoleStatus = Callable[[str], dict[str, Any]]
RequestsGet = Callable[..., requests.Response]



def probe_console_status(console_base_url: str, *, requests_get: RequestsGet = requests.get) -> dict[str, Any]:
    normalized_base_url = str(console_base_url or "").rstrip("/")
    if not normalized_base_url:
        return {
            "ok": False,
            "status_code": 0,
            "target_url": "",
            "error": "console_base_url_missing",
        }
    target_url = f"{normalized_base_url}/index.html"
    try:
        response = requests_get(target_url, timeout=5)
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status_code": 0,
            "target_url": target_url,
            "error": str(exc),
        }
    return {
        "ok": bool(response.ok),
        "status_code": int(response.status_code),
        "target_url": target_url,
        "error": "",
    }



def derive_monitor_service_map(
    environment: str,
    base_payload: dict[str, Any],
    scheduler_summary: dict[str, Any],
    *,
    console_probe: dict[str, Any],
    pb_health: dict[str, Any],
    build_service_topology: BuildServiceTopology,
) -> dict[str, Any]:
    topology = base_payload.get("service_topology") if isinstance(base_payload.get("service_topology"), dict) else build_service_topology()
    services = topology.get("services") if isinstance(topology.get("services"), dict) else {}
    runtime = base_payload.get("runtime") if isinstance(base_payload.get("runtime"), dict) else {}
    gateway = runtime.get("gateway") if isinstance(runtime.get("gateway"), dict) else {}
    compute = base_payload.get("compute") if isinstance(base_payload.get("compute"), dict) else {}
    monitor_status = str(base_payload.get("status") or "").strip().lower()

    def _normalize_service_status(raw_status: Any, *, fallback_running: bool) -> str:
        text = str(raw_status or "").strip().lower()
        if text in {"ok", "running", "healthy", "ready"}:
            return "running"
        if text in {"warning", "warn", "degraded", "partial"}:
            return "degraded"
        if text == "error":
            return "degraded" if fallback_running else "offline"
        if text in {"offline", "down", "stopped"}:
            return "offline"
        return "running" if fallback_running else "offline"

    def _topology_meta(name: str) -> dict[str, Any]:
        item = services.get(name) if isinstance(services.get(name), dict) else {}
        return dict(item)

    def _detail_parts(*parts: Any) -> str:
        normalized = [str(part).strip() for part in parts if str(part or "").strip()]
        return " · ".join(normalized)

    console_meta = _topology_meta("ibkr-console")
    console_running = bool(console_probe.get("ok"))
    pb_meta = _topology_meta("pocketbase")
    pb_disk = ((base_payload.get("pocketbase") or {}).get("disk") or {}) if isinstance(base_payload.get("pocketbase"), dict) else {}
    pb_flags = [item for item in (base_payload.get("flags") or []) if str((item or {}).get("code") or "").startswith("pb_")]
    pb_status = "running" if pb_health.get("ok") else "offline"
    if pb_status == "running" and pb_flags:
        pb_status = "degraded"
    elif pb_status != "running" and pb_disk.get("status") == "partial":
        pb_status = "degraded"

    compute_status = _normalize_service_status(compute.get("status"), fallback_running=bool(compute))
    ready_engines = int(compute.get("ready_engines") or 0)
    total_engines = int(compute.get("total_engines") or 0)
    if total_engines > 0 and ready_engines < total_engines and compute_status == "running":
        compute_status = "degraded"
    if not compute and monitor_status in {"warning", "warn", "degraded"}:
        compute_status = "degraded"
    if not compute and monitor_status in {"offline", "error"}:
        compute_status = "offline"

    runtime_status = _normalize_service_status(runtime.get("status"), fallback_running=bool(runtime))
    runtime_phase = str(runtime.get("runtime_phase") or "").strip().lower()
    session = runtime.get("session") if isinstance(runtime.get("session"), dict) else {}
    websocket = runtime.get("websocket") if isinstance(runtime.get("websocket"), dict) else {}
    gateway_reachable = bool(gateway.get("running") or gateway.get("reachable"))
    websocket_ready = bool(websocket.get("connected") or websocket.get("ready"))
    session_authenticated = bool(session.get("authenticated"))
    if runtime:
        if runtime_phase in {"stopped", "stop_requested", "stopping"}:
            runtime_status = "degraded" if gateway_reachable or session_authenticated or websocket_ready else "offline"
        elif not gateway_reachable or not session_authenticated or not websocket_ready:
            runtime_status = "degraded"
    elif compute_status != "running":
        runtime_status = "offline"

    gateway_status = "running" if bool(gateway.get("running") or gateway.get("reachable")) else "offline"
    scheduler_status = str(scheduler_summary.get("status") or "").strip().lower() or "offline"
    if scheduler_status == "running" and float(scheduler_summary.get("dispatch_lag_min") or 0) >= 10:
        scheduler_status = "degraded"

    service_map = {
        "ibkr-console": {
            **console_meta,
            "status": "running" if console_running else "offline",
            "detail": _detail_parts(
                "static console",
                console_probe.get("target_url"),
                f"http {console_probe.get('status_code')}" if console_probe.get("status_code") else console_probe.get("error"),
            ),
        },
        "ibkr-api": {
            **_topology_meta("ibkr-api"),
            "status": "running",
            "detail": _detail_parts(
                "compat routes active",
                f"env {environment}",
                f"scheduler jobs {int(scheduler_summary.get('job_count') or 0)}",
            ),
        },
        "ibkr-scheduler": {
            **_topology_meta("ibkr-scheduler"),
            "status": scheduler_status,
            "detail": _detail_parts(
                f"loop {int(float(scheduler_summary.get('loop_interval_seconds') or 0))}s" if scheduler_summary.get("loop_interval_seconds") else "",
                (
                    f"lag {float(scheduler_summary.get('dispatch_lag_min') or 0):.2f}m"
                    if scheduler_summary.get("latest_ingested_bar_time_ms")
                    else "awaiting bars"
                ),
                f"jobs {int(scheduler_summary.get('job_count') or 0)}",
            ),
        },
        "ibkr-compute": {
            **_topology_meta("ibkr-compute"),
            "status": compute_status,
            "detail": _detail_parts(
                f"engines {int(compute.get('ready_engines') or 0)}/{int(compute.get('total_engines') or 0)}",
                f"compute {int(compute.get('compute_count') or 0)}",
                f"tracked {int(compute.get('tracked_cursors') or 0)}",
            ),
        },
        "ibkr-runtime": {
            **_topology_meta("ibkr-runtime"),
            "status": runtime_status,
            "detail": _detail_parts(
                f"phase {runtime.get('runtime_phase') or '--'}",
                f"session {'AUTHED' if ((runtime.get('session') or {}).get('authenticated')) else 'PENDING'}",
                f"ws {'READY' if ((runtime.get('websocket') or {}).get('connected')) else 'PENDING'}",
            ),
        },
        "ibkr-gateway": {
            **_topology_meta("ibkr-gateway"),
            "status": gateway_status,
            "detail": _detail_parts(
                f"managed_by {gateway.get('managed_by') or '--'}",
                f"pid {int(gateway.get('pid') or 0)}" if gateway.get("pid") else "",
                "reachable" if gateway.get("reachable") else "not reachable",
            ),
        },
        "pocketbase": {
            **pb_meta,
            "status": pb_status,
            "detail": _detail_parts(
                f"pb_data {pb_disk.get('data_path') or '--'}",
                f"size {pb_disk.get('status') or 'unknown'}",
                f"http {pb_health.get('status_code')}" if pb_health.get("status_code") else pb_health.get("error"),
            ),
        },
    }

    counts: dict[str, int] = {}
    for service in service_map.values():
        normalized = str(service.get("status") or "unknown").strip().lower() or "unknown"
        counts[normalized] = counts.get(normalized, 0) + 1
    return {
        "environment": environment,
        "services": service_map,
        "status_counts": counts,
    }



def build_system_monitor_payload(
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    fetch_compute_monitor: FetchPayload,
    as_dict: AsDict,
    config_refresh: ConfigRefresh,
    scheduler_status: SchedulerStatus,
    build_cron_payload: BuildCronPayload,
    config: Any,
    build_scheduler_summary: BuildSchedulerSummary,
    augment_scheduler_summary: AugmentSchedulerSummary,
    request_json: RequestJson,
    pb_base_url: str,
    console_base_url: str,
    probe_console_status: ProbeConsoleStatus,
    load_effective_config_map: LoadEffectiveConfigMap,
    monitor_config_keys: tuple[str, ...],
    load_recent_system_events: LoadRecentSystemEvents,
    enrich_monitor_payload_with_pocketbase_disk: EnrichMonitorPayload,
    derive_monitor_service_map: DeriveMonitorServiceMap,
    merge_service_topology: MergeServiceTopology,
    build_service_topology: BuildServiceTopology,
    service_profile: str = "api",
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    base_monitor_result = fetch_compute_monitor(runtime_environment)
    base_payload = as_dict(base_monitor_result.get("payload"))
    config_refresh()
    scheduler_payload = scheduler_status(runtime_environment)
    scheduler_jobs = scheduler_payload.get("jobs") if isinstance(scheduler_payload.get("jobs"), dict) else {}
    scheduler_items = build_cron_payload(config, runtime_environment, scheduler_jobs)
    scheduler_summary = augment_scheduler_summary(build_scheduler_summary(runtime_environment, scheduler_payload), scheduler_items)
    pb_health = request_json(pb_base_url, "/api/health", timeout=5)
    console_probe_payload = probe_console_status(console_base_url)

    merged_payload = dict(base_payload)
    merged_payload.setdefault("ok", bool(base_monitor_result.get("ok", False)))
    merged_payload["status"] = str(
        merged_payload.get("status") or ("offline" if merged_payload.get("ok") is False else "ok")
    ).strip().lower() or "ok"
    actual_runtime_environment = normalize_environment(
        merged_payload.get("environment") or as_dict(merged_payload.get("runtime")).get("environment") or runtime_environment,
        runtime_environment,
    )
    merged_payload["requested_environment"] = runtime_environment
    merged_payload["actual_runtime_environment"] = actual_runtime_environment
    merged_payload["runtime_environment_mismatch"] = actual_runtime_environment != runtime_environment
    merged_payload["config"] = load_effective_config_map(runtime_environment, monitor_config_keys)
    merged_payload["recent_events"] = load_recent_system_events(runtime_environment, 20)
    merged_payload["source"] = "ibkr-api"
    merged_payload["upstream_monitor"] = {
        "ok": bool(base_monitor_result.get("ok", False)),
        "status_code": int(base_monitor_result.get("status_code") or 0),
        "target_url": base_monitor_result.get("target_url") or "",
        "error": base_monitor_result.get("error") or "",
    }
    merged_payload["scheduler"] = scheduler_summary
    merged_payload["control_plane"] = {
        "api": {
            "ok": True,
            "status": "running",
            "service_profile": str(service_profile or "api"),
        },
        "scheduler": scheduler_summary,
    }
    merged_payload["service_topology"] = merge_service_topology(merged_payload, build_service_topology())
    merged_payload = enrich_monitor_payload_with_pocketbase_disk(merged_payload)
    merged_payload["service_monitor"] = derive_monitor_service_map(
        runtime_environment,
        merged_payload,
        scheduler_summary,
        console_probe=console_probe_payload,
        pb_health=pb_health,
        build_service_topology=build_service_topology,
    )
    return merged_payload


__all__ = [
    "build_system_monitor_payload",
    "derive_monitor_service_map",
    "probe_console_status",
]
