from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.modes import request_market_data_mode


HEARTBEAT_STATE_KEY = "system_notify_heartbeat"
HEARTBEAT_ALERT_COOLDOWN_MS = 30 * 60 * 1000
DEFAULT_OPEN_REPORT_TIME_ET = "09:30"
DEFAULT_OPEN_REPORT_WINDOW_MINUTES = 10
ALERT_FLAG_SEVERITIES = {"warning", "error"}
CONNECTION_ISSUE_CODES = {"gateway_offline", "session_unauthenticated", "websocket_not_ready"}
DEGRADED_SERVICE_STATUSES = {"degraded", "warning"}
OFFLINE_SERVICE_STATUSES = {"offline", "error"}
PARTIAL_RECOVERY_ISSUE_BASES = CONNECTION_ISSUE_CODES | {"services_offline", "services_degraded", "runtime", "summary"}
TRUTHY_TEXT = {"1", "true", "yes", "on"}
IB_CLIENT_SERVICE_LABELS = (
    ("ibkr-runtime", "Runtime"),
    ("ibkr-compute", "Compute"),
    ("ibkr-api", "API"),
    ("ibkr-scheduler", "Scheduler"),
    ("ibkr-backtest", "Backtest"),
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


def _backtest_service_line(services: dict[str, Any]) -> str:
    service = _as_dict(services.get("ibkr-backtest"))
    if not service:
        return ""
    status = _to_text(service.get("status")) or "unknown"
    worker = _to_text(service.get("worker_status") or service.get("readiness_phase"))
    client_id = _to_int(service.get("ib_gateway_client_id"), 0)
    active_runs = _to_int(service.get("active_runs"), 0)
    queue_depth = _to_int(service.get("queue_depth"), 0)
    parts = [f"Backtest {status}"]
    if worker and worker.lower() != status.lower():
        parts.append(f"worker {worker}")
    if client_id:
        parts.append(f"client {client_id}")
    if active_runs or queue_depth:
        parts.append(f"active {active_runs} / queue {queue_depth}")
    return " | ".join(parts)


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
    runtime = {**_as_dict(summary.get("ibkr_runtime")), **_as_dict(monitor.get("runtime"))}
    compute = _as_dict(summary.get("ibkr_compute") or monitor.get("compute"))
    scheduler = _as_dict(monitor.get("scheduler"))
    service_monitor = _as_dict(monitor.get("service_monitor"))
    services = _as_dict(service_monitor.get("services"))
    flags = [
        _as_dict(item)
        for item in (monitor.get("flags") or [])
        if isinstance(item, dict) and _to_text(item.get("code"))
    ]
    alert_flags = [item for item in flags if _is_alert_flag(item)]
    counts = _as_dict(service_monitor.get("status_counts"))
    degraded_count = _to_int(counts.get("degraded"), 0) + _to_int(counts.get("warning"), 0)
    offline_count = _to_int(counts.get("offline"), 0) + _to_int(counts.get("error"), 0)
    degraded_services = _service_status_items(services, DEGRADED_SERVICE_STATUSES)
    offline_services = _service_status_items(services, OFFLINE_SERVICE_STATUSES)
    session = _as_dict(runtime.get("session"))
    websocket = _as_dict(runtime.get("websocket"))
    gateway = _as_dict(runtime.get("gateway"))
    daily_scan = _as_dict(runtime.get("daily_scan") or summary.get("daily_scan"))
    today = _as_dict(summary.get("today"))
    issue_codes: list[str] = []
    for flag in [item for item in flags if _is_alert_flag(item)][:8]:
        issue_codes.append(_to_text(flag.get("code")))
    monitor_status = _normalized_status(monitor.get("status"))
    summary_status = _normalized_status(summary.get("status"))
    runtime_status = _normalized_status(runtime.get("status"))
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
        "degraded_services": degraded_services,
        "actionable_degraded_services": actionable_degraded_services,
        "offline_services": offline_services,
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
    backtest_line = _backtest_service_line(services)
    if backtest_line:
        service_parts.append(backtest_line)
    service_line = " | ".join(service_parts)
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
    scheduler = _as_dict(snapshot.get("scheduler"))
    daily_scan = _as_dict(snapshot.get("daily_scan"))
    today = _as_dict(snapshot.get("today"))
    service_monitor = _as_dict(snapshot.get("service_monitor"))
    services = _as_dict(snapshot.get("services"))
    human = _human_issue_detail(snapshot)
    services_line, connection_line = _status_overview(snapshot)
    backtest_line = _backtest_service_line(services)
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
        "DispatchLag": (
            f"{float(scheduler.get('dispatch_lag_min') or 0):.2f}m"
            if scheduler.get("latest_ingested_bar_time_ms")
            else "awaiting bars"
        ),
        "数据": (
            f"bars {_to_int(today.get('ibkr_bars'), 0)} | "
            f"signals {_to_int(today.get('ibkr_signals'), 0)} | "
            f"orders {_today_order_count(today)} | "
            f"日筛 {_to_text(daily_scan.get('status')) or 'unknown'}"
        ),
    }
    if backtest_line:
        detail["Backtest"] = f"{backtest_line} | independent non-blocking"
    if client_ids_line:
        detail["IB ClientID"] = client_ids_line
    if snapshot.get("unhealthy"):
        offline_text = _format_service_items([_as_dict(item) for item in snapshot.get("offline_services") or []])
        degraded_text = _format_service_items([_as_dict(item) for item in snapshot.get("actionable_degraded_services") or []])
        abnormal_services = "；".join(item for item in [offline_text, degraded_text] if item)
        if abnormal_services:
            detail["异常服务"] = abnormal_services
    compact_codes = _compact_issue_codes(snapshot)
    if compact_codes:
        detail["诊断码"] = compact_codes
    last_bar_us = _to_text(_as_dict(runtime.get("latest_bar")).get("us_time"))
    if last_bar_us:
        detail["最新5m"] = last_bar_us
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
        "protected_active": "持仓保护中",
        "protection_incomplete": "保护不完整",
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


def _target_chain_label(item: dict[str, Any], *, include_signal_status: bool = False) -> str:
    symbol = _to_text(item.get("symbol")).upper()
    if not symbol:
        return ""
    parts: list[str] = []
    direction = _direction_label(item.get("direction_bias"))
    if direction:
        parts.append(direction)
    if include_signal_status:
        parts.append(_signal_status_label(item.get("latest_signal_status")) or "已触发")
    else:
        parts.append(_to_text(item.get("technical_state")) or "unknown")
    return f"{symbol}({','.join(parts)})" if parts else symbol


def _target_signal_summary(targets_payload: dict[str, Any]) -> dict[str, str]:
    summary = _as_dict(targets_payload.get("summary"))
    items = [_as_dict(item) for item in targets_payload.get("items") or [] if isinstance(item, dict)]
    trading_items = [item for item in items if _to_text(item.get("symbol"))]
    expired_items = [item for item in items if _to_text(item.get("latest_signal_status")).lower() == "expired"]
    no_signal_items = [item for item in items if not bool(item.get("has_signal_today"))]
    operable_waiting_items = [
        item for item in trading_items if bool(item.get("is_operable")) and not bool(item.get("has_signal_today"))
    ]
    ready_waiting_items = [
        item
        for item in trading_items
        if _to_text(item.get("technical_state")).lower() == "ready" and not bool(item.get("has_signal_today"))
    ]
    signaled_items = [item for item in trading_items if bool(item.get("has_signal_today"))]
    return {
        "今日标的": (
            f"total {_to_int(summary.get('total'), len(trading_items))} | "
            f"active {_to_int(summary.get('active_count'), 0)} | "
            f"candidate {_to_int(summary.get('candidate_count'), 0)} | "
            f"operable {_to_int(summary.get('operable_count'), 0)} | "
            f"ready {_to_int(summary.get('technical_ready_count'), 0)} | "
            f"signals {_to_int(summary.get('signaled_count'), 0)} | "
            f"awaiting {_to_int(summary.get('awaiting_confirm_count'), 0)} | "
            f"pending {_to_int(summary.get('pending_count'), 0)} | "
            f"submitted {_to_int(summary.get('submitted_count'), 0)} | "
            f"protected {_to_int(summary.get('protected_active_count'), 0)} | "
            f"protect_incomplete {_to_int(summary.get('protection_incomplete_count'), 0)} | "
            f"expired {len(expired_items)} | "
            f"no_signal {len(no_signal_items)}"
        ),
        "今日交易标的": _join_limited([_target_label(item, include_signal_status=True) for item in trading_items]),
        "已过期标的": _join_limited([_target_label(item, include_signal_status=True) for item in expired_items]),
        "未出信号标的": _join_limited([_target_label(item) for item in no_signal_items]),
        "标的链路": (
            f"可操作待信号 {len(operable_waiting_items)}: "
            f"{_join_limited([_target_chain_label(item) for item in operable_waiting_items])} | "
            f"技术就绪待信号 {len(ready_waiting_items)}: "
            f"{_join_limited([_target_chain_label(item) for item in ready_waiting_items])} | "
            f"已触发信号 {len(signaled_items)}: "
            f"{_join_limited([_target_chain_label(item, include_signal_status=True) for item in signaled_items])}"
        ),
    }


def _window_side_label(item: dict[str, Any]) -> str:
    upper_valid = bool(item.get("sd_upper_valid"))
    lower_valid = bool(item.get("sd_lower_valid"))
    if upper_valid and lower_valid:
        return "上下窗口"
    if upper_valid:
        return "上窗口"
    if lower_valid:
        return "下窗口"
    status = _to_text(item.get("window_status") or item.get("status")).lower()
    return {
        "upper_active": "上窗口",
        "lower_active": "下窗口",
        "both_active": "上下窗口",
        "near_expiry": "临近过期",
    }.get(status, status)


def _window_status_label(value: Any) -> str:
    status = _to_text(value).lower()
    return {
        "upper_active": "上窗口",
        "lower_active": "下窗口",
        "both_active": "上下窗口",
        "near_expiry": "临近过期",
        "used": "已使用",
        "expired": "已过期",
        "no_window": "无窗口",
        "blocked": "受阻",
        "candidate": "候选信号",
        "confirmed": "已确认",
    }.get(status, status or "未知")


def _window_item_label(item: dict[str, Any], *, active: bool) -> str:
    symbol = _to_text(item.get("symbol")).upper()
    if not symbol:
        return ""
    if active:
        side = _window_side_label(item)
        bars_remaining = _to_int(item.get("bars_remaining"), 0)
        return f"{symbol}({side},{bars_remaining} bars)" if bars_remaining > 0 else f"{symbol}({side})"
    return f"{symbol}({_window_status_label(item.get('window_status') or item.get('status'))})"


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


def _is_operable_waiting_target(item: dict[str, Any]) -> bool:
    return bool(item.get("is_operable")) and not bool(item.get("has_signal_today"))


def _active_window_item(item: dict[str, Any]) -> bool:
    status = _window_status_value(item)
    if (
        status == "blocked"
        or _to_text(item.get("trace_stage")).lower() == "blocked"
        or _to_text(item.get("blocked_reason"))
    ):
        return False
    return bool(item.get("sd_upper_valid") or item.get("sd_lower_valid")) or status in {
        "upper_active",
        "lower_active",
        "both_active",
        "near_expiry",
    }


def _window_reason(value: Any, *, max_len: int = 40) -> str:
    text = _to_text(value)
    if len(text) <= max_len:
        return text
    return f"{text[:max_len - 3]}..."


def _window_attention_item(item: dict[str, Any], target: dict[str, Any]) -> bool:
    status = _window_status_value(item)
    if status in {"blocked", "near_expiry"} or _window_trace_error(item):
        return True
    return _is_operable_waiting_target(target) and not _active_window_item(item)


def _window_attention_label(item: dict[str, Any], target: dict[str, Any]) -> str:
    symbol = _to_text(item.get("symbol")).upper()
    if not symbol:
        return ""
    status = _window_status_value(item)
    parts: list[str] = []
    if status == "near_expiry":
        parts.append(_window_side_label(item) or "临近过期")
        bars_remaining = _to_int(item.get("bars_remaining"), 0)
        if bars_remaining > 0:
            parts.append(f"{bars_remaining} bars")
    else:
        status_label = _window_status_label(status)
        if status_label:
            parts.append(status_label)
    if _is_operable_waiting_target(target):
        parts.append("可操作待信号")
    blocked_reason = _window_reason(item.get("blocked_reason") or item.get("filter_reason"))
    if blocked_reason:
        parts.append(blocked_reason)
    trace_error = _window_reason(_window_trace_error(item))
    if trace_error:
        parts.append(f"trace错误:{trace_error}")
    return f"{symbol}({','.join(parts)})" if parts else symbol


def _active_window_summary(active_window_payload: dict[str, Any], targets_payload: dict[str, Any]) -> dict[str, str]:
    summary = _as_dict(active_window_payload.get("summary"))
    window_items = [_as_dict(item) for item in active_window_payload.get("items") or [] if isinstance(item, dict)]
    window_by_symbol = {_to_text(item.get("symbol")).upper(): item for item in window_items if _to_text(item.get("symbol"))}
    target_items = [_as_dict(item) for item in targets_payload.get("items") or [] if isinstance(item, dict)]
    target_by_symbol = {_to_text(item.get("symbol")).upper(): item for item in target_items if _to_text(item.get("symbol"))}
    target_symbols = [_to_text(item.get("symbol")).upper() for item in target_items if _to_text(item.get("symbol"))]
    symbols = [symbol for symbol in target_symbols if symbol] or list(window_by_symbol)
    active_items: list[dict[str, Any]] = []
    attention_items: list[dict[str, Any]] = []
    for symbol in symbols:
        item = window_by_symbol.get(symbol) or {"symbol": symbol, "window_status": "no_window"}
        target = target_by_symbol.get(symbol) or {}
        if _active_window_item(item):
            active_items.append(item)
        if _window_attention_item(item, target):
            attention_items.append(item)

    valid_count = _window_count(summary, "window_valid_count", window_items, _active_window_item)
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
    if valid_count <= 0 and not attention_items and blocked_count <= 0 and near_expiry_count <= 0 and trace_error_count <= 0:
        return {}

    result = {
        "窗口统计": (
            f"active {_to_int(summary.get('window_active_count'), 0)} | "
            f"valid {valid_count} | "
            f"candidate {_to_int(summary.get('candidate_signal_count'), 0)} | "
            f"blocked {blocked_count} | "
            f"near_expiry {near_expiry_count} | "
            f"trace_error {trace_error_count}"
        )
    }
    if active_items:
        result["窗口已激活"] = _join_limited([_window_item_label(item, active=True) for item in active_items])
    if attention_items:
        result["窗口异常"] = _join_limited(
            [
                _window_attention_label(item, target_by_symbol.get(_to_text(item.get("symbol")).upper()) or {})
                for item in attention_items
            ]
        )
    return result


def _load_today_targets_payload(
    *,
    environment: str,
    market_date: str,
    build_today_targets_response: BuildTodayTargetsResponse | None,
) -> dict[str, Any]:
    if not callable(build_today_targets_response):
        return {}
    try:
        payload, _ = build_today_targets_response(
            payload={
                "environment": environment,
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
    environment: str,
    market_date: str,
    build_active_window_progress_response: BuildActiveWindowProgressResponse | None,
) -> dict[str, Any]:
    if not callable(build_active_window_progress_response):
        return {}
    try:
        payload, _ = build_active_window_progress_response(
            payload={
                "environment": environment,
                "market_date": market_date,
                "date": market_date,
                "status": "all",
                "interval": "5m",
                "limit": 200,
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
            detail["窗口摘要错误"] = _to_text(active_window_payload.get("error"))
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
    environment = request_market_data_mode(request_payload)
    emit_nominal_ok = _truthy(request_payload.get("emit_nominal_ok"), default=False)
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

    if snapshot.get("unhealthy"):
        recovered_codes = _recovered_issue_codes(previous_issue_codes, current_issue_codes)
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
                environment=environment,
            )
            next_state["last_partial_recovery_at"] = times["us"]
            next_state["last_partial_recovery_codes"] = recovered_codes
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
                title=_heartbeat_title(snapshot, reminder=False),
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
            if emit_nominal_ok:
                issue_event = emit_system_event(
                    event_type="heartbeat",
                    level="info",
                    source="ibkr-api",
                    title="IBKR 系统心跳（native）",
                    detail=_heartbeat_detail(snapshot, timestamp_us=times["us"]),
                    environment=environment,
                )
                next_state["last_ok_hour"] = current_hour
            else:
                nominal_ok_suppressed = True
                next_state["last_ok_suppressed_hour"] = current_hour

    upsert_state(HEARTBEAT_STATE_KEY, environment, next_state, times["date"])
    return {
        "ok": True,
        "environment": environment,
        "job_id": "system_heartbeat",
        "unhealthy": bool(snapshot.get("unhealthy")),
        "severity": _to_text(snapshot.get("severity")) or "warning",
        "summary_status": _to_text(snapshot.get("summary_status")) or "unknown",
        "monitor_status": _to_text(snapshot.get("monitor_status")) or "unknown",
        "issue_codes": current_issue_codes,
        "event": issue_event,
        "recovery_event": recovery_event,
        "partial_recovery": bool(recovery_event),
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
    environment = request_market_data_mode(request_payload)
    times = time_strings()
    if _matches_open_report_time_window(
        times.get("us", ""),
        _to_text(request_payload.get("open_report_time_et")) or DEFAULT_OPEN_REPORT_TIME_ET,
        _to_int(request_payload.get("open_report_window_minutes"), DEFAULT_OPEN_REPORT_WINDOW_MINUTES),
    ):
        return {
            "ok": True,
            "environment": environment,
            "job_id": "system_status_reminder",
            "skipped": True,
            "reason": "open_report_window",
            "source": "ibkr-api",
        }, 200
    snapshot = _runtime_health_snapshot(
        environment=environment,
        build_system_summary_payload=build_system_summary_payload,
        build_system_monitor_payload=build_system_monitor_payload,
    )
    targets_payload = _load_today_targets_payload(
        environment=environment,
        market_date=times["date"],
        build_today_targets_response=build_today_targets_response,
    )
    active_window_payload = _load_active_window_payload(
        environment=environment,
        market_date=times["date"],
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
