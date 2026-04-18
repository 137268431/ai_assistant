from __future__ import annotations

from ibkr_compute.api.monitor.host import _api_app
from ibkr_compute.api.compute.runtime_state.universe import get_market_monitor_symbols


def _resolve_total_subscription_limit(runtime_environment: str) -> int:
    api_app = _api_app()
    total_limit = max(
        0,
        int(api_app.cfg.get_int_for_environment("ibkr_total_subscription_limit", runtime_environment, 80) or 0),
    )
    if total_limit > 0:
        return total_limit
    return max(
        0,
        int(api_app.cfg.get_int_for_environment("ibkr_target_subscription_limit", runtime_environment, 80) or 0),
    )


def _resolve_trade_subscription_limit(runtime_environment: str) -> int:
    api_app = _api_app()
    trade_limit = max(
        0,
        int(api_app.cfg.get_int_for_environment("ibkr_target_subscription_limit", runtime_environment, 80) or 0),
    )
    total_limit = max(
        0,
        int(api_app.cfg.get_int_for_environment("ibkr_total_subscription_limit", runtime_environment, 80) or 0),
    )
    resolved_trade_limit: int | None = trade_limit if trade_limit > 0 else None
    if total_limit > 0:
        remaining_budget = max(0, total_limit - len(get_market_monitor_symbols(runtime_environment)))
        resolved_trade_limit = remaining_budget if resolved_trade_limit is None else min(resolved_trade_limit, remaining_budget)
    if resolved_trade_limit is not None:
        return int(resolved_trade_limit)
    return _resolve_total_subscription_limit(runtime_environment)


def _build_empty_monitor_samples() -> dict:
    return {
        "active_subscriptions": [],
        "active_bar_symbols": [],
        "pending_symbols": [],
        "stale_symbols": [],
        "repair_reasons": [],
    }


def _build_empty_api_utilization_snapshot(runtime_environment: str) -> dict:
    return {
        "subscription_limit": _resolve_total_subscription_limit(runtime_environment),
        "total_subscription_limit": _resolve_total_subscription_limit(runtime_environment),
        "active_subscription_count": 0,
        "active_trade_symbol_count": 0,
        "active_monitor_symbol_count": 0,
        "trade_subscription_limit": _resolve_trade_subscription_limit(runtime_environment),
        "ws_subscribed_count": 0,
        "pending_subscription_count": 0,
        "utilization_pct": 0.0,
        "total_utilization_pct": 0.0,
        "trade_utilization_pct": 0.0,
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
