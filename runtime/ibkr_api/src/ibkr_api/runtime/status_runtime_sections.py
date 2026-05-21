from __future__ import annotations

from typing import Any

from .status_types import AsDict, TrimArray, TrimObjectEntries


def _list_total(values: Any, fallback: Any = 0) -> int:
    if isinstance(values, list):
        return len(values)
    return int(fallback or 0)


def build_auth_recovery_summary(auth_recovery_payload: Any, *, as_dict: AsDict) -> dict[str, Any]:
    auth_recovery = as_dict(auth_recovery_payload)
    return {
        "cycle_id": str(auth_recovery.get("cycle_id") or ""),
        "recovery_phase": str(auth_recovery.get("recovery_phase") or ""),
        "recovery_class": str(auth_recovery.get("recovery_class") or ""),
        "recovery_reason": str(auth_recovery.get("recovery_reason") or ""),
        "interruption_kind": str(auth_recovery.get("interruption_kind") or ""),
        "last_runtime_authenticated_at": auth_recovery.get("last_runtime_authenticated_at") or "",
        "last_gateway_status_code": int(auth_recovery.get("last_gateway_status_code") or 0),
        "last_recovery_source": str(auth_recovery.get("last_recovery_source") or ""),
        "probe_result": str(auth_recovery.get("probe_result") or ""),
        "probe_last_checked_at": auth_recovery.get("probe_last_checked_at") or "",
        "probe_attempts": int(auth_recovery.get("probe_attempts") or 0),
        "auto_restart_scheduled": bool(auth_recovery.get("auto_restart_scheduled")),
        "manual_takeover_active": bool(auth_recovery.get("manual_takeover_active")),
        "lock_owner": str(auth_recovery.get("lock_owner") or ""),
    }


def build_gateway_payload(gateway_payload: Any, *, as_dict: AsDict) -> dict[str, Any]:
    gateway = as_dict(gateway_payload)
    return {
        "running": bool(gateway.get("running")),
        "reachable": bool(gateway.get("reachable")),
        "managed_by": str(gateway.get("managed_by") or ""),
        "status_code": int(gateway.get("status_code") or 0),
        "pid": int(gateway.get("pid") or 0),
        "uptime_s": int(gateway.get("uptime_s") or 0),
    }


def build_session_payload(session_payload: Any, *, as_dict: AsDict) -> dict[str, Any]:
    session = as_dict(session_payload)
    return {
        "authenticated": bool(session.get("authenticated")),
        "running": bool(session.get("running")),
        "consecutive_failures": int(session.get("consecutive_failures") or 0),
        "last_check": session.get("last_check") or session.get("last_tickle") or "",
        "last_tickle": session.get("last_tickle") or session.get("last_check") or "",
    }


def build_websocket_payload(websocket_payload: Any, *, as_dict: AsDict) -> dict[str, Any]:
    websocket = as_dict(websocket_payload)
    return {
        "connected": bool(websocket.get("connected")),
        "ready": bool(websocket.get("ready")),
        "running": bool(websocket.get("running")),
        "last_message": websocket.get("last_message") or "",
        "message_count": int(websocket.get("message_count") or 0),
        "subscribed_count": _list_total(websocket.get("subscribed_conids"), websocket.get("subscribed_count")),
        "pending_count": _list_total(websocket.get("pending_conids"), websocket.get("pending_count")),
    }


def build_realtime_quotes_payload(realtime_quotes_payload: Any, *, as_dict: AsDict) -> dict[str, Any]:
    realtime_quotes = as_dict(realtime_quotes_payload)
    return {
        "total_quotes": int(realtime_quotes.get("total_quotes") or 0),
        "stale_quotes": int(realtime_quotes.get("stale_quotes") or 0),
        "tick_count": int(realtime_quotes.get("tick_count") or 0),
        "update_count": int(realtime_quotes.get("update_count") or 0),
    }


def build_canonical_5m_payload(
    canonical_payload: Any,
    *,
    as_dict: AsDict,
    trim_array: TrimArray,
) -> dict[str, Any]:
    canonical_5m = as_dict(canonical_payload)
    written_symbols = canonical_5m.get("written_symbols")
    pending_symbols = canonical_5m.get("pending_symbols")
    return {
        "enabled": bool(canonical_5m.get("enabled", True)),
        "driver": str(canonical_5m.get("driver") or ""),
        "running": bool(canonical_5m.get("running")),
        "phase": str(canonical_5m.get("phase") or ""),
        "cycle_started_at_ms": int(canonical_5m.get("cycle_started_at_ms") or 0),
        "cycle_age_s": float(canonical_5m.get("cycle_age_s") or 0),
        "current_due_bucket_ms": int(canonical_5m.get("current_due_bucket_ms") or 0),
        "close_delay_sec": int(canonical_5m.get("close_delay_sec") or 0),
        "request_period": str(canonical_5m.get("request_period") or ""),
        "last_run": canonical_5m.get("last_run") or "",
        "last_due_bucket_ms": int(canonical_5m.get("last_due_bucket_ms") or 0),
        "last_completed_bucket_ms": int(canonical_5m.get("last_completed_bucket_ms") or 0),
        "lag_s": int(canonical_5m.get("lag_s") or 0),
        "last_written_bars": int(canonical_5m.get("last_written_bars") or 0),
        "written_symbols": trim_array(written_symbols, 24),
        "written_symbols_total": _list_total(written_symbols, canonical_5m.get("written_symbols_total")),
        "pending_symbols": trim_array(pending_symbols, 24),
        "pending_symbols_total": _list_total(pending_symbols, canonical_5m.get("pending_symbols_total")),
        "last_error": str(canonical_5m.get("last_error") or ""),
    }


def build_data_backfill_payload(data_backfill_payload: Any, *, as_dict: AsDict) -> dict[str, Any]:
    data_backfill = as_dict(data_backfill_payload)
    return {
        "total_backfilled": int(data_backfill.get("total_backfilled") or 0),
    }


def build_order_tracker_payload(order_tracker_payload: Any, *, as_dict: AsDict) -> dict[str, Any]:
    order_tracker = as_dict(order_tracker_payload)
    return {
        "running": bool(order_tracker.get("running")),
        "last_poll": order_tracker.get("last_poll") or "",
        "tracked_orders": int(order_tracker.get("tracked_orders") or 0),
    }


def build_realtime_compute_payload(realtime_compute_payload: Any, *, as_dict: AsDict) -> dict[str, Any]:
    realtime_compute = as_dict(realtime_compute_payload)
    realtime_result = as_dict(realtime_compute.get("last_result"))
    return {
        "runs": int(realtime_compute.get("runs") or 0),
        "queue_size": int(realtime_compute.get("queue_size") or 0),
        "thread_alive": bool(realtime_compute.get("thread_alive")),
        "inflight": bool(realtime_compute.get("inflight")),
        "inflight_age_s": int(realtime_compute.get("inflight_age_s") or 0),
        "inflight_timeout_threshold_s": int(realtime_compute.get("inflight_timeout_threshold_s") or 0),
        "stalled": bool(realtime_compute.get("stalled")),
        "stall_reason": str(realtime_compute.get("stall_reason") or ""),
        "last_started": realtime_compute.get("last_started") or "",
        "last_run": realtime_compute.get("last_run") or "",
        "last_bar_close": realtime_compute.get("last_bar_close") or "",
        "last_elapsed_s": float(realtime_compute.get("last_elapsed_s") or realtime_result.get("elapsed_s") or 0),
        "last_processed": int(realtime_result.get("processed") or 0),
        "last_signals": int(realtime_result.get("signals") or 0),
        "last_errors": int(realtime_result.get("errors") or 0),
    }


def build_daily_scan_payload(daily_scan_payload: Any, *, as_dict: AsDict) -> dict[str, Any]:
    daily_scan = as_dict(daily_scan_payload)
    return {
        "market_date": str(daily_scan.get("market_date") or ""),
        "status": str(daily_scan.get("status") or ""),
        "reason": str(daily_scan.get("reason") or ""),
        "started_at": daily_scan.get("started_at") or "",
        "finished_at": daily_scan.get("finished_at") or "",
        "last_error": str(daily_scan.get("last_error") or ""),
        "result": as_dict(daily_scan.get("result")),
    }


def build_market_universe_payload(
    market_universe_payload: Any,
    *,
    as_dict: AsDict,
    trim_array: TrimArray,
    trim_object_entries: TrimObjectEntries,
) -> dict[str, Any]:
    market_universe = as_dict(market_universe_payload)
    active_trade_symbols = market_universe.get("active_trade_symbols")
    market_ws_symbols = market_universe.get("market_ws_symbols")
    market_ws_subscribed_symbols = market_universe.get("market_ws_subscribed_symbols")
    active_repair_symbols = market_universe.get("last_active_repair_symbols")
    active_repair_reasons = market_universe.get("last_active_repair_reasons")
    return {
        "market_date": str(market_universe.get("market_date") or ""),
        "last_daily_reset": market_universe.get("last_daily_reset") or "",
        "watchlist_pool_count": int(market_universe.get("watchlist_pool_count") or 0),
        "active_target_date": str(market_universe.get("active_target_date") or ""),
        "active_target_count": int(market_universe.get("active_target_count") or 0),
        "active_trade_symbols": trim_array(
            active_trade_symbols,
            len(active_trade_symbols) if isinstance(active_trade_symbols, list) else 0,
        ),
        "active_trade_symbols_total": _list_total(
            active_trade_symbols,
            market_universe.get("active_trade_symbols_total"),
        ),
        "market_ws_ready": bool(market_universe.get("market_ws_ready")),
        "market_ws_symbols": trim_array(
            market_ws_symbols,
            len(market_ws_symbols) if isinstance(market_ws_symbols, list) else 0,
        ),
        "market_ws_symbols_total": _list_total(
            market_ws_symbols,
            market_universe.get("market_ws_symbols_total"),
        ),
        "market_ws_symbols_ready": int(market_universe.get("market_ws_symbols_ready") or 0),
        "market_ws_subscribed_symbols": trim_array(
            market_ws_subscribed_symbols,
            len(market_ws_subscribed_symbols) if isinstance(market_ws_subscribed_symbols, list) else 0,
        ),
        "last_target_refresh": market_universe.get("last_target_refresh") or "",
        "active_repair_interval_min": int(market_universe.get("active_repair_interval_min") or 0),
        "last_active_repair": market_universe.get("last_active_repair") or "",
        "last_active_repair_symbols": trim_array(active_repair_symbols, 12),
        "last_active_repair_symbols_total": _list_total(
            active_repair_symbols,
            market_universe.get("last_active_repair_symbols_total"),
        ),
        "last_active_repair_reasons": trim_object_entries(active_repair_reasons, 12),
        "watchlist_backfill_interval_min": int(market_universe.get("watchlist_backfill_interval_min") or 0),
        "last_watchlist_backfill": market_universe.get("last_watchlist_backfill") or "",
    }


__all__ = [
    "build_auth_recovery_summary",
    "build_canonical_5m_payload",
    "build_daily_scan_payload",
    "build_data_backfill_payload",
    "build_gateway_payload",
    "build_market_universe_payload",
    "build_order_tracker_payload",
    "build_realtime_compute_payload",
    "build_realtime_quotes_payload",
    "build_session_payload",
    "build_websocket_payload",
]
