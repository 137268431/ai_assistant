from __future__ import annotations

import os
import platform
import resource
import shutil
import socket
import sys
import threading
import time
from datetime import datetime, timezone


def _api_app():
    from . import app as api_app

    return api_app


def _copy_active_subscription_map(service) -> dict:
    lock = getattr(service, "_subscription_lock", None)
    if lock:
        with lock:
            return dict(getattr(service, "_active_subscription_map", {}) or {})
    return dict(getattr(service, "_active_subscription_map", {}) or {})


def _normalize_symbol_list(values) -> list[str]:
    seen = set()
    normalized = []
    for item in values or []:
        symbol = str(item or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    normalized.sort()
    return normalized


def _parse_proc_kv_text(raw_text: str) -> dict[str, str]:
    payload = {}
    for line in str(raw_text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        payload[str(key).strip()] = str(value).strip()
    return payload


def _parse_meminfo_text(raw_text: str) -> dict[str, int]:
    payload = {}
    for key, value in _parse_proc_kv_text(raw_text).items():
        number_text = str(value).split()[0]
        try:
            payload[str(key)] = int(number_text) * 1024
        except (TypeError, ValueError):
            continue
    return payload


def _read_proc_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _read_proc_cpu_times() -> dict | None:
    raw_text = _read_proc_text("/proc/stat")
    if not raw_text:
        return None
    for line in raw_text.splitlines():
        if not line.startswith("cpu "):
            continue
        parts = line.split()
        if len(parts) < 5:
            return None
        try:
            values = [int(item) for item in parts[1:]]
        except (TypeError, ValueError):
            return None
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return {
            "total": total,
            "idle": idle,
            "sampled_at": time.time(),
        }
    return None


def _build_cpu_usage_snapshot(previous: dict | None, current: dict | None, source: str = "/proc/stat") -> dict:
    if not previous or not current:
        return {
            "used_pct": None,
            "idle_pct": None,
            "sample_span_s": None,
            "source": source,
        }

    total_delta = int(current.get("total", 0) or 0) - int(previous.get("total", 0) or 0)
    idle_delta = int(current.get("idle", 0) or 0) - int(previous.get("idle", 0) or 0)
    sample_span_s = max(0.0, float(current.get("sampled_at", 0) or 0) - float(previous.get("sampled_at", 0) or 0))
    if total_delta <= 0:
        return {
            "used_pct": None,
            "idle_pct": None,
            "sample_span_s": round(sample_span_s, 3) if sample_span_s > 0 else None,
            "source": source,
        }

    used_pct = max(0.0, min(100.0, ((total_delta - idle_delta) / total_delta) * 100.0))
    idle_pct = max(0.0, min(100.0, (idle_delta / total_delta) * 100.0))
    return {
        "used_pct": round(used_pct, 2),
        "idle_pct": round(idle_pct, 2),
        "sample_span_s": round(sample_span_s, 3) if sample_span_s > 0 else None,
        "source": source,
    }


def _collect_cpu_usage_snapshot(prime_interval_s: float = 0.05) -> dict:
    api_app = _api_app()
    with api_app.host_cpu_snapshot_lock:
        previous = api_app.host_cpu_snapshot_cache
        current = _read_proc_cpu_times()
        if not current:
            return {
                "used_pct": None,
                "idle_pct": None,
                "sample_span_s": None,
                "source": "unavailable",
            }

        if previous is None and prime_interval_s > 0:
            previous = current
            time.sleep(prime_interval_s)
            current = _read_proc_cpu_times() or current

        api_app.host_cpu_snapshot_cache = current

    return _build_cpu_usage_snapshot(previous, current)


def _collect_host_memory_snapshot() -> dict:
    meminfo = _parse_meminfo_text(_read_proc_text("/proc/meminfo"))
    total_bytes = int(meminfo.get("MemTotal", 0) or 0)
    available_bytes = int(meminfo.get("MemAvailable", meminfo.get("MemFree", 0)) or 0)
    if total_bytes <= 0:
        return {
            "total_bytes": None,
            "available_bytes": None,
            "used_bytes": None,
            "used_pct": None,
            "source": "unavailable",
        }
    used_bytes = max(0, total_bytes - max(0, available_bytes))
    return {
        "total_bytes": total_bytes,
        "available_bytes": max(0, available_bytes),
        "used_bytes": used_bytes,
        "used_pct": round((used_bytes / total_bytes) * 100.0, 2),
        "source": "/proc/meminfo",
    }


def _collect_disk_snapshot(path: str | None = None) -> dict:
    target_path = str(path or os.environ.get("IBKR_MONITOR_DISK_PATH", "/") or "/")
    try:
        usage = shutil.disk_usage(target_path)
    except OSError:
        return {
            "path": target_path,
            "total_bytes": None,
            "free_bytes": None,
            "used_bytes": None,
            "used_pct": None,
        }
    used_bytes = max(0, int(usage.total or 0) - int(usage.free or 0))
    used_pct = round((used_bytes / usage.total) * 100.0, 2) if usage.total else None
    return {
        "path": target_path,
        "total_bytes": int(usage.total or 0),
        "free_bytes": int(usage.free or 0),
        "used_bytes": used_bytes,
        "used_pct": used_pct,
    }


def _collect_load_snapshot() -> dict:
    cpu_count = max(1, int(os.cpu_count() or 1))
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = None
    return {
        "cpu_count": cpu_count,
        "loadavg": {
            "1": round(load1, 2) if load1 is not None else None,
            "5": round(load5, 2) if load5 is not None else None,
            "15": round(load15, 2) if load15 is not None else None,
            "per_cpu_1": round(load1 / cpu_count, 3) if load1 is not None and cpu_count > 0 else None,
        },
    }


def _resource_rss_bytes() -> int | None:
    try:
        rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)
    except Exception:
        return None
    if rss <= 0:
        return None
    if sys.platform == "darwin":
        return rss
    return rss * 1024


def _collect_process_snapshot() -> dict:
    api_app = _api_app()
    proc_status = _parse_proc_kv_text(_read_proc_text("/proc/self/status"))
    thread_count = None
    try:
        thread_count = int(proc_status.get("Threads", "0") or 0)
    except (TypeError, ValueError):
        thread_count = None
    fd_count = None
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            fd_count = len([name for name in os.listdir(path) if name not in {".", ".."}])
            break
        except OSError:
            continue
    return {
        "pid": os.getpid(),
        "uptime_s": round(time.time() - api_app._start_time, 1),
        "rss_bytes": _resource_rss_bytes(),
        "threads": thread_count if thread_count is not None else threading.active_count(),
        "fd_count": fd_count,
    }


def _collect_host_snapshot() -> dict:
    load_snapshot = _collect_load_snapshot()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpu_count": load_snapshot["cpu_count"],
        "cpu": _collect_cpu_usage_snapshot(),
        "loadavg": load_snapshot["loadavg"],
        "memory": _collect_host_memory_snapshot(),
        "disk": _collect_disk_snapshot(),
        "process": _collect_process_snapshot(),
    }


def _build_compute_summary() -> dict:
    api_app = _api_app()
    return {
        "status": "running",
        "total_engines": len(api_app.engines),
        "ready_engines": sum(1 for engine in api_app.engines.values() if engine.is_ready()),
        "tracked_cursors": len(api_app.last_processed_ms),
        "compute_count": api_app.compute_count,
        "error_count": api_app.error_count,
        "last_compute": datetime.fromtimestamp(api_app.last_compute_time, timezone.utc).isoformat() if api_app.last_compute_time else None,
        "last_scan": datetime.fromtimestamp(api_app.last_scan_time, timezone.utc).isoformat() if api_app.last_scan_time else None,
        "uptime_s": round(time.time() - api_app._start_time, 1),
    }


def _build_monitor_samples(service, runtime_status: dict) -> dict:
    api_app = _api_app()
    warmup = runtime_status.get("warmup") or {}
    market_universe = runtime_status.get("market_universe") or {}
    bar_aggregator = runtime_status.get("bar_aggregator") or {}
    realtime_quotes = runtime_status.get("realtime_quotes") or {}
    active_bars = bar_aggregator.get("active_bars") or {}
    quote_map = realtime_quotes.get("quotes") or {}
    if not isinstance(active_bars, dict):
        active_bars = {}
    if not isinstance(quote_map, dict):
        quote_map = {}

    subscription_map = _copy_active_subscription_map(service)
    trade_symbols = set(_normalize_symbol_list(market_universe.get("active_trade_symbols") or []))
    monitor_symbols = set(_normalize_symbol_list(warmup.get("monitor_symbols") or []))
    visible_symbols = set(_normalize_symbol_list(quote_map.keys()))

    active_subscriptions = []
    for symbol in sorted(subscription_map.keys()):
        conid = subscription_map.get(symbol)
        bar_info = active_bars.get(symbol) or active_bars.get(symbol.upper()) or {}
        quote_info = quote_map.get(symbol) or quote_map.get(symbol.upper()) or {}
        quote_age_s = (
            round(float(quote_info.get("quote_age_s")), 1)
            if isinstance(quote_info, dict) and quote_info.get("quote_age_s") is not None
            else None
        )
        role = (
            api_app.WATCHLIST_SYMBOL_ROLE_TRADE
            if symbol in trade_symbols
            else (api_app.WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR if symbol in monitor_symbols else "subscription")
        )
        active_subscriptions.append(
            {
                "symbol": symbol,
                "conid": int(conid) if conid is not None else None,
                "role": role,
                "visible": symbol in visible_symbols,
                "stale": symbol not in visible_symbols,
                "quote_age_s": quote_age_s,
                "last_update_age_s": (
                    round(float(bar_info.get("last_update_age_s")), 1)
                    if isinstance(bar_info, dict) and bar_info.get("last_update_age_s") is not None
                    else None
                ),
                "last_price": api_app._coerce_float(quote_info.get("last_price")),
                "day_change_pct": api_app._coerce_float(quote_info.get("day_change_pct")),
                "tick_count": int(bar_info.get("tick_count", 0) or 0) if isinstance(bar_info, dict) else 0,
                "volume_updates": int(bar_info.get("volume_updates", 0) or 0) if isinstance(bar_info, dict) else 0,
            }
        )

    active_bar_symbols = []
    for symbol, item in sorted(active_bars.items()):
        if not isinstance(item, dict):
            continue
        active_bar_symbols.append(
            {
                "symbol": str(symbol or "").strip().upper(),
                "last_update_age_s": round(float(item.get("last_update_age_s", 0) or 0), 1),
                "tick_count": int(item.get("tick_count", 0) or 0),
                "volume_updates": int(item.get("volume_updates", 0) or 0),
                "interval_start": item.get("interval_start"),
            }
        )

    repair_reasons = [
        {
            "symbol": str(symbol or "").strip().upper(),
            "reason": str(reason or ""),
        }
        for symbol, reason in sorted((market_universe.get("last_active_repair_reasons") or {}).items())
    ]

    stale_symbols = [
        item["symbol"]
        for item in active_subscriptions
        if item.get("stale")
    ]

    return {
        "active_subscriptions": active_subscriptions,
        "active_bar_symbols": active_bar_symbols,
        "pending_symbols": _normalize_symbol_list(warmup.get("pending_symbols") or []),
        "stale_symbols": stale_symbols,
        "repair_reasons": repair_reasons,
    }


def _build_api_utilization_snapshot(service, runtime_environment: str, runtime_status: dict, sample_payload: dict) -> dict:
    api_app = _api_app()
    websocket = runtime_status.get("websocket") or {}
    market_universe = runtime_status.get("market_universe") or {}
    data_backfill = runtime_status.get("data_backfill") or {}
    active_subscription_count = int(
        market_universe.get("active_subscription_count")
        or len(sample_payload.get("active_subscriptions") or [])
        or 0
    )
    ws_subscribed_count = int(
        websocket.get("subscribed_count")
        or len(websocket.get("subscribed_conids") or [])
        or 0
    )
    pending_subscription_count = int(
        websocket.get("pending_count")
        or len(websocket.get("pending_conids") or [])
        or 0
    )
    config_source = getattr(service, "config", None) or api_app.cfg
    if hasattr(config_source, "refresh"):
        try:
            config_source.refresh()
        except Exception:
            pass
    subscription_limit = max(
        0,
        int(config_source.get_int_for_environment("ibkr_target_subscription_limit", runtime_environment, 60) or 0),
    )
    utilization_pct = round((active_subscription_count / subscription_limit) * 100.0, 2) if subscription_limit > 0 else None
    return {
        "subscription_limit": subscription_limit,
        "active_subscription_count": active_subscription_count,
        "active_trade_symbol_count": int(market_universe.get("active_target_count") or 0),
        "ws_subscribed_count": ws_subscribed_count,
        "pending_subscription_count": pending_subscription_count,
        "utilization_pct": utilization_pct,
        "request_count": int(data_backfill.get("request_count", 0) or 0),
        "retry_count": int(data_backfill.get("retry_count", 0) or 0),
        "throttle_count": int(data_backfill.get("throttle_count", 0) or 0),
        "request_spacing_s": float(data_backfill.get("request_spacing_s", 0) or 0),
        "max_concurrency": int(data_backfill.get("max_concurrency", 0) or 0),
        "websocket_message_count": int(websocket.get("message_count", 0) or 0),
        "order_update_count": int(websocket.get("order_update_count", 0) or 0),
        "last_message": websocket.get("last_message"),
        "last_message_age_s": websocket.get("last_message_age_s"),
        "last_tic": websocket.get("last_tic"),
        "last_tic_age_s": websocket.get("last_tic_age_s"),
    }


def _build_uninitialized_runtime_status(runtime_environment: str, error: str | None = None) -> dict:
    api_app = _api_app()
    detail = str(error or "IBKR service not initialized").strip() or "IBKR service not initialized"
    manual_start_restart_gateway = api_app.cfg.get_bool_for_environment("ibkr_manual_start_restart_gateway", runtime_environment, True)
    weekly_reauth_restart_gateway = api_app.cfg.get_bool_for_environment("ibkr_weekly_reauth_restart_gateway", runtime_environment, True)
    server_boot_resume_only = api_app.cfg.get_bool_for_environment("ibkr_server_boot_resume_only", runtime_environment, True)
    server_boot_publish_startup_card = api_app.cfg.get_bool_for_environment("ibkr_server_boot_publish_startup_card", runtime_environment, False)
    startup_strategy_summary = (
        "手动启动 / 每周重验 / Gateway 重启走 fresh cycle；"
        "server_boot 默认只做 resume，不主动新开 2FA。"
    )
    if not server_boot_resume_only:
        startup_strategy_summary = "手动启动、每周重验、Gateway 重启与 server_boot 都会走 fresh cycle。"
    elif server_boot_publish_startup_card:
        startup_strategy_summary = (
            "手动启动 / 每周重验 / Gateway 重启走 fresh cycle；"
            "server_boot 默认只做 resume，但会同步发送启动卡片。"
        )
    return {
        "ok": False,
        "environment": api_app._normalize_runtime_environment_name(runtime_environment, "live"),
        "error": detail,
        "gateway_control_available": True,
        "startup_strategy": {
            "manual_start_mode": "fresh_cycle" if manual_start_restart_gateway else "resume_only",
            "weekly_reauth_mode": "fresh_cycle" if weekly_reauth_restart_gateway else "resume_only",
            "manual_gateway_restart_mode": "fresh_cycle",
            "server_boot_mode": "resume_only" if server_boot_resume_only else "fresh_cycle",
            "server_boot_publish_startup_card": bool(server_boot_publish_startup_card),
            "fresh_cycle_requires_manual_2fa": True,
            "startup_card_scope": "all_startups" if server_boot_publish_startup_card else "fresh_cycles_only",
            "summary": startup_strategy_summary,
        },
        "auto_restore_guard": {
            "allowed": True,
            "blocked": False,
            "reasons": [],
            "startup_active": False,
            "startup_status": "",
            "startup_label": "",
            "auth_recovery_phase": "",
            "auth_recovery_lock_owner": "",
        },
        "starting": False,
        "startup_complete": False,
        "runtime_phase": "stopped",
        "gateway": {
            "managed_by": "",
            "pid": 0,
            "reachable": False,
            "running": False,
            "status_code": 0,
            "uptime_s": 0,
        },
        "session": {
            "authenticated": False,
            "consecutive_failures": 0,
            "last_tickle": "",
            "running": False,
        },
        "websocket": {
            "connected": False,
            "last_message": "",
            "message_count": 0,
            "order_update_count": 0,
            "pending_count": 0,
            "ping_interval_s": 45,
            "ready": False,
            "running": False,
            "subscribed_count": 0,
        },
        "bar_aggregator": {
            "active_bars": {},
        },
        "data_backfill": {
            "max_concurrency": 0,
            "request_count": 0,
            "request_spacing_s": 0,
            "retry_count": 0,
            "throttle_count": 0,
            "total_backfilled": 0,
        },
        "order_tracker": {
            "last_poll": "",
            "running": False,
            "tracked_orders": 0,
        },
        "signal_router": {
            "last_poll": "",
            "running": False,
        },
        "daily_scan": {
            "market_date": "",
            "status": "idle",
            "reason": detail,
            "started_at": "",
            "finished_at": "",
            "last_error": detail,
            "result": {},
        },
        "realtime_compute": {
            "last_bar_close": "",
            "last_elapsed_s": 0,
            "last_errors": 0,
            "last_processed": 0,
            "last_run": "",
            "last_signals": 0,
            "queue_size": 0,
            "runs": 0,
        },
        "market_universe": {
            "pipeline_stage": "resolve_universe",
            "pipeline_status": "idle",
            "active_repair_interval_min": 0,
            "active_subscription_count": 0,
            "active_target_count": 0,
            "active_target_date": "",
            "active_trade_symbols": [],
            "data_symbols_total": 0,
            "scan_symbols_total": 0,
            "market_ws_symbols_total": 0,
            "data_symbols": [],
            "scan_symbols": [],
            "market_ws_symbols": [],
            "last_successful_scan_market_date": "",
            "last_successful_scan_at": "",
            "bar_freshness": {
                "status": "stale",
                "lag_s": 0,
                "last_completed_bucket_us": "",
                "pending_symbols_total": 0,
            },
            "indicator_freshness": {
                "status": "stale",
                "lag_since_last_run_s": 0,
                "last_run": "",
                "stalled": False,
                "stall_reason": "",
            },
            "last_active_repair": "",
            "last_active_repair_reasons": {},
            "last_active_repair_symbols": [],
            "last_active_repair_symbols_total": 0,
            "last_daily_reset": "",
            "last_target_refresh": "",
            "last_watchlist_backfill": "",
            "market_date": "",
            "watchlist_backfill_interval_min": 0,
            "watchlist_pool_count": 0,
        },
        "warmup": {
            "finished_at": "",
            "integrity_pending_symbols": [],
            "integrity_pending_symbols_total": 0,
            "integrity_repair_reasons": {},
            "last_error": detail,
            "last_success_at": "",
            "monitor_symbols": [],
            "monitor_symbols_total": 0,
            "pending_symbols": [],
            "pending_symbols_total": 0,
            "phase": "idle",
            "preflight_repair": {},
            "ready_scan_symbols": 0,
            "ready_subscription_symbols": 0,
            "ready_monitor_symbols": 0,
            "ready_symbols": 0,
            "ready_symbols_list": [],
            "ready_trade_symbols": 0,
            "reason": detail,
            "requested_at": "",
            "required_interval": "",
            "scan_symbols": [],
            "scan_symbols_total": 0,
            "started_at": "",
            "subscription_symbols": [],
            "subscription_symbols_total": 0,
            "symbol_status": [],
            "symbols": [],
            "symbols_total": 0,
            "target_date": "",
            "trade_symbols": [],
            "trade_symbols_total": 0,
            "trading_gate_open": False,
            "trading_gate_reason": "runtime_unavailable",
        },
    }


def _build_gateway_action_payload(
    service,
    action: str,
    *,
    ok: bool,
    message: str,
    reason: str = "",
    source: str = "",
    extra: dict | None = None,
) -> dict:
    api_app = _api_app()
    payload = {
        "ok": bool(ok),
        "action": str(action or "").strip() or "gateway",
        "message": str(message or "").strip(),
        "environment": api_app._ibkr_service_environment(service),
        "reason": str(reason or "").strip(),
        "source": str(source or "").strip(),
        "gateway": service.gateway_manager.status(),
        "runtime_running": bool(getattr(service, "is_running", False)),
        "runtime_starting": bool(getattr(service, "is_starting", False)),
        "startup": service.startup_progress_snapshot() if hasattr(service, "startup_progress_snapshot") else {},
        "startup_strategy": service.startup_strategy() if hasattr(service, "startup_strategy") else {},
        "auto_restore_guard": service.auto_restore_guard() if hasattr(service, "auto_restore_guard") else {},
    }
    if extra:
        payload.update(extra)
    return payload


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


def _build_ibkr_monitor_snapshot(service, requested_environment: str | None = None, service_error: str | None = None) -> dict:
    api_app = _api_app()
    runtime_environment = api_app._normalize_runtime_environment_name(
        requested_environment or api_app._ibkr_service_environment(service),
        "live",
    )
    service_available = service is not None
    runtime_status = (
        service.status()
        if service_available and hasattr(service, "status")
        else _build_uninitialized_runtime_status(runtime_environment, service_error)
    )
    compute_summary = _build_compute_summary()
    sample_payload = _build_monitor_samples(service, runtime_status) if service_available else {
        "active_subscriptions": [],
        "active_bar_symbols": [],
        "pending_symbols": [],
        "stale_symbols": [],
        "repair_reasons": [],
    }
    api_utilization = _build_api_utilization_snapshot(service, runtime_environment, runtime_status, sample_payload) if service_available else {
        "subscription_limit": max(
            0,
            int(api_app.cfg.get_int_for_environment("ibkr_target_subscription_limit", runtime_environment, 60) or 0),
        ),
        "active_subscription_count": 0,
        "active_trade_symbol_count": 0,
        "ws_subscribed_count": 0,
        "pending_subscription_count": 0,
        "utilization_pct": 0.0,
        "request_count": 0,
        "retry_count": 0,
        "throttle_count": 0,
        "request_spacing_s": 0.0,
        "max_concurrency": 0,
        "websocket_message_count": 0,
        "order_update_count": 0,
        "last_message": "",
        "last_message_age_s": None,
        "last_tic": "",
        "last_tic_age_s": None,
    }
    host_snapshot = _collect_host_snapshot()
    flags = _build_monitor_flags(runtime_status, api_utilization, host_snapshot, sample_payload)
    payload = {
        "ok": service_available,
        "status": _derive_monitor_status(flags),
        "environment": runtime_environment,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "compute": compute_summary,
        "runtime": runtime_status,
        "runtime_control": api_app.get_ibkr_runtime_control(runtime_environment),
        "api_utilization": api_utilization,
        "samples": sample_payload,
        "host": host_snapshot,
        "flags": flags,
    }
    if not service_available and service_error:
        payload["error"] = str(service_error)
    return payload
