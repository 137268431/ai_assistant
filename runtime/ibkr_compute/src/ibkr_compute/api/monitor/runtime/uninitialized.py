from __future__ import annotations

from ibkr_compute.api.monitor.host import _api_app
from ibkr_compute.market.timeframe_utils import build_market_session_snapshot


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
        "market_session": build_market_session_snapshot(),
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
            "last_check": "",
            "last_tickle": "",
            "running": False,
        },
        "auth_recovery": {
            "cycle_id": "",
            "recovery_phase": "idle",
            "recovery_class": "",
            "recovery_reason": "",
            "interruption_kind": "",
            "probe_result": "",
            "auto_restart_scheduled": False,
            "last_runtime_authenticated_at": "",
            "last_gateway_status_code": 0,
            "last_recovery_source": "",
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
        "resource_governor": {
            "status": "critical",
            "health": "unhealthy",
            "metrics": {},
            "thresholds": {},
            "reasons": [{"code": "runtime_unavailable", "message": detail}],
            "admission": {
                "watchlist_idle_topup": {
                    "admit": False,
                    "blockers": [{"code": "runtime_unavailable", "message": detail}],
                },
                "non_priority": {
                    "admit": False,
                    "blockers": [{"code": "runtime_unavailable", "message": detail}],
                },
            },
        },
        "host_resources": {
            "ok": False,
            "reason": "runtime_unavailable",
        },
        "watchlist_idle_topup": {
            "enabled": False,
            "running": False,
            "status": "unavailable",
            "skip_reason": "runtime_unavailable",
            "completion": {
                "total": 0,
                "fresh": 0,
                "stale": 0,
                "missing": 0,
                "unobserved": 0,
                "progress_pct": 0,
            },
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
            "trade_universe_ready": False,
            "trade_universe_status": "runtime_unavailable",
            "trade_universe_reason": "runtime_unavailable",
            "no_active_targets": False,
            "inactive_trade_symbols_total": 0,
            "inactive_trade_symbols_sample": [],
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
            "watchlist_idle_topup": {
                "enabled": False,
                "running": False,
                "status": "unavailable",
                "skip_reason": "runtime_unavailable",
            },
            "market_date": "",
            "watchlist_backfill_interval_min": 0,
            "watchlist_pool_count": 0,
            "watchlist_trade_count": 0,
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


__all__ = ["_build_uninitialized_runtime_status"]
