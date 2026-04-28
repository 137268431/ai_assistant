from __future__ import annotations

import threading
import time
from datetime import datetime


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceRuntimeOpsMixin:
    def _on_order_fill(self, order: dict):
        service_mod = _service_mod()
        symbol = str(order.get("ticker") or order.get("symbol") or "").strip().upper()
        order_type = str(order.get("orderType") or order.get("order_type") or "").strip().upper()
        has_parent = bool(str(order.get("parentId") or order.get("parent_id") or "").strip())
        side = str(order.get("side") or "").strip().upper()
        role = "entry"
        if has_parent and order_type in {"STP", "STOP", "STOPLOSS"}:
            role = "stop_loss"
        elif has_parent:
            role = "take_profit"
        service_mod.logger.info("Order filled: %s role=%s", symbol or order.get("ticker"), role)
        if not symbol:
            return
        if role == "entry":
            direction = "long" if side == "BUY" else "short" if side == "SELL" else ""
            self.signal_processor.register_filled_position(
                symbol,
                {
                    "direction": direction,
                    "broker_order_id": str(order.get("orderId") or order.get("order_id") or ""),
                    "state": "filled_position",
                },
            )
            return
        self.signal_processor.remove_position(symbol)
        if role == "stop_loss":
            self.order_lifecycle.increment_sl_count()
            self.signal_processor.start_cooldown(
                symbol,
                self.signal_processor.cooldown_bars_after_sl(),
                "cooldown_after_stop_loss",
            )

    def _on_order_cancel(self, order: dict):
        service_mod = _service_mod()
        symbol = str(order.get("ticker") or order.get("symbol") or "").strip().upper()
        has_parent = bool(str(order.get("parentId") or order.get("parent_id") or "").strip())
        service_mod.logger.info("Order cancelled: %s", symbol or order.get("ticker"))
        if symbol and not has_parent:
            self.signal_processor.remove_position(symbol)

    def _schedule_retention(self):
        service_mod = _service_mod()

        def retention_loop():
            last_handled_hour = ""
            while self._running:
                et_now = datetime.now(service_mod.ET)
                hour_key = et_now.strftime("%Y-%m-%d %H")
                if et_now.minute == 12 and hour_key != last_handled_hour:
                    result = self.data_retention.cleanup(source="runtime_thread")
                    env_result = (result.get("environments") or [{}])[0]
                    service_mod.logger.info(
                        "Runtime retention cleanup finished: environment=%s deleted=%s errors=%s skipped=%s reason=%s",
                        service_mod.ENVIRONMENT,
                        int(result.get("total_deleted", 0) or 0),
                        int(result.get("total_errors", 0) or 0),
                        bool(env_result.get("skipped")),
                        str(env_result.get("reason") or ""),
                    )
                    last_handled_hour = hour_key
                time.sleep(30)

        thread = threading.Thread(
            target=retention_loop,
            daemon=True,
            name="data-retention",
        )
        thread.start()

    def stop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Stopping IBKR Trading Service...")
        with self._state_lock:
            self._starting = False
            self._startup_progress_enabled = False
            self._startup_cycle_id = ""
            self._startup_reason = ""
            self._startup_source = ""
            self._startup_trigger_login = False
            self._startup_status_message_id = ""
        self._running = False
        self._last_session_authenticated = False
        self._auth_probe_stop.set()

        self.auth_handler.cancel()
        self.bar_aggregator.force_close_all()
        self.timeframe_builder.reset()
        self.realtime_quote_book.reset()
        self.ws_client.stop()
        self.session_keeper.stop()
        self.order_tracker.stop()
        self.order_lifecycle.stop()
        self.data_writer.close()
        self._signal_wakeup.set()
        self._warmup_wakeup.set()
        self._compute_queue.put(None)

        if self._signal_thread:
            self._signal_thread.join(timeout=10)
        if self._subscription_thread:
            self._subscription_thread.join(timeout=10)
        if self._active_repair_thread:
            self._active_repair_thread.join(timeout=10)
        if self._watchlist_backfill_thread:
            self._watchlist_backfill_thread.join(timeout=10)
        if self._compute_thread:
            self._compute_thread.join(timeout=10)
        if self._official_close_thread:
            self._official_close_thread.join(timeout=10)
        if self._bar_close_thread:
            self._bar_close_thread.join(timeout=10)
        if self._warmup_thread:
            self._warmup_thread.join(timeout=10)
        if self._interval_prime_thread:
            self._interval_prime_thread.join(timeout=10)
        if self._auth_probe_thread and self._auth_probe_thread is not threading.current_thread():
            self._auth_probe_thread.join(timeout=5)

        self._set_warmup_state(
            phase="stopped",
            reason="service_stopped",
            finished_at=self._now_iso(),
            trading_gate_open=False,
            trading_gate_reason="runtime_stopped",
        )
        self._set_auth_recovery_state(
            recovery_phase="runtime_stopped",
            last_recovery_source="runtime_stop",
            lock_owner="",
            lock_expires_at="",
        )

        service_mod.logger.info("IBKR Trading Service stopped")
