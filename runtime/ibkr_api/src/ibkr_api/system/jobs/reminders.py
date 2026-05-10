from __future__ import annotations

from typing import Any, Callable

from ibkr_api.system.jobs.market_calendar import is_nyse_non_trading_day


DAILY_REMINDER_STATE_KEY = "system_notify_daily"
DEFAULT_MARKET_OPEN_REMINDER_TIME_ET = "09:30"
DEFAULT_MARKET_OPEN_REMINDER_WINDOW_MINUTES = 10
DEFAULT_DAILY_REPORT_TIME_ET = "16:05"

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildSystemSummaryPayload = Callable[..., dict[str, Any]]
BuildSystemMonitorPayload = Callable[[str], dict[str, Any]]
EmitSystemEvent = Callable[..., dict[str, Any]]
FeishuSendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
WriteSystemEventRecord = Callable[..., dict[str, Any]]
GetStatePayload = Callable[[str, str], dict[str, Any]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
ConsoleBaseUrl = Callable[[], str]
StartupChatId = Callable[[str], str]
BuildTodayTargetsResponse = Callable[..., tuple[dict[str, Any], int]]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    return _to_text(value).lower() not in {"", "0", "false", "no", "off"}


def _today_order_count(today: dict[str, Any]) -> int:
    if "main_orders" in today:
        return _to_int(today.get("main_orders"), 0)
    if "order_groups" in today:
        return _to_int(today.get("order_groups"), 0)
    return _to_int(today.get("orders"), 0)


def _matches_time_window(current_us: str, target_et: str, *, window_minutes: int = 1) -> bool:
    current = _to_text(current_us)
    target = _to_text(target_et)
    if len(current) < 16 or len(target) < 5:
        return False
    try:
        current_hour, current_minute = current[11:16].split(":", 1)
        target_hour, target_minute = target[:5].split(":", 1)
        current_total = int(current_hour) * 60 + int(current_minute)
        target_total = int(target_hour) * 60 + int(target_minute)
    except Exception:
        return False
    window = max(1, int(window_minutes or 1))
    return target_total <= current_total < target_total + window


def _event_delivery_finalized(event_result: dict[str, Any]) -> bool:
    return bool(
        event_result.get("notified")
        or event_result.get("skipped")
        or event_result.get("suppressed")
    )


def _event_result_error(event_result: dict[str, Any]) -> str:
    explicit_error = _to_text(event_result.get("error"))
    if explicit_error:
        return explicit_error
    if _event_delivery_finalized(event_result):
        return ""
    return _to_text(event_result.get("reason")) or "notification_failed"


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
        "今日orders": str(_today_order_count(today)),
        "今日events": str(_to_int(today.get("events"), 0)),
        "日筛状态": _to_text(daily_scan.get("status")) or "unknown",
        "日筛日期": _to_text(daily_scan.get("market_date")) or "",
        "故障域统计": ", ".join(f"{key}:{value}" for key, value in sorted(services.items())) if services else "n/a",
    }
    if _to_text(daily_scan.get("last_error")):
        detail["日筛错误"] = _to_text(daily_scan.get("last_error"))
    return detail


def _target_summary_line(targets_payload: dict[str, Any]) -> str:
    summary = _as_dict(targets_payload.get("summary"))
    return (
        f"total {_to_int(summary.get('total'))} | "
        f"active {_to_int(summary.get('active_count'))} | "
        f"candidate {_to_int(summary.get('candidate_count'))} | "
        f"operable {_to_int(summary.get('operable_count'))} | "
        f"ready {_to_int(summary.get('technical_ready_count'))} | "
        f"signals {_to_int(summary.get('signaled_count'))}"
    )


def _close_context(summary: dict[str, Any], monitor: dict[str, Any], targets_payload: dict[str, Any]) -> dict[str, Any]:
    runtime_inner = _as_dict(monitor.get("runtime"))
    compute = _as_dict(summary.get("ibkr_compute") or monitor.get("compute"))
    runtime = {**_as_dict(summary.get("ibkr_runtime")), **runtime_inner}
    scheduler = _as_dict(monitor.get("scheduler"))
    gateway = _as_dict(runtime.get("gateway"))
    session = _as_dict(runtime.get("session"))
    websocket = _as_dict(runtime.get("websocket"))
    daily_scan = (
        _as_dict(targets_payload.get("daily_scan"))
        or _as_dict(runtime_inner.get("daily_scan"))
        or _as_dict(summary.get("daily_scan"))
    )
    services = _as_dict(_as_dict(monitor.get("service_monitor")).get("status_counts")) or _as_dict(
        _as_dict(summary.get("service_monitor")).get("status_counts")
    )
    today = _as_dict(summary.get("today"))
    return {
        "compute": compute,
        "runtime": runtime,
        "scheduler": scheduler,
        "gateway": gateway,
        "session": session,
        "websocket": websocket,
        "daily_scan": daily_scan,
        "services": services,
        "today": today,
    }


def _status_text(value: Any, default: str = "unknown") -> str:
    return _to_text(value).lower() or default


def _status_problem(status: Any) -> bool:
    text = _status_text(status, "")
    return bool(text) and text not in {"running", "ok", "ready", "healthy", "connected", "authenticated", "completed"}


def _service_stats_line(services: dict[str, Any]) -> str:
    return ", ".join(f"{key}:{value}" for key, value in sorted(services.items())) if services else "n/a"


def _system_line(context: dict[str, Any]) -> str:
    compute = _as_dict(context.get("compute"))
    runtime = _as_dict(context.get("runtime"))
    scheduler = _as_dict(context.get("scheduler"))
    return " | ".join(
        [
            f"Compute {_to_text(compute.get('status')) or 'unknown'}",
            f"Runtime {_to_text(runtime.get('status')) or 'unknown'}",
            f"Scheduler {_to_text(scheduler.get('status')) or 'unknown'}",
        ]
    )


def _link_line(context: dict[str, Any]) -> str:
    gateway = _as_dict(context.get("gateway"))
    session = _as_dict(context.get("session"))
    websocket = _as_dict(context.get("websocket"))
    return " | ".join(
        [
            f"Gateway {'running' if gateway.get('running') or gateway.get('reachable') else 'offline'}",
            f"Session {'authenticated' if session.get('authenticated') else 'pending'}",
            f"WebSocket {'connected' if websocket.get('connected') or websocket.get('ready') else 'offline'}",
        ]
    )


def _dispatch_lag_text(context: dict[str, Any]) -> str:
    scheduler = _as_dict(context.get("scheduler"))
    return f"{float(scheduler.get('dispatch_lag_min') or 0):.2f}m" if scheduler.get("latest_ingested_bar_time_ms") else "awaiting bars"


def _close_issue_lines(
    summary: dict[str, Any],
    monitor: dict[str, Any],
    targets_payload: dict[str, Any],
    environment: str,
) -> tuple[list[str], bool]:
    context = _close_context(summary, monitor, targets_payload)
    issues: list[str] = []
    blocking = False
    summary_status = _status_text(summary.get("status"), "")
    monitor_status = _status_text(monitor.get("status"), "")
    if summary_status and summary_status not in {"running", "ok"}:
        issues.append(f"总体状态 {summary_status}")
        blocking = blocking or summary_status in {"offline", "error", "failed"}
    if monitor_status and monitor_status not in {"running", "ok"}:
        issues.append(f"监控状态 {monitor_status}")
        blocking = blocking or monitor_status in {"offline", "error", "failed"}
    for label, status in (
        ("Compute", _as_dict(context.get("compute")).get("status")),
        ("Runtime", _as_dict(context.get("runtime")).get("status")),
        ("Scheduler", _as_dict(context.get("scheduler")).get("status")),
    ):
        if _status_problem(status):
            issues.append(f"{label} {_to_text(status)}")
            blocking = True
    gateway = _as_dict(context.get("gateway"))
    session = _as_dict(context.get("session"))
    websocket = _as_dict(context.get("websocket"))
    runtime = _as_dict(context.get("runtime"))
    if "gateway" in runtime and not (gateway.get("running") or gateway.get("reachable")):
        issues.append("Gateway offline")
        blocking = True
    if "session" in runtime and not session.get("authenticated"):
        issues.append("Session pending")
        blocking = True
    if "websocket" in runtime and not (websocket.get("connected") or websocket.get("ready")):
        issues.append("WebSocket offline")
        blocking = True
    services = _as_dict(context.get("services"))
    service_issues = {
        key: value
        for key, value in services.items()
        if _to_text(key).lower() not in {"running", "ok", "healthy"} and _to_int(value, 0) > 0
    }
    if service_issues:
        issues.append("故障域 " + _service_stats_line(service_issues))
    daily_scan = _as_dict(context.get("daily_scan"))
    scan_status = _status_text(daily_scan.get("status"), "")
    scan_error = _to_text(daily_scan.get("last_error")) or _to_text(_as_dict(daily_scan.get("result")).get("error"))
    if scan_status in {"failed", "error"}:
        issues.append(f"日筛失败: {scan_error or 'unknown_error'}")
    elif scan_error:
        issues.append(f"日筛异常: {scan_error}")
    today = _as_dict(context.get("today"))
    if _to_text(environment).lower() == "live" and _to_int(today.get("ibkr_bars"), 0) <= 0:
        issues.append("收盘时今日 bars 为 0，数据链路可能未落库；如确认休市可忽略")
    return issues, blocking


def _close_conclusion(issue_lines: list[str], blocking: bool) -> str:
    if blocking:
        return "不建议继续自动交易: " + "；".join(issue_lines[:3])
    if issue_lines:
        return "需关注: " + "；".join(issue_lines[:3])
    return "运行正常: 收盘链路和日内汇总未发现需要立即处理的问题。"


def _close_operator_action(issue_lines: list[str], blocking: bool) -> str:
    if not issue_lines:
        return "无需处理；保留日报作为今日收盘审计。"
    joined = "；".join(issue_lines)
    if "bars 为 0" in joined:
        return "优先检查 Scheduler ingest、Runtime WebSocket、watchlist/targets 写入；若当天美股休市可忽略。"
    if blocking:
        return "先恢复 Gateway / Session / WebSocket 与核心服务，再检查今日数据是否完整。"
    return "按需要检查日筛、服务统计与控制台详情，确认后无需重复处理。"


def _close_report_template(issue_lines: list[str], blocking: bool) -> str:
    if blocking:
        return "red"
    if issue_lines:
        return "orange"
    return "green"


def _report_url(console_base_url: str, environment: str, market_date: str, page: str) -> str:
    base = _to_text(console_base_url).rstrip("/")
    if not base:
        return ""
    if page == "system":
        return f"{base}/ibkr_system.html?environment={environment}"
    return f"{base}/ibkr_screener.html?environment={environment}&tab=screener&view=current&date={market_date}&market_date={market_date}"


def _build_close_report_card(
    *,
    environment: str,
    times: dict[str, str],
    summary: dict[str, Any],
    monitor: dict[str, Any],
    targets_payload: dict[str, Any],
    console_base_url: str,
) -> dict[str, Any]:
    context = _close_context(summary, monitor, targets_payload)
    today = _as_dict(context.get("today"))
    daily_scan = _as_dict(context.get("daily_scan"))
    services = _as_dict(context.get("services"))
    market_date = _to_text(targets_payload.get("market_date")) or _to_text(daily_scan.get("market_date")) or _to_text(times.get("date"))
    issue_lines, blocking = _close_issue_lines(summary, monitor, targets_payload, environment)
    issue_text = "；".join(issue_lines) if issue_lines else "无"
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**结论**: {_close_conclusion(issue_lines, blocking)}\n"
                f"**需要处理**: {_close_operator_action(issue_lines, blocking)}\n"
                f"**交易日**: {market_date or 'n/a'}\n"
                f"**检查时间**: 美东 {_to_text(times.get('us')) or 'n/a'} | 北京 {_to_text(times.get('cn')) or 'n/a'}"
            ),
        },
        {
            "tag": "markdown",
            "content": (
                f"**今日结果**: bars {_to_int(today.get('ibkr_bars'), 0)} | "
                f"signals {_to_int(today.get('ibkr_signals'), 0)} | "
                f"orders {_today_order_count(today)} | "
                f"events {_to_int(today.get('events'), 0)}\n"
                f"**今日标的**: {_target_summary_line(targets_payload)}\n"
                f"**日筛**: {_to_text(daily_scan.get('status')) or 'unknown'}"
                f"{(' · ' + _to_text(daily_scan.get('market_date'))) if _to_text(daily_scan.get('market_date')) else ''}"
            ),
        },
        {
            "tag": "markdown",
            "content": (
                f"**系统链路**: {_system_line(context)}\n"
                f"**IBKR链路**: {_link_line(context)}\n"
                f"**DispatchLag**: {_dispatch_lag_text(context)}\n"
                f"**故障域统计**: {_service_stats_line(services)}\n"
                f"**关注点**: {issue_text}"
            ),
        },
    ]
    actions = []
    system_url = _report_url(console_base_url, environment, market_date, "system")
    screener_url = _report_url(console_base_url, environment, market_date, "screener")
    if system_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看系统状态"},
                "type": "primary" if issue_lines else "default",
                "multi_url": {"url": system_url, "pc_url": system_url, "ios_url": system_url, "android_url": system_url},
            }
        )
    if screener_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看今日标的榜"},
                "type": "default",
                "multi_url": {"url": screener_url, "pc_url": screener_url, "ios_url": screener_url, "android_url": screener_url},
            }
        )
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"IBKR 16:05 收盘汇总 · {environment.upper()}"},
            "template": _close_report_template(issue_lines, blocking),
        },
        "elements": elements,
    }


def _close_event_detail(
    *,
    times: dict[str, str],
    summary: dict[str, Any],
    monitor: dict[str, Any],
    targets_payload: dict[str, Any],
    environment: str,
) -> dict[str, Any]:
    context = _close_context(summary, monitor, targets_payload)
    today = _as_dict(context.get("today"))
    daily_scan = _as_dict(context.get("daily_scan"))
    services = _as_dict(context.get("services"))
    issue_lines, blocking = _close_issue_lines(summary, monitor, targets_payload, environment)
    detail = {
        "阶段": "close",
        "检查时间": _to_text(times.get("us")),
        "结论": _close_conclusion(issue_lines, blocking),
        "需要处理": _close_operator_action(issue_lines, blocking),
        "今日结果": (
            f"bars {_to_int(today.get('ibkr_bars'), 0)} | "
            f"signals {_to_int(today.get('ibkr_signals'), 0)} | "
            f"orders {_today_order_count(today)} | "
            f"events {_to_int(today.get('events'), 0)}"
        ),
        "今日标的": _target_summary_line(targets_payload),
        "日筛": f"{_to_text(daily_scan.get('status')) or 'unknown'} {_to_text(daily_scan.get('market_date'))}".strip(),
        "系统链路": _system_line(context),
        "IBKR链路": _link_line(context),
        "DispatchLag": _dispatch_lag_text(context),
        "故障域统计": _service_stats_line(services),
    }
    if issue_lines:
        detail["关注点"] = "；".join(issue_lines)
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
    target_time_et = _to_text(request_payload.get("target_time_et")) or DEFAULT_MARKET_OPEN_REMINDER_TIME_ET
    window_minutes = _to_int(request_payload.get("window_minutes"), DEFAULT_MARKET_OPEN_REMINDER_WINDOW_MINUTES)
    if not _matches_time_window(times["us"], target_time_et, window_minutes=window_minutes):
        return {
            "ok": True,
            "environment": environment,
            "skipped": True,
            "reason": "outside_time_window",
            "target_time_et": target_time_et,
            "window_minutes": window_minutes,
            "source": "ibkr-api",
            "job_id": "system_market_open_reminder",
        }, 200

    current_state = _as_dict(get_state_payload(DAILY_REMINDER_STATE_KEY, environment).get("data"))
    if _to_text(current_state.get("open_sent_at")):
        return {"ok": True, "environment": environment, "skipped": True, "reason": "already_sent", "source": "ibkr-api", "job_id": "system_market_open_reminder"}, 200
    market_date = _to_text(times.get("date"))
    if is_nyse_non_trading_day(market_date):
        next_state = {
            **current_state,
            "open_title": "IBKR 09:30 开盘系统检查",
            "open_status": "skipped",
            "open_last_attempt_at": times["us"],
            "open_notified": False,
            "open_persisted": False,
            "open_message_id": "",
            "open_skipped": True,
            "open_suppressed": False,
            "open_reason": "non_trading_day",
            "open_error": "",
            "open_sent_at": times["us"],
        }
        upsert_state(DAILY_REMINDER_STATE_KEY, environment, next_state, times["date"])
        return {
            "ok": True,
            "environment": environment,
            "notified": False,
            "persisted": False,
            "message_id": "",
            "skipped": True,
            "reason": "non_trading_day",
            "trading_day": False,
            "market_date": market_date,
            "error": "",
            "state": next_state,
            "source": "ibkr-api",
            "job_id": "system_market_open_reminder",
        }, 200
    summary = build_system_summary_payload(environment)
    monitor = build_system_monitor_payload(environment)
    level = "warning" if _to_text(summary.get("status")).lower() not in {"running", "ok"} else "info"
    event_result = _as_dict(emit_system_event(
        event_type="status_change",
        level=level,
        source="ibkr_api",
        title="IBKR 09:30 开盘系统检查",
        detail=_summary_detail(summary, monitor, phase="open", timestamp_us=times["us"]),
        environment=environment,
    ))
    finalized = _event_delivery_finalized(event_result)
    open_error = _event_result_error(event_result)
    next_state = {
        **current_state,
        "open_title": "IBKR 09:30 开盘系统检查",
        "open_status": _to_text(summary.get("status")) or "offline",
        "open_last_attempt_at": times["us"],
        "open_notified": bool(event_result.get("notified")),
        "open_persisted": bool(event_result.get("persisted")),
        "open_message_id": _to_text(event_result.get("message_id")),
        "open_skipped": bool(event_result.get("skipped")),
        "open_suppressed": bool(event_result.get("suppressed")),
        "open_reason": _to_text(event_result.get("reason")),
        "open_error": open_error,
    }
    if finalized:
        next_state["open_sent_at"] = times["us"]
    upsert_state(DAILY_REMINDER_STATE_KEY, environment, next_state, times["date"])
    return {
        "ok": finalized,
        "environment": environment,
        "notified": bool(event_result.get("notified")),
        "persisted": bool(event_result.get("persisted")),
        "message_id": _to_text(event_result.get("message_id")),
        "skipped": bool(event_result.get("skipped")),
        "suppressed": bool(event_result.get("suppressed")),
        "error": open_error,
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
    feishu_send_interactive: FeishuSendInteractive,
    write_system_event_record: WriteSystemEventRecord,
    get_state_payload: GetStatePayload,
    upsert_state: UpsertState,
    config_value: ConfigValue,
    console_base_url: ConsoleBaseUrl,
    startup_chat_id: StartupChatId,
    build_today_targets_response: BuildTodayTargetsResponse,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    times = time_strings()
    target_time_et = _to_text(request_payload.get("target_time_et")) or DEFAULT_DAILY_REPORT_TIME_ET
    if not _matches_time_window(times["us"], target_time_et):
        return {
            "ok": True,
            "environment": environment,
            "skipped": True,
            "reason": "outside_time_window",
            "target_time_et": target_time_et,
            "source": "ibkr-api",
            "job_id": "system_daily_report",
        }, 200

    current_state = _as_dict(get_state_payload(DAILY_REMINDER_STATE_KEY, environment).get("data"))
    if _to_text(current_state.get("close_sent_at")):
        return {"ok": True, "environment": environment, "skipped": True, "reason": "already_sent", "source": "ibkr-api", "job_id": "system_daily_report"}, 200

    market_date = _to_text(times.get("date"))
    if is_nyse_non_trading_day(market_date):
        next_state = {
            **current_state,
            "close_title": "IBKR 16:05 收盘汇总",
            "close_status": "skipped",
            "close_last_attempt_at": times["us"],
            "close_notified": False,
            "close_persisted": False,
            "close_message_id": "",
            "close_skipped": True,
            "close_suppressed": False,
            "close_reason": "non_trading_day",
            "close_error": "",
            "close_report_market_date": market_date,
            "close_target_total": 0,
            "close_daily_scan_status": "non_trading_day",
            "close_sent_at": times["us"],
        }
        upsert_state(DAILY_REMINDER_STATE_KEY, environment, next_state, times["date"])
        return {
            "ok": True,
            "environment": environment,
            "notified": False,
            "persisted": False,
            "message_id": "",
            "skipped": True,
            "suppressed": False,
            "reason": "non_trading_day",
            "trading_day": False,
            "market_date": market_date,
            "error": "",
            "state": next_state,
            "source": "ibkr-api",
            "job_id": "system_daily_report",
        }, 200

    if not _truthy(config_value("daily_summary_notify_enabled", "TRUE", environment)):
        next_state = {
            **current_state,
            "close_title": "IBKR 16:05 收盘汇总",
            "close_status": _to_text(current_state.get("close_status")) or "skipped",
            "close_last_attempt_at": times["us"],
            "close_notified": False,
            "close_persisted": False,
            "close_message_id": "",
            "close_skipped": True,
            "close_suppressed": False,
            "close_reason": "daily_summary_notify_disabled",
            "close_error": "",
            "close_sent_at": times["us"],
        }
        upsert_state(DAILY_REMINDER_STATE_KEY, environment, next_state, times["date"])
        return {
            "ok": True,
            "environment": environment,
            "notified": False,
            "persisted": False,
            "message_id": "",
            "skipped": True,
            "suppressed": False,
            "reason": "daily_summary_notify_disabled",
            "error": "",
            "state": next_state,
            "source": "ibkr-api",
            "job_id": "system_daily_report",
        }, 200

    summary = build_system_summary_payload(environment, lite_mode=True)
    monitor = build_system_monitor_payload(environment)
    try:
        targets_payload, _ = build_today_targets_response(
            payload={
                "environment": environment,
                "market_date": times["date"],
                "date": times["date"],
                "per_page": 5,
                "page": 1,
                "paginate": False,
                "sort_by": "attention_asc",
            }
        )
        targets_payload = _as_dict(targets_payload)
    except Exception as exc:
        targets_payload = {
            "market_date": times["date"],
            "summary": {},
            "daily_scan": {"status": "unknown", "last_error": f"targets_summary_error:{exc}", "market_date": times["date"]},
            "items": [],
        }
    issue_lines, blocking = _close_issue_lines(summary, monitor, targets_payload, environment)
    level = "error" if blocking else ("warning" if issue_lines else "info")
    card = _build_close_report_card(
        environment=environment,
        times=times,
        summary=summary,
        monitor=monitor,
        targets_payload=targets_payload,
        console_base_url=console_base_url(),
    )
    result = _as_dict(feishu_send_interactive(card, startup_chat_id(environment), environment))
    notified = bool(result.get("success")) and not bool(result.get("suppressed"))
    finalized = bool(notified or result.get("skipped") or result.get("suppressed"))
    close_error = "" if finalized else (_to_text(result.get("error")) or _to_text(result.get("reason")) or "send_failed")
    event_record = write_system_event_record(
        "daily_report",
        level,
        "ibkr-api",
        "IBKR 16:05 收盘汇总",
        _close_event_detail(times=times, summary=summary, monitor=monitor, targets_payload=targets_payload, environment=environment),
        environment,
        notified,
    )
    persisted = bool(event_record)
    next_state = {
        **current_state,
        "close_title": "IBKR 16:05 收盘汇总",
        "close_status": _to_text(summary.get("status")) or "offline",
        "close_last_attempt_at": times["us"],
        "close_notified": notified,
        "close_persisted": persisted,
        "close_message_id": _to_text(result.get("message_id")),
        "close_skipped": bool(result.get("skipped")),
        "close_suppressed": bool(result.get("suppressed")),
        "close_reason": _to_text(result.get("reason")),
        "close_error": close_error,
        "close_report_market_date": _to_text(targets_payload.get("market_date")) or times["date"],
        "close_target_total": _to_int(_as_dict(targets_payload.get("summary")).get("total"), 0),
        "close_daily_scan_status": _to_text(_as_dict(targets_payload.get("daily_scan")).get("status")),
    }
    if finalized:
        next_state["close_sent_at"] = times["us"]
    upsert_state(DAILY_REMINDER_STATE_KEY, environment, next_state, times["date"])
    return {
        "ok": finalized,
        "environment": environment,
        "notified": notified,
        "persisted": persisted,
        "message_id": _to_text(result.get("message_id")),
        "skipped": bool(result.get("skipped")),
        "suppressed": bool(result.get("suppressed")),
        "error": close_error,
        "state": next_state,
        "source": "ibkr-api",
        "job_id": "system_daily_report",
    }, 200
