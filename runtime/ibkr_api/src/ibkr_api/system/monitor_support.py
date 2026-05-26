from __future__ import annotations

import inspect
from typing import Any, Callable

import requests

from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode, resolve_data_environment
from ibkr_api.runtime.effective_gate import build_effective_trading_gate
from ibkr_api.system.service_state import (
    apply_service_monitor_to_topology,
    derive_backtest_state,
    derive_compute_state,
    derive_gateway_state,
    derive_runtime_state,
    rebuild_service_monitor,
    utc_timestamp,
)

NormalizeEnvironment = Callable[[Any, str], str]
FetchPayload = Callable[[str], dict[str, Any]]
AsDict = Callable[[Any], dict[str, Any]]
ConfigRefresh = Callable[[], None]
SchedulerStatus = Callable[..., dict[str, Any]]
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
AccountSnapshotProbe = Callable[[str], dict[str, Any]]

ACCOUNT_SNAPSHOT_WARN_MS = 12_000.0


def _monitor_builder_error(stage: str, exc: Any, *, severity: str = "warning") -> dict[str, str]:
    detail = str(exc or "").strip() or "unknown_error"
    return {
        "stage": str(stage or "unknown").strip() or "unknown",
        "severity": str(severity or "warning").strip().lower() or "warning",
        "detail": detail,
    }


def _monitor_builder_flag(error: dict[str, Any]) -> dict[str, str]:
    stage = str(error.get("stage") or "unknown").strip() or "unknown"
    severity = str(error.get("severity") or "warning").strip().lower() or "warning"
    detail = str(error.get("detail") or "unknown_error").strip() or "unknown_error"
    return {
        "severity": severity,
        "code": f"monitor_builder_{stage}",
        "title": f"Monitor aggregation degraded ({stage})",
        "detail": detail,
    }


def _fallback_scheduler_payload(environment: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "unknown",
        "environment": environment,
        "jobs": {},
        "ingest_cursor": {},
        "compute_dispatch_cursor": {},
    }


def _fallback_scheduler_summary(environment: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "unknown",
        "environment": environment,
        "loop_interval_seconds": 0.0,
        "job_count": 0,
        "job_status_counts": {},
        "jobs": {},
        "ingest_cursor": {},
        "compute_dispatch_cursor": {},
        "latest_ingested_bar_time_ms": 0,
        "latest_dispatched_bar_time_ms": 0,
        "last_dispatch_at_ms": 0,
        "dispatch_lag_ms": 0,
        "dispatch_lag_min": 0.0,
        "enabled_job_count": 0,
        "native_job_count": 0,
        "compatibility_job_count": 0,
    }


def _call_scheduler_status_lite(scheduler_status: SchedulerStatus, environment: str) -> dict[str, Any]:
    try:
        return scheduler_status(environment, lite=True)
    except TypeError as exc:
        if "lite" not in str(exc):
            raise
        return scheduler_status(environment)


def _fallback_service_monitor(environment: str, topology: dict[str, Any]) -> dict[str, Any]:
    services = topology.get("services") if isinstance(topology.get("services"), dict) else {}
    service_map: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for name, raw_item in services.items():
        item = dict(raw_item) if isinstance(raw_item, dict) else {}
        status = str(item.get("status") or "unknown").strip().lower() or "unknown"
        item["status"] = status
        service_map[str(name)] = item
        counts[status] = counts.get(status, 0) + 1
    return {
        "environment": environment,
        "services": service_map,
        "status_counts": counts,
    }


def _merge_monitor_builder_flags(existing_flags: Any, errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in existing_flags if isinstance(existing_flags, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code and code in seen:
            continue
        if code:
            seen.add(code)
        merged.append(dict(item))
    for error in errors:
        flag = _monitor_builder_flag(error)
        code = str(flag.get("code") or "").strip()
        if code in seen:
            continue
        seen.add(code)
        merged.append(flag)
    return merged


def _append_effective_gate_flag(existing_flags: Any, gate: dict[str, Any]) -> list[dict[str, Any]]:
    merged = [dict(item) for item in existing_flags if isinstance(item, dict)] if isinstance(existing_flags, list) else []
    codes = {str(item.get("code") or "").strip() for item in merged if isinstance(item, dict)}
    raw_signal = gate.get("raw_signal_gate") if isinstance(gate.get("raw_signal_gate"), dict) else {}
    raw_snapshot = gate.get("raw_startup_snapshot") if isinstance(gate.get("raw_startup_snapshot"), dict) else {}
    if bool(gate.get("open")) and (raw_signal.get("open") is False or raw_snapshot.get("open") is False):
        code = "trading_gate_snapshot_stale"
        if code not in codes:
            merged.append(
                {
                    "severity": "warning",
                    "code": code,
                    "title": "Trading gate display uses live readiness",
                    "detail": (
                        "Effective gate is open from current readiness; stale signal/startup snapshot "
                        "is diagnostic only and does not block trading."
                    ),
                }
            )
    if not bool(gate.get("open")) and not gate.get("reason"):
        code = "trading_gate_reason_missing"
        if code not in codes:
            merged.append(
                {
                    "severity": "error",
                    "code": code,
                    "title": "Trading gate closed without reason",
                    "detail": "Effective gate is closed but no reason was provided.",
                }
            )
    return merged


def _merge_unique_flags(existing_flags: Any, extra_flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = [dict(item) for item in existing_flags if isinstance(item, dict)] if isinstance(existing_flags, list) else []
    seen = {str(item.get("code") or "").strip() for item in merged if str(item.get("code") or "").strip()}
    for item in extra_flags:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code and code in seen:
            continue
        if code:
            seen.add(code)
        merged.append(dict(item))
    return merged


def _set_status_from_flags(payload: dict[str, Any], flags: list[dict[str, Any]]) -> None:
    severities = {str((item or {}).get("severity") or "").strip().lower() for item in flags if isinstance(item, dict)}
    current_status = str(payload.get("status") or "ok").strip().lower() or "ok"
    if "error" in severities and current_status not in {"offline", "error"}:
        payload["status"] = "error"
        payload["ok"] = False
    elif "warning" in severities and current_status == "ok":
        payload["status"] = "warning"
        payload["ok"] = False


def _elapsed_from_account_snapshot_probe(probe: dict[str, Any], snapshot: dict[str, Any]) -> float:
    elapsed = probe.get("elapsed_ms")
    if elapsed not in (None, ""):
        try:
            return float(elapsed)
        except Exception:
            return 0.0
    diagnostics = snapshot.get("diagnostics") if isinstance(snapshot.get("diagnostics"), dict) else {}
    account = diagnostics.get("account_snapshot") if isinstance(diagnostics.get("account_snapshot"), dict) else {}
    try:
        return float(account.get("total_elapsed_ms") or 0.0)
    except Exception:
        return 0.0


def _account_snapshot_flags(probe: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(probe, dict) or probe.get("skipped"):
        return []
    snapshot = probe.get("payload") if isinstance(probe.get("payload"), dict) else {}
    flags: list[dict[str, Any]] = []
    status_code = int(probe.get("status_code") or 0)
    ok = bool(probe.get("ok")) and status_code < 400 and snapshot.get("ok") is not False
    service_running = snapshot.get("service_running")
    gateway_running = snapshot.get("gateway_running")
    session_authenticated = snapshot.get("session_authenticated")
    if service_running is False or gateway_running is False or session_authenticated is False:
        flags.append(
            {
                "severity": "error",
                "code": "account_runtime_unavailable",
                "title": "Account runtime unavailable",
                "detail": (
                    f"service_running={service_running} gateway_running={gateway_running} "
                    f"session_authenticated={session_authenticated}"
                ),
            }
        )
    if not ok:
        detail = str(probe.get("error") or snapshot.get("error") or snapshot.get("message") or f"status={status_code}")
        flags.append(
            {
                "severity": "warning",
                "code": "account_snapshot_degraded",
                "title": "Account snapshot unavailable",
                "detail": detail,
            }
        )
    elapsed_ms = _elapsed_from_account_snapshot_probe(probe, snapshot)
    if elapsed_ms >= ACCOUNT_SNAPSHOT_WARN_MS:
        flags.append(
            {
                "severity": "warning",
                "code": "account_snapshot_timeout",
                "title": "Account snapshot slow",
                "detail": f"account snapshot took {elapsed_ms:.0f}ms",
            }
        )
    errors = snapshot.get("errors") if isinstance(snapshot.get("errors"), dict) else {}
    blocking_errors = [
        f"{name}: {errors.get(name)}"
        for name in ("summary", "positions", "orders")
        if str(errors.get(name) or "").strip()
    ]
    if blocking_errors:
        flags.append(
            {
                "severity": "warning",
                "code": "account_snapshot_degraded",
                "title": "Account snapshot degraded",
                "detail": " | ".join(blocking_errors[:3]),
            }
        )
    pnl_error = str(errors.get("pnl") or "").strip()
    if pnl_error:
        flags.append(
            {
                "severity": "warning",
                "code": "account_pnl_unavailable",
                "title": "Account PnL unavailable",
                "detail": pnl_error,
            }
        )
    return flags


def _history_backfill_detail(compute: dict[str, Any]) -> str:
    data_backfill = compute.get("data_backfill") if isinstance(compute.get("data_backfill"), dict) else {}
    if not data_backfill:
        return ""
    parts: list[str] = []
    active_requests = int(data_backfill.get("active_requests") or 0)
    active_symbols_total = int(data_backfill.get("active_symbols_total") or 0)
    if active_requests or active_symbols_total:
        parts.append(f"history active {active_requests}/{active_symbols_total}")
    last_trace = data_backfill.get("last_trace") if isinstance(data_backfill.get("last_trace"), dict) else {}
    if last_trace:
        source = str(last_trace.get("source") or "history").strip()
        duration_s = float(last_trace.get("duration_s") or 0)
        requests = int(last_trace.get("request_count") or 0)
        retry = int(last_trace.get("retry_count") or 0)
        throttle = int(last_trace.get("throttle_count") or 0)
        if duration_s >= 60 or requests >= 100 or retry or throttle:
            parts.append(f"history last {source} {duration_s:.0f}s req {requests} retry {retry} throttle {throttle}")
    return " | ".join(parts)


def _call_console_probe(probe_console_status: ProbeConsoleStatus, console_base_url: str) -> dict[str, Any]:
    try:
        signature = inspect.signature(probe_console_status)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        positional_params = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        variadic = any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        if not positional_params and not variadic:
            return probe_console_status()
    return probe_console_status(console_base_url)


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
    backtest_health: dict[str, Any] | None = None,
    build_service_topology: BuildServiceTopology,
) -> dict[str, Any]:
    topology = base_payload.get("service_topology") if isinstance(base_payload.get("service_topology"), dict) else build_service_topology()
    services = topology.get("services") if isinstance(topology.get("services"), dict) else {}
    runtime = base_payload.get("runtime") if isinstance(base_payload.get("runtime"), dict) else {}
    gateway = runtime.get("gateway") if isinstance(runtime.get("gateway"), dict) else {}
    compute = base_payload.get("compute") if isinstance(base_payload.get("compute"), dict) else {}
    monitor_status = str(base_payload.get("status") or "").strip().lower()
    monitor_source_unavailable = bool(base_payload.get("monitor_source_unavailable"))

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

    def _compute_startup_preload_snapshot() -> dict[str, Any]:
        preload = compute.get("compute_startup_preload") if isinstance(compute.get("compute_startup_preload"), dict) else {}
        if preload:
            return dict(preload)
        root_preload = base_payload.get("compute_startup_preload")
        if isinstance(root_preload, dict):
            return dict(root_preload)
        return {}

    def _compute_startup_preload_active() -> bool:
        preload = _compute_startup_preload_snapshot()
        status = str(preload.get("status") or "").strip().lower()
        return bool(preload.get("running")) or status in {"running", "scheduled"}

    def _scheduler_compute_preload_deferred() -> bool:
        jobs = scheduler_summary.get("jobs") if isinstance(scheduler_summary.get("jobs"), dict) else {}
        compute_job = jobs.get("ibkr_compute_runtime") if isinstance(jobs.get("ibkr_compute_runtime"), dict) else {}
        last_result = compute_job.get("last_result") if isinstance(compute_job.get("last_result"), dict) else {}
        result_payloads = [last_result]
        nested_payload = last_result.get("payload") if isinstance(last_result.get("payload"), dict) else {}
        if nested_payload:
            result_payloads.append(nested_payload)

        for payload in result_payloads:
            reason = str(payload.get("reason") or "").strip().lower()
            if reason == "compute_startup_preload_running":
                return True
            preload = payload.get("compute_startup_preload") if isinstance(payload.get("compute_startup_preload"), dict) else {}
            status = str(preload.get("status") or "").strip().lower()
            if bool(preload.get("running")) or status in {"running", "scheduled"}:
                return True
        return False

    def _scheduler_close_compute_deferred() -> bool:
        reason = str(scheduler_summary.get("dispatch_lag_reason") or "").strip().lower()
        in_progress = (
            bool(scheduler_summary.get("compute_in_progress"))
            or bool(scheduler_summary.get("official_5m_close_in_progress"))
            or reason in {"close_compute_inflight", "official_5m_close_inflight"}
        )
        stalled = bool(
            scheduler_summary.get("compute_in_progress_stalled")
            or scheduler_summary.get("inflight_stalled")
            or scheduler_summary.get("official_5m_close_stalled")
        )
        return bool(in_progress and not stalled)

    observed_at = utc_timestamp()
    backtest_probe = backtest_health if isinstance(backtest_health, dict) else {}
    if not backtest_probe:
        backtest_probe = base_payload.get("backtest_service") if isinstance(base_payload.get("backtest_service"), dict) else {}
    if not backtest_probe:
        backtest_probe = base_payload.get("backtest") if isinstance(base_payload.get("backtest"), dict) else {}
    backtest_meta = _topology_meta("ibkr-backtest")
    backtest_state = (
        derive_backtest_state(backtest_probe, observed_at=observed_at)
        if backtest_probe
        else {
            "service_name": "ibkr-backtest",
            "status": str(backtest_meta.get("status") or "unknown").strip().lower() or "unknown",
            "ready": False,
            "readiness_phase": "unknown",
            "status_source": "topology",
            "last_observed_at": observed_at,
            "stale": False,
            "detail": str(backtest_meta.get("responsibility") or "backtest health not probed").strip(),
        }
    )
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
    history_detail = _history_backfill_detail(compute)
    ready_engines = int(compute.get("ready_engines") or 0)
    total_engines = int(compute.get("total_engines") or 0)
    if total_engines > 0 and ready_engines < total_engines and compute_status == "running":
        compute_status = "degraded"
    if monitor_source_unavailable and not compute:
        compute_status = "unknown"
    if not compute and monitor_status in {"warning", "warn", "degraded"}:
        compute_status = "unknown" if monitor_source_unavailable else "degraded"
    if not compute and monitor_status in {"offline", "error"}:
        compute_status = "unknown" if monitor_source_unavailable else "offline"

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
    elif monitor_source_unavailable:
        runtime_status = "unknown"
    elif compute_status != "running":
        runtime_status = "offline"

    gateway_status = (
        "running"
        if bool(gateway.get("running") or gateway.get("reachable"))
        else ("unknown" if monitor_source_unavailable and not gateway else "offline")
    )
    scheduler_status = str(scheduler_summary.get("status") or "").strip().lower() or "unknown"
    scheduler_unavailable = scheduler_status == "unknown" and not bool(scheduler_summary.get("ok", True))
    compute_preload_active = _compute_startup_preload_active() or _scheduler_compute_preload_deferred()
    close_compute_deferred = _scheduler_close_compute_deferred()
    scheduler_lag_compute_relevant = bool(scheduler_summary.get("dispatch_lag_compute_relevant", True))
    if (
        scheduler_status == "running"
        and scheduler_lag_compute_relevant
        and float(scheduler_summary.get("dispatch_lag_min") or 0) > 10
        and not compute_preload_active
        and not close_compute_deferred
    ):
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
                "status unavailable" if scheduler_unavailable else "",
                f"loop {int(float(scheduler_summary.get('loop_interval_seconds') or 0))}s" if scheduler_summary.get("loop_interval_seconds") else "",
                (
                    f"lag {float(scheduler_summary.get('dispatch_lag_min') or 0):.2f}m"
                    if scheduler_summary.get("latest_ingested_bar_time_ms")
                    else "awaiting bars"
                ),
                "non-compute ingest" if scheduler_summary.get("dispatch_lag_reason") == "non_compute_ingest_source" else "",
                "deferred by compute preload" if compute_preload_active else "",
                (
                    f"official close in progress {float(scheduler_summary.get('official_5m_close_age_s') or scheduler_summary.get('inflight_age_s') or 0):.1f}s"
                    if close_compute_deferred and scheduler_summary.get("dispatch_lag_reason") == "official_5m_close_inflight"
                    else (
                        f"close compute in progress {float(scheduler_summary.get('inflight_age_s') or 0):.1f}s"
                        if close_compute_deferred
                        else ""
                    )
                ),
                (
                    f"missing indicators {int(scheduler_summary.get('missing_indicator_symbol_count') or 0)}"
                    if close_compute_deferred and scheduler_summary.get("missing_indicator_symbol_count")
                    else ""
                ),
                (
                    f"busy deferred {int(scheduler_summary.get('deferred_busy_symbol_count') or 0)}"
                    if scheduler_summary.get("deferred_compute_busy")
                    else ""
                ),
                f"jobs {int(scheduler_summary.get('job_count') or 0)}",
            ),
        },
        "ibkr-compute": {
            **_topology_meta("ibkr-compute"),
            "status": compute_status,
            "detail": _detail_parts(
                "monitor source unavailable" if monitor_source_unavailable and not compute else "",
                f"engines {int(compute.get('ready_engines') or 0)}/{int(compute.get('total_engines') or 0)}",
                f"compute {int(compute.get('compute_count') or 0)}",
                f"tracked {int(compute.get('tracked_cursors') or 0)}",
                history_detail,
            ),
        },
        "ibkr-backtest": {
            **backtest_meta,
            **backtest_state,
        },
        "ibkr-runtime": {
            **_topology_meta("ibkr-runtime"),
            "status": runtime_status,
            "detail": _detail_parts(
                "monitor source unavailable" if monitor_source_unavailable and not runtime else "",
                f"phase {runtime.get('runtime_phase') or '--'}" if runtime else "",
                f"session {'AUTHED' if ((runtime.get('session') or {}).get('authenticated')) else 'PENDING'}" if runtime else "",
                f"ws {'READY' if ((runtime.get('websocket') or {}).get('connected')) else 'PENDING'}" if runtime else "",
            ),
        },
        "ibkr-gateway": {
            **_topology_meta("ibkr-gateway"),
            "status": gateway_status,
            "detail": _detail_parts(
                "monitor source unavailable" if monitor_source_unavailable and not gateway else "",
                f"managed_by {gateway.get('managed_by') or '--'}" if gateway else "",
                f"pid {int(gateway.get('pid') or 0)}" if gateway.get("pid") else "",
                ("reachable" if gateway.get("reachable") else "not reachable") if gateway else "",
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

    if compute:
        compute_state_payload = dict(compute)
        compute_preload = _compute_startup_preload_snapshot()
        if compute_preload and not isinstance(compute_state_payload.get("compute_startup_preload"), dict):
            compute_state_payload["compute_startup_preload"] = compute_preload
        service_map["ibkr-compute"] = {
            **service_map.get("ibkr-compute", {}),
            **derive_compute_state(compute_state_payload, observed_at=observed_at),
        }
        if history_detail:
            service_map["ibkr-compute"]["detail"] = _detail_parts(
                service_map["ibkr-compute"].get("detail"),
                history_detail,
            )
    if runtime:
        service_map["ibkr-runtime"] = {
            **service_map.get("ibkr-runtime", {}),
            **derive_runtime_state(runtime, observed_at=observed_at),
        }
        service_map["ibkr-gateway"] = {
            **service_map.get("ibkr-gateway", {}),
            **derive_gateway_state(runtime, observed_at=observed_at),
        }

    return rebuild_service_monitor(environment, service_map)



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
    backtest_base_url: str = "",
    probe_console_status: ProbeConsoleStatus,
    load_effective_config_map: LoadEffectiveConfigMap,
    monitor_config_keys: tuple[str, ...],
    load_recent_system_events: LoadRecentSystemEvents,
    enrich_monitor_payload_with_pocketbase_disk: EnrichMonitorPayload,
    derive_monitor_service_map: DeriveMonitorServiceMap,
    merge_service_topology: MergeServiceTopology,
    build_service_topology: BuildServiceTopology,
    account_snapshot_probe: AccountSnapshotProbe | None = None,
    service_profile: str = "api",
) -> dict[str, Any]:
    runtime_environment = normalize_broker_mode(environment, configured_broker_mode())
    data_environment = resolve_data_environment(runtime_environment)
    builder_errors: list[dict[str, str]] = []
    base_monitor_result = fetch_compute_monitor(data_environment)
    base_payload = as_dict(base_monitor_result.get("payload"))
    base_monitor_ok = bool(base_monitor_result.get("ok"))
    monitor_source_unavailable = bool(not base_monitor_ok and not base_payload)
    if (not bool(base_monitor_result.get("ok"))) and (
        str(base_monitor_result.get("error") or "").strip() or int(base_monitor_result.get("status_code") or 0) >= 400
    ):
        detail = str(base_monitor_result.get("error") or "").strip() or (
            f"upstream_status={int(base_monitor_result.get('status_code') or 0)} target={base_monitor_result.get('target_url') or ''}"
        )
        severity = "warning" if monitor_source_unavailable else "error"
        builder_errors.append(_monitor_builder_error("compute_monitor", detail, severity=severity))
    try:
        config_refresh()
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("config_refresh", exc))
    try:
        scheduler_payload = _call_scheduler_status_lite(scheduler_status, data_environment)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("scheduler_status", exc))
        scheduler_payload = _fallback_scheduler_payload(data_environment)
    else:
        scheduler_meta = as_dict(scheduler_payload.get("_meta")) if isinstance(scheduler_payload, dict) else {}
        scheduler_status_text = str((scheduler_payload or {}).get("status") or "").strip().lower()
        scheduler_error = str(scheduler_meta.get("error") or "").strip()
        scheduler_status_code = int(scheduler_meta.get("status_code") or 0)
        if isinstance(scheduler_payload, dict) and not bool(scheduler_payload.get("ok", False)) and (
            scheduler_error or scheduler_status_text in {"unknown", "offline", "error"} or scheduler_status_code >= 400
        ):
            detail = scheduler_error or (
                f"status={scheduler_status_text or 'unknown'} status_code={scheduler_status_code}"
            )
            severity = "error" if scheduler_status_text in {"offline", "error"} else "warning"
            builder_errors.append(_monitor_builder_error("scheduler_status", detail, severity=severity))
    scheduler_jobs = scheduler_payload.get("jobs") if isinstance(scheduler_payload.get("jobs"), dict) else {}
    try:
        scheduler_items = build_cron_payload(config, data_environment, scheduler_jobs)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("scheduler_cronz", exc))
        scheduler_items = []
    try:
        scheduler_summary = augment_scheduler_summary(build_scheduler_summary(data_environment, scheduler_payload), scheduler_items)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("scheduler_summary", exc))
        scheduler_summary = _fallback_scheduler_summary(data_environment)
    try:
        pb_health = request_json(pb_base_url, "/api/health", timeout=5)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("pocketbase_health", exc))
        pb_health = {
            "ok": False,
            "status_code": 0,
            "payload": {},
            "error": str(exc),
            "target_url": f"{str(pb_base_url or '').rstrip('/')}/api/health",
        }
    if str(backtest_base_url or "").strip():
        try:
            backtest_health = request_json(backtest_base_url, "/health", params=[("environment", data_environment)], timeout=5)
        except Exception as exc:
            builder_errors.append(_monitor_builder_error("backtest_health", exc))
            backtest_health = {
                "ok": False,
                "status_code": 0,
                "payload": {},
                "error": str(exc),
                "target_url": f"{str(backtest_base_url or '').rstrip('/')}/health",
            }
    else:
        backtest_health = {
            "ok": False,
            "status_code": 0,
            "payload": {},
            "error": "backtest_base_url_missing",
            "target_url": "",
        }
    try:
        console_probe_payload = _call_console_probe(probe_console_status, console_base_url)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("console_probe", exc))
        console_probe_payload = {
            "ok": False,
            "status_code": 0,
            "target_url": f"{str(console_base_url or '').rstrip('/')}/index.html",
            "error": str(exc),
        }
    if callable(account_snapshot_probe):
        try:
            account_snapshot_probe_payload = account_snapshot_probe(runtime_environment)
        except Exception as exc:
            builder_errors.append(_monitor_builder_error("account_snapshot", exc))
            account_snapshot_probe_payload = {
                "ok": False,
                "status_code": 0,
                "payload": {},
                "error": str(exc),
            }
    else:
        account_snapshot_probe_payload = {"ok": True, "skipped": True, "reason": "account_snapshot_probe_not_configured"}

    merged_payload = dict(base_payload)
    merged_payload.setdefault("ok", base_monitor_ok)
    if monitor_source_unavailable:
        merged_payload["monitor_source_unavailable"] = True
        merged_payload["monitor_source_error"] = str(base_monitor_result.get("error") or "").strip()
    merged_payload["status"] = str(
        merged_payload.get("status")
        or ("warning" if monitor_source_unavailable else ("offline" if merged_payload.get("ok") is False else "ok"))
    ).strip().lower() or "ok"
    actual_runtime_environment = normalize_environment(
        merged_payload.get("broker_mode")
        or as_dict(merged_payload.get("runtime")).get("broker_mode")
        or merged_payload.get("environment")
        or as_dict(merged_payload.get("runtime")).get("environment")
        or runtime_environment,
        runtime_environment,
    )
    merged_payload["requested_environment"] = runtime_environment
    merged_payload["broker_mode"] = runtime_environment
    merged_payload["data_environment"] = data_environment
    merged_payload["market_data_environment"] = data_environment
    merged_payload["shared_market_data"] = data_environment == "live"
    merged_payload["actual_runtime_environment"] = actual_runtime_environment
    merged_payload["runtime_environment_mismatch"] = actual_runtime_environment != runtime_environment
    try:
        broker_config = load_effective_config_map(runtime_environment, monitor_config_keys)
        data_config = load_effective_config_map(data_environment, monitor_config_keys)
        merged_payload["config"] = {**data_config, **broker_config}
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("config_map", exc))
        merged_payload["config"] = {}
    try:
        merged_payload["recent_events"] = load_recent_system_events(runtime_environment, 20)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("recent_events", exc))
        merged_payload["recent_events"] = []
    merged_payload["source"] = "ibkr-api"
    merged_payload["upstream_monitor"] = {
        "ok": base_monitor_ok,
        "status_code": int(base_monitor_result.get("status_code") or 0),
        "target_url": base_monitor_result.get("target_url") or "",
        "error": base_monitor_result.get("error") or "",
        "elapsed_ms": base_monitor_result.get("elapsed_ms"),
        "timeout_s": base_monitor_result.get("timeout_s"),
        "source_unavailable": monitor_source_unavailable,
    }
    merged_payload["account_snapshot_probe"] = account_snapshot_probe_payload
    merged_payload["scheduler"] = scheduler_summary
    merged_payload["backtest_service"] = {
        **backtest_health,
        "payload": as_dict(backtest_health.get("payload")),
    }
    if as_dict(merged_payload["backtest_service"].get("payload")).get("backtest"):
        merged_payload["backtest"] = as_dict(as_dict(merged_payload["backtest_service"].get("payload")).get("backtest"))
    merged_payload["control_plane"] = {
        "api": {
            "ok": True,
            "status": "running",
            "service_profile": str(service_profile or "api"),
        },
        "scheduler": scheduler_summary,
    }
    try:
        merged_payload["service_topology"] = merge_service_topology(
            merged_payload,
            as_dict(merged_payload["backtest_service"].get("payload")),
            build_service_topology(),
        )
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("service_topology", exc))
        merged_payload["service_topology"] = build_service_topology()
    try:
        merged_payload = enrich_monitor_payload_with_pocketbase_disk(merged_payload)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("pocketbase_disk", exc))
    try:
        merged_payload["service_monitor"] = derive_monitor_service_map(
            runtime_environment,
            merged_payload,
            scheduler_summary,
            console_probe=console_probe_payload,
            pb_health=pb_health,
            backtest_health=merged_payload["backtest_service"],
            build_service_topology=build_service_topology,
        )
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("service_monitor", exc))
        merged_payload["service_monitor"] = _fallback_service_monitor(
            runtime_environment,
            merged_payload.get("service_topology") if isinstance(merged_payload.get("service_topology"), dict) else build_service_topology(),
        )
    merged_payload["service_topology"] = apply_service_monitor_to_topology(
        merged_payload.get("service_topology") if isinstance(merged_payload.get("service_topology"), dict) else {},
        merged_payload["service_monitor"],
    )
    try:
        runtime_section = as_dict(merged_payload.get("runtime"))
        gate = build_effective_trading_gate(
            runtime_section,
            live_readiness=as_dict(merged_payload.get("live_readiness")),
        )
        merged_payload["effective_trading_gate"] = gate
        if runtime_section:
            runtime_section["effective_trading_gate"] = gate
            merged_payload["runtime"] = runtime_section
        merged_payload["flags"] = _append_effective_gate_flag(merged_payload.get("flags"), gate)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("effective_trading_gate", exc))
    account_flags = _account_snapshot_flags(account_snapshot_probe_payload)
    if account_flags:
        merged_payload["flags"] = _merge_unique_flags(merged_payload.get("flags"), account_flags)
        _set_status_from_flags(merged_payload, account_flags)
    if builder_errors:
        merged_payload["monitor_builder_errors"] = builder_errors
        merged_payload["flags"] = _merge_monitor_builder_flags(merged_payload.get("flags"), builder_errors)
        current_status = str(merged_payload.get("status") or "ok").strip().lower() or "ok"
        if current_status == "ok":
            merged_payload["status"] = "warning"
        merged_payload["ok"] = False
    return merged_payload


__all__ = [
    "build_system_monitor_payload",
    "derive_monitor_service_map",
    "probe_console_status",
]
