from __future__ import annotations


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

    if gateway_active and not bool(session.get("authenticated")):
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

    utilization_pct = api_utilization.get("utilization_pct")
    subscription_limit = int(api_utilization.get("subscription_limit", 0) or 0)
    active_subscription_count = int(api_utilization.get("active_subscription_count", 0) or 0)
    if utilization_pct is not None and subscription_limit > 0:
        if active_subscription_count > subscription_limit:
            _append_monitor_flag(
                flags,
                "error",
                "subscription_utilization_critical",
                "Subscription utilization critical",
                f"当前订阅占用 {active_subscription_count}/{subscription_limit} ({utilization_pct:.2f}%) ，已经超过上限。",
            )
        elif active_subscription_count >= subscription_limit:
            _append_monitor_flag(
                flags,
                "warning",
                "subscription_utilization_high",
                "Subscription utilization high",
                f"当前订阅占用 {active_subscription_count}/{subscription_limit} ({utilization_pct:.2f}%) ，已经达到上限。",
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
            f"当前有 {len(stale_symbols)} 个已订阅 symbol 没有出现在活跃 bar 列表。",
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
