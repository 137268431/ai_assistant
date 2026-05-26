from __future__ import annotations

from datetime import datetime, timezone

from ibkr_compute.api.support.market_data_session import (
    MARKET_DATA_SESSION_CONFLICT_CODE,
    detect_market_data_session_conflict,
    is_market_data_session_conflict_text,
)


SESSION_UNAUTHENTICATED_GRACE_SECONDS = 300
SESSION_UNAUTHENTICATED_LATE_SESSION_GRACE_SECONDS = 480
WS_SILENCE_REGULAR_WARN_SEC = 60
WS_SILENCE_REGULAR_CRITICAL_SEC = 180
WS_SILENCE_LATE_SESSION_WARN_SEC = 600
WS_SILENCE_LATE_SESSION_CRITICAL_SEC = 1200
WS_SILENCE_REGULAR_WARN_CONFIG_KEY = "system_monitor_ws_message_age_regular_warn_sec"
WS_SILENCE_REGULAR_CRITICAL_CONFIG_KEY = "system_monitor_ws_message_age_regular_critical_sec"
WS_SILENCE_LATE_SESSION_WARN_CONFIG_KEY = "system_monitor_ws_message_age_late_session_warn_sec"
WS_SILENCE_LATE_SESSION_CRITICAL_CONFIG_KEY = "system_monitor_ws_message_age_late_session_critical_sec"


def _parse_monitor_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _should_suppress_session_unauthenticated(runtime_status: dict) -> bool:
    auth_recovery = runtime_status.get("auth_recovery") or {}
    if not auth_recovery:
        return False

    phase = str(auth_recovery.get("recovery_phase") or "").strip().lower()
    recovery_class = str(auth_recovery.get("recovery_class") or "").strip().lower()
    probe_result = str(auth_recovery.get("probe_result") or "").strip().lower()
    interruption_kind = str(auth_recovery.get("interruption_kind") or "").strip().lower()
    auto_restart_scheduled = bool(auth_recovery.get("auto_restart_scheduled"))
    started_at = _parse_monitor_timestamp(
        auth_recovery.get("probe_started_at")
        or auth_recovery.get("probe_last_checked_at")
        or auth_recovery.get("updated_at")
    )
    if started_at is None:
        return False

    market_session = runtime_status.get("market_session") or {}
    market_session_kind = str(market_session.get("kind") or "").strip().lower()
    grace_seconds = SESSION_UNAUTHENTICATED_GRACE_SECONDS
    if market_session_kind in {"close_transition", "afterhours"}:
        grace_seconds = max(
            grace_seconds,
            SESSION_UNAUTHENTICATED_LATE_SESSION_GRACE_SECONDS,
        )

    age_seconds = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
    if age_seconds > grace_seconds:
        return False
    if recovery_class == "manual_auth_required" or phase in {"requested", "manual_takeover"}:
        return False
    if auto_restart_scheduled and recovery_class in {"scheduled_restart", "stale_broker"}:
        return True
    if recovery_class == "stale_broker" and probe_result == "stale_broker_restart_scheduled":
        return True
    if phase != "silent_probe":
        return False
    if probe_result in {"pending", "self_heal", "self_heal_pending"} and (
        recovery_class == "scheduled_restart" or interruption_kind in {"session_expired", "gateway_down"}
    ):
        return True
    return False


def _append_monitor_flag(flags: list[dict], severity: str, code: str, title: str, detail: str) -> None:
    flags.append(
        {
            "severity": severity,
            "code": code,
            "title": title,
            "detail": detail,
        }
    )


def _should_warn_no_active_targets(runtime_status: dict) -> bool:
    market_session = runtime_status.get("market_session") or {}
    market_session_kind = str(market_session.get("kind") or "").strip().lower()
    if not market_session_kind:
        return True
    return market_session_kind == "regular"


def _normalize_ws_silence_thresholds(
    warn_seconds,
    critical_seconds,
    default_warn_seconds: int,
    default_critical_seconds: int,
) -> tuple[int, int]:
    try:
        warn_value = int(warn_seconds)
    except (TypeError, ValueError):
        warn_value = 0
    try:
        critical_value = int(critical_seconds)
    except (TypeError, ValueError):
        critical_value = 0
    if warn_value <= 0 or critical_value <= warn_value:
        return default_warn_seconds, default_critical_seconds
    return warn_value, critical_value


def _resolve_ws_silence_policy_kind(runtime_status: dict) -> str:
    market_session = runtime_status.get("market_session") or {}
    market_session_kind = str(market_session.get("kind") or "").strip().lower()
    if market_session_kind == "closed":
        return "disabled"
    if market_session_kind in {"close_transition", "afterhours"}:
        return "late_session"
    return "regular"


def _default_ws_silence_thresholds(policy_kind: str) -> tuple[int, int]:
    if policy_kind == "late_session":
        return WS_SILENCE_LATE_SESSION_WARN_SEC, WS_SILENCE_LATE_SESSION_CRITICAL_SEC
    if policy_kind == "disabled":
        return 0, 0
    return WS_SILENCE_REGULAR_WARN_SEC, WS_SILENCE_REGULAR_CRITICAL_SEC


def _build_ws_silence_policy(
    runtime_status: dict,
    *,
    api_utilization: dict | None = None,
    config_source=None,
    runtime_environment: str = "live",
) -> dict:
    payload = api_utilization or {}
    explicit_enabled = payload.get("ws_silence_enabled")
    if explicit_enabled is False:
        return {
            "ws_silence_enabled": False,
            "ws_silence_policy": "disabled",
            "ws_silence_warn_sec": 0,
            "ws_silence_critical_sec": 0,
        }

    policy_kind = str(payload.get("ws_silence_policy") or "").strip().lower()
    if not policy_kind:
        policy_kind = _resolve_ws_silence_policy_kind(runtime_status)
    if policy_kind == "disabled":
        return {
            "ws_silence_enabled": False,
            "ws_silence_policy": "disabled",
            "ws_silence_warn_sec": 0,
            "ws_silence_critical_sec": 0,
        }

    default_warn_seconds, default_critical_seconds = _default_ws_silence_thresholds(policy_kind)
    warn_seconds = payload.get("ws_silence_warn_sec")
    critical_seconds = payload.get("ws_silence_critical_sec")
    if warn_seconds is None or critical_seconds is None:
        if config_source is not None and hasattr(config_source, "get_int_for_environment"):
            if policy_kind == "late_session":
                warn_key = WS_SILENCE_LATE_SESSION_WARN_CONFIG_KEY
                critical_key = WS_SILENCE_LATE_SESSION_CRITICAL_CONFIG_KEY
            else:
                warn_key = WS_SILENCE_REGULAR_WARN_CONFIG_KEY
                critical_key = WS_SILENCE_REGULAR_CRITICAL_CONFIG_KEY
            warn_seconds = config_source.get_int_for_environment(
                warn_key,
                runtime_environment,
                default_warn_seconds,
            )
            critical_seconds = config_source.get_int_for_environment(
                critical_key,
                runtime_environment,
                default_critical_seconds,
            )
        else:
            warn_seconds = default_warn_seconds
            critical_seconds = default_critical_seconds

    warn_seconds, critical_seconds = _normalize_ws_silence_thresholds(
        warn_seconds,
        critical_seconds,
        default_warn_seconds,
        default_critical_seconds,
    )
    return {
        "ws_silence_enabled": True,
        "ws_silence_policy": policy_kind,
        "ws_silence_warn_sec": warn_seconds,
        "ws_silence_critical_sec": critical_seconds,
    }


def _format_ws_silence_detail(last_message_age_s, runtime_status: dict, ws_silence_policy: dict) -> str:
    market_session = runtime_status.get("market_session") or {}
    market_session_kind = str(market_session.get("kind") or "").strip().lower() or "unknown"
    warn_seconds = int(ws_silence_policy.get("ws_silence_warn_sec", 0) or 0)
    critical_seconds = int(ws_silence_policy.get("ws_silence_critical_sec", 0) or 0)
    if warn_seconds <= 0 or critical_seconds <= warn_seconds:
        return f"最近一条 WebSocket 消息已经过去 {last_message_age_s}s。"
    return (
        f"最近一条 WebSocket 消息已经过去 {last_message_age_s}s"
        f"（session={market_session_kind}，warning={warn_seconds}s，critical={critical_seconds}s）。"
    )


def _stale_control_symbols(sample_payload: dict) -> tuple[list[str], int]:
    active_subscriptions = sample_payload.get("active_subscriptions") or []
    if isinstance(active_subscriptions, list) and active_subscriptions:
        symbols: list[str] = []
        monitor_count = 0
        seen: set[str] = set()
        for item in active_subscriptions:
            if not isinstance(item, dict) or not item.get("stale"):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            role = str(item.get("role") or "").strip().lower()
            if role == "market_monitor":
                monitor_count += 1
                continue
            if symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
        return symbols, monitor_count

    fallback_symbols = []
    seen: set[str] = set()
    for symbol in sample_payload.get("stale_symbols") or []:
        normalized = str(symbol or "").strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        fallback_symbols.append(normalized)
    return fallback_symbols, 0


def _format_resource_governor_detail(resource_governor: dict) -> str:
    status = str(resource_governor.get("status") or "unknown").strip().lower() or "unknown"
    reasons = resource_governor.get("reasons") or []
    reason_parts = []
    if isinstance(reasons, list):
        for item in reasons[:3]:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            message = str(item.get("message") or "").strip()
            actual = item.get("actual")
            if actual is None:
                actual = item.get("metric")
            threshold = item.get("threshold")
            metric_detail = ""
            if actual is not None and threshold is not None:
                metric_detail = f" actual={actual}, threshold={threshold}"
            text = code or message
            if code and message:
                text = f"{code}: {message}"
            if text:
                reason_parts.append(f"{text}{metric_detail}")
    if not reason_parts:
        return f"Resource governor status={status}。"
    more_count = max(0, len(reasons) - len(reason_parts)) if isinstance(reasons, list) else 0
    more_suffix = f"；另有 {more_count} 项" if more_count else ""
    return f"Resource governor status={status}：{'；'.join(reason_parts)}{more_suffix}。"


def _append_resource_governor_flag(flags: list[dict], runtime_status: dict) -> bool:
    resource_governor = runtime_status.get("resource_governor")
    if not isinstance(resource_governor, dict):
        return False
    status = str(resource_governor.get("status") or "").strip().lower()
    if status == "critical":
        _append_monitor_flag(
            flags,
            "error",
            "resource_governor_critical",
            "Resource governor critical",
            _format_resource_governor_detail(resource_governor),
        )
        return True
    if status == "warning":
        _append_monitor_flag(
            flags,
            "warning",
            "resource_governor_warning",
            "Resource governor warning",
            _format_resource_governor_detail(resource_governor),
        )
        return True
    return False


def _build_monitor_flags(runtime_status: dict, api_utilization: dict, host_snapshot: dict, sample_payload: dict) -> list[dict]:
    flags = []
    gateway = runtime_status.get("gateway") or {}
    session = runtime_status.get("session") or {}
    websocket = runtime_status.get("websocket") or {}
    market_universe = runtime_status.get("market_universe") or {}

    gateway_active = bool(gateway.get("running") or gateway.get("reachable"))
    if not gateway_active:
        _append_monitor_flag(
            flags,
            "error",
            "gateway_offline",
            "Gateway offline",
            "Gateway 既不在运行也不可达，IBKR 链路当前不可用。",
        )

    if gateway_active and not bool(session.get("authenticated")) and not _should_suppress_session_unauthenticated(runtime_status):
        _append_monitor_flag(
            flags,
            "warning",
            "session_unauthenticated",
            "Session unauthenticated",
            "Gateway 已在线，但当前 Session 尚未认证，订阅与交易链路会降级。",
        )

    if not bool(websocket.get("connected")) or not bool(websocket.get("ready")):
        _append_monitor_flag(
            flags,
            "error",
            "websocket_not_ready",
            "WebSocket not ready",
            "实时行情 WebSocket 未连通或未进入 ready 状态。",
        )

    trade_utilization_pct = api_utilization.get("trade_utilization_pct")
    if trade_utilization_pct is None:
        trade_utilization_pct = api_utilization.get("utilization_pct")
    trade_subscription_limit = int(
        api_utilization.get("trade_subscription_limit")
        or api_utilization.get("subscription_limit", 0)
        or 0
    )
    active_subscription_count = int(api_utilization.get("active_subscription_count", 0) or 0)
    active_trade_symbol_count = int(
        api_utilization.get("active_trade_symbol_count")
        if api_utilization.get("active_trade_symbol_count") is not None
        else active_subscription_count
    )
    active_monitor_symbol_count = int(
        api_utilization.get("active_monitor_symbol_count")
        or max(0, active_subscription_count - active_trade_symbol_count)
        or 0
    )
    if trade_utilization_pct is not None and trade_subscription_limit > 0:
        detail_suffix = (
            f" 另有固定 market monitor {active_monitor_symbol_count} 个。"
            if active_monitor_symbol_count > 0 else ""
        )
        if active_trade_symbol_count > trade_subscription_limit:
            _append_monitor_flag(
                flags,
                "error",
                "subscription_utilization_critical",
                "Subscription utilization critical",
                (
                    f"当前 trade 订阅占用 {active_trade_symbol_count}/{trade_subscription_limit} "
                    f"({trade_utilization_pct:.2f}%) ，已经超过上限。{detail_suffix}"
                ),
            )
        elif active_trade_symbol_count >= trade_subscription_limit:
            _append_monitor_flag(
                flags,
                "warning",
                "subscription_utilization_high",
                "Subscription utilization high",
                (
                    f"当前 trade 订阅占用 {active_trade_symbol_count}/{trade_subscription_limit} "
                    f"({trade_utilization_pct:.2f}%) ，已经达到上限。{detail_suffix}"
                ),
            )

    pending_subscription_count = int(api_utilization.get("pending_subscription_count", 0) or 0)
    if pending_subscription_count > 0:
        _append_monitor_flag(
            flags,
            "warning",
            "pending_subscriptions",
            "Pending subscriptions",
            f"当前还有 {pending_subscription_count} 个待完成订阅。",
        )

    if bool(market_universe.get("no_active_targets")) and _should_warn_no_active_targets(runtime_status):
        inactive_total = int(market_universe.get("inactive_trade_symbols_total", 0) or 0)
        sample_symbols = [
            str(symbol or "").strip().upper()
            for symbol in (market_universe.get("inactive_trade_symbols_sample") or [])
            if str(symbol or "").strip()
        ]
        sample_suffix = f" 示例: {', '.join(sample_symbols[:8])}。" if sample_symbols else ""
        _append_monitor_flag(
            flags,
            "warning",
            "no_active_targets",
            "No active trade targets",
            (
                "watchlist 中存在交易标的，但当前 active target 数为 0，"
                "实时信号生成没有可交易目标。"
                f"未激活交易标的数 {inactive_total}。{sample_suffix}"
            ),
        )

    if bool(market_universe.get("no_execution_eligible_targets")) and _should_warn_no_active_targets(runtime_status):
        active_total = int(market_universe.get("active_target_count", 0) or 0)
        observe_total = int(market_universe.get("observe_target_count", active_total) or 0)
        sample_symbols = [
            str(symbol or "").strip().upper()
            for symbol in (market_universe.get("observe_target_symbols") or market_universe.get("active_trade_symbols") or [])
            if str(symbol or "").strip()
        ]
        sample_suffix = f" 示例: {', '.join(sample_symbols[:8])}。" if sample_symbols else ""
        _append_monitor_flag(
            flags,
            "warning",
            "no_execution_eligible_targets",
            "No executable trade targets",
            (
                f"当前 active target 有 {active_total} 个，但 execution_eligible 数为 0，"
                f"这些标的只会观察/回补，不会自动入场。observe {observe_total}。{sample_suffix}"
            ),
        )

    last_trace_retry_count = int(api_utilization.get("last_trace_retry_count", 0) or 0)
    last_trace_throttle_count = int(api_utilization.get("last_trace_throttle_count", 0) or 0)
    last_trace_error = str(api_utilization.get("last_trace_error") or "").strip()
    session_conflict = detect_market_data_session_conflict(runtime_status)
    data_backfill_present = isinstance(runtime_status.get("data_backfill"), dict) and bool(runtime_status.get("data_backfill"))
    fallback_session_conflict = is_market_data_session_conflict_text(last_trace_error) and not data_backfill_present
    if bool(session_conflict.get("active")) or fallback_session_conflict:
        conflict_detail = str(session_conflict.get("message") or last_trace_error or "").strip()
        _append_monitor_flag(
            flags,
            "error",
            MARKET_DATA_SESSION_CONFLICT_CODE,
            "Market data session conflict",
            (
                "IBKR 行情/历史数据会话疑似被另一个 IP 占用；"
                "请退出其他电脑/服务器上的 TWS、IB Gateway 或 IBKR Desktop 后，再重启 Gateway。"
                f" 原始错误：{conflict_detail or '--'}。"
            ),
        )
    elif last_trace_error or last_trace_retry_count > 0:
        _append_monitor_flag(
            flags,
            "warning",
            "history_request_retry_or_error",
            "History request retry/error",
            (
                f"最近一次历史回填 trace 出现 retry={last_trace_retry_count}、"
                f"throttle={last_trace_throttle_count}、error={last_trace_error or '--'}。"
            ),
        )

    last_message_age_s = api_utilization.get("last_message_age_s")
    active_subscription_count = int(api_utilization.get("active_subscription_count", 0) or 0)
    ws_silence_policy = _build_ws_silence_policy(runtime_status, api_utilization=api_utilization)
    if (
        bool(session.get("authenticated"))
        and active_subscription_count > 0
        and last_message_age_s is not None
        and bool(ws_silence_policy.get("ws_silence_enabled"))
    ):
        critical_seconds = int(ws_silence_policy.get("ws_silence_critical_sec", 0) or 0)
        warn_seconds = int(ws_silence_policy.get("ws_silence_warn_sec", 0) or 0)
        detail = _format_ws_silence_detail(last_message_age_s, runtime_status, ws_silence_policy)
        if float(last_message_age_s) > critical_seconds:
            _append_monitor_flag(
                flags,
                "error",
                "market_data_silent_critical",
                "Market data silent",
                detail,
            )
        elif float(last_message_age_s) > warn_seconds:
            _append_monitor_flag(
                flags,
                "warning",
                "market_data_silent",
                "Market data slowed",
                detail,
            )

    active_bar_symbols = sample_payload.get("active_bar_symbols") or []
    if active_bar_symbols:
        max_active_bar_age_s = max(float(item.get("last_update_age_s", 0) or 0) for item in active_bar_symbols)
        max_active_bar_age_min = round(max_active_bar_age_s / 60.0, 2)
        if max_active_bar_age_min > 15:
            _append_monitor_flag(
                flags,
                "error",
                "data_freshness_offline",
                "Data freshness offline",
                f"活跃订阅里最慢的 symbol 已经 {max_active_bar_age_min} 分钟没有更新。",
            )
        elif max_active_bar_age_min > 5:
            _append_monitor_flag(
                flags,
                "warning",
                "data_freshness_delayed",
                "Data freshness delayed",
                f"活跃订阅里最慢的 symbol 已经 {max_active_bar_age_min} 分钟没有更新。",
            )

    stale_symbols, stale_monitor_count = _stale_control_symbols(sample_payload)
    if stale_symbols:
        monitor_suffix = (
            f" 另有 {stale_monitor_count} 个 market monitor 缺少实时样本，按监控数据不可用记录，不触发控制面告警。"
            if stale_monitor_count else ""
        )
        _append_monitor_flag(
            flags,
            "warning",
            "stale_active_symbols",
            "Stale active symbols",
            (
                f"当前有 {len(stale_symbols)} 个交易/控制订阅 symbol 没有出现在实时样本"
                f"（quote / active bar / warmup / canonical 5m）中。{monitor_suffix}"
            ),
        )

    governor_flagged = _append_resource_governor_flag(flags, runtime_status)
    if not governor_flagged:
        memory_used_pct = (host_snapshot.get("memory") or {}).get("used_pct")
        if memory_used_pct is not None:
            if float(memory_used_pct) >= 90:
                _append_monitor_flag(
                    flags,
                    "error",
                    "host_memory_critical",
                    "Host memory critical",
                    f"主机内存占用 {memory_used_pct:.2f}% 。",
                )
            elif float(memory_used_pct) >= 80:
                _append_monitor_flag(
                    flags,
                    "warning",
                    "host_memory_high",
                    "Host memory high",
                    f"主机内存占用 {memory_used_pct:.2f}% 。",
                )

        disk_used_pct = (host_snapshot.get("disk") or {}).get("used_pct")
        if disk_used_pct is not None:
            if float(disk_used_pct) >= 92:
                _append_monitor_flag(
                    flags,
                    "error",
                    "host_disk_critical",
                    "Host disk critical",
                    f"磁盘占用 {disk_used_pct:.2f}% 。",
                )
            elif float(disk_used_pct) >= 85:
                _append_monitor_flag(
                    flags,
                    "warning",
                    "host_disk_high",
                    "Host disk high",
                    f"磁盘占用 {disk_used_pct:.2f}% 。",
                )

        load_per_cpu = (host_snapshot.get("loadavg") or {}).get("per_cpu_1")
        cpu_count = int(host_snapshot.get("cpu_count", 0) or 0)
        if load_per_cpu is not None and cpu_count > 0:
            if float(load_per_cpu) >= 1.5:
                _append_monitor_flag(
                    flags,
                    "error",
                    "host_load_critical",
                    "Host load critical",
                    f"1 分钟 load / CPU = {load_per_cpu:.3f} 。",
                )
            elif float(load_per_cpu) >= 1.0:
                _append_monitor_flag(
                    flags,
                    "warning",
                    "host_load_high",
                    "Host load high",
                    f"1 分钟 load / CPU = {load_per_cpu:.3f} 。",
                )

        cpu_used_pct = (host_snapshot.get("cpu") or {}).get("used_pct")
        if cpu_used_pct is not None:
            if float(cpu_used_pct) >= 95:
                _append_monitor_flag(
                    flags,
                    "error",
                    "host_cpu_critical",
                    "Host CPU critical",
                    f"主机 CPU 占用 {cpu_used_pct:.2f}% 。",
                )
            elif float(cpu_used_pct) >= 85:
                _append_monitor_flag(
                    flags,
                    "warning",
                    "host_cpu_high",
                    "Host CPU high",
                    f"主机 CPU 占用 {cpu_used_pct:.2f}% 。",
                )

    if not flags and int(market_universe.get("active_subscription_count", 0) or 0) > 0:
        _append_monitor_flag(
            flags,
            "info",
            "monitor_nominal",
            "Monitor nominal",
            "当前没有触发阈值告警，链路处于可用状态。",
        )
    return flags


def _derive_monitor_status(flags: list[dict]) -> str:
    severities = {str(item.get("severity") or "").lower() for item in flags or []}
    if "error" in severities:
        return "error"
    if "warning" in severities:
        return "warning"
    return "ok"
