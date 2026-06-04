from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.system.jobs.market_session_text import market_session_detail_fields


HEARTBEAT_STATE_KEY = "system_notify_heartbeat"
HEARTBEAT_ALERT_COOLDOWN_MS = 30 * 60 * 1000
DEFAULT_MONITOR_SOURCE_DEBOUNCE_COUNT = 2
DEFAULT_MONITOR_SOURCE_DEBOUNCE_MS = 90 * 1000
MONITOR_SOURCE_DEBOUNCE_ISSUE_BASES = {"monitor", "monitor_builder_compute_monitor"}
DEFAULT_OPEN_REPORT_TIME_ET = "09:30"
DEFAULT_OPEN_REPORT_WINDOW_MINUTES = 10
DEFAULT_STATUS_REMINDER_ACTIVE_WINDOW_LIMIT = 50
ALERT_FLAG_SEVERITIES = {"warning", "error"}
CONNECTION_ISSUE_CODES = {"gateway_offline", "session_unauthenticated", "websocket_not_ready"}
DEGRADED_SERVICE_STATUSES = {"degraded", "warning"}
OFFLINE_SERVICE_STATUSES = {"offline", "error"}
PARTIAL_RECOVERY_ISSUE_BASES = CONNECTION_ISSUE_CODES | {"services_offline", "services_degraded", "runtime", "summary"}
TRUTHY_TEXT = {"1", "true", "yes", "on"}
FALSE_TEXT = {"0", "false", "no", "off", "disabled", "disable"}
BAR_PIPELINE_DISABLED_STATUSES = {"disabled", "disabled_tv_primary", "legacy_bar_pipeline_disabled"}
TV_PRIMARY_SUPPRESSED_ISSUE_BASES = {
    "no_active_targets",
    "no_execution_eligible_targets",
    "data_freshness_delayed",
    "data_freshness_offline",
    "stale_active_symbols",
}
IB_CLIENT_SERVICE_LABELS = (
    ("ibkr-runtime", "Runtime"),
    ("ibkr-compute", "Compute"),
    ("ibkr-api", "API"),
    ("ibkr-scheduler", "Scheduler"),
)

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildSystemSummaryPayload = Callable[..., dict[str, Any]]
BuildSystemMonitorPayload = Callable[[str], dict[str, Any]]
BuildTodayTargetsResponse = Callable[..., tuple[dict[str, Any], int]]
BuildActiveWindowProgressResponse = Callable[..., tuple[dict[str, Any], int]]
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


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(str(os.environ.get(name, "") or default).strip())
    except Exception:
        value = int(default)
    value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def _monitor_source_debounce_count() -> int:
    return _env_int(
        "IBKR_HEARTBEAT_MONITOR_SOURCE_DEBOUNCE_COUNT",
        DEFAULT_MONITOR_SOURCE_DEBOUNCE_COUNT,
        minimum=1,
        maximum=10,
    )


def _monitor_source_debounce_ms() -> int:
    return _env_int(
        "IBKR_HEARTBEAT_MONITOR_SOURCE_DEBOUNCE_SEC",
        DEFAULT_MONITOR_SOURCE_DEBOUNCE_MS // 1000,
        minimum=0,
        maximum=1800,
    ) * 1000


def _time_window_minutes(value: Any) -> int | None:
    text = _to_text(value)
    if len(text) < 5 or ":" not in text[:5]:
        return None
    try:
        hour, minute = text[:5].split(":", 1)
        return int(hour) * 60 + int(minute)
    except Exception:
        return None


def _matches_open_report_time_window(current_us: str, target_et: str, window_minutes: int) -> bool:
    current = _to_text(current_us)
    if len(current) < 16:
        return False
    current_minute = _time_window_minutes(current[11:16])
    target_minute = _time_window_minutes(target_et)
    if current_minute is None or target_minute is None:
        return False
    window = max(1, int(window_minutes or DEFAULT_OPEN_REPORT_WINDOW_MINUTES))
    return target_minute <= current_minute < target_minute + window


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _truthy(value: Any, *, default: bool = False) -> bool:
    text = _to_text(value).lower()
    if not text:
        return default
    return text in TRUTHY_TEXT


def _false_text(value: Any) -> bool:
    return _to_text(value).lower() in FALSE_TEXT


def _bar_pipeline_candidate_disabled(candidate: dict[str, Any]) -> bool:
    status = _to_text(candidate.get("status") or candidate.get("bar_pipeline_status")).lower()
    reason = _to_text(candidate.get("reason") or candidate.get("bar_pipeline_reason")).lower()
    if status in BAR_PIPELINE_DISABLED_STATUSES or reason == "legacy_bar_pipeline_disabled":
        return True
    if candidate.get("enabled") is False or candidate.get("legacy_bar_pipeline_enabled") is False:
        return True
    return _false_text(candidate.get("enabled")) or _false_text(candidate.get("legacy_bar_pipeline_enabled"))


def _bar_pipeline_disabled(snapshot_payload: dict[str, Any]) -> bool:
    runtime = _as_dict(snapshot_payload.get("runtime"))
    monitor = _as_dict(snapshot_payload.get("monitor"))
    monitor_runtime = _as_dict(monitor.get("runtime"))
    candidates = [
        runtime.get("bar_pipeline"),
        _as_dict(runtime.get("data_backfill")).get("bar_pipeline"),
        runtime.get("data_backfill"),
        _as_dict(runtime.get("data_writer")).get("bar_pipeline"),
        runtime.get("data_writer"),
        monitor_runtime.get("bar_pipeline"),
        _as_dict(monitor_runtime.get("data_backfill")).get("bar_pipeline"),
        monitor_runtime.get("data_backfill"),
    ]
    return any(_bar_pipeline_candidate_disabled(_as_dict(candidate)) for candidate in candidates if isinstance(candidate, dict))


def _filter_tv_primary_legacy_flags(flags: list[dict[str, Any]], snapshot_payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not _bar_pipeline_disabled(snapshot_payload):
        return flags
    return [
        item
        for item in flags
        if _issue_base_code(item.get("code")) not in TV_PRIMARY_SUPPRESSED_ISSUE_BASES
    ]


def _today_order_count(today: dict[str, Any]) -> int:
    if "main_orders" in today:
        return _to_int(today.get("main_orders"), 0)
    if "order_groups" in today:
        return _to_int(today.get("order_groups"), 0)
    return _to_int(today.get("orders"), 0)


def _normalized_status(value: Any) -> str:
    return _to_text(value).lower() or "unknown"


def _is_alert_flag(flag: dict[str, Any]) -> bool:
    return _normalized_status(flag.get("severity")) in ALERT_FLAG_SEVERITIES


def _issue_base_code(value: Any) -> str:
    return _to_text(value).split(":", 1)[0]


def _issue_code_set(snapshot: dict[str, Any]) -> set[str]:
    return {_issue_base_code(item) for item in snapshot.get("issue_codes") or [] if _issue_base_code(item)}


def _issue_bases_from_codes(codes: Any) -> set[str]:
    return {_issue_base_code(item) for item in codes or [] if _issue_base_code(item)}


def _recovered_issue_codes(previous_codes: Any, current_codes: Any) -> list[str]:
    previous_items = [_to_text(item) for item in previous_codes or [] if _to_text(item)]
    current_bases = _issue_bases_from_codes(current_codes)
    recovered_bases = {
        _issue_base_code(item)
        for item in previous_items
        if _issue_base_code(item) in PARTIAL_RECOVERY_ISSUE_BASES and _issue_base_code(item) not in current_bases
    }
    recovered: list[str] = []
    for item in previous_items:
        if _issue_base_code(item) in recovered_bases and item not in recovered:
            recovered.append(item)
    return recovered


def _append_unique(items: list[str], value: str) -> None:
    text = _to_text(value)
    if text and text not in items:
        items.append(text)


def _join_human(items: list[str], fallback: str) -> str:
    return "；".join(item for item in items if _to_text(item)) or fallback


def _join_limited(items: list[str], *, limit: int = 12, fallback: str = "无") -> str:
    cleaned = [item for item in items if _to_text(item)]
    if not cleaned:
        return fallback
    shown = cleaned[:limit]
    if len(cleaned) > len(shown):
        shown.append(f"另有 {len(cleaned) - len(shown)} 个")
    return "、".join(shown)


def _service_status_items(services: dict[str, Any], statuses: set[str]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for name, raw in sorted(services.items()):
        service = _as_dict(raw)
        status = _normalized_status(service.get("status"))
        if status not in statuses:
            continue
        items.append(
            {
                "name": _to_text(name),
                "status": status,
                "detail": _to_text(service.get("detail") or service.get("reason")),
                "fault_domain": _to_text(service.get("fault_domain")),
            }
        )
    return items


def _format_service_items(items: list[dict[str, str]], *, limit: int = 3) -> str:
    parts: list[str] = []
    for item in items[:limit]:
        name = _to_text(item.get("name"))
        status = _to_text(item.get("status"))
        detail = _to_text(item.get("detail"))
        label = f"{name} {status}".strip()
        parts.append(f"{label} ({detail})" if detail else label)
    if len(items) > limit:
        parts.append(f"另有 {len(items) - limit} 项")
    return "；".join(parts)


def _service_counts_line(service_monitor: dict[str, Any]) -> str:
    services = _as_dict(service_monitor.get("services"))
    counts = _as_dict(service_monitor.get("status_counts"))
    parts = []
    if services:
        parts.append(f"total {len(services)}")
    parts.extend(
        f"{_to_text(status).lower()}:{_to_int(count, 0)}"
        for status, count in sorted(counts.items())
        if _to_int(count, 0) > 0
    )
    return " | ".join(parts) or "n/a"


def _service_client_id(service: dict[str, Any]) -> int:
    return _to_int(
        service.get("ib_gateway_client_id")
        or service.get("broker_client_id")
        or service.get("client_id"),
        0,
    )


def _ib_client_ids_line(services: dict[str, Any]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for service_key, label in IB_CLIENT_SERVICE_LABELS:
        service = _as_dict(services.get(service_key))
        client_id = _service_client_id(service)
        if not client_id:
            continue
        marker = f"{label}:{client_id}"
        if marker in seen:
            continue
        seen.add(marker)
        parts.append(f"{label} client {client_id}")
    return " | ".join(parts)


def _actionable_degraded_services(
    degraded_services: list[dict[str, str]],
    *,
    alert_flags: list[dict[str, Any]],
    monitor_status: str,
    summary_status: str,
) -> list[dict[str, str]]:
    if not degraded_services:
        return []
    if alert_flags or monitor_status not in {"ok", "running"} or summary_status not in {"ok", "running"}:
        return degraded_services
    # Compute can report a transient engine-count mismatch before daily targets are ready.
    # If the monitor is otherwise nominal, keep it visible on the page but do not page.
    return [item for item in degraded_services if _to_text(item.get("name")) != "ibkr-compute"]


def _runtime_health_snapshot(
    *,
    environment: str,
    build_system_summary_payload: BuildSystemSummaryPayload,
    build_system_monitor_payload: BuildSystemMonitorPayload,
) -> dict[str, Any]:
    summary = _as_dict(build_system_summary_payload(environment, lite_mode=True))
    monitor = _as_dict(build_system_monitor_payload(environment))
    monitor_runtime = _as_dict(monitor.get("runtime"))
    runtime = {**_as_dict(summary.get("ibkr_runtime")), **monitor_runtime}
    compute = _as_dict(summary.get("ibkr_compute") or monitor.get("compute"))
    scheduler = _as_dict(monitor.get("scheduler"))
    service_monitor = _as_dict(monitor.get("service_monitor"))
    services = _as_dict(service_monitor.get("services"))
    upstream_monitor = _as_dict(monitor.get("upstream_monitor"))
    monitor_source_unavailable = bool(monitor.get("monitor_source_unavailable") or upstream_monitor.get("source_unavailable"))
    flags = [
        _as_dict(item)
        for item in (monitor.get("flags") or [])
        if isinstance(item, dict) and _to_text(item.get("code"))
    ]
    flags = _filter_tv_primary_legacy_flags(flags, {"runtime": runtime, "monitor": monitor})
    alert_flags = [item for item in flags if _is_alert_flag(item)]
    counts = _as_dict(service_monitor.get("status_counts"))
    degraded_count = _to_int(counts.get("degraded"), 0) + _to_int(counts.get("warning"), 0)
    offline_count = _to_int(counts.get("offline"), 0) + _to_int(counts.get("error"), 0)
    degraded_services = _service_status_items(services, DEGRADED_SERVICE_STATUSES)
    offline_services = _service_status_items(services, OFFLINE_SERVICE_STATUSES)
    session = _as_dict(runtime.get("session"))
    websocket = _as_dict(runtime.get("websocket"))
    gateway = _as_dict(runtime.get("gateway"))
    connection_snapshot_available = bool(
        monitor_runtime
        or "gateway" in runtime
        or "session" in runtime
        or "websocket" in runtime
    )
    daily_scan = _as_dict(runtime.get("daily_scan") or summary.get("daily_scan"))
    today = _as_dict(summary.get("today"))
    issue_codes: list[str] = []
    for flag in [item for item in flags if _is_alert_flag(item)][:8]:
        issue_codes.append(_to_text(flag.get("code")))
    monitor_status = _normalized_status(monitor.get("status"))
    summary_status = _normalized_status(summary.get("status"))
    runtime_status = _normalized_status(runtime.get("status"))
    if _bar_pipeline_disabled({"runtime": runtime, "monitor": monitor}) and monitor_status == "warning" and not alert_flags:
        monitor_status = "ok"
    actionable_degraded_services = _actionable_degraded_services(
        degraded_services,
        alert_flags=alert_flags,
        monitor_status=monitor_status,
        summary_status=summary_status,
    )
    actionable_degraded_count = len(actionable_degraded_services)
    if degraded_count > 0 and not degraded_services and (
        alert_flags or monitor_status not in {"ok", "running"} or summary_status not in {"ok", "running"}
    ):
        actionable_degraded_count = degraded_count
    if monitor_status not in {"ok", "running"}:
        issue_codes.append(f"monitor:{monitor_status}")
    if summary_status not in {"ok", "running"}:
        issue_codes.append(f"summary:{summary_status}")
    if runtime_status not in {"ok", "running"}:
        issue_codes.append(f"runtime:{runtime_status}")
    if connection_snapshot_available:
        if not bool(session.get("authenticated")):
            issue_codes.append("session_unauthenticated")
        if not bool(websocket.get("connected") or websocket.get("ready")):
            issue_codes.append("websocket_not_ready")
        if not bool(gateway.get("running") or gateway.get("reachable")):
            issue_codes.append("gateway_offline")
    if actionable_degraded_count > 0:
        issue_codes.append(f"services_degraded:{actionable_degraded_count}")
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
    if (monitor_status in {"offline", "error"} and not monitor_source_unavailable) or offline_count > 0:
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
        "degraded_services": degraded_services,
        "actionable_degraded_services": actionable_degraded_services,
        "offline_services": offline_services,
        "flags": flags,
        "today": today,
        "daily_scan": daily_scan,
        "session": session,
        "websocket": websocket,
        "gateway": gateway,
        "connection_snapshot_available": connection_snapshot_available,
        "monitor_source_unavailable": monitor_source_unavailable,
        "summary_status": summary_status,
        "monitor_status": monitor_status,
        "runtime_status": runtime_status,
        "unhealthy": unhealthy,
        "severity": severity,
        "issue_codes": deduped_issue_codes,
    }


def _heartbeat_fingerprint(snapshot: dict[str, Any]) -> str:
    scheduler = _as_dict(snapshot.get("scheduler"))
    upstream_monitor = _as_dict(_as_dict(snapshot.get("monitor")).get("upstream_monitor"))
    return str(
        {
            "monitor_status": snapshot.get("monitor_status"),
            "summary_status": snapshot.get("summary_status"),
            "runtime_status": snapshot.get("runtime_status"),
            "monitor_source_unavailable": bool(snapshot.get("monitor_source_unavailable")),
            "upstream_monitor_status_code": _to_int(upstream_monitor.get("status_code"), 0),
            "upstream_monitor_target": _to_text(upstream_monitor.get("target_url")),
            "issue_codes": list(snapshot.get("issue_codes") or []),
            "dispatch_lag_min": round(float(scheduler.get("dispatch_lag_min") or 0.0), 2),
        }
    )


def _monitor_source_debounce_hash(snapshot: dict[str, Any]) -> str:
    upstream_monitor = _as_dict(_as_dict(snapshot.get("monitor")).get("upstream_monitor"))
    return str(
        {
            "monitor_source_unavailable": bool(snapshot.get("monitor_source_unavailable")),
            "monitor_status": snapshot.get("monitor_status"),
            "upstream_monitor_status_code": _to_int(upstream_monitor.get("status_code"), 0),
            "upstream_monitor_target": _to_text(upstream_monitor.get("target_url")),
            "issue_codes": [
                _to_text(item)
                for item in snapshot.get("issue_codes") or []
                if _issue_base_code(item) in MONITOR_SOURCE_DEBOUNCE_ISSUE_BASES
            ],
        }
    )


def _should_debounce_monitor_source(snapshot: dict[str, Any]) -> bool:
    if not bool(snapshot.get("monitor_source_unavailable")):
        return False
    bases = _issue_code_set(snapshot)
    return bool(bases) and bases.issubset(MONITOR_SOURCE_DEBOUNCE_ISSUE_BASES)


def _monitor_source_debounce_snapshot(
    *,
    snapshot: dict[str, Any],
    state: dict[str, Any],
    current_ms: int,
    timestamp_us: str,
) -> tuple[bool, dict[str, Any]]:
    if not _should_debounce_monitor_source(snapshot):
        return False, {}

    threshold_count = _monitor_source_debounce_count()
    threshold_ms = _monitor_source_debounce_ms()
    pending_hash = _monitor_source_debounce_hash(snapshot)
    previous_hash = _to_text(state.get("pending_monitor_source_hash"))
    previous_first_ms = _to_int(state.get("pending_monitor_source_first_ms"), 0)
    same_pending = bool(previous_hash) and previous_hash == pending_hash and previous_first_ms > 0
    first_ms = previous_first_ms if same_pending else current_ms
    first_at = _to_text(state.get("pending_monitor_source_first_at")) if same_pending else timestamp_us
    count = (_to_int(state.get("pending_monitor_source_count"), 0) if same_pending else 0) + 1
    elapsed_ms = max(0, current_ms - first_ms)
    suppressed = count < threshold_count and elapsed_ms < threshold_ms
    debounce = {
        "suppressed": suppressed,
        "count": count,
        "threshold_count": threshold_count,
        "first_ms": first_ms,
        "first_at": first_at,
        "last_ms": current_ms,
        "last_at": timestamp_us,
        "elapsed_ms": elapsed_ms,
        "threshold_ms": threshold_ms,
        "hash": pending_hash,
    }
    snapshot["monitor_debounce"] = debounce
    return suppressed, debounce


def _apply_monitor_source_debounce_state(next_state: dict[str, Any], debounce: dict[str, Any]) -> None:
    if debounce:
        next_state.update(
            {
                "pending_monitor_source_hash": _to_text(debounce.get("hash")),
                "pending_monitor_source_first_ms": _to_int(debounce.get("first_ms"), 0),
                "pending_monitor_source_first_at": _to_text(debounce.get("first_at")),
                "pending_monitor_source_last_ms": _to_int(debounce.get("last_ms"), 0),
                "pending_monitor_source_last_at": _to_text(debounce.get("last_at")),
                "pending_monitor_source_count": _to_int(debounce.get("count"), 0),
                "last_monitor_source_debounce_suppressed": bool(debounce.get("suppressed")),
            }
        )
        return
    for key in (
        "pending_monitor_source_hash",
        "pending_monitor_source_first_ms",
        "pending_monitor_source_first_at",
        "pending_monitor_source_last_ms",
        "pending_monitor_source_last_at",
        "pending_monitor_source_count",
    ):
        next_state.pop(key, None)
    next_state["last_monitor_source_debounce_suppressed"] = False


def _status_overview(snapshot: dict[str, Any]) -> tuple[str, str]:
    runtime = _as_dict(snapshot.get("runtime"))
    compute = _as_dict(snapshot.get("compute"))
    scheduler = _as_dict(snapshot.get("scheduler"))
    services = _as_dict(snapshot.get("services"))
    session = _as_dict(snapshot.get("session"))
    websocket = _as_dict(snapshot.get("websocket"))
    gateway = _as_dict(snapshot.get("gateway"))
    service_parts = [
        f"Compute {_to_text(compute.get('status')) or 'unknown'}",
        f"Runtime {_to_text(runtime.get('status')) or 'unknown'}",
        f"Scheduler {_to_text(scheduler.get('status')) or 'unknown'}",
    ]
    service_line = " | ".join(service_parts)
    if not bool(snapshot.get("connection_snapshot_available", True)):
        connection_line = "Gateway unknown | Session unknown | WebSocket unknown"
    else:
        connection_line = " | ".join(
            [
                f"Gateway {'running' if gateway.get('running') or gateway.get('reachable') else 'offline'}",
                f"Session {'authenticated' if session.get('authenticated') else 'pending'}",
                f"WebSocket {'connected' if websocket.get('connected') or websocket.get('ready') else 'offline'}",
            ]
        )
    return service_line, connection_line


def _human_issue_detail(snapshot: dict[str, Any]) -> dict[str, str]:
    codes = _issue_code_set(snapshot)
    impacts: list[str] = []
    reasons: list[str] = []
    advice: list[str] = []
    connection_issue = bool(codes & CONNECTION_ISSUE_CODES)
    monitor_source_unavailable = bool(snapshot.get("monitor_source_unavailable"))

    if monitor_source_unavailable:
        upstream = _as_dict(_as_dict(snapshot.get("monitor")).get("upstream_monitor"))
        _append_unique(impacts, "监控聚合暂时无法确认完整 IBKR 链路状态")
        _append_unique(reasons, f"监控源请求失败或超时: {_to_text(upstream.get('error')) or 'compute_monitor_unavailable'}")
        _append_unique(advice, "等待下一轮检测；若连续超时，再检查 ibkr-compute / ibkr-runtime monitor 耗时")

    if "gateway_offline" in codes:
        _append_unique(impacts, "实时行情、信号生成和自动下单会暂停")
        _append_unique(reasons, "IB Gateway/TWS 不可达")
        _append_unique(advice, "等待自动恢复；若持续 5-10 分钟，检查 Gateway 进程、登录状态和网络")
    if "session_unauthenticated" in codes:
        _append_unique(impacts, "IBKR 订阅与交易链路降级，可能需要 2FA")
        _append_unique(reasons, "IBKR 会话尚未认证")
        _append_unique(advice, "查看飞书 2FA 卡片或 IBKR 手机验证，完成后等待 Session 恢复")
    if "websocket_not_ready" in codes:
        _append_unique(impacts, "实时行情 WebSocket 暂不可用")
        _append_unique(reasons, "行情 WebSocket 未连接或未进入 ready")
        _append_unique(advice, "Gateway 和 Session 恢复后通常会自动重连")
    if "services_offline" in codes:
        offline_text = _format_service_items([_as_dict(item) for item in snapshot.get("offline_services") or []])
        _append_unique(impacts, "部分系统服务不可用")
        _append_unique(reasons, f"离线服务: {offline_text}" if offline_text else "至少一个服务处于 offline/error")
        _append_unique(advice, "打开系统状态页定位故障域，必要时重启对应服务")
    if "services_degraded" in codes:
        degraded_text = _format_service_items(
            [_as_dict(item) for item in snapshot.get("actionable_degraded_services") or snapshot.get("degraded_services") or []]
        )
        _append_unique(impacts, "部分服务降级，但主服务可能仍在运行")
        _append_unique(reasons, f"降级服务: {degraded_text}" if degraded_text else "至少一个服务处于 degraded/warning")
        _append_unique(advice, "查看系统状态页里的故障域统计和最近事件")

    for code in sorted(item for item in codes if item in {"monitor", "summary", "runtime"}):
        raw = next((_to_text(item) for item in snapshot.get("issue_codes") or [] if _issue_base_code(item) == code), code)
        _append_unique(reasons, f"{code} 状态异常（{raw}）")
        _append_unique(impacts, "控制面或运行态状态异常")
        _append_unique(advice, "查看系统状态页的 Monitor / Summary / Runtime 详情")

    known = CONNECTION_ISSUE_CODES | {"services_offline", "services_degraded", "monitor", "summary", "runtime"}
    alert_flags = [
        item for item in snapshot.get("flags") or []
        if _is_alert_flag(_as_dict(item)) and _issue_base_code(_as_dict(item).get("code")) not in known
    ]
    for flag in alert_flags[:3]:
        flag_dict = _as_dict(flag)
        title = _to_text(flag_dict.get("title") or flag_dict.get("code"))
        detail = _to_text(flag_dict.get("detail"))
        _append_unique(reasons, f"{title}: {detail}" if detail else title)
    if alert_flags:
        _append_unique(impacts, "监控阈值触发，相关链路可能降级")
        _append_unique(advice, "按触发项检查资源、订阅、数据新鲜度或回填节流")

    if not snapshot.get("unhealthy"):
        return {
            "结论": "系统与 IBKR 连接正常。",
            "影响": "未发现影响交易链路的问题",
            "原因": "服务、Session 与 WebSocket 均处于可用状态",
            "建议": "无需处理",
        }
    conclusion = "系统服务存活，但 IBKR 交易/行情链路未就绪。" if connection_issue else "发现系统状态异常，请按建议处理。"
    return {
        "结论": conclusion,
        "影响": _join_human(impacts, "影响范围待进一步确认"),
        "原因": _join_human(reasons, "检测到健康检查异常"),
        "建议": _join_human(advice, "打开系统状态页查看详情"),
    }


def _compact_issue_codes(snapshot: dict[str, Any]) -> str:
    codes = list(snapshot.get("issue_codes") or [])
    if not codes:
        return ""
    shown = [_to_text(item) for item in codes[:4] if _to_text(item)]
    suffix = " ..." if len(codes) > len(shown) else ""
    return ", ".join(shown) + suffix


def _heartbeat_title(snapshot: dict[str, Any], *, reminder: bool) -> str:
    if not snapshot.get("unhealthy"):
        return "IBKR 系统状态摘要" if reminder else "IBKR 系统心跳（native）"
    codes = _issue_code_set(snapshot)
    if codes & CONNECTION_ISSUE_CODES:
        return "IBKR 连接未就绪" if reminder else "IBKR 连接链路未就绪"
    return "IBKR 系统状态需关注" if reminder else "IBKR 系统心跳异常"


def _heartbeat_detail(snapshot: dict[str, Any], *, timestamp_us: str) -> dict[str, Any]:
    runtime = _as_dict(snapshot.get("runtime"))
    today = _as_dict(snapshot.get("today"))
    service_monitor = _as_dict(snapshot.get("service_monitor"))
    services = _as_dict(snapshot.get("services"))
    human = _human_issue_detail(snapshot)
    services_line, connection_line = _status_overview(snapshot)
    client_ids_line = _ib_client_ids_line(services)
    detail = {
        "检查时间": timestamp_us,
        "结论": human["结论"],
        "影响": human["影响"],
        "原因": human["原因"],
        "建议": human["建议"],
        "系统服务": services_line,
        "服务统计": _service_counts_line(service_monitor),
        "IBKR链路": connection_line,
        "状态": _to_text(snapshot.get("monitor_status") or snapshot.get("summary_status")) or "unknown",
        "TV webhook": f"webhooks {_to_int(today.get('tv_webhook_events'), 0)} | signals {_to_int(today.get('ibkr_signals'), 0)} | events {_to_int(today.get('events'), 0)}",
        "Targets": f"targets {_to_int(today.get('ibkr_targets'), 0)}",
        "Orders": f"orders {_today_order_count(today)} | positions {_to_int(today.get('positions'), 0)}",
        "Execution/Protection": (
            f"TP {_to_int(today.get('take_profit_filled'), 0)} | "
            f"SL {_to_int(today.get('stop_loss_filled'), 0)} | "
            f"protect_incomplete {_to_int(today.get('protection_incomplete'), 0)}"
        ),
    }
    detail.update(market_session_detail_fields(_as_dict(runtime.get("market_session"))))
    if client_ids_line:
        detail["IB ClientID"] = client_ids_line
    upstream_monitor = _as_dict(_as_dict(snapshot.get("monitor")).get("upstream_monitor"))
    if upstream_monitor.get("elapsed_ms") is not None:
        detail["检测耗时"] = f"{float(upstream_monitor.get('elapsed_ms') or 0):.1f}ms"
    if upstream_monitor.get("timeout_s") is not None:
        detail["检测超时阈值"] = f"{float(upstream_monitor.get('timeout_s') or 0):.1f}s"
    if snapshot.get("monitor_source_unavailable"):
        detail["监控源"] = "compute_monitor unavailable"
    debounce = _as_dict(snapshot.get("monitor_debounce"))
    if debounce:
        detail["是否去抖中"] = "yes" if debounce.get("suppressed") else "no"
        detail["连续异常次数"] = str(_to_int(debounce.get("count"), 0))
        detail["去抖窗口"] = f"{_to_int(debounce.get('elapsed_ms'), 0) / 1000:.1f}s/{_to_int(debounce.get('threshold_ms'), 0) / 1000:.1f}s"
    if snapshot.get("unhealthy"):
        offline_text = _format_service_items([_as_dict(item) for item in snapshot.get("offline_services") or []])
        degraded_text = _format_service_items([_as_dict(item) for item in snapshot.get("actionable_degraded_services") or []])
        abnormal_services = "；".join(item for item in [offline_text, degraded_text] if item)
        if abnormal_services:
            detail["异常服务"] = abnormal_services
    compact_codes = _compact_issue_codes(snapshot)
    if compact_codes:
        detail["诊断码"] = compact_codes
    return detail


def _partial_recovery_detail(
    snapshot: dict[str, Any],
    *,
    timestamp_us: str,
    recovered_codes: list[str],
    remaining_codes: list[str],
) -> dict[str, Any]:
    detail = _heartbeat_detail(snapshot, timestamp_us=timestamp_us)
    detail["结论"] = "部分系统故障已恢复，仍有项目需要关注。"
    detail["已恢复诊断码"] = ", ".join(recovered_codes) or "n/a"
    detail["仍存在诊断码"] = ", ".join(remaining_codes) or "无"
    detail["建议"] = "已恢复项无需重复处理；继续关注仍存在的诊断码。"
    return detail


def _direction_label(value: Any) -> str:
    direction = _to_text(value).lower()
    return {"long": "多", "short": "空", "neutral": "中性"}.get(direction, direction)


def _signal_status_label(value: Any) -> str:
    status = _to_text(value).lower()
    return {
        "awaiting_confirm": "待确认",
        "pending": "待执行",
        "submitted": "已提交",
        "submitted_waiting_fill": "已提交待成交",
        "protected_active": "持仓保护中",
        "filled_repricing_protection": "保护单重定价中",
        "filled_position": "持仓已建立",
        "protection_incomplete": "保护不完整",
        "protection_reprice_failed": "保护重定价失败",
        "entry_missed_limit_cap": "入场未成交",
        "executed": "已执行",
        "expired": "已过期",
        "rejected": "已拒绝",
        "closed": "已平仓",
    }.get(status, status)


def _target_label(item: dict[str, Any], *, include_signal_status: bool = False) -> str:
    symbol = _to_text(item.get("symbol")).upper()
    if not symbol:
        return ""
    parts: list[str] = []
    direction = _direction_label(item.get("direction_bias"))
    if direction:
        parts.append(direction)
    if include_signal_status:
        status = _signal_status_label(item.get("latest_signal_status"))
        if status:
            parts.append(status)
    return f"{symbol}({','.join(parts)})" if parts else symbol


def _target_signal_summary(targets_payload: dict[str, Any]) -> dict[str, str]:
    summary = _as_dict(targets_payload.get("summary"))
    items = [_as_dict(item) for item in targets_payload.get("items") or [] if isinstance(item, dict)]
    trading_items = [item for item in items if _to_text(item.get("symbol"))]
    expired_items = [item for item in items if _to_text(item.get("latest_signal_status")).lower() == "expired"]
    missed_items = [item for item in items if _to_text(item.get("latest_signal_status")).lower() == "entry_missed_limit_cap"]
    signaled_items = [item for item in trading_items if bool(item.get("has_signal_today"))]
    action_items = [
        item
        for item in trading_items
        if _to_text(item.get("latest_signal_status")).lower()
        in {
            "awaiting_confirm",
            "pending",
            "submitted",
            "submitted_waiting_fill",
            "protection_incomplete",
            "protection_reprice_failed",
        }
    ]
    return {
        "TV signals": (
            f"signals {_to_int(summary.get('signaled_count'), 0)} | "
            f"awaiting {_to_int(summary.get('awaiting_confirm_count'), 0)} | "
            f"pending {_to_int(summary.get('pending_count'), 0)} | "
            f"submitted {_to_int(summary.get('submitted_count'), 0)} | "
            f"expired {len(expired_items)} | "
            f"missed {_to_int(summary.get('entry_missed_count'), len(missed_items))}"
        ),
        "Targets": (
            f"total {_to_int(summary.get('total'), len(trading_items))} | "
            f"active {_to_int(summary.get('active_count'), 0)} | "
            f"candidate {_to_int(summary.get('candidate_count'), 0)} | "
            f"execution_eligible {_to_int(summary.get('execution_eligible_count'), _to_int(summary.get('operable_count'), 0))} | "
            f"observe {_to_int(summary.get('observe_only_count'), 0)} | "
            f"watch {_to_int(summary.get('watch_only_count'), 0)}"
        ),
        "Execution/Protection": (
            f"executed {_to_int(summary.get('executed_count'), 0)} | "
            f"submitted {_to_int(summary.get('submitted_count'), 0)} | "
            f"protected {_to_int(summary.get('protected_active_count'), 0)} | "
            f"protect_incomplete {_to_int(summary.get('protection_incomplete_count'), 0)}"
        ),
        "TV信号标的": _join_limited([_target_label(item, include_signal_status=True) for item in signaled_items]),
        "待处理标的": _join_limited([_target_label(item, include_signal_status=True) for item in action_items]),
    }


def _window_status_value(item: dict[str, Any]) -> str:
    return _to_text(item.get("window_status") or item.get("status")).lower()


def _window_trace_error(item: dict[str, Any]) -> str:
    return _to_text(item.get("trace_error") or item.get("error"))


def _window_count(
    summary: dict[str, Any],
    key: str,
    items: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> int:
    return max(_to_int(summary.get(key), 0), sum(1 for item in items if predicate(item)))


def _attention_reason(value: Any, *, max_len: int = 40) -> str:
    text = _to_text(value)
    if len(text) <= max_len:
        return text
    return f"{text[:max_len - 3]}..."


def _execution_attention_item(item: dict[str, Any], target: dict[str, Any]) -> bool:
    status = _window_status_value(item)
    if status in {"blocked", "near_expiry"} or _window_trace_error(item):
        return True
    return bool(target.get("is_execution_eligible")) and not bool(target.get("has_signal_today"))


def _execution_attention_label(item: dict[str, Any], target: dict[str, Any]) -> str:
    symbol = _to_text(item.get("symbol")).upper()
    if not symbol:
        return ""
    status = _window_status_value(item)
    parts: list[str] = []
    status_label = {
        "blocked": "受阻",
        "near_expiry": "需复核",
        "no_window": "待TV触发",
        "used": "已处理",
        "expired": "已过期",
        "candidate": "候选",
        "confirmed": "已确认",
    }.get(status)
    if status_label:
        parts.append(status_label)
    if bool(target.get("is_execution_eligible")) and not bool(target.get("has_signal_today")):
        parts.append("execution_eligible待TV")
    blocked_reason = _attention_reason(item.get("blocked_reason") or item.get("filter_reason"))
    if blocked_reason:
        parts.append(blocked_reason)
    trace_error = _attention_reason(_window_trace_error(item))
    if trace_error:
        parts.append(f"trace_error:{trace_error}")
    return f"{symbol}({','.join(parts)})" if parts else symbol


def _active_window_summary(active_window_payload: dict[str, Any], targets_payload: dict[str, Any]) -> dict[str, str]:
    summary = _as_dict(active_window_payload.get("summary"))
    window_items = [_as_dict(item) for item in active_window_payload.get("items") or [] if isinstance(item, dict)]
    window_by_symbol = {_to_text(item.get("symbol")).upper(): item for item in window_items if _to_text(item.get("symbol"))}
    target_items = [_as_dict(item) for item in targets_payload.get("items") or [] if isinstance(item, dict)]
    target_by_symbol = {_to_text(item.get("symbol")).upper(): item for item in target_items if _to_text(item.get("symbol"))}
    target_symbols = [_to_text(item.get("symbol")).upper() for item in target_items if _to_text(item.get("symbol"))]
    symbols = [symbol for symbol in target_symbols if symbol] or list(window_by_symbol)
    attention_items: list[dict[str, Any]] = []
    for symbol in symbols:
        item = window_by_symbol.get(symbol) or {"symbol": symbol, "window_status": "no_window"}
        target = target_by_symbol.get(symbol) or {}
        if _execution_attention_item(item, target):
            attention_items.append(item)

    blocked_count = _window_count(
        summary,
        "blocked_count",
        window_items,
        lambda item: _window_status_value(item) == "blocked",
    )
    near_expiry_count = _window_count(
        summary,
        "near_expiry_count",
        window_items,
        lambda item: _window_status_value(item) == "near_expiry",
    )
    trace_error_count = _window_count(
        summary,
        "trace_error_count",
        window_items,
        lambda item: bool(_window_trace_error(item)),
    )
    candidate_count = _to_int(summary.get("current_candidate_signal_count"), _to_int(summary.get("candidate_signal_count"), 0))
    attention_count = max(len(attention_items), blocked_count + near_expiry_count + trace_error_count)
    if attention_count <= 0:
        return {}

    result = {
        "Execution关注": (
            f"candidate {candidate_count} | "
            f"deferred {blocked_count} | "
            f"review {near_expiry_count} | "
            f"trace_error {trace_error_count}"
        )
    }
    if attention_items:
        result["待复核标的"] = _join_limited(
            [
                _execution_attention_label(item, target_by_symbol.get(_to_text(item.get("symbol")).upper()) or {})
                for item in attention_items
            ]
        )
    return result


def _load_today_targets_payload(
    *,
    broker_mode: str,
    data_environment: str,
    market_date: str,
    build_today_targets_response: BuildTodayTargetsResponse | None,
) -> dict[str, Any]:
    if not callable(build_today_targets_response):
        return {}
    try:
        payload, _ = build_today_targets_response(
            payload={
                "environment": data_environment,
                "broker_mode": broker_mode,
                "market_data_mode": data_environment,
                "data_environment": data_environment,
                "market_date": market_date,
                "date": market_date,
                "per_page": 200,
                "page": 1,
                "paginate": False,
                "sort_by": "attention_asc",
            }
        )
    except Exception as exc:
        return {"summary": {}, "items": [], "error": f"targets_summary_error:{exc}"}
    return _as_dict(payload)


def _load_active_window_payload(
    *,
    broker_mode: str,
    data_environment: str,
    market_date: str,
    limit: int = DEFAULT_STATUS_REMINDER_ACTIVE_WINDOW_LIMIT,
    build_active_window_progress_response: BuildActiveWindowProgressResponse | None,
) -> dict[str, Any]:
    if not callable(build_active_window_progress_response):
        return {}
    bounded_limit = max(1, min(200, _to_int(limit, DEFAULT_STATUS_REMINDER_ACTIVE_WINDOW_LIMIT)))
    try:
        payload, _ = build_active_window_progress_response(
            payload={
                "environment": data_environment,
                "broker_mode": broker_mode,
                "market_data_mode": data_environment,
                "data_environment": data_environment,
                "market_date": market_date,
                "date": market_date,
                "status": "all",
                "interval": "5m",
                "limit": bounded_limit,
            }
        )
    except Exception as exc:
        return {"summary": {}, "items": [], "error": f"active_window_error:{exc}"}
    return _as_dict(payload)


def _enrich_status_detail_with_targets(
    detail: dict[str, Any],
    *,
    targets_payload: dict[str, Any],
    active_window_payload: dict[str, Any],
) -> dict[str, Any]:
    if targets_payload:
        detail.update(_target_signal_summary(targets_payload))
        if targets_payload.get("error"):
            detail["标的摘要错误"] = _to_text(targets_payload.get("error"))
    if active_window_payload:
        detail.update(_active_window_summary(active_window_payload, targets_payload))
        if active_window_payload.get("error"):
            detail["Execution摘要错误"] = _to_text(active_window_payload.get("error"))
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
    broker_mode = request_broker_mode(request_payload)
    data_environment = request_market_data_mode(request_payload)
    emit_nominal_ok = _truthy(request_payload.get("emit_nominal_ok"), default=False)
    times = time_strings()
    state = _as_dict(get_state_payload(HEARTBEAT_STATE_KEY, broker_mode).get("data"))
    snapshot = _runtime_health_snapshot(
        environment=broker_mode,
        build_system_summary_payload=build_system_summary_payload,
        build_system_monitor_payload=build_system_monitor_payload,
    )
    fingerprint = _heartbeat_fingerprint(snapshot)
    current_ms = _to_int(datetime.now(timezone.utc).timestamp() * 1000, 0)
    issue_event: dict[str, Any] = {}
    recovery_event: dict[str, Any] = {}
    current_issue_codes = list(snapshot.get("issue_codes") or [])
    previous_issue_codes = list(state.get("last_issue_codes") or [])
    next_state = {
        **state,
        "last_checked_at": times["us"],
        "last_monitor_status": _to_text(snapshot.get("monitor_status")),
        "last_summary_status": _to_text(snapshot.get("summary_status")),
        "last_issue_codes": current_issue_codes,
    }
    last_issue_hash = _to_text(state.get("last_issue_hash"))
    last_issue_ms = _to_int(state.get("last_issue_ms"), 0)
    current_hour = _to_text(times.get("us"))[:13]
    nominal_ok_suppressed = False
    event_suppressed_by_debounce, monitor_debounce = _monitor_source_debounce_snapshot(
        snapshot=snapshot,
        state=state,
        current_ms=current_ms,
        timestamp_us=times["us"],
    )
    _apply_monitor_source_debounce_state(next_state, monitor_debounce)

    if snapshot.get("unhealthy"):
        recovered_codes = (
            []
            if snapshot.get("monitor_source_unavailable")
            else _recovered_issue_codes(previous_issue_codes, current_issue_codes)
        )
        if recovered_codes:
            recovery_event = emit_system_event(
                event_type="alert",
                level="info",
                source="ibkr-api",
                title="IBKR 系统部分恢复",
                detail=_partial_recovery_detail(
                    snapshot,
                    timestamp_us=times["us"],
                    recovered_codes=recovered_codes,
                    remaining_codes=current_issue_codes,
                ),
                environment=broker_mode,
            )
            next_state["last_partial_recovery_at"] = times["us"]
            next_state["last_partial_recovery_codes"] = recovered_codes
        should_notify = fingerprint != last_issue_hash or last_issue_ms <= 0 or (current_ms - last_issue_ms) >= HEARTBEAT_ALERT_COOLDOWN_MS
        if not event_suppressed_by_debounce:
            next_state.update(
                {
                    "last_issue_hash": fingerprint,
                    "last_issue_ms": current_ms,
                    "last_issue_at": times["us"],
                    "last_recovery_at": "",
                }
            )
        if should_notify and not event_suppressed_by_debounce:
            issue_event = emit_system_event(
                event_type="heartbeat",
                level=_to_text(snapshot.get("severity")) or "warning",
                source="ibkr-api",
                title=_heartbeat_title(snapshot, reminder=False),
                detail=_heartbeat_detail(snapshot, timestamp_us=times["us"]),
                environment=broker_mode,
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
                environment=broker_mode,
            )
            next_state["last_recovery_at"] = times["us"]
        elif _to_text(times.get("us"))[14:16] == "00" and _to_text(state.get("last_ok_hour")) != current_hour:
            if emit_nominal_ok:
                issue_event = emit_system_event(
                    event_type="heartbeat",
                    level="info",
                    source="ibkr-api",
                    title="IBKR 系统心跳（native）",
                    detail=_heartbeat_detail(snapshot, timestamp_us=times["us"]),
                    environment=broker_mode,
                )
                next_state["last_ok_hour"] = current_hour
            else:
                nominal_ok_suppressed = True
                next_state["last_ok_suppressed_hour"] = current_hour

    upsert_state(HEARTBEAT_STATE_KEY, broker_mode, next_state, times["date"])
    return {
        "ok": True,
        "environment": broker_mode,
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        "data_environment": data_environment,
        "job_id": "system_heartbeat",
        "unhealthy": bool(snapshot.get("unhealthy")),
        "severity": _to_text(snapshot.get("severity")) or "warning",
        "summary_status": _to_text(snapshot.get("summary_status")) or "unknown",
        "monitor_status": _to_text(snapshot.get("monitor_status")) or "unknown",
        "issue_codes": current_issue_codes,
        "event": issue_event,
        "recovery_event": recovery_event,
        "partial_recovery": bool(recovery_event),
        "event_suppressed_by_debounce": event_suppressed_by_debounce,
        "monitor_debounce": monitor_debounce,
        "nominal_ok_suppressed": nominal_ok_suppressed,
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
    build_today_targets_response: BuildTodayTargetsResponse | None = None,
    build_active_window_progress_response: BuildActiveWindowProgressResponse | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    broker_mode = request_broker_mode(request_payload)
    data_environment = request_market_data_mode(request_payload)
    times = time_strings()
    if _matches_open_report_time_window(
        times.get("us", ""),
        _to_text(request_payload.get("open_report_time_et")) or DEFAULT_OPEN_REPORT_TIME_ET,
        _to_int(request_payload.get("open_report_window_minutes"), DEFAULT_OPEN_REPORT_WINDOW_MINUTES),
    ):
        return {
            "ok": True,
            "environment": broker_mode,
            "broker_mode": broker_mode,
            "market_data_mode": data_environment,
            "data_environment": data_environment,
            "job_id": "system_status_reminder",
            "skipped": True,
            "reason": "open_report_window",
            "source": "ibkr-api",
        }, 200
    snapshot = _runtime_health_snapshot(
        environment=broker_mode,
        build_system_summary_payload=build_system_summary_payload,
        build_system_monitor_payload=build_system_monitor_payload,
    )
    targets_payload = _load_today_targets_payload(
        broker_mode=broker_mode,
        data_environment=data_environment,
        market_date=times["date"],
        build_today_targets_response=build_today_targets_response,
    )
    active_window_payload = _load_active_window_payload(
        broker_mode=broker_mode,
        data_environment=data_environment,
        market_date=times["date"],
        limit=_to_int(
            request_payload.get("active_window_limit"),
            DEFAULT_STATUS_REMINDER_ACTIVE_WINDOW_LIMIT,
        ),
        build_active_window_progress_response=build_active_window_progress_response,
    )
    detail = _enrich_status_detail_with_targets(
        _heartbeat_detail(snapshot, timestamp_us=times["us"]),
        targets_payload=targets_payload,
        active_window_payload=active_window_payload,
    )
    title = _heartbeat_title(snapshot, reminder=True)
    event = emit_system_event(
        event_type="status_change",
        level="warning" if snapshot.get("unhealthy") else "info",
        source="ibkr-api",
        title=title,
        detail=detail,
        environment=broker_mode,
    )
    return {
        "ok": True,
        "environment": broker_mode,
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        "data_environment": data_environment,
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
