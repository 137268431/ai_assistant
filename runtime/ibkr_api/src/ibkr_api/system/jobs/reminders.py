from __future__ import annotations

from typing import Any, Callable


DAILY_REMINDER_STATE_KEY = "system_notify_daily"

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildSystemSummaryPayload = Callable[[str], dict[str, Any]]
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


def _summary_detail(summary: dict[str, Any], monitor: dict[str, Any], *, phase: str, timestamp_us: str) -> dict[str, Any]:
    compute = _as_dict(summary.get("ibkr_compute"))
    runtime = _as_dict(summary.get("ibkr_runtime"))
    runtime_inner = _as_dict(monitor.get("runtime"))
    scheduler = _as_dict(monitor.get("scheduler"))
    gateway = _as_dict(runtime_inner.get("gateway"))
    session = _as_dict(runtime_inner.get("session"))
    websocket = _as_dict(runtime_inner.get("websocket"))
    daily_scan = _as_dict(runtime_inner.get("daily_scan")) or _as_dict(summary.get("daily_scan"))
    services = _as_dict(_as_dict(monitor.get("service_monitor")).get("status_counts"))
    today = _as_dict(summary.get("today"))
    detail = {
        "阶段": phase,
        "检查时间": timestamp_us,
        "总体状态": _to_text(summary.get("status")) or "offline",
        "Compute": _to_text(compute.get("status")) or "offline",
        "Runtime": _to_text(runtime.get("status")) or "offline",
        "Scheduler": _to_text(scheduler.get("status")) or "offline",
        "Gateway": "running" if gateway.get("running") or gateway.get("reachable") else "offline",
        "Session": "authenticated" if session.get("authenticated") else "pending",
        "WebSocket": "connected" if websocket.get("connected") else "offline",
        "DispatchLag": f"{float(scheduler.get('dispatch_lag_min') or 0):.2f}m" if scheduler.get("latest_ingested_bar_time_ms") else "awaiting bars",
        "今日bars": str(_to_int(today.get("ibkr_bars"), 0)),
        "今日signals": str(_to_int(today.get("ibkr_signals"), 0)),
        "今日orders": str(_to_int(today.get("orders"), 0)),
        "今日events": str(_to_int(today.get("events"), 0)),
        "日筛状态": _to_text(daily_scan.get("status")) or "unknown",
        "日筛日期": _to_text(daily_scan.get("market_date")) or "",
        "故障域统计": ", ".join(f"{key}:{value}" for key, value in sorted(services.items())) if services else "n/a",
    }
    if _to_text(daily_scan.get("last_error")):
        detail["日筛错误"] = _to_text(daily_scan.get("last_error"))
    return detail


def build_system_market_open_reminder_response(
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
    current_state = _as_dict(get_state_payload(DAILY_REMINDER_STATE_KEY, environment).get("data"))
    if _to_text(current_state.get("open_sent_at")):
        return {"ok": True, "environment": environment, "skipped": True, "reason": "already_sent", "source": "ibkr-api", "job_id": "system_market_open_reminder"}, 200
    summary = build_system_summary_payload(environment)
    monitor = build_system_monitor_payload(environment)
    level = "warning" if _to_text(summary.get("status")).lower() not in {"running", "ok"} else "info"
    event_result = emit_system_event(
        event_type="status_change",
        level=level,
        source="ibkr_api",
        title="IBKR 开盘前系统检查",
        detail=_summary_detail(summary, monitor, phase="open", timestamp_us=times["us"]),
        environment=environment,
    )
    next_state = {
        **current_state,
        "open_sent_at": times["us"],
        "open_title": "IBKR 开盘前系统检查",
        "open_status": _to_text(summary.get("status")) or "offline",
    }
    upsert_state(DAILY_REMINDER_STATE_KEY, environment, next_state, times["date"])
    return {
        "ok": True,
        "environment": environment,
        "notified": bool(event_result.get("notified")),
        "state": next_state,
        "source": "ibkr-api",
        "job_id": "system_market_open_reminder",
    }, 200


def build_system_daily_report_response(
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
    current_state = _as_dict(get_state_payload(DAILY_REMINDER_STATE_KEY, environment).get("data"))
    if _to_text(current_state.get("close_sent_at")):
        return {"ok": True, "environment": environment, "skipped": True, "reason": "already_sent", "source": "ibkr-api", "job_id": "system_daily_report"}, 200
    summary = build_system_summary_payload(environment)
    monitor = build_system_monitor_payload(environment)
    event_result = emit_system_event(
        event_type="daily_report",
        level="info",
        source="ibkr_api",
        title="IBKR 收盘汇总",
        detail=_summary_detail(summary, monitor, phase="close", timestamp_us=times["us"]),
        environment=environment,
    )
    next_state = {
        **current_state,
        "close_sent_at": times["us"],
        "close_title": "IBKR 收盘汇总",
        "close_status": _to_text(summary.get("status")) or "offline",
    }
    upsert_state(DAILY_REMINDER_STATE_KEY, environment, next_state, times["date"])
    return {
        "ok": True,
        "environment": environment,
        "notified": bool(event_result.get("notified")),
        "state": next_state,
        "source": "ibkr-api",
        "job_id": "system_daily_report",
    }, 200
