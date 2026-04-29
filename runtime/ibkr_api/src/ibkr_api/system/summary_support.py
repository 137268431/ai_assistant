from __future__ import annotations

from typing import Any, Callable

from ibkr_api.system.service_state import build_service_monitor_from_topology

NormalizeEnvironment = Callable[[Any, str], str]
LoadEffectiveConfigMap = Callable[[str, tuple[str, ...] | list[str] | set[str] | None], dict[str, str]]
IsEnabledText = Callable[[Any], bool]
FetchPayload = Callable[[str], dict[str, Any]]
AsDict = Callable[[Any], dict[str, Any]]
MergeServiceTopology = Callable[..., dict[str, Any]]
LoadRecentSystemEvents = Callable[[str, int], list[dict[str, Any]]]
TimeStrings = Callable[[float | None], dict[str, str]]



def build_system_summary_payload(
    environment: str,
    *,
    lite_mode: bool,
    normalize_environment: NormalizeEnvironment,
    load_effective_config_map: LoadEffectiveConfigMap,
    is_enabled_text: IsEnabledText,
    fetch_compute_health: FetchPayload,
    fetch_compute_status: FetchPayload,
    fetch_runtime_status: FetchPayload,
    as_dict: AsDict,
    merge_service_topology: MergeServiceTopology,
    load_recent_system_events: LoadRecentSystemEvents,
    time_strings: TimeStrings,
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    config_map = load_effective_config_map(runtime_environment)
    compute_enabled = runtime_environment != "backtest" and is_enabled_text(config_map.get("ibkr_compute_enabled", "TRUE"))
    trading_enabled = runtime_environment != "backtest" and is_enabled_text(
        config_map.get("ibkr_trading_enabled", config_map.get("trading_enabled", "TRUE"))
    )

    compute_health = fetch_compute_health(runtime_environment)
    compute_status = fetch_compute_status(runtime_environment)
    runtime_status = fetch_runtime_status(runtime_environment)

    compute_health_payload = as_dict(compute_health.get("payload"))
    compute_status_payload = as_dict(compute_status.get("payload"))
    runtime_payload = as_dict(runtime_status.get("payload"))

    compute_summary = {
        "ok": bool(compute_health.get("ok")) or bool(compute_status.get("ok")) or bool(compute_health_payload) or bool(compute_status_payload),
        "status": str(
            compute_status_payload.get("status")
            or compute_health_payload.get("status")
            or ("running" if (compute_health.get("ok") or compute_status.get("ok")) else "offline")
        ).strip().lower() or "offline",
        "engines": (
            compute_status_payload.get("engines")
            if isinstance(compute_status_payload.get("engines"), dict) and not lite_mode
            else {}
        ),
        "total_engines": int(compute_status_payload.get("total_engines") or compute_health_payload.get("total_engines") or 0),
        "ready_engines": int(compute_status_payload.get("ready_engines") or compute_health_payload.get("ready_engines") or 0),
        "compute_count": int(compute_health_payload.get("compute_count") or compute_status_payload.get("compute_count") or 0),
        "error_count": int(compute_health_payload.get("error_count") or compute_status_payload.get("error_count") or 0),
        "uptime_s": int(compute_health_payload.get("uptime_s") or 0),
        "last_compute": compute_health_payload.get("last_compute") or compute_status_payload.get("last_compute"),
        "last_scan": compute_health_payload.get("last_scan") or compute_status_payload.get("last_scan"),
        "compute_startup_preload": as_dict(
            compute_status_payload.get("compute_startup_preload") or compute_health_payload.get("compute_startup_preload")
        ),
        "service_topology": merge_service_topology(compute_status_payload, compute_health_payload),
    }
    if compute_health.get("error") or compute_status.get("error"):
        compute_summary["error"] = "; ".join(
            part for part in (str(compute_health.get("error") or ""), str(compute_status.get("error") or "")) if part
        )

    merged_topology = merge_service_topology(compute_summary, runtime_payload)
    service_monitor = build_service_monitor_from_topology(runtime_environment, merged_topology)
    runtime_summary = {
        "ok": bool(runtime_status.get("ok")) or bool(runtime_payload),
        "status": str(runtime_payload.get("status") or ("running" if runtime_payload else "offline")).strip().lower() or "offline",
        "environment": normalize_environment(runtime_payload.get("environment") or runtime_environment, runtime_environment),
        "service_topology": merged_topology,
        "proxy_upstream": runtime_status.get("selected_upstream") or runtime_status.get("proxy_upstream") or "",
    }
    if runtime_status.get("error"):
        runtime_summary["error"] = str(runtime_status.get("error") or "")

    actual_runtime_environment = normalize_environment(runtime_summary.get("environment") or runtime_environment, runtime_environment)
    ok = bool(compute_summary.get("ok")) and (bool(runtime_summary.get("ok")) or not runtime_payload)
    degraded = bool(compute_summary.get("ok")) or bool(runtime_summary.get("ok")) or bool(runtime_payload)
    return {
        "ok": ok,
        "status": "running" if ok else ("degraded" if degraded else "offline"),
        "timestamp": time_strings()["us"],
        "environment": runtime_environment,
        "requested_environment": runtime_environment,
        "actual_runtime_environment": actual_runtime_environment,
        "runtime_environment_mismatch": actual_runtime_environment != runtime_environment,
        "compute_enabled": compute_enabled,
        "ibkr_trading_enabled": trading_enabled,
        "config": config_map,
        "today": {
            "ibkr_signals": 0,
            "ibkr_indicators": 0,
            "orders": 0,
            "ibkr_bars": 0,
            "ibkr_targets": 0,
            "events": 0,
        },
        "ibkr_compute": compute_summary,
        "ibkr_runtime": runtime_summary,
        "service_topology": merged_topology,
        "service_monitor": service_monitor,
        "recent_events": load_recent_system_events(runtime_environment, 20),
        "data_freshness": [],
        "lite_mode": bool(lite_mode),
        "source": "ibkr-api",
    }


__all__ = ["build_system_summary_payload"]
