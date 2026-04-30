from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo


OPEN_REPORT_STATE_KEY = "system_notify_daily"
DEFAULT_OPEN_REPORT_TIME_ET = "09:30"
DEFAULT_OPEN_REPORT_WINDOW_MINUTES = 10
DEFAULT_MARKET_SYMBOLS = "SPY,QQQ,VIX"
ET = ZoneInfo("America/New_York")

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildTodayTargetsResponse = Callable[..., tuple[dict[str, Any], int]]
BuildSystemSummaryPayload = Callable[..., dict[str, Any]]
BuildSystemMonitorPayload = Callable[[str], dict[str, Any]]
FeishuSendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
WriteSystemEventRecord = Callable[..., dict[str, Any]]
GetStatePayload = Callable[[str, str], dict[str, Any]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
ConsoleBaseUrl = Callable[[], str]
SystemStatusChatId = Callable[[str], str]
LoadMarketSnapshots = Callable[[str, list[str], str, int], list[dict[str, Any]]]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    return _to_text(value).lower() not in {"", "0", "false", "no", "off"}


def _normalize_symbols(value: Any) -> list[str]:
    raw_items = list(value) if isinstance(value, (list, tuple, set)) else _to_text(value).split(",")
    items: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        symbol = _to_text(raw).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        items.append(symbol)
    return items


def _escape_filter(value: Any) -> str:
    return _to_text(value).replace("\\", "\\\\").replace('"', '\\"')


def _build_symbol_filter(symbols: list[str]) -> str:
    normalized = _normalize_symbols(symbols)
    if not normalized:
        return ""
    return "(" + " || ".join(f'symbol = "{_escape_filter(symbol)}"' for symbol in normalized) + ")"


def _build_bar_environment_filter(environment: str) -> str:
    clauses = [f'environment = "{_escape_filter(environment)}"']
    if _to_text(environment).lower() == "live":
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})"


def _load_records_for_symbols(
    pb: Any,
    collection: str,
    *,
    base_filter_parts: list[str],
    symbols: list[str],
    sort: str,
    max_pages: int,
    chunk_size: int = 24,
) -> list[dict[str, Any]]:
    normalized_symbols = _normalize_symbols(symbols)
    if not normalized_symbols:
        return []
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(normalized_symbols), max(1, int(chunk_size or 1))):
        chunk = normalized_symbols[offset:offset + max(1, int(chunk_size or 1))]
        filter_parts = list(base_filter_parts)
        symbol_filter = _build_symbol_filter(chunk)
        if symbol_filter:
            filter_parts.append(symbol_filter)
        rows.extend(
            dict(row)
            for row in (pb.get_all_records(collection, filter=" && ".join(filter_parts), sort=sort, max_pages=max_pages) or [])
            if isinstance(row, dict)
        )
    return rows


def _parse_market_date_bounds_ms(market_date: str) -> tuple[int, int]:
    start_dt = datetime.strptime(_to_text(market_date), "%Y-%m-%d").replace(tzinfo=ET)
    end_dt = start_dt + timedelta(days=1)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def _format_et_datetime(ms: int) -> str:
    return datetime.fromtimestamp(int(ms or 0) / 1000, tz=ET).strftime("%Y-%m-%d %H:%M:%S") if int(ms or 0) > 0 else ""


def _parse_hhmm(value: Any) -> int | None:
    text = _to_text(value)
    if len(text) < 5 or ":" not in text[:5]:
        return None
    try:
        hour, minute = text[:5].split(":", 1)
        return int(hour) * 60 + int(minute)
    except Exception:
        return None


def _matches_time_window(current_us: str, target_et: str, *, window_minutes: int = DEFAULT_OPEN_REPORT_WINDOW_MINUTES) -> bool:
    current = _to_text(current_us)
    if len(current) < 16:
        return False
    current_minute = _parse_hhmm(current[11:16])
    target_minute = _parse_hhmm(target_et)
    if current_minute is None or target_minute is None:
        return False
    window = max(1, int(window_minutes or DEFAULT_OPEN_REPORT_WINDOW_MINUTES))
    return target_minute <= current_minute < target_minute + window


def matches_open_report_time_window(current_us: str, target_et: str = DEFAULT_OPEN_REPORT_TIME_ET, window_minutes: int = DEFAULT_OPEN_REPORT_WINDOW_MINUTES) -> bool:
    return _matches_time_window(current_us, target_et, window_minutes=window_minutes)


def _parse_us_time_ms(value: Any) -> int:
    text = _to_text(value)
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text[:19], fmt).replace(tzinfo=ET).timestamp() * 1000)
        except Exception:
            continue
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _first_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        symbol = _to_text(row.get("symbol")).upper()
        if symbol and symbol not in result:
            result[symbol] = dict(row)
    return result


def load_market_snapshots_from_pb(pb: Any, environment: str, symbols: list[str], market_date: str, computed_at_ms: int) -> list[dict[str, Any]]:
    normalized_symbols = _normalize_symbols(symbols)
    if not normalized_symbols:
        return []
    try:
        market_start_ms, market_end_ms = _parse_market_date_bounds_ms(market_date)
    except Exception:
        return [{"symbol": symbol, "status": "date_invalid"} for symbol in normalized_symbols]

    intraday_rows = _load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "5m"',
            _build_bar_environment_filter(environment),
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=normalized_symbols,
        sort="-bar_time_ms",
        max_pages=4,
    )
    daily_rows = _load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "1d"',
            _build_bar_environment_filter(environment),
            f"bar_time_ms >= {market_start_ms - 14 * 24 * 60 * 60 * 1000}",
            f"bar_time_ms < {market_start_ms}",
        ],
        symbols=normalized_symbols,
        sort="-bar_time_ms",
        max_pages=3,
    )
    latest_intraday = _first_by_symbol(intraday_rows)
    previous_daily = _first_by_symbol(daily_rows)
    snapshots: list[dict[str, Any]] = []
    for symbol in normalized_symbols:
        intraday = latest_intraday.get(symbol, {})
        daily = previous_daily.get(symbol, {})
        intraday_ms = _to_int(intraday.get("bar_time_ms"), 0)
        price = _to_float(intraday.get("close"), 0.0)
        prev_close = _to_float(daily.get("close"), 0.0)
        if price <= 0 and prev_close > 0:
            price = prev_close
        change_pct = round(((price - prev_close) / prev_close) * 100, 2) if price > 0 and prev_close > 0 else 0.0
        snapshots.append(
            {
                "symbol": symbol,
                "price": round(price, 4) if price > 0 else 0.0,
                "prev_close": round(prev_close, 4) if prev_close > 0 else 0.0,
                "change_pct": change_pct,
                "latest_us_time": _to_text(intraday.get("us_time")) or (_format_et_datetime(intraday_ms) if intraday_ms > 0 else ""),
                "freshness_min": max(0, int((computed_at_ms - intraday_ms) // 60000)) if computed_at_ms > 0 and intraday_ms > 0 else None,
                "status": "live" if intraday_ms > 0 else ("prev_close" if prev_close > 0 else "missing"),
            }
        )
    return snapshots


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


def _format_target_line(item: dict[str, Any], index: int) -> str:
    symbol = _to_text(item.get("symbol")) or f"#{index}"
    status = _to_text(item.get("status") or item.get("target_status")) or "watch"
    direction = _to_text(item.get("direction_bias")) or "neutral"
    score = _to_float(item.get("score") or item.get("target_score"), 0.0)
    technical_state = _to_text(item.get("technical_state")) or "unknown"
    signal_status = _to_text(item.get("latest_signal_status")) or "no_signal"
    price = _to_float(item.get("price"), 0.0)
    day_change = _to_float(item.get("day_change_pct"), 0.0)
    latest_time = _to_text(item.get("latest_us_time")) or "n/a"
    volume = _to_float(item.get("today_volume") or item.get("premarket_volume"), 0.0)
    return (
        f"{index}. {symbol} | {status}/{direction} | score {score:.1f} | "
        f"{technical_state}/{signal_status} | ${price:.2f} | {day_change:+.2f}% | "
        f"vol {volume:,.0f} | {latest_time}"
    )


def _format_market_line(item: dict[str, Any]) -> str:
    symbol = _to_text(item.get("symbol")) or "--"
    price = _to_float(item.get("price"), 0.0)
    change_pct = _to_float(item.get("change_pct"), 0.0)
    latest = _to_text(item.get("latest_us_time")) or "n/a"
    freshness = item.get("freshness_min")
    freshness_text = f"{int(freshness)}m" if isinstance(freshness, int) else "n/a"
    status = _to_text(item.get("status")) or "unknown"
    return f"{symbol}: ${price:.2f} ({change_pct:+.2f}%) · {latest} · fresh {freshness_text} · {status}"


def _scan_issue_text(targets_payload: dict[str, Any]) -> str:
    daily_scan = _as_dict(targets_payload.get("daily_scan"))
    status = _to_text(daily_scan.get("status")) or "unknown"
    error = _to_text(daily_scan.get("last_error")) or _to_text(_as_dict(daily_scan.get("result")).get("error"))
    if status.lower() in {"failed", "error"}:
        return f"日筛失败: {error or 'unknown_error'}"
    if error:
        return f"日筛异常: {error}"
    return ""


def _system_lines(summary: dict[str, Any], monitor: dict[str, Any]) -> tuple[str, str, str]:
    compute = _as_dict(summary.get("ibkr_compute") or monitor.get("compute"))
    runtime = {**_as_dict(summary.get("ibkr_runtime")), **_as_dict(monitor.get("runtime"))}
    scheduler = _as_dict(monitor.get("scheduler"))
    gateway = _as_dict(runtime.get("gateway"))
    session = _as_dict(runtime.get("session"))
    websocket = _as_dict(runtime.get("websocket"))
    services = _as_dict(_as_dict(monitor.get("service_monitor")).get("status_counts")) or _as_dict(_as_dict(summary.get("service_monitor")).get("status_counts"))
    service_line = " | ".join(
        [
            f"Compute {_to_text(compute.get('status')) or 'unknown'}",
            f"Runtime {_to_text(runtime.get('status')) or 'unknown'}",
            f"Scheduler {_to_text(scheduler.get('status')) or 'unknown'}",
        ]
    )
    link_line = " | ".join(
        [
            f"Gateway {'running' if gateway.get('running') or gateway.get('reachable') else 'offline'}",
            f"Session {'authenticated' if session.get('authenticated') else 'pending'}",
            f"WebSocket {'connected' if websocket.get('connected') or websocket.get('ready') else 'offline'}",
        ]
    )
    services_line = ", ".join(f"{key}:{value}" for key, value in sorted(services.items())) if services else "n/a"
    return service_line, link_line, services_line


def _report_level(summary: dict[str, Any], monitor: dict[str, Any], targets_payload: dict[str, Any]) -> str:
    if _scan_issue_text(targets_payload):
        return "warning"
    status_values = [
        _to_text(summary.get("status")).lower(),
        _to_text(monitor.get("status")).lower(),
    ]
    if any(value in {"offline", "error"} for value in status_values):
        return "warning"
    if any(value in {"degraded", "warning"} for value in status_values):
        return "warning"
    return "info"


def _report_template(level: str, targets_payload: dict[str, Any]) -> str:
    if _scan_issue_text(targets_payload):
        return "orange"
    return "green" if level == "info" else "orange"


def _report_url(console_base_url: str, environment: str, market_date: str, page: str) -> str:
    base = _to_text(console_base_url).rstrip("/")
    if not base:
        return ""
    if page == "system":
        return f"{base}/ibkr_system.html?environment={environment}"
    return f"{base}/ibkr_screener.html?environment={environment}&tab=screener&view=current&date={market_date}&market_date={market_date}"


def _build_open_report_card(
    *,
    environment: str,
    times: dict[str, str],
    summary: dict[str, Any],
    monitor: dict[str, Any],
    targets_payload: dict[str, Any],
    market_snapshots: list[dict[str, Any]],
    console_base_url: str,
) -> dict[str, Any]:
    market_date = _to_text(targets_payload.get("market_date")) or _to_text(times.get("date"))
    target_items = [_as_dict(item) for item in (targets_payload.get("items") or []) if isinstance(item, dict)]
    target_lines = [_format_target_line(item, index) for index, item in enumerate(target_items[:5], start=1)]
    if not target_lines:
        target_lines = ["今日暂无 active / candidate 标的。"]
    market_lines = [_format_market_line(item) for item in market_snapshots[:6]] or ["大盘监控数据暂不可用。"]
    service_line, link_line, services_line = _system_lines(summary, monitor)
    issue_text = _scan_issue_text(targets_payload)
    level = _report_level(summary, monitor, targets_payload)
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**交易日**: {market_date or 'n/a'}\n"
                f"**检查时间**: 美东 {_to_text(times.get('us')) or 'n/a'} | 北京 {_to_text(times.get('cn')) or 'n/a'}\n"
                f"**系统**: {service_line}\n"
                f"**IBKR链路**: {link_line}\n"
                f"**服务统计**: {services_line}"
            ),
        },
        {"tag": "markdown", "content": f"**今日标的**: {_target_summary_line(targets_payload)}\n" + "\n".join(target_lines)},
        {"tag": "markdown", "content": "**大盘监控**:\n" + "\n".join(market_lines)},
    ]
    if issue_text:
        elements.append(
            {
                "tag": "markdown",
                "content": f"**需要关注**: {issue_text}\n**建议**: 先检查 compute / screener / targets 写入链路，修复后手动重跑 scan；未刷新前不要只按旧标的池操作。",
            }
        )
    actions = []
    system_url = _report_url(console_base_url, environment, market_date, "system")
    screener_url = _report_url(console_base_url, environment, market_date, "screener")
    if system_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看系统状态"},
                "type": "default",
                "multi_url": {"url": system_url, "pc_url": system_url, "ios_url": system_url, "android_url": system_url},
            }
        )
    if screener_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看今日标的榜"},
                "type": "primary" if target_items else "default",
                "multi_url": {"url": screener_url, "pc_url": screener_url, "ios_url": screener_url, "android_url": screener_url},
            }
        )
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"IBKR 09:30 开盘交易摘要 · {environment.upper()}"},
            "template": _report_template(level, targets_payload),
        },
        "elements": elements,
    }


def _event_detail(
    *,
    times: dict[str, str],
    summary: dict[str, Any],
    monitor: dict[str, Any],
    targets_payload: dict[str, Any],
    market_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    service_line, link_line, services_line = _system_lines(summary, monitor)
    market_line = " | ".join(_format_market_line(item) for item in market_snapshots[:3]) or "n/a"
    issue_text = _scan_issue_text(targets_payload)
    detail = {
        "检查时间": _to_text(times.get("us")),
        "交易日": _to_text(targets_payload.get("market_date")) or _to_text(times.get("date")),
        "系统": service_line,
        "IBKR链路": link_line,
        "服务统计": services_line,
        "今日标的": _target_summary_line(targets_payload),
        "大盘": market_line,
    }
    if issue_text:
        detail["需要关注"] = issue_text
    return detail


def build_system_open_report_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    build_today_targets_response: BuildTodayTargetsResponse,
    build_system_summary_payload: BuildSystemSummaryPayload,
    build_system_monitor_payload: BuildSystemMonitorPayload,
    feishu_send_interactive: FeishuSendInteractive,
    write_system_event_record: WriteSystemEventRecord,
    get_state_payload: GetStatePayload,
    upsert_state: UpsertState,
    config_value: ConfigValue,
    console_base_url: ConsoleBaseUrl,
    system_status_chat_id: SystemStatusChatId,
    load_market_snapshots: LoadMarketSnapshots | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    times = time_strings()
    target_time_et = _to_text(request_payload.get("target_time_et")) or DEFAULT_OPEN_REPORT_TIME_ET
    window_minutes = _to_int(request_payload.get("window_minutes"), DEFAULT_OPEN_REPORT_WINDOW_MINUTES)
    if not _matches_time_window(times.get("us", ""), target_time_et, window_minutes=window_minutes):
        return {
            "ok": True,
            "environment": environment,
            "job_id": "system_open_report",
            "skipped": True,
            "reason": "outside_time_window",
            "target_time_et": target_time_et,
            "window_minutes": window_minutes,
            "source": "ibkr-api",
        }, 200

    state = _as_dict(get_state_payload(OPEN_REPORT_STATE_KEY, environment).get("data"))
    if _to_text(state.get("open_sent_at")):
        return {
            "ok": True,
            "environment": environment,
            "job_id": "system_open_report",
            "skipped": True,
            "reason": "already_sent",
            "source": "ibkr-api",
        }, 200

    if not _truthy(config_value("status_notify_enabled", "TRUE", environment)):
        next_state = {**state, "open_skipped_at": times["us"], "open_skipped_reason": "status_notify_disabled"}
        upsert_state(OPEN_REPORT_STATE_KEY, environment, next_state, times["date"])
        return {
            "ok": True,
            "environment": environment,
            "job_id": "system_open_report",
            "skipped": True,
            "reason": "status_notify_disabled",
            "state": next_state,
            "source": "ibkr-api",
        }, 200

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
    computed_at_ms = _to_int(targets_payload.get("computed_at_ms"), 0) or _parse_us_time_ms(times.get("us"))
    market_symbols = _normalize_symbols(config_value("ibkr_market_ws_symbols", DEFAULT_MARKET_SYMBOLS, environment)) or _normalize_symbols(DEFAULT_MARKET_SYMBOLS)
    market_snapshots: list[dict[str, Any]] = []
    if load_market_snapshots:
        try:
            market_snapshots = load_market_snapshots(
                environment,
                market_symbols,
                _to_text(targets_payload.get("market_date")) or _to_text(times.get("date")),
                computed_at_ms,
            )
        except Exception as exc:
            market_snapshots = [{"symbol": symbol, "status": f"snapshot_error:{exc}"} for symbol in market_symbols]
    summary = _as_dict(build_system_summary_payload(environment, lite_mode=True))
    monitor = _as_dict(build_system_monitor_payload(environment))
    card = _build_open_report_card(
        environment=environment,
        times=times,
        summary=summary,
        monitor=monitor,
        targets_payload=targets_payload,
        market_snapshots=market_snapshots,
        console_base_url=console_base_url(),
    )
    result = _as_dict(feishu_send_interactive(card, system_status_chat_id(environment), environment))
    notified = bool(result.get("success")) and not bool(result.get("suppressed"))
    finalized = bool(notified or result.get("skipped") or result.get("suppressed"))
    level = _report_level(summary, monitor, targets_payload)
    event_record = write_system_event_record(
        "open_report",
        level,
        "ibkr-api",
        "IBKR 09:30 开盘交易摘要",
        _event_detail(times=times, summary=summary, monitor=monitor, targets_payload=targets_payload, market_snapshots=market_snapshots),
        environment,
        notified,
    )
    persisted = bool(event_record)
    next_state = {
        **state,
        "open_title": "IBKR 09:30 开盘交易摘要",
        "open_status": _to_text(summary.get("status")) or "offline",
        "open_last_attempt_at": times["us"],
        "open_notified": notified,
        "open_persisted": persisted,
        "open_message_id": _to_text(result.get("message_id")),
        "open_skipped": bool(result.get("skipped")),
        "open_suppressed": bool(result.get("suppressed")),
        "open_reason": _to_text(result.get("reason")),
        "open_error": "" if finalized else (_to_text(result.get("error")) or "send_failed"),
        "open_report_market_date": _to_text(targets_payload.get("market_date")) or times["date"],
        "open_target_total": _to_int(_as_dict(targets_payload.get("summary")).get("total"), 0),
        "open_daily_scan_status": _to_text(_as_dict(targets_payload.get("daily_scan")).get("status")),
    }
    if finalized:
        next_state["open_sent_at"] = times["us"]
    upsert_state(OPEN_REPORT_STATE_KEY, environment, next_state, times["date"])
    return {
        "ok": finalized,
        "environment": environment,
        "job_id": "system_open_report",
        "notified": notified,
        "persisted": persisted,
        "message_id": _to_text(result.get("message_id")),
        "skipped": bool(result.get("skipped")),
        "suppressed": bool(result.get("suppressed")),
        "error": "" if finalized else next_state["open_error"],
        "state": next_state,
        "source": "ibkr-api",
    }, 200


__all__ = [
    "DEFAULT_OPEN_REPORT_TIME_ET",
    "DEFAULT_OPEN_REPORT_WINDOW_MINUTES",
    "OPEN_REPORT_STATE_KEY",
    "build_system_open_report_response",
    "load_market_snapshots_from_pb",
    "matches_open_report_time_window",
]
