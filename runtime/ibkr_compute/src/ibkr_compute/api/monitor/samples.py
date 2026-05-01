from __future__ import annotations

from ibkr_compute.api.monitor.host import _api_app, _copy_active_subscription_map, _normalize_symbol_list
from ibkr_compute.api.compute.runtime_state.universe import get_market_monitor_symbols


def _build_warmup_symbol_status_map(warmup: dict) -> dict[str, dict]:
    payload = {}
    for item in warmup.get("symbol_status") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        payload[symbol] = dict(item)
    return payload


def _is_recent_warmup_symbol(status_row: dict, latest_completed_bucket_ms: int, max_lag_ms: int = 15 * 60 * 1000) -> bool:
    if not isinstance(status_row, dict):
        return False
    last_bar_time_ms = int(status_row.get("last_bar_time_ms", 0) or 0)
    if last_bar_time_ms <= 0:
        return False
    if latest_completed_bucket_ms <= 0:
        return True
    return last_bar_time_ms >= max(0, latest_completed_bucket_ms - max_lag_ms)


def _resolve_total_subscription_limit(config_source, runtime_environment: str) -> int:
    total_limit = max(
        0,
        int(config_source.get_int_for_environment("ibkr_total_subscription_limit", runtime_environment, 80) or 0),
    )
    if total_limit > 0:
        return total_limit
    return max(
        0,
        int(config_source.get_int_for_environment("ibkr_target_subscription_limit", runtime_environment, 80) or 0),
    )


def _resolve_trade_subscription_limit(config_source, runtime_environment: str) -> int:
    trade_limit = max(
        0,
        int(config_source.get_int_for_environment("ibkr_target_subscription_limit", runtime_environment, 80) or 0),
    )
    total_limit = max(
        0,
        int(config_source.get_int_for_environment("ibkr_total_subscription_limit", runtime_environment, 80) or 0),
    )
    resolved_trade_limit: int | None = trade_limit if trade_limit > 0 else None
    if total_limit > 0:
        try:
            monitor_count = len(get_market_monitor_symbols(runtime_environment))
        except Exception:
            monitor_count = 0
        remaining_budget = max(0, total_limit - monitor_count)
        resolved_trade_limit = remaining_budget if resolved_trade_limit is None else min(resolved_trade_limit, remaining_budget)
    if resolved_trade_limit is not None:
        return int(resolved_trade_limit)
    return _resolve_total_subscription_limit(config_source, runtime_environment)


def _build_monitor_samples(service, runtime_status: dict) -> dict:
    api_app = _api_app()
    warmup = runtime_status.get("warmup") or {}
    market_universe = runtime_status.get("market_universe") or {}
    bar_aggregator = runtime_status.get("bar_aggregator") or {}
    canonical_5m = runtime_status.get("canonical_5m") or {}
    realtime_quotes = runtime_status.get("realtime_quotes") or {}
    active_bars = bar_aggregator.get("active_bars") or {}
    quote_map = realtime_quotes.get("quotes") or {}
    if not isinstance(active_bars, dict):
        active_bars = {}
    if not isinstance(quote_map, dict):
        quote_map = {}

    subscription_map = _copy_active_subscription_map(service)
    trade_symbols = set(_normalize_symbol_list(market_universe.get("active_trade_symbols") or []))
    monitor_symbols = set(
        _normalize_symbol_list(warmup.get("monitor_symbols") or [])
        + _normalize_symbol_list(market_universe.get("market_ws_symbols") or [])
    )
    quote_symbols = set(_normalize_symbol_list(quote_map.keys()))
    active_bar_symbols = set(_normalize_symbol_list(active_bars.keys()))
    warmup_status_map = _build_warmup_symbol_status_map(warmup)
    latest_completed_bucket_ms = int(canonical_5m.get("last_completed_bucket_ms", 0) or 0)
    canonical_written_symbols = set(_normalize_symbol_list(canonical_5m.get("written_symbols") or []))

    active_subscriptions = []
    for symbol in sorted(subscription_map.keys()):
        conid = subscription_map.get(symbol)
        normalized_symbol = str(symbol or "").strip().upper()
        bar_info = active_bars.get(symbol) or active_bars.get(normalized_symbol) or {}
        quote_info = quote_map.get(symbol) or quote_map.get(normalized_symbol) or {}
        warmup_status = warmup_status_map.get(normalized_symbol) or {}
        bar_visible = normalized_symbol in active_bar_symbols
        quote_visible = normalized_symbol in quote_symbols
        warmup_visible = _is_recent_warmup_symbol(warmup_status, latest_completed_bucket_ms)
        canonical_visible = latest_completed_bucket_ms > 0 and normalized_symbol in canonical_written_symbols
        visibility_sources = []
        if bar_visible:
            visibility_sources.append("bar")
        if quote_visible:
            visibility_sources.append("quote")
        if warmup_visible:
            visibility_sources.append("warmup")
        if canonical_visible:
            visibility_sources.append("canonical_5m")
        visible = bool(visibility_sources)
        quote_age_s = (
            round(float(quote_info.get("quote_age_s")), 1)
            if isinstance(quote_info, dict) and quote_info.get("quote_age_s") is not None
            else None
        )
        role = (
            api_app.WATCHLIST_SYMBOL_ROLE_TRADE
            if normalized_symbol in trade_symbols
            else (api_app.WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR if normalized_symbol in monitor_symbols else "subscription")
        )
        active_subscriptions.append(
            {
                "symbol": normalized_symbol,
                "conid": int(conid) if conid is not None else None,
                "role": role,
                "visible": visible,
                "stale": not visible,
                "visibility_sources": visibility_sources,
                "warmup_ready": bool(warmup_status.get("ready")),
                "warmup_last_bar_time_ms": (
                    int(warmup_status.get("last_bar_time_ms", 0) or 0)
                    if isinstance(warmup_status, dict) and warmup_status.get("last_bar_time_ms") is not None
                    else None
                ),
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
    stale_monitor_symbols = [
        item["symbol"]
        for item in active_subscriptions
        if item.get("stale") and str(item.get("role") or "").strip().lower() == api_app.WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
    ]
    stale_control_symbols = [
        item["symbol"]
        for item in active_subscriptions
        if item.get("stale") and str(item.get("role") or "").strip().lower() != api_app.WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
    ]

    return {
        "active_subscriptions": active_subscriptions,
        "active_bar_symbols": active_bar_symbols,
        "pending_symbols": _normalize_symbol_list(warmup.get("pending_symbols") or []),
        "stale_symbols": stale_symbols,
        "stale_monitor_symbols": stale_monitor_symbols,
        "stale_control_symbols": stale_control_symbols,
        "repair_reasons": repair_reasons,
    }


def _build_api_utilization_snapshot(service, runtime_environment: str, runtime_status: dict, sample_payload: dict) -> dict:
    api_app = _api_app()
    websocket = runtime_status.get("websocket") or {}
    market_universe = runtime_status.get("market_universe") or {}
    data_backfill = runtime_status.get("data_backfill") or {}
    last_trace = data_backfill.get("last_trace") if isinstance(data_backfill.get("last_trace"), dict) else {}
    active_trade_symbol_count = int(market_universe.get("active_target_count") or 0)
    active_subscription_count = int(
        market_universe.get("active_subscription_count")
        or len(sample_payload.get("active_subscriptions") or [])
        or 0
    )
    active_monitor_symbol_count = sum(
        1
        for item in (sample_payload.get("active_subscriptions") or [])
        if str((item or {}).get("role") or "").strip().lower() == api_app.WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
    )
    if active_monitor_symbol_count <= 0:
        active_monitor_symbol_count = max(0, active_subscription_count - active_trade_symbol_count)
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
    total_subscription_limit = _resolve_total_subscription_limit(config_source, runtime_environment)
    trade_subscription_limit = _resolve_trade_subscription_limit(config_source, runtime_environment)
    total_utilization_pct = (
        round((active_subscription_count / total_subscription_limit) * 100.0, 2)
        if total_subscription_limit > 0 else None
    )
    trade_utilization_pct = (
        round((active_trade_symbol_count / trade_subscription_limit) * 100.0, 2)
        if trade_subscription_limit > 0 else None
    )
    legacy_utilization_pct = (
        round((active_subscription_count / trade_subscription_limit) * 100.0, 2)
        if trade_subscription_limit > 0 else None
    )
    return {
        "subscription_limit": trade_subscription_limit,
        "total_subscription_limit": total_subscription_limit,
        "active_subscription_count": active_subscription_count,
        "active_trade_symbol_count": active_trade_symbol_count,
        "active_monitor_symbol_count": active_monitor_symbol_count,
        "trade_subscription_limit": trade_subscription_limit,
        "ws_subscribed_count": ws_subscribed_count,
        "pending_subscription_count": pending_subscription_count,
        "utilization_pct": legacy_utilization_pct,
        "total_utilization_pct": total_utilization_pct,
        "trade_utilization_pct": trade_utilization_pct,
        "request_count": int(data_backfill.get("request_count", 0) or 0),
        "retry_count": int(data_backfill.get("retry_count", 0) or 0),
        "throttle_count": int(data_backfill.get("throttle_count", 0) or 0),
        "request_spacing_s": float(data_backfill.get("request_spacing_s", 0) or 0),
        "max_concurrency": int(data_backfill.get("max_concurrency", 0) or 0),
        "active_requests": int(data_backfill.get("active_requests", 0) or 0),
        "active_symbols": list(data_backfill.get("active_symbols") or []),
        "active_symbols_total": int(data_backfill.get("active_symbols_total", 0) or 0),
        "slowest_recent_stage": dict(data_backfill.get("slowest_recent_stage") or {}),
        "last_trace_id": str(last_trace.get("trace_id") or ""),
        "websocket_message_count": int(websocket.get("message_count", 0) or 0),
        "order_update_count": int(websocket.get("order_update_count", 0) or 0),
        "last_message": websocket.get("last_message"),
        "last_message_age_s": websocket.get("last_message_age_s"),
        "last_tic": websocket.get("last_tic"),
        "last_tic_age_s": websocket.get("last_tic_age_s"),
    }
