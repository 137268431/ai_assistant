from __future__ import annotations

import queue
import threading
import time

from ibkr_compute.market.timeframe_utils import bucket_start_ms, format_us_time, interval_to_ms


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceRuntimePipelineMixin:
    def _official_5m_enabled(self) -> bool:
        service_mod = _service_mod()
        return self.config.get_bool_for_environment("ibkr_official_5m_enabled", service_mod.ENVIRONMENT, True)

    def _official_5m_close_delay_sec(self) -> int:
        service_mod = _service_mod()
        return max(
            1,
            self.config.get_int_for_environment(
                "ibkr_official_5m_close_delay_sec",
                service_mod.ENVIRONMENT,
                service_mod.DEFAULT_OFFICIAL_5M_CLOSE_DELAY_SECONDS,
            ),
        )

    def _official_5m_request_period(self) -> str:
        service_mod = _service_mod()
        return str(
            self.config.get_for_environment(
                "ibkr_official_5m_request_period",
                service_mod.ENVIRONMENT,
                service_mod.DEFAULT_OFFICIAL_5M_REQUEST_PERIOD,
            )
            or service_mod.DEFAULT_OFFICIAL_5M_REQUEST_PERIOD
        ).strip() or service_mod.DEFAULT_OFFICIAL_5M_REQUEST_PERIOD

    def _initial_official_5m_state(self) -> dict:
        service_mod = _service_mod()
        return {
            "enabled": True,
            "driver": "ibkr_history_close",
            "close_delay_sec": service_mod.DEFAULT_OFFICIAL_5M_CLOSE_DELAY_SECONDS,
            "request_period": service_mod.DEFAULT_OFFICIAL_5M_REQUEST_PERIOD,
            "last_run": "",
            "last_due_bucket_ms": 0,
            "last_due_bucket_us": "",
            "last_completed_bucket_ms": 0,
            "last_completed_bucket_us": "",
            "last_written_bars": 0,
            "written_symbols": [],
            "written_symbols_total": 0,
            "pending_symbols": [],
            "pending_symbols_total": 0,
            "last_error": "",
        }

    def _copy_official_5m_state(self, source: dict | None = None) -> dict:
        payload = source if source is not None else self._official_5m_state
        copied = {}
        for key, value in payload.items():
            if isinstance(value, dict):
                copied[key] = dict(value)
            elif isinstance(value, list):
                copied[key] = [dict(item) if isinstance(item, dict) else item for item in value]
            else:
                copied[key] = value
        return copied

    def _set_official_5m_state(self, **updates) -> dict:
        with self._official_5m_lock:
            next_state = self._copy_official_5m_state()
            for key, value in updates.items():
                if isinstance(value, dict):
                    next_state[key] = dict(value)
                elif isinstance(value, list):
                    next_state[key] = [dict(item) if isinstance(item, dict) else item for item in value]
                else:
                    next_state[key] = value
            self._official_5m_state = next_state
            return self._copy_official_5m_state(next_state)

    def _copy_interval_prime_state(self) -> dict:
        with self._interval_prime_lock:
            return dict(self._interval_prime_state)

    def _schedule_interval_prime(self, symbols: list[str], source: str = "startup") -> bool:
        service_mod = _service_mod()
        normalized_symbols = [
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        ]
        if not normalized_symbols:
            return False

        with self._interval_prime_lock:
            if self._interval_prime_state.get("running"):
                return False
            self._interval_prime_state = {
                **self._interval_prime_state,
                "running": True,
                "completed_intervals": [],
                "symbol_count": len(normalized_symbols),
                "last_started_at": self._now_iso(),
                "last_finished_at": "",
                "last_duration_s": 0.0,
                "last_error": "",
                "last_source": source,
            }

        def worker():
            started = time.time()
            completed_intervals = []
            last_error = ""
            try:
                from ibkr_compute.api import server as compute_server

                try:
                    with compute_server.compute_lock:
                        compute_server.load_persisted_compute_cursors(service_mod.ENVIRONMENT)
                except Exception as exc:
                    service_mod.logger.warning("Interval prime cursor preload failed: %s", exc)

                for interval in service_mod.STARTUP_BACKGROUND_PRIME_INTERVALS:
                    if not self._running:
                        break
                    for index in range(
                        0,
                        len(normalized_symbols),
                        service_mod.STARTUP_BACKGROUND_PRIME_CHUNK_SIZE,
                    ):
                        if not self._running:
                            break
                        chunk = normalized_symbols[index:index + service_mod.STARTUP_BACKGROUND_PRIME_CHUNK_SIZE]
                        with compute_server.compute_lock:
                            compute_server.materialize_engines_from_storage(
                                service_mod.ENVIRONMENT,
                                chunk,
                                interval,
                            )
                    completed_intervals.append(interval)
                    service_mod.logger.info(
                        "Background interval prime finished (%s): interval=%s symbols=%d",
                        source,
                        interval,
                        len(normalized_symbols),
                    )
            except Exception as exc:
                last_error = str(exc)
                service_mod.logger.warning("Background interval prime failed (%s): %s", source, exc)
            finally:
                finished_at = self._now_iso()
                with self._interval_prime_lock:
                    self._interval_prime_state = {
                        **self._interval_prime_state,
                        "running": False,
                        "completed_intervals": completed_intervals,
                        "last_finished_at": finished_at,
                        "last_duration_s": round(max(0.0, time.time() - started), 3),
                        "last_error": last_error,
                        "last_source": source,
                    }

        self._interval_prime_thread = threading.Thread(
            target=worker,
            daemon=True,
            name="interval-prime",
        )
        self._interval_prime_thread.start()
        return True

    def _trigger_realtime_compute(self, source: str = "bar_close", symbols: list[str] | None = None) -> dict:
        service_mod = _service_mod()
        try:
            from ibkr_compute.api import server as compute_server

            payload = {"source": source, "environments": [service_mod.ENVIRONMENT]}
            normalized_symbols = sorted(
                {str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()}
            )
            if normalized_symbols:
                payload["symbols"] = normalized_symbols
            with compute_server.app.test_request_context(
                "/compute",
                method="POST",
                json=payload,
            ):
                response = compute_server.compute()
            if hasattr(response, "get_json"):
                return response.get_json() or {}
        except Exception as exc:
            service_mod.logger.error("Realtime compute trigger failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "empty_response"}

    def _normalize_compute_event(self, item) -> dict | None:
        if item is None:
            return None
        if isinstance(item, dict):
            symbols = sorted(
                {
                    str(symbol or "").strip().upper()
                    for symbol in (item.get("symbols") or [])
                    if str(symbol or "").strip()
                }
            )
            return {
                "source": str(item.get("source") or "bar_close").strip().lower() or "bar_close",
                "bar_count": max(0, int(item.get("bar_count", 0) or 0)),
                "symbols": symbols,
            }
        return {
            "source": "bar_close",
            "bar_count": max(0, int(item or 0)),
            "symbols": [],
        }

    def _queue_compute_event(self, source: str, bar_count: int = 0, symbols: list[str] | None = None):
        if not self._running:
            return
        self._compute_queue.put(
            {
                "source": str(source or "bar_close").strip().lower() or "bar_close",
                "bar_count": max(0, int(bar_count or 0)),
                "symbols": sorted(
                    {str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()}
                ),
            }
        )

    def _latest_safe_closed_5m_ms(self, now_ts: float | None = None) -> int:
        delay_ms = int(self._official_5m_close_delay_sec() * 1000)
        effective_ms = int((now_ts or time.time()) * 1000) - delay_ms
        if effective_ms <= interval_to_ms("5m"):
            return 0
        return bucket_start_ms(effective_ms - interval_to_ms("5m"), "5m")

    def _run_official_5m_close_cycle(self, symbols_override: list[str] | None = None):
        service_mod = _service_mod()
        state = self._copy_official_5m_state()
        due_bucket_ms = self._latest_safe_closed_5m_ms()
        if due_bucket_ms <= 0:
            return

        last_completed_bucket_ms = int(state.get("last_completed_bucket_ms", 0) or 0)
        pending_symbols = sorted(
            {
                str(symbol or "").strip().upper()
                for symbol in (state.get("pending_symbols") or [])
                if str(symbol or "").strip()
            }
        )
        if due_bucket_ms <= last_completed_bucket_ms and not pending_symbols:
            return
        if (
            due_bucket_ms == int(state.get("last_due_bucket_ms", 0) or 0)
            and pending_symbols
            and (time.time() - float(self._official_5m_last_cycle_at or 0.0)) < 5.0
        ):
            return

        if self._starting:
            self._set_official_5m_state(
                enabled=self._official_5m_enabled(),
                close_delay_sec=self._official_5m_close_delay_sec(),
                request_period=self._official_5m_request_period(),
                last_due_bucket_ms=due_bucket_ms,
                last_due_bucket_us=format_us_time(due_bucket_ms) if due_bucket_ms > 0 else "",
            )
            return

        snapshot = self._warmup_snapshot_from_subscriptions()
        override_set = {
            str(symbol or "").strip().upper()
            for symbol in (symbols_override or [])
            if str(symbol or "").strip()
        }
        symbols = [
            symbol for symbol in list(snapshot.get("symbols") or [])
            if not override_set or symbol in override_set
        ]
        conid_map = dict(snapshot.get("conid_map") or {})
        symbol_meta = dict(snapshot.get("symbol_meta") or {})
        if not symbols:
            self._set_official_5m_state(
                enabled=self._official_5m_enabled(),
                close_delay_sec=self._official_5m_close_delay_sec(),
                request_period=self._official_5m_request_period(),
                last_run=self._now_iso(),
                last_due_bucket_ms=due_bucket_ms,
                last_due_bucket_us=format_us_time(due_bucket_ms),
                pending_symbols=[],
                pending_symbols_total=0,
                written_symbols=[],
                written_symbols_total=0,
                last_written_bars=0,
                last_error="",
            )
            return

        request_period = self._official_5m_request_period()
        written_symbols = []
        next_pending_symbols = []
        written_bars = 0
        cycle_errors = []

        for symbol in symbols:
            conid = int(conid_map.get(symbol) or 0)
            if conid <= 0:
                next_pending_symbols.append(symbol)
                continue

            exchange = str((symbol_meta.get(symbol) or {}).get("exchange") or "").upper()
            latest_stored_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
            if latest_stored_ms >= due_bucket_ms:
                continue

            try:
                fetched_rows = self.data_backfill.fetch_history(
                    conid,
                    symbol,
                    interval="5m",
                    exchange=exchange,
                    repair=False,
                    request_period=request_period,
                )
            except Exception as exc:
                fetched_rows = []
                cycle_errors.append(f"{symbol}:{exc}")

            candidate_rows = [
                dict(row)
                for row in (fetched_rows or [])
                if latest_stored_ms < int(row.get("bar_time_ms", 0) or 0) <= due_bucket_ms
            ]
            candidate_rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))

            max_written_ms = latest_stored_ms
            wrote_symbol = False
            for row in candidate_rows:
                payload = dict(row)
                payload["environment"] = service_mod.ENVIRONMENT
                payload["exchange"] = exchange
                payload["source"] = "ibkr_history_close"
                extra = dict(payload.get("extra") or {})
                extra.update(
                    {
                        "source": "ibkr_history_close",
                        "canonical": True,
                        "request_period": request_period,
                    }
                )
                payload["extra"] = extra
                if self.data_writer.write_bar(payload):
                    wrote_symbol = True
                    written_bars += 1
                    max_written_ms = max(max_written_ms, int(payload.get("bar_time_ms", 0) or 0))

            if wrote_symbol:
                written_symbols.append(symbol)
            if max_written_ms < due_bucket_ms:
                next_pending_symbols.append(symbol)

        compute_symbols = [
            symbol for symbol in symbols
            if symbol not in next_pending_symbols
        ]
        if written_bars > 0 and compute_symbols:
            self._last_bar_close_at = time.time()
            self.data_writer.flush()
            self._queue_compute_event("canonical_close", bar_count=written_bars, symbols=compute_symbols)

        blocking_pending_symbols = self._non_monitor_pending_symbols(
            next_pending_symbols,
            snapshot.get("monitor_symbols") or [],
        )
        next_last_completed_bucket_ms = last_completed_bucket_ms
        if not blocking_pending_symbols:
            next_last_completed_bucket_ms = max(last_completed_bucket_ms, due_bucket_ms)

        self._set_official_5m_state(
            enabled=self._official_5m_enabled(),
            close_delay_sec=self._official_5m_close_delay_sec(),
            request_period=request_period,
            last_run=self._now_iso(),
            last_due_bucket_ms=due_bucket_ms,
            last_due_bucket_us=format_us_time(due_bucket_ms),
            last_completed_bucket_ms=next_last_completed_bucket_ms,
            last_completed_bucket_us=format_us_time(next_last_completed_bucket_ms)
            if next_last_completed_bucket_ms > 0 else "",
            last_written_bars=written_bars,
            written_symbols=written_symbols,
            written_symbols_total=len(written_symbols),
            pending_symbols=blocking_pending_symbols,
            pending_symbols_total=len(blocking_pending_symbols),
            last_error="; ".join(cycle_errors),
        )
        self._official_5m_last_cycle_at = time.time()

    def _official_5m_close_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Official 5m close loop started")
        while self._running:
            try:
                enabled = self._official_5m_enabled()
                self._set_official_5m_state(
                    enabled=enabled,
                    close_delay_sec=self._official_5m_close_delay_sec(),
                    request_period=self._official_5m_request_period(),
                )
                if enabled:
                    self._run_official_5m_close_cycle()
            except Exception as exc:
                service_mod.logger.error("Official 5m close loop error: %s", exc)
                self._set_official_5m_state(
                    enabled=self._official_5m_enabled(),
                    close_delay_sec=self._official_5m_close_delay_sec(),
                    request_period=self._official_5m_request_period(),
                    last_run=self._now_iso(),
                    last_error=str(exc),
                )
            time.sleep(1)

    def _compute_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Realtime close-driven compute loop started")
        startup_wait_logged_at = 0.0
        while self._running or not self._compute_queue.empty():
            if self._starting:
                queue_size = int(self._compute_queue.qsize())
                now = time.time()
                if queue_size > 0 and (now - startup_wait_logged_at) >= 15:
                    service_mod.logger.info(
                        "Realtime compute deferred while startup warmup is active: queue=%d",
                        queue_size,
                    )
                    startup_wait_logged_at = now
                time.sleep(1)
                continue

            startup_wait_logged_at = 0.0
            try:
                first_item = self._compute_queue.get(timeout=1)
            except queue.Empty:
                continue

            if first_item is None:
                continue

            close_events = 1
            merged_event = self._normalize_compute_event(first_item) or {
                "source": "bar_close",
                "bar_count": 0,
                "symbols": [],
            }
            drain_until = time.time() + 0.25
            while time.time() < drain_until:
                try:
                    next_item = self._compute_queue.get_nowait()
                except queue.Empty:
                    break
                if next_item is None:
                    continue
                close_events += 1
                next_event = self._normalize_compute_event(next_item)
                if not next_event:
                    continue
                merged_event["bar_count"] += int(next_event.get("bar_count", 0) or 0)
                merged_event["symbols"] = sorted(
                    set(merged_event.get("symbols") or []).union(next_event.get("symbols") or [])
                )
                if str(next_event.get("source") or "") == "canonical_close":
                    merged_event["source"] = "canonical_close"

            try:
                self._last_realtime_compute_started_at = time.time()
                self.data_writer.flush()
                result = self._trigger_realtime_compute(
                    source=str(merged_event.get("source") or "bar_close"),
                    symbols=list(merged_event.get("symbols") or []),
                )
                self._realtime_compute_runs += 1
                self._last_realtime_compute_at = time.time()
                self._last_realtime_compute_result = result or {}
                service_mod.logger.info(
                    "Realtime compute finished: source=%s events=%d bars=%d symbols=%d processed=%s signals=%s errors=%s elapsed_s=%s",
                    merged_event.get("source"),
                    close_events,
                    merged_event.get("bar_count", 0),
                    len(merged_event.get("symbols") or []),
                    result.get("processed", 0),
                    result.get("signals", 0),
                    result.get("errors", 0),
                    result.get("elapsed_s", 0),
                )
                if int(result.get("signals", 0) or 0) > 0:
                    self._signal_wakeup.set()
            except Exception as exc:
                service_mod.logger.error("Realtime compute loop error: %s", exc)
            finally:
                self._last_realtime_compute_started_at = 0.0

    def _bar_close_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Bar close guard loop started")
        while self._running:
            try:
                self.bar_aggregator.force_close_due()
            except Exception as exc:
                service_mod.logger.error("Bar close guard loop error: %s", exc)
            time.sleep(1)

    def _on_bar_close(self, bar_data: dict):
        _ = bar_data
        self._last_bar_close_at = time.time()
