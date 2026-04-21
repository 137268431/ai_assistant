from __future__ import annotations

from datetime import datetime, timezone


SESSION_UNAUTHENTICATED_GRACE_SECONDS = 300
SESSION_UNAUTHENTICATED_LATE_SESSION_GRACE_SECONDS = 480


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
    trade_subscription_limit = int(
        api_utilization.get("trade_subscription_limit")
        or api_utilization.get("subscription_limit", 0)
        or 0
    )
    active_trade_symbol_count = int(api_utilization.get("active_trade_symbol_count", 0) or 0)
    active_subscription_count = int(api_utilization.get("active_subscription_count", 0) or 0)
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

    throttle_count = int(api_utilization.get("throttle_count", 0) or 0)
    if throttle_count > 0:
        _append_monitor_flag(
            flags,
            "warning",
            "history_throttle_detected",
            "History throttle detected",
            f"历史回填已累计出现 {throttle_count} 次节流。",
        )

    last_message_age_s = api_utilization.get("last_message_age_s")
    active_subscription_count = int(api_utilization.get("active_subscription_count", 0) or 0)
    if bool(session.get("authenticated")) and active_subscription_count > 0 and last_message_age_s is not None:
        if float(last_message_age_s) > 180:
            _append_monitor_flag(
                flags,
                "error",
                "market_data_silent_critical",
                "Market data silent",
                f"最近一条 WebSocket 消息已经过去 {last_message_age_s}s。",
            )
        elif float(last_message_age_s) > 60:
            _append_monitor_flag(
                flags,
                "warning",
                "market_data_silent",
                "Market data slowed",
                f"最近一条 WebSocket 消息已经过去 {last_message_age_s}s。",
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

    stale_symbols = sample_payload.get("stale_symbols") or []
    if stale_symbols:
        _append_monitor_flag(
            flags,
            "warning",
            "stale_active_symbols",
            "Stale active symbols",
            f"当前有 {len(stale_symbols)} 个已订阅 symbol 没有出现在实时样本（quote / active bar / warmup ready）中。",
        )

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
