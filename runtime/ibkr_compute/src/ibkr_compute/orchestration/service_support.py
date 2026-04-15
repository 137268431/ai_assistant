from __future__ import annotations

import queue
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
        safe_environment = str(service_mod.ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
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
        self.bar_aggregator.on_tick(tick_data)

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
