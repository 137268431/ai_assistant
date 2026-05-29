from __future__ import annotations

import queue
import threading
from datetime import datetime

from ibkr_compute.market.timeframe_utils import interval_to_ms


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceSupportMixin:
    def _refresh_runtime_settings(self):
        service_mod = _service_mod()
        self._manual_start_restart_gateway = self.config.get_bool_for_environment(
            "ibkr_manual_start_restart_gateway",
            service_mod.ENVIRONMENT,
            True,
        )
        self._weekly_reauth_restart_gateway = self.config.get_bool_for_environment(
            "ibkr_weekly_reauth_restart_gateway",
            service_mod.ENVIRONMENT,
            True,
        )
        self._server_boot_resume_only = self.config.get_bool_for_environment(
            "ibkr_server_boot_resume_only",
            service_mod.ENVIRONMENT,
            True,
        )
        self._server_boot_publish_startup_card = self.config.get_bool_for_environment(
            "ibkr_server_boot_publish_startup_card",
            service_mod.ENVIRONMENT,
            False,
        )
        self.ws_client.set_order_updates_enabled(self.order_tracker.uses_websocket_updates())

    def _runtime_signal_source_mode(self) -> str:
        service_mod = _service_mod()
        return str(
            self.config.get_for_environment("ibkr_signal_source", service_mod.ENVIRONMENT, "tradingview")
            or "tradingview"
        ).strip().lower()

    def _runtime_tv_primary_mode(self) -> bool:
        return self._runtime_signal_source_mode() in {"tv", "tradingview", "webhook_tv"}

    def _runtime_tv_primary_slim_enabled(self) -> bool:
        service_mod = _service_mod()
        return self._runtime_tv_primary_mode() and self.config.get_bool_for_environment(
            "ibkr_tv_primary_runtime_slim_enabled",
            service_mod.ENVIRONMENT,
            self._runtime_config_default_bool("ibkr_tv_primary_runtime_slim_enabled", True),
        )

    def _runtime_config_default_bool(self, key: str, default: bool) -> bool:
        defaults = getattr(self.config, "DEFAULTS", {}) or {}
        value = str(defaults.get(key, str(default).lower()) or "").strip().lower()
        return value in {"true", "1", "yes", "on"}

    def _runtime_technical_pipeline_config_enabled(self) -> bool:
        service_mod = _service_mod()
        return self.config.get_bool_for_environment(
            "ibkr_runtime_technical_pipeline_enabled",
            service_mod.ENVIRONMENT,
            self._runtime_config_default_bool("ibkr_runtime_technical_pipeline_enabled", False),
        )

    def _runtime_technical_pipeline_enabled(self) -> bool:
        return self._runtime_technical_pipeline_config_enabled() and not self._runtime_tv_primary_slim_enabled()

    def _runtime_tv_primary_slim_mode_enabled(self) -> bool:
        return self._runtime_tv_primary_slim_enabled() and not self._runtime_technical_pipeline_enabled()

    def _runtime_slim_mode_enabled(self) -> bool:
        return not self._runtime_technical_pipeline_enabled()

    def _runtime_feed_technical_ticks_enabled(self) -> bool:
        service_mod = _service_mod()
        return self._runtime_technical_pipeline_enabled() and self.config.get_bool_for_environment(
            "ibkr_runtime_feed_technical_ticks_enabled",
            service_mod.ENVIRONMENT,
            self._runtime_config_default_bool("ibkr_runtime_feed_technical_ticks_enabled", True),
        )

    def _runtime_background_thread_specs(self) -> list[tuple[str, str, object]]:
        specs: list[tuple[str, str, object]] = [
            ("_signal_thread", "signal-loop", self._signal_loop),
            ("_subscription_thread", "target-refresh", self._subscription_refresh_loop),
        ]
        if self._runtime_technical_pipeline_enabled():
            specs.extend(
                [
                    ("_active_repair_thread", "active-repair", self._active_repair_loop),
                    ("_watchlist_backfill_thread", "watchlist-backfill", self._watchlist_backfill_loop),
                    ("_compute_thread", "close-compute", self._compute_loop),
                    ("_official_close_thread", "official-5m-close", self._official_5m_close_loop),
                    ("_direct_topup_thread", "direct-history-topup", self._runtime_direct_topup_loop),
                    ("_bar_close_thread", "bar-close-guard", self._bar_close_loop),
                    ("_warmup_thread", "runtime-warmup", self._warmup_loop),
                ]
            )
        return specs

    def _start_runtime_background_threads(self) -> list[str]:
        started: list[str] = []
        for attr_name, thread_name, target in self._runtime_background_thread_specs():
            thread = threading.Thread(
                target=target,
                daemon=True,
                name=thread_name,
            )
            setattr(self, attr_name, thread)
            thread.start()
            started.append(thread_name)
        return started

    def _open_slim_runtime_gate(self, reason: str = "runtime_slim_mode"):
        snapshot = self._warmup_snapshot_from_subscriptions()
        trade_symbols_total = int(snapshot.get("trade_symbols_total", 0) or 0)
        if not getattr(self, "_running", False):
            gate_open = False
            gate_reason = "runtime_stopped"
            phase = "blocked"
        elif not bool(getattr(self.session_keeper, "is_authenticated", False)):
            gate_open = False
            gate_reason = "session_unauthenticated"
            phase = "blocked"
        elif trade_symbols_total <= 0:
            gate_open = False
            gate_reason = "no_trade_symbols"
            phase = "blocked"
        else:
            gate_open = True
            gate_reason = "runtime_slim_mode"
            phase = "ready"
        self._set_warmup_state(
            phase=phase,
            reason=reason or gate_reason,
            requested_at=self._now_iso(),
            started_at=None,
            finished_at=self._now_iso(),
            last_error="",
            trading_gate_open=gate_open,
            trading_gate_reason=gate_reason,
            **self._warmup_scope_fields(snapshot),
            ready_symbols=snapshot["symbols_total"],
            ready_scan_symbols=snapshot["scan_symbols_total"],
            ready_subscription_symbols=snapshot["subscription_symbols_total"],
            ready_trade_symbols=snapshot["trade_symbols_total"],
            ready_monitor_symbols=snapshot["monitor_symbols_total"],
            ready_symbols_list=snapshot["symbols"],
            pending_symbols=[],
            symbol_status=[],
            integrity_pending_symbols=[],
            integrity_repair_reasons={},
            preflight_repair={},
            backfill_written=0,
            backfill_result={},
            compute_result={},
            timings={},
            last_duration_s=0.0,
        )

    def _host_resource_monitor_enabled(self) -> bool:
        service_mod = _service_mod()
        return self.config.get_bool_for_environment(
            "ibkr_host_resource_monitor_enabled",
            service_mod.ENVIRONMENT,
            True,
        )

    def _host_resource_monitor_interval_sec(self) -> float:
        service_mod = _service_mod()
        return max(
            1.0,
            self.config.get_float_for_environment(
                "ibkr_host_resource_monitor_interval_sec",
                service_mod.ENVIRONMENT,
                5.0,
            ),
        )

    def _start_host_resource_monitor(self):
        if not self._host_resource_monitor_enabled():
            return
        if self._resource_monitor_thread and self._resource_monitor_thread.is_alive():
            return
        self._resource_monitor_stop.clear()
        try:
            self.host_resource_monitor.sample_once()
        except Exception:
            _service_mod().logger.debug("Initial host resource sample failed", exc_info=True)
        self._resource_monitor_thread = threading.Thread(
            target=self.host_resource_monitor.run,
            args=(self._resource_monitor_stop, self._host_resource_monitor_interval_sec),
            daemon=True,
            name="host-resource-monitor",
        )
        self._resource_monitor_thread.start()

    def _host_resources_snapshot(self) -> dict:
        monitor = getattr(self, "host_resource_monitor", None)
        if monitor is None or not hasattr(monitor, "snapshot"):
            return {}
        try:
            return monitor.snapshot()
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    def _resource_governor_snapshot(self) -> dict:
        service_mod = _service_mod()
        monitor = getattr(self, "host_resource_monitor", None)
        if monitor is None or not hasattr(monitor, "governor_snapshot"):
            return {
                "status": "critical",
                "health": "unhealthy",
                "reasons": [{"code": "host_resource_monitor_unavailable"}],
                "admission": {
                    "watchlist_idle_topup": {
                        "admit": False,
                        "blockers": [{"code": "host_resource_monitor_unavailable"}],
                    }
                },
            }
        try:
            return monitor.governor_snapshot(self.config, service_mod.ENVIRONMENT)
        except Exception as exc:
            return {
                "status": "critical",
                "health": "unhealthy",
                "reasons": [{"code": "host_resource_governor_error", "message": str(exc)}],
                "admission": {
                    "watchlist_idle_topup": {
                        "admit": False,
                        "blockers": [{"code": "host_resource_governor_error", "message": str(exc)}],
                    }
                },
            }

    def _get_prev_close_for_quote(self, symbol: str) -> float | None:
        service_mod = _service_mod()
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return None
        current_date = self._current_market_date or self._market_date()
        if self._quote_prev_close_cache_date != current_date:
            self._quote_prev_close_cache = {}
            self._quote_prev_close_cache_date = current_date
        if normalized_symbol in self._quote_prev_close_cache:
            return self._quote_prev_close_cache.get(normalized_symbol)

        safe_symbol = normalized_symbol.replace('"', '\\"')
        safe_environment = str(service_mod.DATA_ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        environment_filter = f'(environment = "{safe_environment}"'
        if safe_environment == "live":
            environment_filter += ' || environment = "")'
        else:
            environment_filter += ")"

        prev_close = None
        current_date_ms = int(
            datetime.strptime(current_date, "%Y-%m-%d").replace(tzinfo=service_mod.ET).timestamp() * 1000
        )
        try:
            row = self.pb.get_first_record(
                "ibkr_bars",
                filter=(
                    f'symbol = "{safe_symbol}" && '
                    'interval = "1d" && '
                    f"{environment_filter} && "
                    f"bar_time_ms < {current_date_ms}"
                ),
                sort="-bar_time_ms",
            )
            close_value = float((row or {}).get("close", 0) or 0)
            if close_value > 0:
                prev_close = close_value
        except Exception as exc:
            service_mod.logger.debug("Prev close daily lookup failed for %s: %s", normalized_symbol, exc)

        if prev_close is None:
            previous_date_start_ms = max(0, current_date_ms - interval_to_ms("1d"))
            try:
                row = self.pb.get_first_record(
                    "ibkr_bars",
                    filter=(
                        f'symbol = "{safe_symbol}" && '
                        'interval = "5m" && '
                        f"{environment_filter} && "
                        f"bar_time_ms >= {previous_date_start_ms} && "
                        f"bar_time_ms < {current_date_ms}"
                    ),
                    sort="-bar_time_ms",
                )
                close_value = float((row or {}).get("close", 0) or 0)
                if close_value > 0:
                    prev_close = close_value
            except Exception as exc:
                service_mod.logger.debug("Prev close fallback lookup failed for %s: %s", normalized_symbol, exc)

        self._quote_prev_close_cache[normalized_symbol] = prev_close
        return prev_close

    def _on_ws_market_tick(self, tick_data: dict):
        self.realtime_quote_book.on_tick(tick_data)
        if not self._runtime_feed_technical_ticks_enabled():
            return
        self.bar_aggregator.on_tick(tick_data)
        order_flow_manager = getattr(self, "order_flow_manager", None)
        if order_flow_manager is not None:
            order_flow_manager.on_market_tick(tick_data)

    def _market_date(self) -> str:
        return datetime.now(_service_mod().ET).strftime("%Y-%m-%d")

    def _drain_compute_queue(self) -> int:
        drained = 0
        while True:
            try:
                self._compute_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        return drained

    @property
    def is_starting(self) -> bool:
        return self._starting

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_busy(self) -> bool:
        return self._starting or self._running
