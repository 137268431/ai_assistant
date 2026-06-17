from __future__ import annotations

from ast import literal_eval
from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.modes import request_market_data_mode
from ibkr_api.system.jobs.legacy_target_universe import (
    legacy_target_market_closed,
    legacy_target_universe_suppressed,
)


MONITOR_ALERT_STATE_KEY = "system_monitor_alert"
MARKET_DATA_SESSION_CONFLICT_CODE = "market_data_session_conflict"
DEFAULT_MONITOR_ALERT_ERROR_COOLDOWN_MIN = 15
DEFAULT_MONITOR_ALERT_WARNING_COOLDOWN_MIN = 60
DEFAULT_ACCOUNT_SNAPSHOT_WARNING_CONSECUTIVE_COUNT = 2
DEFAULT_MONITOR_SOURCE_WARNING_CONSECUTIVE_COUNT = 3
MONITOR_ALERT_COOLDOWN_MS = DEFAULT_MONITOR_ALERT_ERROR_COOLDOWN_MIN * 60 * 1000
ACCOUNT_SNAPSHOT_WARNING_CODES = {
    "account_snapshot_degraded",
    "account_snapshot_timeout",
    "account_pnl_unavailable",
}
MONITOR_SOURCE_WARNING_CODES = {
    "monitor_builder_compute_monitor",
}
LEGACY_TARGET_FLAG_CODES = {"no_active_targets", "no_execution_eligible_targets"}
IB_CLIENT_SERVICE_LABELS = (
    ("ibkr-runtime", "Runtime"),
    ("ibkr-compute", "Compute"),
    ("ibkr-api", "API"),
    ("ibkr-scheduler", "Scheduler"),
    ("ibkr-backtest", "Backtest"),
)

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildSystemMonitorPayload = Callable[[str], dict[str, Any]]
EmitSystemEvent = Callable[..., dict[str, Any]]
GetStatePayload = Callable[[str, str], dict[str, Any]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]
BuildAdmissionPreview = Callable[[dict[str, Any]], Any]
ConfigValue = Callable[[str, str, str], Any]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _runtime_config_switch_enabled(runtime: dict[str, Any], key: str) -> bool | None:
    switches = _as_dict(runtime.get("runtime_config_switches"))
    items = switches.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or _to_text(item.get("key")) != key:
            continue
        if isinstance(item.get("enabled"), bool):
            return bool(item.get("enabled"))
        if item.get("enabled") is not None:
            value = _to_text(item.get("enabled")).lower()
            if value in {"true", "1", "yes", "on"}:
                return True
            if value in {"false", "0", "no", "off"}:
                return False
        value = _to_text(item.get("value")).lower()
        if value in {"true", "1", "yes", "on"}:
            return True
        if value in {"false", "0", "no", "off"}:
            return False
    return None


def _market_data_conflict_order_impact(runtime: dict[str, Any]) -> dict[str, Any]:
    gateway = _as_dict(runtime.get("gateway"))
    broker = _as_dict(gateway.get("broker"))
    session = _as_dict(runtime.get("session"))
    websocket = _as_dict(runtime.get("websocket"))
    order_flow = _as_dict(runtime.get("order_flow"))
    signal_router = _as_dict(runtime.get("signal_router"))

    order_flow_enabled = order_flow.get("enabled")
    if order_flow_enabled is None:
        order_flow_enabled = _runtime_config_switch_enabled(runtime, "ibkr_order_flow_enabled")
    order_flow_known = order_flow_enabled is not None
    order_flow_enabled = bool(order_flow_enabled)
    signal_source = _to_text(signal_router.get("signal_source")).lower() or "unknown"
    tradingview_signal = signal_source in {"tradingview", "tv", "tv_webhook", "webhook_tv"}
    gateway_active = bool(gateway.get("running") or gateway.get("reachable"))
    session_authenticated = bool(session.get("authenticated"))
    broker_ready = bool(broker.get("ready") or broker.get("connected"))

    subscribed_count = websocket.get("subscribed_count")
    if subscribed_count is None:
        subscribed_count = broker.get("subscriptions")
    pending_count = websocket.get("pending_count")
    if pending_count is None:
        pending_count = 0

    if gateway_active and session_authenticated and broker_ready and order_flow_known and not order_flow_enabled and tradingview_signal:
        order_path = "正常"
        impact = "当前不阻断下单"
    elif gateway_active and session_authenticated and broker_ready and order_flow_enabled:
        order_path = "正常但报价相关逻辑可能受影响"
        impact = "可能影响依赖实时报价的入场/出场"
    else:
        order_path = "未知/可能受影响"
        impact = "订单通道可能受影响"

    return {
        "order_path": order_path,
        "impact": impact,
        "signal_source": signal_source,
        "order_flow": "enabled" if order_flow_enabled else ("disabled" if order_flow_known else "unknown"),
        "subscribed_count": _to_int(subscribed_count, 0),
        "pending_count": _to_int(pending_count, 0),
    }


def _config_int(
    config_value: ConfigValue | None,
    key: str,
    default: int,
    environment: str,
    *,
    minimum: int = 0,
) -> int:
    try:
        raw = config_value(key, str(default), environment) if callable(config_value) else default
        value = _to_int(raw, default)
    except Exception:
        value = default
    return max(minimum, value)


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


def _backtest_service_line(service_monitor: dict[str, Any]) -> str:
    services = _as_dict(service_monitor.get("services"))
    service = _as_dict(services.get("ibkr-backtest"))
    if not service:
        return "n/a"
    status = _to_text(service.get("status")) or "unknown"
    worker = _to_text(service.get("worker_status") or service.get("readiness_phase"))
    client_id = _to_int(service.get("ib_gateway_client_id"), 0)
    parts = [f"{status}"]
    if worker and worker.lower() != status.lower():
        parts.append(f"worker {worker}")
    if client_id:
        parts.append(f"client {client_id}")
    return " | ".join(parts)


def _service_client_id(service: dict[str, Any]) -> int:
    return _to_int(
        service.get("ib_gateway_client_id")
        or service.get("broker_client_id")
        or service.get("client_id"),
        0,
    )


def _ib_client_ids_line(service_monitor: dict[str, Any]) -> str:
    services = _as_dict(service_monitor.get("services"))
    parts: list[str] = []
    seen: set[str] = set()
    for service_key, label in IB_CLIENT_SERVICE_LABELS:
        client_id = _service_client_id(_as_dict(services.get(service_key)))
        if not client_id:
            continue
        marker = f"{label}:{client_id}"
        if marker in seen:
            continue
        seen.add(marker)
        parts.append(f"{label} client {client_id}")
    return " | ".join(parts)


def _alert_flags(monitor_payload: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for item in monitor_payload.get("flags") or []:
        if not isinstance(item, dict):
            continue
        code = _to_text(item.get("code"))
        severity = _to_text(item.get("severity")).lower()
        if not code or severity not in {"warning", "error"}:
            continue
        flags.append(dict(item))
    return flags


def _filter_legacy_target_flags(flags: list[dict[str, Any]], *, suppressed: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not suppressed:
        return flags, []
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for item in flags:
        if _to_text(item.get("code")) in LEGACY_TARGET_FLAG_CODES:
            removed.append(item)
        else:
            kept.append(item)
    return kept, removed


def _flag_codes(flags: list[dict[str, Any]]) -> list[str]:
    return [_to_text(item.get("code")) for item in flags if _to_text(item.get("code"))]


def _issue_base_code(value: Any) -> str:
    return _to_text(value).split(":", 1)[0]


def _last_alert_codes_from_state(state: dict[str, Any]) -> list[str]:
    codes = state.get("last_monitor_alert_codes")
    if isinstance(codes, list):
        return [_to_text(item) for item in codes if _to_text(item)]
    fingerprint = _to_text(state.get("last_monitor_alert_hash"))
    if not fingerprint:
        return []
    try:
        parsed = literal_eval(fingerprint)
    except Exception:
        return []
    if not isinstance(parsed, dict) or not isinstance(parsed.get("flag_codes"), list):
        return []
    return [_to_text(item) for item in parsed.get("flag_codes") if _to_text(item)]


def _is_error_flag(item: dict[str, Any]) -> bool:
    return _to_text(item.get("severity")).lower() == "error"


def _is_account_snapshot_warning(item: dict[str, Any]) -> bool:
    return (
        _to_text(item.get("severity")).lower() == "warning"
        and _to_text(item.get("code")) in ACCOUNT_SNAPSHOT_WARNING_CODES
    )


def _is_monitor_source_warning(item: dict[str, Any]) -> bool:
    return (
        _to_text(item.get("severity")).lower() == "warning"
        and _to_text(item.get("code")) in MONITOR_SOURCE_WARNING_CODES
    )


def _account_snapshot_warning_streak(state: dict[str, Any], flags: list[dict[str, Any]]) -> int:
    if not any(_is_account_snapshot_warning(item) for item in flags):
        return 0
    return _to_int(state.get("account_snapshot_warning_streak"), 0) + 1


def _monitor_source_warning_streak(state: dict[str, Any], flags: list[dict[str, Any]]) -> int:
    if not any(_is_monitor_source_warning(item) for item in flags):
        return 0
    return _to_int(state.get("monitor_source_warning_streak"), 0) + 1


def _filter_alert_flags(
    flags: list[dict[str, Any]],
    *,
    account_snapshot_warning_streak: int,
    account_snapshot_warning_consecutive_count: int,
    monitor_source_warning_streak: int,
    monitor_source_warning_consecutive_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if any(_is_error_flag(item) for item in flags):
        return list(flags), []
    alert_flags: list[dict[str, Any]] = []
    suppressed_flags: list[dict[str, Any]] = []
    for item in flags:
        if (
            account_snapshot_warning_consecutive_count > 1
            and _is_account_snapshot_warning(item)
            and account_snapshot_warning_streak < account_snapshot_warning_consecutive_count
        ):
            suppressed_flags.append(item)
        elif (
            monitor_source_warning_consecutive_count > 1
            and _is_monitor_source_warning(item)
            and monitor_source_warning_streak < monitor_source_warning_consecutive_count
        ):
            suppressed_flags.append(item)
        else:
            alert_flags.append(item)
    return alert_flags, suppressed_flags


def _fingerprint(monitor_payload: dict[str, Any], flags: list[dict[str, Any]]) -> str:
    return str(
        {
            "status": _to_text(monitor_payload.get("status")).lower(),
            "flag_codes": sorted(_to_text(item.get("code")) for item in flags if _to_text(item.get("code"))),
        }
    )


def _admission_preview_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple) and result:
        payload = result[0]
    else:
        payload = result
    return dict(payload) if isinstance(payload, dict) else {}


def _admission_preview_detail(admission_preview: dict[str, Any]) -> dict[str, str]:
    if not admission_preview:
        return {}
    if admission_preview.get("error") and not admission_preview.get("ok", False):
        return {"入池预览": f"unavailable:{_to_text(admission_preview.get('error'))}"}
    admitted_items = [
        dict(item)
        for item in (admission_preview.get("admitted_items") or [])
        if isinstance(item, dict) and _to_text(item.get("symbol"))
    ]
    summary = (
        f"would_admit {_to_int(admission_preview.get('would_admit'), len(admitted_items))} | "
        f"eligible {_to_int(admission_preview.get('eligible'), 0)} | "
        f"scanned {_to_int(admission_preview.get('scanned'), 0)}"
    )
    detail = {"入池预览": summary}
    if admitted_items:
        parts = []
        for item in admitted_items[:8]:
            symbol = _to_text(item.get("symbol")).upper()
            direction = _to_text(item.get("direction_bias"))
            score = item.get("score")
            window = _to_text(item.get("window_status"))
            bars = _to_int(item.get("bars_remaining"), 0)
            score_text = f"score {score}" if score not in (None, "") else "score n/a"
            bars_text = f", {bars} bars" if bars > 0 else ""
            parts.append(f"{symbol}({direction or 'n/a'}, {score_text}, {window or 'window'}{bars_text})")
        detail["可能加入"] = " | ".join(parts)
    else:
        rejection_summary = admission_preview.get("rejection_summary")
        if isinstance(rejection_summary, dict) and rejection_summary:
            parts = [
                f"{_to_text(reason)} {_to_int(count, 0)}"
                for reason, count in list(rejection_summary.items())[:5]
            ]
            detail["未入池原因"] = " | ".join(parts)
    return detail


def _detail(
    monitor_payload: dict[str, Any],
    flags: list[dict[str, Any]],
    *,
    timestamp_us: str,
    admission_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    service_monitor = _as_dict(monitor_payload.get("service_monitor"))
    counts = _as_dict(service_monitor.get("status_counts"))
    runtime = _as_dict(monitor_payload.get("runtime"))
    scheduler = _as_dict(monitor_payload.get("scheduler"))
    session = _as_dict(runtime.get("session"))
    websocket = _as_dict(runtime.get("websocket"))
    pb_disk = _as_dict(_as_dict(monitor_payload.get("pocketbase")).get("disk"))
    filesystem = _as_dict(pb_disk.get("filesystem"))
    detail = {
        "检查时间": timestamp_us,
        "监控状态": _to_text(monitor_payload.get("status")).upper() or "UNKNOWN",
        "触发项": " | ".join(
            f"{_to_text(item.get('title') or item.get('code'))}: {_to_text(item.get('detail'))}"
            for item in flags[:4]
        ) or "none",
        "服务统计": _service_counts_line(service_monitor),
        "Backtest": _backtest_service_line(service_monitor),
        "IB ClientID": _ib_client_ids_line(service_monitor) or "n/a",
        "Session": "authenticated" if session.get("authenticated") else "pending",
        "WebSocket": "connected" if websocket.get("connected") or websocket.get("ready") else "offline",
        "DispatchLag": (
            f"{float(scheduler.get('dispatch_lag_min') or 0):.2f}m"
            if scheduler.get("latest_ingested_bar_time_ms")
            else "awaiting bars"
        ),
        "PB磁盘": (
            f"{filesystem.get('used_pct')}%"
            if filesystem.get("used_pct") is not None
            else "unknown"
        ),
    }
    conflict_state = _as_dict(runtime.get("market_data_session_conflict"))
    if conflict_state.get("active"):
        impact = _market_data_conflict_order_impact(runtime)
        detail.update(
            {
                "行情冲突": "active",
                "冲突首次": _to_text(conflict_state.get("first_seen_at")) or "unknown",
                "冲突最近": _to_text(conflict_state.get("last_seen_at") or conflict_state.get("last_error_at")) or "unknown",
                "最近10197": _to_text(conflict_state.get("last_error_at")) or "unknown",
                "冲突次数": _to_int(conflict_state.get("count"), 0),
                "下单通道": impact["order_path"],
                "下单影响": impact["impact"],
                "信号来源": impact["signal_source"],
                "OrderFlow": impact["order_flow"],
                "行情订阅": f"subscribed:{impact['subscribed_count']} | pending:{impact['pending_count']}",
                "处理建议": (
                    "这是 IBKR live 行情会话冲突，不等同于订单通道故障；如需恢复服务器 live 行情，"
                    "退出其它 TWS/IB Gateway/IBKR Desktop/Client Portal/手机行情页或第三方行情客户端，"
                    "等待 1-3 分钟；仍未恢复再重启 Gateway。"
                ),
            }
        )
    detail.update(_admission_preview_detail(admission_preview or {}))
    return detail


def _monitor_recovery_detail(
    monitor_payload: dict[str, Any],
    *,
    timestamp_us: str,
    recovered_codes: list[str],
) -> dict[str, Any]:
    detail = _detail(monitor_payload, [], timestamp_us=timestamp_us)
    detail.update(
        {
            "结论": "IBKR Monitor 告警已恢复。",
            "已恢复诊断码": ", ".join(recovered_codes) or "n/a",
            "恢复时间": timestamp_us,
            "建议": "已恢复项无需重复处理；继续观察后续 Monitor 状态。",
        }
    )
    return detail


def _market_data_conflict_recovery_detail(
    monitor_payload: dict[str, Any],
    *,
    timestamp_us: str,
    recovered_codes: list[str],
) -> dict[str, Any]:
    detail = _monitor_recovery_detail(
        monitor_payload,
        timestamp_us=timestamp_us,
        recovered_codes=recovered_codes,
    )
    runtime = _as_dict(monitor_payload.get("runtime"))
    state = _as_dict(runtime.get("market_data_session_conflict"))
    recovery_evidence = _as_dict(state.get("recovery_evidence"))
    detail.update(
        {
            "结论": "IBKR live 行情会话冲突已恢复。",
            "影响": "服务器 live 行情已恢复检测，系统已按需重订阅当前行情。",
            "原因": "最近 10197 已超过活动窗口，Gateway/Session/WebSocket/报价恢复证据通过。",
            "建议": "后续手机端尽量只用于 2FA，避免停留在实时行情页。",
            "行情冲突": "已恢复",
            "冲突首次": _to_text(state.get("first_seen_at")) or "unknown",
            "最近10197": _to_text(state.get("last_error_at")) or "unknown",
            "恢复时间": _to_text(state.get("resolved_at")) or timestamp_us,
            "恢复动作": _to_text(state.get("recovery_action")) or "cleared",
            "重订阅数量": str(_to_int(state.get("resubscribed_count"), 0)),
            "恢复依据": (
                f"session={bool(recovery_evidence.get('session_authenticated'))}, "
                f"ws={bool(recovery_evidence.get('websocket_ready'))}, "
                f"fresh_quotes={_to_int(recovery_evidence.get('fresh_quotes'), 0)}"
            ),
        }
    )
    return detail


def _should_load_admission_preview(flags: list[dict[str, Any]]) -> bool:
    preview_codes = {"no_active_targets", "no_execution_eligible_targets"}
    return any(_to_text(item.get("code")) in preview_codes for item in flags)


def _load_admission_preview(
    builder: BuildAdmissionPreview | None,
    *,
    environment: str,
    market_date: str,
) -> dict[str, Any]:
    if not callable(builder):
        return {}
    try:
        result = builder(
            {
                "environment": environment,
                "market_data_mode": environment,
                "data_environment": environment,
                "market_date": market_date,
                "dry_run": True,
                "force": True,
                "max_admit": 8,
                "max_scan_symbols": 200,
                "trigger_source": "monitor_alert_preview",
            }
        )
        return _admission_preview_payload(result)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def build_system_monitor_alert_guard_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    build_system_monitor_payload: BuildSystemMonitorPayload,
    emit_system_event: EmitSystemEvent,
    get_state_payload: GetStatePayload,
    upsert_state: UpsertState,
    build_admission_preview: BuildAdmissionPreview | None = None,
    config_value: ConfigValue | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = request_market_data_mode(request_payload)
    times = time_strings()
    monitor_payload = _as_dict(build_system_monitor_payload(environment))
    flags = _alert_flags(monitor_payload)
    legacy_suppressed, _legacy_reason, _legacy_detail = legacy_target_universe_suppressed(config_value, environment)
    if not legacy_suppressed:
        legacy_suppressed, _legacy_reason, _legacy_detail = legacy_target_market_closed(_to_text(times.get("date")))
    flags, legacy_suppressed_flags = _filter_legacy_target_flags(flags, suppressed=legacy_suppressed)
    state = _as_dict(get_state_payload(MONITOR_ALERT_STATE_KEY, environment).get("data"))
    current_ms = _to_int(datetime.now(timezone.utc).timestamp() * 1000, 0)
    error_cooldown_min = _config_int(
        config_value,
        "system_monitor_alert_error_cooldown_min",
        DEFAULT_MONITOR_ALERT_ERROR_COOLDOWN_MIN,
        environment,
        minimum=1,
    )
    warning_cooldown_min = _config_int(
        config_value,
        "system_monitor_alert_warning_cooldown_min",
        DEFAULT_MONITOR_ALERT_WARNING_COOLDOWN_MIN,
        environment,
        minimum=1,
    )
    account_snapshot_warning_consecutive_count = _config_int(
        config_value,
        "system_monitor_account_snapshot_warning_consecutive_count",
        DEFAULT_ACCOUNT_SNAPSHOT_WARNING_CONSECUTIVE_COUNT,
        environment,
        minimum=1,
    )
    monitor_source_warning_consecutive_count = _config_int(
        config_value,
        "system_monitor_source_warning_consecutive_count",
        DEFAULT_MONITOR_SOURCE_WARNING_CONSECUTIVE_COUNT,
        environment,
        minimum=1,
    )
    account_snapshot_warning_streak = _account_snapshot_warning_streak(state, flags)
    monitor_source_warning_streak = _monitor_source_warning_streak(state, flags)
    next_state = {
        **state,
        "last_monitor_check_at": times["us"],
        "account_snapshot_warning_streak": account_snapshot_warning_streak,
        "monitor_source_warning_streak": monitor_source_warning_streak,
    }

    if not flags:
        previous_codes = _last_alert_codes_from_state(state)
        had_alert = bool(_to_text(state.get("last_monitor_alert_hash")) or _to_text(state.get("last_monitor_issue_at")))
        recovered_bases = {_issue_base_code(item) for item in previous_codes}
        recovery_event: dict[str, Any] = {}
        next_state.update(
            {
                "last_monitor_issue_at": "",
                "last_monitor_alert_hash": "",
                "last_monitor_alert_ms": 0,
                "last_monitor_alert_codes": [],
                "last_monitor_alert_level": "",
                "account_snapshot_warning_streak": 0,
                "monitor_source_warning_streak": 0,
            }
        )
        if had_alert:
            recovery_event = emit_system_event(
                event_type="alert",
                level="info",
                source="ibkr-api",
                title=(
                    "IBKR 行情会话冲突已恢复"
                    if MARKET_DATA_SESSION_CONFLICT_CODE in recovered_bases
                    else "IBKR Monitor 已恢复"
                ),
                detail=(
                    _market_data_conflict_recovery_detail(
                        monitor_payload,
                        timestamp_us=times["us"],
                        recovered_codes=previous_codes,
                    )
                    if MARKET_DATA_SESSION_CONFLICT_CODE in recovered_bases
                    else _monitor_recovery_detail(
                        monitor_payload,
                        timestamp_us=times["us"],
                        recovered_codes=previous_codes,
                    )
                ),
                environment=environment,
            )
            next_state["last_monitor_recovery_at"] = times["us"]
            next_state["last_monitor_recovery_codes"] = previous_codes
        upsert_state(MONITOR_ALERT_STATE_KEY, environment, next_state, times["date"])
        return {
            "ok": True,
            "environment": environment,
            "job_id": "system_monitor_alert_guard",
            "triggered": bool(recovery_event),
            "recovered": bool(recovery_event),
            "flag_codes": [],
            "recovered_flag_codes": previous_codes if recovery_event else [],
            "suppressed_flag_codes": _flag_codes(legacy_suppressed_flags),
            "event": recovery_event,
            "state": next_state,
            "source": "ibkr-api",
        }, 200

    alert_flags, suppressed_flags = _filter_alert_flags(
        flags,
        account_snapshot_warning_streak=account_snapshot_warning_streak,
        account_snapshot_warning_consecutive_count=account_snapshot_warning_consecutive_count,
        monitor_source_warning_streak=monitor_source_warning_streak,
        monitor_source_warning_consecutive_count=monitor_source_warning_consecutive_count,
    )
    if not alert_flags:
        upsert_state(MONITOR_ALERT_STATE_KEY, environment, next_state, times["date"])
        return {
            "ok": True,
            "environment": environment,
            "job_id": "system_monitor_alert_guard",
            "triggered": False,
            "flag_codes": _flag_codes(flags),
            "alert_flag_codes": [],
            "suppressed_flag_codes": _flag_codes(suppressed_flags + legacy_suppressed_flags),
            "account_snapshot_warning_streak": account_snapshot_warning_streak,
            "monitor_source_warning_streak": monitor_source_warning_streak,
            "state": next_state,
            "source": "ibkr-api",
        }, 200

    fingerprint = _fingerprint(monitor_payload, alert_flags)
    last_hash = _to_text(state.get("last_monitor_alert_hash"))
    last_ms = _to_int(state.get("last_monitor_alert_ms"), 0)
    level = "error" if any(_is_error_flag(item) for item in alert_flags) else "warning"
    cooldown_ms = (error_cooldown_min if level == "error" else warning_cooldown_min) * 60 * 1000
    should_notify = fingerprint != last_hash or last_ms <= 0 or (current_ms - last_ms) >= cooldown_ms
    title = f"IBKR Monitor {'严重告警' if level == 'error' else '告警'}（{len(alert_flags)}项）"
    event: dict[str, Any] = {}
    admission_preview: dict[str, Any] = {}
    if should_notify:
        if _should_load_admission_preview(alert_flags):
            admission_preview = _load_admission_preview(
                build_admission_preview,
                environment=environment,
                market_date=_to_text((times or {}).get("date")),
            )
        event = emit_system_event(
            event_type="alert",
            level=level,
            source="ibkr-api",
            title=title,
            detail=_detail(
                monitor_payload,
                alert_flags,
                timestamp_us=times["us"],
                admission_preview=admission_preview,
            ),
            environment=environment,
        )
        next_state.update(
            {
                "last_monitor_issue_at": times["us"],
                "last_monitor_alert_hash": fingerprint,
                "last_monitor_alert_ms": current_ms,
                "last_monitor_alert_codes": _flag_codes(alert_flags),
                "last_monitor_alert_level": level,
                "last_monitor_recovery_at": "",
                "last_monitor_recovery_codes": [],
            }
        )
    upsert_state(MONITOR_ALERT_STATE_KEY, environment, next_state, times["date"])
    return {
        "ok": True,
        "environment": environment,
        "job_id": "system_monitor_alert_guard",
        "triggered": bool(should_notify),
        "flag_codes": _flag_codes(flags),
        "alert_flag_codes": _flag_codes(alert_flags),
        "suppressed_flag_codes": _flag_codes(suppressed_flags + legacy_suppressed_flags),
        "account_snapshot_warning_streak": account_snapshot_warning_streak,
        "monitor_source_warning_streak": monitor_source_warning_streak,
        "admission_preview": admission_preview,
        "event": event,
        "state": next_state,
        "source": "ibkr-api",
    }, 200


__all__ = ["build_system_monitor_alert_guard_response"]
