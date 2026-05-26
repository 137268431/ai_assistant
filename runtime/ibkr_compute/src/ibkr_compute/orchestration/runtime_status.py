from __future__ import annotations

import time
from datetime import datetime


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceRuntimeStatusMixin:
    def _market_calendar_contract_args(self, service_mod) -> dict[str, str]:
        config = getattr(self, "config", None)
        environment = str(getattr(service_mod, "DATA_ENVIRONMENT", "live") or "live")

        def config_text(key: str, default: str) -> str:
            if config is None or not hasattr(config, "get_for_environment"):
                return default
            try:
                return str(config.get_for_environment(key, environment, default) or default).strip() or default
            except Exception:
                return default

        return {
            "symbol": config_text("ibkr_market_calendar_symbol", "SPY").upper(),
            "exchange": config_text("ibkr_market_calendar_exchange", "SMART").upper(),
            "sec_type": config_text("ibkr_market_calendar_sec_type", "STK").upper(),
        }

    def _runtime_market_session_snapshot(self, service_mod) -> dict:
        from ibkr_compute.market.calendar import (
            IBKR_SCHEDULE_SOURCE,
            build_ibkr_calendar_snapshot,
            build_local_nyse_calendar_snapshot,
            build_market_session_from_calendar,
        )

        now = datetime.now(service_mod.ET)
        market_date = str(getattr(self, "_current_market_date", "") or now.strftime("%Y-%m-%d")).strip()
        contract_args = self._market_calendar_contract_args(service_mod)
        fallback = service_mod.build_market_session_snapshot(now)
        cache = getattr(self, "_market_session_calendar_cache", None)
        signature = (
            market_date,
            contract_args["symbol"],
            contract_args["exchange"],
            contract_args["sec_type"],
        )
        now_ts = time.time()
        if isinstance(cache, dict) and cache.get("signature") == signature and float(cache.get("expires_at") or 0) > now_ts:
            payload = dict(cache.get("payload") or {})
            session = build_market_session_from_calendar(payload, now=now) if payload else {}
            if session:
                merged = {**fallback, **session}
                merged["calendar"] = {
                    key: payload.get(key)
                    for key in (
                        "source",
                        "source_error",
                        "market_date",
                        "symbol",
                        "exchange",
                        "sec_type",
                        "schedule_kind",
                        "time_zone_id",
                        "is_trading_day",
                        "is_closed",
                        "closed_reason",
                        "session",
                        "next_open_us",
                        "next_open_beijing",
                    )
                    if key in payload
                }
                return merged

        source_error = ""
        payload = {}
        broker = getattr(self, "broker", None)
        try:
            if broker is None or not hasattr(broker, "resolve_contract"):
                raise RuntimeError("broker_unavailable")
            gateway_manager = getattr(self, "gateway_manager", None)
            gateway_status = gateway_manager.status() if gateway_manager is not None and hasattr(gateway_manager, "status") else {}
            if gateway_status and not (gateway_status.get("running") or gateway_status.get("reachable")):
                raise RuntimeError("gateway_unreachable")
            session_keeper = getattr(self, "session_keeper", None)
            if session_keeper is not None and getattr(session_keeper, "is_authenticated", True) is False:
                raise RuntimeError("session_unauthenticated")
            contract = broker.resolve_contract(
                symbol=contract_args["symbol"],
                conid=0,
                exchange=contract_args["exchange"],
                sec_type=contract_args["sec_type"],
            )
            if not contract:
                raise RuntimeError("contract_not_found")
            payload = build_ibkr_calendar_snapshot(
                contract,
                market_date=market_date,
                symbol=contract_args["symbol"],
                exchange=contract_args["exchange"],
                sec_type=contract_args["sec_type"],
                now=now,
            )
            if not payload.get("ok"):
                raise RuntimeError(str(payload.get("error") or "ibkr_schedule_unavailable"))
        except Exception as exc:
            source_error = str(exc)
            payload = build_local_nyse_calendar_snapshot(
                market_date,
                symbol=contract_args["symbol"],
                exchange=contract_args["exchange"],
                sec_type=contract_args["sec_type"],
                source_error=source_error,
                now=now,
            )

        ttl_seconds = 300 if payload.get("source") == IBKR_SCHEDULE_SOURCE and payload.get("ok") else 60
        try:
            self._market_session_calendar_cache = {
                "signature": signature,
                "expires_at": now_ts + ttl_seconds,
                "payload": dict(payload),
            }
        except Exception:
            pass
        session = build_market_session_from_calendar(payload, now=now) if payload else {}
        if not session:
            return fallback
        merged = {**fallback, **session}
        merged["calendar"] = {
            key: payload.get(key)
            for key in (
                "source",
                "source_error",
                "market_date",
                "symbol",
                "exchange",
                "sec_type",
                "schedule_kind",
                "time_zone_id",
                "is_trading_day",
                "is_closed",
                "closed_reason",
                "session",
                "next_open_us",
                "next_open_beijing",
            )
            if key in payload
        }
        if source_error and not merged.get("source_error"):
            merged["source_error"] = source_error
        return merged

    def status(self, refresh_auth: bool = True) -> dict:
        service_mod = _service_mod()
        session_status = self.session_keeper.status()
        if refresh_auth:
            try:
                session_status = self.session_keeper.check_auth_status()
            except Exception:
                service_mod.logger.debug("Failed to refresh IB Gateway auth status", exc_info=True)
        now_ts = time.time()
        queue_size = int(self._compute_queue.qsize())
        last_bar_close_at = float(self._last_bar_close_at or 0.0)
        last_run_at = float(self._last_realtime_compute_at or 0.0)
        last_started_at = float(self._last_realtime_compute_started_at or 0.0)
        compute_thread_alive = bool(self._compute_thread and self._compute_thread.is_alive())
        inflight = bool(last_started_at and last_started_at > last_run_at)
        inflight_age_s = round(max(0.0, now_ts - last_started_at), 1) if inflight else None
        data_symbols = self._data_universe_symbols()
        last_result = (
            dict(self._last_realtime_compute_result)
            if isinstance(self._last_realtime_compute_result, dict)
            else {}
        )
        last_elapsed_s = round(max(0.0, float(last_result.get("elapsed_s", 0) or 0)), 1)
        symbol_based_timeout_s = 120 + (max(1, len(data_symbols)) * 20)
        historical_timeout_s = last_elapsed_s * 2 if last_elapsed_s > 0 else 0.0
        inflight_timeout_threshold_s = int(
            min(
                3600,
                max(
                    900,
                    symbol_based_timeout_s,
                    historical_timeout_s,
                ),
            )
        )
        lag_since_last_run_s = 0.0
        if last_bar_close_at and last_run_at and last_bar_close_at > last_run_at:
            lag_since_last_run_s = round(max(0.0, last_bar_close_at - last_run_at), 1)

        stalled = False
        stall_reason = ""
        if not self._starting:
            if (queue_size > 0 or inflight) and not compute_thread_alive:
                stalled = True
                stall_reason = "thread_dead"
            elif (
                inflight
                and inflight_age_s is not None
                and inflight_age_s >= inflight_timeout_threshold_s
            ):
                stalled = True
                stall_reason = "inflight_timeout"
            elif queue_size > 0 and lag_since_last_run_s >= 600:
                stalled = True
                stall_reason = "lagging"

        market_session = self._runtime_market_session_snapshot(service_mod)
        auth_recovery = self._copy_auth_recovery_state()
        official_5m = self._copy_official_5m_state()
        direct_history_topup = self._copy_direct_topup_state()
        due_bucket_ms = int(official_5m.get("last_due_bucket_ms", 0) or 0)
        completed_bucket_ms = int(official_5m.get("last_completed_bucket_ms", 0) or 0)
        cycle_started_at_ms = int(official_5m.get("cycle_started_at_ms", 0) or 0)
        official_5m_running = bool(official_5m.get("running"))
        official_5m["cycle_age_s"] = (
            round(max(0.0, now_ts - (cycle_started_at_ms / 1000.0)), 1)
            if official_5m_running and cycle_started_at_ms > 0
            else 0.0
        )
        official_5m["running"] = official_5m_running
        official_5m["lag_s"] = (
            round(max(0.0, (due_bucket_ms - completed_bucket_ms) / 1000.0), 1)
            if due_bucket_ms > completed_bucket_ms else 0.0
        )
        websocket_status = self.ws_client.status()
        realtime_quotes = self.realtime_quote_book.status()
        warmup_state = self._copy_warmup_state()
        daily_scan_state = self._copy_daily_scan_state()
        broker_client_id = int(getattr(getattr(self, "broker", None), "client_id", 0) or 0)
        bar_repair_queue = {}
        coordinator = getattr(self, "bar_repair_coordinator", None)
        if coordinator is not None and hasattr(coordinator, "status"):
            try:
                bar_repair_queue = coordinator.status()
            except Exception as exc:
                bar_repair_queue = {"ok": False, "error": str(exc), "pending": 0, "inflight": 0, "failed": 0}
        scan_symbols = self._normalize_symbol_list(self._watchlist_trade_symbols)
        market_ws_symbols = self._market_ws_symbols()
        with self._subscription_lock:
            active_subscription_symbols = list(self._active_subscription_symbols)
        active_subscription_set = set(active_subscription_symbols)
        active_trade_symbol_set = set(self._active_trade_symbols)
        inactive_trade_symbols = [
            symbol for symbol in scan_symbols
            if symbol not in active_trade_symbol_set
        ]
        no_active_targets = bool(scan_symbols) and not bool(active_trade_symbol_set)
        if active_trade_symbol_set:
            trade_universe_status = "ready"
        elif scan_symbols:
            trade_universe_status = "no_active_targets"
        else:
            trade_universe_status = "no_trade_symbols"
        market_ws_subscribed_symbols = [
            symbol for symbol in market_ws_symbols if symbol in active_subscription_set
        ]
        market_ws_ready = bool(websocket_status.get("connected") or websocket_status.get("ready")) and bool(market_ws_symbols) and (
            len(market_ws_subscribed_symbols) >= len(market_ws_symbols)
        )
        blocking_canonical_pending_symbols = self._non_monitor_pending_symbols(
            official_5m.get("pending_symbols") or [],
            market_ws_symbols,
        )

        if str(daily_scan_state.get("status") or "").strip().lower() == "running":
            pipeline_stage = "run_daily_scan"
            pipeline_status = "running"
        elif str(warmup_state.get("phase") or "").strip().lower() in {"pending", "running"}:
            pipeline_stage = "materialize_indicators"
            pipeline_status = "running"
        elif str(daily_scan_state.get("status") or "").strip().lower() == "completed":
            pipeline_stage = "run_target_realtime"
            pipeline_status = "ready"
        elif str(warmup_state.get("phase") or "").strip().lower() in {"ready", "degraded"}:
            pipeline_stage = "activate_targets"
            pipeline_status = str(warmup_state.get("phase") or "ready")
        else:
            pipeline_stage = "resolve_universe"
            pipeline_status = str(warmup_state.get("phase") or "idle")

        market_session_kind = str(market_session.get("kind") or "").strip().lower()
        live_freshness_required = market_session_kind in {"regular", "close_transition"}
        bar_freshness_status = "fresh"
        if live_freshness_required:
            bar_freshness_status = (
                "fresh"
                if completed_bucket_ms > 0
                and float(official_5m.get("lag_s", 0) or 0) <= 90
                and not blocking_canonical_pending_symbols
                else "stale"
            )
        indicator_freshness_status = "fresh"
        if live_freshness_required:
            indicator_freshness_status = (
                "fresh"
                if last_run_at > 0 and lag_since_last_run_s <= 90 and not stalled
                else "stale"
            )
        multi_timeframe_readiness = {}
        try:
            from ibkr_compute.api.service_topology import uses_remote_compute_service

            if uses_remote_compute_service():
                from ibkr_compute.api.compute_status_client import get_remote_compute_status

                compute_status = get_remote_compute_status(force_refresh=False, symbols=data_symbols)
                candidate = compute_status.get("multi_timeframe_readiness")
                if isinstance(candidate, dict):
                    multi_timeframe_readiness = dict(candidate)
        except Exception:
            multi_timeframe_readiness = {}
        host_resources = self._host_resources_snapshot()
        resource_governor = self._resource_governor_snapshot()
        watchlist_idle_topup = self._watchlist_idle_topup_status()
        runtime_health = "ok"
        if str(resource_governor.get("status") or "").strip().lower() == "critical":
            runtime_health = "unhealthy"
        elif str(resource_governor.get("status") or "").strip().lower() == "warning":
            runtime_health = "degraded"
        if live_freshness_required and bar_freshness_status != "fresh":
            runtime_health = "unhealthy"
        elif live_freshness_required and indicator_freshness_status != "fresh" and runtime_health == "ok":
            runtime_health = "degraded"
        if stalled:
            runtime_health = "unhealthy"

        return {
            "gateway_control_available": True,
            "starting": self._starting,
            "startup_complete": bool(self._running and not self._starting),
            "runtime_health": runtime_health,
            "runtime_phase": self._runtime_phase_label(),
            "startup_strategy": self.startup_strategy(),
            "auto_restore_guard": self.auto_restore_guard(),
            "environment": service_mod.ENVIRONMENT,
            "broker_mode": service_mod.BROKER_MODE,
            "gateway_mode": service_mod.GATEWAY_MODE,
            "data_environment": service_mod.DATA_ENVIRONMENT,
            "market_data_environment": service_mod.DATA_ENVIRONMENT,
            "shared_market_data": service_mod.DATA_ENVIRONMENT == "live",
            "mode_mismatch": service_mod.BROKER_MODE != service_mod.GATEWAY_MODE,
            "ib_gateway_client_id": broker_client_id,
            "broker_client_id": broker_client_id,
            "market_session": market_session,
            "gateway": self.gateway_manager.status(),
            "auth_recovery": auth_recovery,
            "session": session_status,
            "websocket": websocket_status,
            "bar_aggregator": self.bar_aggregator.status(),
            "realtime_quotes": realtime_quotes,
            "canonical_5m": official_5m,
            "direct_history_topup": direct_history_topup,
            "host_resources": host_resources,
            "resource_governor": resource_governor,
            "watchlist_idle_topup": watchlist_idle_topup,
            "bar_repair_queue": bar_repair_queue,
            "data_writer": self.data_writer.status(),
            "data_backfill": self.data_backfill.status(),
            "data_retention": self.data_retention.status(),
            "order_placer": self.order_placer.status(),
            "order_tracker": self.order_tracker.status(),
            "order_lifecycle": self.order_lifecycle.status(),
            "order_flow": (
                self.order_flow_manager.status()
                if getattr(self, "order_flow_manager", None) is not None
                else {"enabled": False, "reason": "manager_unavailable"}
            ),
            "signal_router": self.signal_router.status(),
            "signal_processor": self.signal_processor.status(),
            "warmup": warmup_state,
            "daily_scan": daily_scan_state,
            "multi_timeframe_readiness": multi_timeframe_readiness,
            "realtime_compute": {
                "runs": self._realtime_compute_runs,
                "queue_size": queue_size,
                "thread_alive": compute_thread_alive,
                "inflight": inflight,
                "inflight_age_s": inflight_age_s,
                "inflight_timeout_threshold_s": inflight_timeout_threshold_s,
                "stalled": stalled,
                "stall_reason": stall_reason,
                "last_bar_close": (
                    datetime.fromtimestamp(last_bar_close_at, service_mod.ET).isoformat()
                    if last_bar_close_at else None
                ),
                "last_started": (
                    datetime.fromtimestamp(last_started_at, service_mod.ET).isoformat()
                    if last_started_at else None
                ),
                "last_run": (
                    datetime.fromtimestamp(last_run_at, service_mod.ET).isoformat()
                    if last_run_at else None
                ),
                "lag_since_last_run_s": lag_since_last_run_s,
                "last_elapsed_s": last_elapsed_s,
                "last_result": last_result,
            },
            "interval_prime": self._copy_interval_prime_state(),
            "market_universe": {
                "market_date": self._current_market_date,
                "pipeline_stage": pipeline_stage,
                "pipeline_status": pipeline_status,
                "last_daily_reset": (
                    datetime.fromtimestamp(self._last_daily_reset_at, service_mod.ET).isoformat()
                    if self._last_daily_reset_at else None
                ),
                "watchlist_pool_count": len(self._watchlist_symbols),
                "watchlist_trade_count": len(self._watchlist_trade_symbols),
                "trade_universe_ready": bool(active_trade_symbol_set),
                "trade_universe_status": trade_universe_status,
                "trade_universe_reason": trade_universe_status,
                "no_active_targets": no_active_targets,
                "inactive_trade_symbols_total": len(inactive_trade_symbols),
                "inactive_trade_symbols_sample": list(inactive_trade_symbols[:25]),
                "data_symbols_total": len(data_symbols),
                "scan_symbols_total": len(scan_symbols),
                "market_ws_symbols_total": len(market_ws_symbols),
                "data_symbols": list(data_symbols),
                "scan_symbols": list(scan_symbols),
                "market_ws_symbols": list(market_ws_symbols),
                "active_target_date": self._active_target_date,
                "active_target_count": len(self._active_trade_symbols),
                "active_subscription_count": len(active_subscription_symbols),
                "active_target_symbols": list(self._active_trade_symbols),
                "active_subscription_symbols": list(active_subscription_symbols),
                "active_trade_symbols": list(self._active_trade_symbols),
                "market_ws_ready": market_ws_ready,
                "market_ws_symbols_ready": len(market_ws_subscribed_symbols),
                "market_ws_subscribed_symbols": list(market_ws_subscribed_symbols),
                "last_successful_scan_market_date": (
                    str(daily_scan_state.get("market_date") or "")
                    if str(daily_scan_state.get("status") or "").strip().lower() == "completed"
                    else ""
                ),
                "last_successful_scan_at": (
                    str(daily_scan_state.get("finished_at") or "")
                    if str(daily_scan_state.get("status") or "").strip().lower() == "completed"
                    else ""
                ),
                "bar_freshness": {
                    "status": bar_freshness_status,
                    "lag_s": float(official_5m.get("lag_s", 0) or 0),
                    "last_completed_bucket_us": str(official_5m.get("last_completed_bucket_us") or ""),
                    "pending_symbols_total": int(official_5m.get("pending_symbols_total", 0) or 0),
                    "pending_symbols": list(official_5m.get("pending_symbols") or []),
                    "pending_symbol_details": list(official_5m.get("pending_symbol_details") or []),
                    "sequence_gap_count": int(official_5m.get("sequence_gap_count", 0) or 0),
                    "missing_required_bars_total": int(official_5m.get("missing_required_bars_total", 0) or 0),
                },
                "indicator_freshness": {
                    "status": indicator_freshness_status,
                    "lag_since_last_run_s": lag_since_last_run_s,
                    "last_run": (
                        datetime.fromtimestamp(last_run_at, service_mod.ET).isoformat()
                        if last_run_at else None
                    ),
                    "stalled": stalled,
                    "stall_reason": stall_reason,
                    "inflight_timeout_threshold_s": inflight_timeout_threshold_s,
                },
                "multi_timeframe_readiness": multi_timeframe_readiness,
                "direct_history_topup": direct_history_topup,
                "watchlist_idle_topup": watchlist_idle_topup,
                "last_watchlist_refresh": (
                    datetime.fromtimestamp(self._last_watchlist_refresh_at, service_mod.ET).isoformat()
                    if self._last_watchlist_refresh_at else None
                ),
                "last_target_refresh": (
                    datetime.fromtimestamp(self._last_target_refresh_at, service_mod.ET).isoformat()
                    if self._last_target_refresh_at else None
                ),
                "active_repair_interval_min": self.config.get_int_for_environment(
                    "ibkr_active_repair_interval_min",
                    service_mod.DATA_ENVIRONMENT,
                    5,
                ),
                "last_active_repair": (
                    datetime.fromtimestamp(self._last_active_repair_at, service_mod.ET).isoformat()
                    if self._last_active_repair_at else None
                ),
                "last_active_repair_symbols": list(self._last_active_repair_symbols),
                "last_active_repair_reasons": dict(self._last_active_repair_reasons),
                "watchlist_backfill_interval_min": self.config.get_int_for_environment(
                    "ibkr_watchlist_backfill_interval_min",
                    service_mod.DATA_ENVIRONMENT,
                    30,
                ),
                "watchlist_integrity_enabled": self.config.get_bool_for_environment(
                    "ibkr_watchlist_integrity_enabled",
                    service_mod.DATA_ENVIRONMENT,
                    True,
                ),
                "watchlist_integrity_batch_size": self.config.get_int_for_environment(
                    "ibkr_watchlist_integrity_batch_size",
                    service_mod.DATA_ENVIRONMENT,
                    service_mod.DEFAULT_WATCHLIST_INTEGRITY_BATCH_SIZE,
                ),
                "last_watchlist_backfill": (
                    datetime.fromtimestamp(self._last_backfill_at, service_mod.ET).isoformat()
                    if self._last_backfill_at else None
                ),
                "last_watchlist_backfill_symbols": list(self._last_backfill_symbols),
                "last_watchlist_integrity": (
                    datetime.fromtimestamp(self._last_watchlist_integrity_at, service_mod.ET).isoformat()
                    if self._last_watchlist_integrity_at else None
                ),
                "last_watchlist_integrity_symbols": list(self._last_watchlist_integrity_symbols),
                "last_watchlist_integrity_repair_symbols": list(
                    self._last_watchlist_integrity_repair_symbols
                ),
                "last_history_repair": (
                    datetime.fromtimestamp(self._last_history_repair_at, service_mod.ET).isoformat()
                    if self._last_history_repair_at else None
                ),
                "last_history_repair_symbols": list(self._last_history_repair_symbols),
                "last_pipeline_repair": (
                    datetime.fromtimestamp(self._last_pipeline_repair_at, service_mod.ET).isoformat()
                    if self._last_pipeline_repair_at else None
                ),
                "last_pipeline_repair_symbols": list(self._last_pipeline_repair_symbols),
            },
        }
