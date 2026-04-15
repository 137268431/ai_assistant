from __future__ import annotations

from ibkr_compute.api.monitor.host import _api_app


def _build_empty_monitor_samples() -> dict:
    return {
        "active_subscriptions": [],
        "active_bar_symbols": [],
        "pending_symbols": [],
        "stale_symbols": [],
        "repair_reasons": [],
    }


def _build_empty_api_utilization_snapshot(runtime_environment: str) -> dict:
    api_app = _api_app()
    return {
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


__all__ = [
    "_build_empty_api_utilization_snapshot",
    "_build_empty_monitor_samples",
]
