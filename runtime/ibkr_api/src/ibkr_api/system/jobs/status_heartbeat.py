from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable


HEARTBEAT_STATE_KEY = "system_notify_heartbeat"
HEARTBEAT_ALERT_COOLDOWN_MS = 30 * 60 * 1000

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildSystemSummaryPayload = Callable[..., dict[str, Any]]
BuildSystemMonitorPayload = Callable[[str], dict[str, Any]]
EmitSystemEvent = Callable[..., dict[str, Any]]
GetStatePayload = Callable[[str, str], dict[str, Any]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalized_status(value: Any) -> str:
    return _to_text(value).lower() or "unknown"


def _runtime_health_snapshot(
    *,
    environment: str,
    build_system_summary_payload: BuildSystemSummaryPayload,
    build_system_monitor_payload: BuildSystemMonitorPayload,
) -> dict[str, Any]:
    summary = _as_dict(build_system_summary_payload(environment, lite_mode=True))
    monitor = _as_dict(build_system_monitor_payload(environment))
    runtime = _as_dict(summary.get("ibkr_runtime") or monitor.get("runtime"))
    compute = _as_dict(summary.get("ibkr_compute") or monitor.get("compute"))
    scheduler = _as_dict(monitor.get("scheduler"))
    service_monitor = _as_dict(monitor.get("service_monitor"))
    services = _as_dict(service_monitor.get("services"))
    flags = [
        _as_dict(item)
        for item in (monitor.get("flags") or [])
        if isinstance(item, dict) and _to_text(item.get("code"))
    ]
    counts = _as_dict(service_monitor.get("status_counts"))
    degraded_count = _to_int(counts.get("degraded"), 0) + _to_int(counts.get("warning"), 0)
    offline_count = _to_int(counts.get("offline"), 0) + _to_int(counts.get("error"), 0)
    session = _as_dict(runtime.get("session"))
    websocket = _as_dict(runtime.get("websocket"))
    gateway = _as_dict(runtime.get("gateway"))
    daily_scan = _as_dict(runtime.get("daily_scan") or summary.get("daily_scan"))
    today = _as_dict(summary.get("today"))
    issue_codes: list[str] = []
    for flag in flags[:8]:
        issue_codes.append(_to_text(flag.get("code")))
    monitor_status = _normalized_status(monitor.get("status"))
    summary_status = _normalized_status(summary.get("status"))
    runtime_status = _normalized_status(runtime.get("status"))
    if monitor_status not in {"ok", "running"}:
        issue_codes.append(f"monitor:{monitor_status}")
    if summary_status not in {"ok", "running"}:
        issue_codes.append(f"summary:{summary_status}")
    if runtime_status not in {"ok", "running"}:
        issue_codes.append(f"runtime:{runtime_status}")
    if not bool(session.get("authenticated")):
        issue_codes.append("session_unauthenticated")
    if not bool(websocket.get("connected") or websocket.get("ready")):
        issue_codes.append("websocket_not_ready")
    if not bool(gateway.get("running") or gateway.get("reachable")):
        issue_codes.append("gateway_offline")
    if degraded_count > 0:
        issue_codes.append(f"services_degraded:{degraded_count}")
    if offline_count > 0:
        issue_codes.append(f"services_offline:{offline_count}")
    seen: set[str] = set()
    deduped_issue_codes: list[str] = []
    for item in issue_codes:
        if not item or item in seen:
            continue
        seen.add(item)
        deduped_issue_codes.append(item)
    unhealthy = bool(deduped_issue_codes)
    severity = "warning"
    if monitor_status in {"offline", "error"} or offline_count > 0:
        severity = "error"
    elif any(_normalized_status(flag.get("severity")) == "error" for flag in flags):
        severity = "error"
    return {
        "summary": summary,
        "monitor": monitor,
        "runtime": runtime,
        "compute": compute,
        "scheduler": scheduler,
        "service_monitor": service_monitor,
        "services": services,
        "flags": flags,
        "today": today,
        "daily_scan": daily_scan,
        "session": session,
        "websocket": websocket,
        "gateway": gateway,
        "summary_status": summary_status,
        "monitor_status": monitor_status,
        "runtime_status": runtime_status,
        "unhealthy": unhealthy,
        "severity": severity,
        "issue_codes": deduped_issue_codes,
    }


def _heartbeat_fingerprint(snapshot: dict[str, Any]) -> str:
    scheduler = _as_dict(snapshot.get("scheduler"))
    return str(
        {
            "monitor_status": snapshot.get("monitor_status"),
            "summary_status": snapshot.get("summary_status"),
            "runtime_status": snapshot.get("runtime_status"),
            "issue_codes": list(snapshot.get("issue_codes") or []),
            "dispatch_lag_min": round(float(scheduler.get("dispatch_lag_min") or 0.0), 2),
        }
    )


def _heartbeat_detail(snapshot: dict[str, Any], *, timestamp_us: str) -> dict[str, Any]:
    runtime = _as_dict(snapshot.get("runtime"))
    compute = _as_dict(snapshot.get("compute"))
    scheduler = _as_dict(snapshot.get("scheduler"))
    session = _as_dict(snapshot.get("session"))
    websocket = _as_dict(snapshot.get("websocket"))
    gateway = _as_dict(snapshot.get("gateway"))
    daily_scan = _as_dict(snapshot.get("daily_scan"))
    today = _as_dict(snapshot.get("today"))
    counts = _as_dict(_as_dict(snapshot.get("service_monitor")).get("status_counts"))
    detail = {
        "检查时间": timestamp_us,
        "总体状态": _to_text(snapshot.get("monitor_status") or snapshot.get("summary_status")) or "unknown",
        "Compute": _to_text(compute.get("status")) or "unknown",
        "Runtime": _to_text(runtime.get("status")) or "unknown",
        "Scheduler": _to_text(scheduler.get("status")) or "unknown",
        "Gateway": "running" if gateway.get("running") or gateway.get("reachable") else "offline",
        "Session": "authenticated" if session.get("authenticated") else "pending",
        "WebSocket": "connected" if websocket.get("connected") or websocket.get("ready") else "offline",
        "DispatchLag": (
            f"{float(scheduler.get('dispatch_lag_min') or 0):.2f}m"
            if scheduler.get("latest_ingested_bar_time_ms")
            else "awaiting bars"
        ),
        "问题码": ", ".join(list(snapshot.get("issue_codes") or [])[:6]) or "none",
        "今日bars": str(_to_int(today.get("ibkr_bars"), 0)),
        "今日signals": str(_to_int(today.get("ibkr_signals"), 0)),
        "今日orders": str(_to_int(today.get("orders"), 0)),
        "日筛状态": _to_text(daily_scan.get("status")) or "unknown",
        "故障域统计": ", ".join(f"{key}:{value}" for key, value in sorted(counts.items())) if counts else "n/a",
    }
    last_bar_us = _to_text(_as_dict(runtime.get("latest_bar")).get("us_time"))
    if last_bar_us:
        detail["最新5m"] = last_bar_us
    return detail


def build_system_heartbeat_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    build_system_summary_payload: BuildSystemSummaryPayload,
    build_system_monitor_payload: BuildSystemMonitorPayload,
    emit_system_event: EmitSystemEvent,
    get_state_payload: GetStatePayload,
    upsert_state: UpsertState,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    times = time_strings()
    state = _as_dict(get_state_payload(HEARTBEAT_STATE_KEY, environment).get("data"))
    snapshot = _runtime_health_snapshot(
        environment=environment,
        build_system_summary_payload=build_system_summary_payload,
        build_system_monitor_payload=build_system_monitor_payload,
    )
    fingerprint = _heartbeat_fingerprint(snapshot)
    current_ms = _to_int(datetime.now(timezone.utc).timestamp() * 1000, 0)
    issue_event: dict[str, Any] = {}
    next_state = {
        **state,
        "last_checked_at": times["us"],
        "last_monitor_status": _to_text(snapshot.get("monitor_status")),
        "last_summary_status": _to_text(snapshot.get("summary_status")),
        "last_issue_codes": list(snapshot.get("issue_codes") or []),
    }
    last_issue_hash = _to_text(state.get("last_issue_hash"))
    last_issue_ms = _to_int(state.get("last_issue_ms"), 0)
    current_hour = _to_text(times.get("us"))[:13]

    if snapshot.get("unhealthy"):
        should_notify = fingerprint != last_issue_hash or last_issue_ms <= 0 or (current_ms - last_issue_ms) >= HEARTBEAT_ALERT_COOLDOWN_MS
        next_state.update(
            {
                "last_issue_hash": fingerprint,
                "last_issue_ms": current_ms,
                "last_issue_at": times["us"],
                "last_recovery_at": "",
            }
        )
        if should_notify:
            issue_event = emit_system_event(
                event_type="heartbeat",
                level=_to_text(snapshot.get("severity")) or "warning",
                source="ibkr-api",
                title="IBKR 系统心跳异常",
                detail=_heartbeat_detail(snapshot, timestamp_us=times["us"]),
                environment=environment,
            )
    else:
        had_issue = bool(last_issue_hash)
        next_state.update(
            {
                "last_issue_hash": "",
                "last_issue_ms": 0,
                "last_issue_at": "",
            }
        )
        if had_issue:
            issue_event = emit_system_event(
                event_type="alert",
                level="info",
                source="ibkr-api",
                title="IBKR 系统状态已恢复",
                detail=_heartbeat_detail(snapshot, timestamp_us=times["us"]),
                environment=environment,
            )
            next_state["last_recovery_at"] = times["us"]
        elif _to_text(times.get("us"))[14:16] == "00" and _to_text(state.get("last_ok_hour")) != current_hour:
            issue_event = emit_system_event(
                event_type="heartbeat",
                level="info",
                source="ibkr-api",
                title="IBKR 系统心跳（native）",
                detail=_heartbeat_detail(snapshot, timestamp_us=times["us"]),
                environment=environment,
            )
            next_state["last_ok_hour"] = current_hour

    upsert_state(HEARTBEAT_STATE_KEY, environment, next_state, times["date"])
    return {
        "ok": True,
        "environment": environment,
        "job_id": "system_heartbeat",
        "unhealthy": bool(snapshot.get("unhealthy")),
        "severity": _to_text(snapshot.get("severity")) or "warning",
        "summary_status": _to_text(snapshot.get("summary_status")) or "unknown",
        "monitor_status": _to_text(snapshot.get("monitor_status")) or "unknown",
        "issue_codes": list(snapshot.get("issue_codes") or []),
        "event": issue_event,
        "state": next_state,
        "source": "ibkr-api",
    }, 200


def build_system_status_reminder_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    build_system_summary_payload: BuildSystemSummaryPayload,
    build_system_monitor_payload: BuildSystemMonitorPayload,
    emit_system_event: EmitSystemEvent,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    times = time_strings()
    snapshot = _runtime_health_snapshot(
        environment=environment,
        build_system_summary_payload=build_system_summary_payload,
        build_system_monitor_payload=build_system_monitor_payload,
    )
    title = "IBKR 系统状态摘要"
    if snapshot.get("unhealthy"):
        title = "IBKR 系统状态摘要（需关注）"
    event = emit_system_event(
        event_type="status_change",
        level="warning" if snapshot.get("unhealthy") else "info",
        source="ibkr-api",
        title=title,
        detail=_heartbeat_detail(snapshot, timestamp_us=times["us"]),
        environment=environment,
    )
    return {
        "ok": True,
        "environment": environment,
        "job_id": "system_status_reminder",
        "summary_status": _to_text(snapshot.get("summary_status")) or "unknown",
        "monitor_status": _to_text(snapshot.get("monitor_status")) or "unknown",
        "issue_codes": list(snapshot.get("issue_codes") or []),
        "event": event,
        "source": "ibkr-api",
    }, 200


__all__ = [
    "HEARTBEAT_STATE_KEY",
    "build_system_heartbeat_response",
    "build_system_status_reminder_response",
]
