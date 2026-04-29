from __future__ import annotations

from datetime import datetime
import queue
import threading
import time

from ibkr_compute.market.timeframe_utils import ET, bucket_start_ms, format_us_time, interval_to_ms
from ibkr_compute.market.timeframe_utils import latest_safe_closed_bucket_ms


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceRuntimePipelineMixin:
    def _load_persisted_compute_cursor_map(self, environment: str) -> dict[str, int]:
        runtime_environment = str(environment or "live").strip().lower() or "live"
        try:
            record = self.pb.get_state("compute_cursors", runtime_environment, date="global")
        except Exception:
            return {}

        payload = (record or {}).get("data") if isinstance(record, dict) else {}
        cursors = payload.get("cursors") if isinstance(payload, dict) else {}
        if not isinstance(cursors, dict):
            return {}

        cursor_map = {}
        for raw_key, raw_value in cursors.items():
            key = str(raw_key or "").strip()
            bar_ms = int(raw_value or 0)
            if key and bar_ms > 0:
                cursor_map[key] = bar_ms
        return cursor_map

    def _official_5m_due_compute_symbols(
        self,
        environment: str,
        symbols: list[str],
        due_bucket_ms: int,
    ) -> list[str]:
        if due_bucket_ms <= 0:
            return []

        cursor_map = self._load_persisted_compute_cursor_map(environment)
        due_symbols = []
        for symbol in (symbols or []):
            normalized_symbol = str(symbol or "").strip().upper()
            if not normalized_symbol:
                continue
            cursor_ms = int(cursor_map.get(f"{normalized_symbol}|5m", 0) or 0)
            if cursor_ms < due_bucket_ms:
                due_symbols.append(normalized_symbol)
        return due_symbols

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
            "pending_symbol_details": [],
            "sequence_gap_count": 0,
            "missing_required_bars_total": 0,
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
                from ibkr_compute.api.service_topology import uses_remote_compute_service

                if uses_remote_compute_service():
                    from ibkr_compute.api.compute_status_client import trigger_remote_prime

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
                            result = trigger_remote_prime(
                                {
                                    "environments": [service_mod.ENVIRONMENT],
                                    "symbols": chunk,
                                    "intervals": [interval],
                                    "persist_latest_indicator": False,
                                }
                            )
                            if result.get("ok") is False:
                                raise RuntimeError(
                                    str(result.get("error") or f"remote_prime_failed:{interval}")
                                )
                        completed_intervals.append(interval)
                        service_mod.logger.info(
                            "Remote background interval prime finished (%s): interval=%s symbols=%d",
                            source,
                            interval,
                            len(normalized_symbols),
                        )
                    return

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
                                persist_latest_indicator=False,
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

    def _trigger_realtime_compute(
        self,
        source: str = "bar_close",
        symbols: list[str] | None = None,
        persist_signals: bool | None = None,
    ) -> dict:
        service_mod = _service_mod()
        try:
            payload = {"source": source, "environments": [service_mod.ENVIRONMENT]}
            if persist_signals is not None:
                payload["persist_signals"] = bool(persist_signals)
            normalized_symbols = sorted(
                {str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()}
            )
            if normalized_symbols:
                payload["symbols"] = normalized_symbols
            from ibkr_compute.api.service_topology import uses_remote_compute_service

            if uses_remote_compute_service():
                from ibkr_compute.api.compute_status_client import trigger_remote_compute

                return trigger_remote_compute(payload)

            from ibkr_compute.api import server as compute_server

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
        return latest_safe_closed_bucket_ms(
            "5m",
            delay_seconds=self._official_5m_close_delay_sec(),
            now_ms=int((now_ts or time.time()) * 1000),
        )

    def _official_5m_required_window_start_ms(
        self,
        last_completed_bucket_ms: int,
        due_bucket_ms: int,
    ) -> int:
        step_ms = interval_to_ms("5m")
        if step_ms <= 0 or due_bucket_ms <= 0:
            return 0
        if last_completed_bucket_ms > 0:
            return last_completed_bucket_ms + step_ms
        return due_bucket_ms

    def _official_5m_session_start_ms(self, due_bucket_ms: int) -> int:
        if due_bucket_ms <= 0:
            return 0
        due_dt = datetime.fromtimestamp(int(due_bucket_ms) / 1000.0, ET)
        session_start_dt = due_dt.replace(hour=9, minute=30, second=0, microsecond=0)
        return int(session_start_dt.timestamp() * 1000)

    def _restore_official_5m_state_from_storage(
        self,
        symbols: list[str],
        monitor_symbols: list[str],
        due_bucket_ms: int,
        request_period: str,
    ):
        session_start_ms = self._official_5m_session_start_ms(due_bucket_ms)
        required_window_active = session_start_ms > 0 and due_bucket_ms >= session_start_ms
        step_ms = interval_to_ms("5m")
        pending_symbols = []
        pending_detail_by_symbol = {}
        earliest_missing_ms = 0
        cycle_errors = []

        for symbol in symbols:
            latest_stored_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
            sequence_status = {}
            if required_window_active:
                sequence_status = self.data_backfill.get_required_sequence_snapshot(
                    symbol,
                    "5m",
                    start_ms=session_start_ms,
                    end_ms=due_bucket_ms,
                    example_limit=4,
                )

            missing_bar_times = list(sequence_status.get("missing_bar_times") or [])
            missing_us_times = list(sequence_status.get("missing_us_times") or [])
            missing_count = int(sequence_status.get("missing_count", 0) or 0)
            query_error = str(sequence_status.get("query_error") or "").strip()
            if query_error:
                cycle_errors.append(f"{symbol}:sequence:{query_error}")
            if missing_bar_times:
                first_missing_ms = int(missing_bar_times[0] or 0)
                if first_missing_ms > 0 and (earliest_missing_ms <= 0 or first_missing_ms < earliest_missing_ms):
                    earliest_missing_ms = first_missing_ms

            symbol_pending = bool(query_error)
            if required_window_active and missing_count > 0:
                symbol_pending = True
            elif required_window_active and latest_stored_ms < due_bucket_ms:
                symbol_pending = True

            if not symbol_pending:
                continue

            pending_symbols.append(symbol)
            pending_detail_by_symbol[symbol] = {
                "symbol": symbol,
                "missing_count": max(missing_count, 1 if required_window_active else 0),
                "missing_us_times": (
                    missing_us_times[:4]
                    if missing_us_times
                    else ([format_us_time(due_bucket_ms)] if required_window_active and due_bucket_ms > 0 else [])
                ),
                "required_start_us": format_us_time(session_start_ms) if required_window_active else "",
                "due_bucket_us": format_us_time(due_bucket_ms) if due_bucket_ms > 0 else "",
                "latest_stored_us": format_us_time(latest_stored_ms) if latest_stored_ms > 0 else "",
                "has_sequence_gap": bool(missing_count > 0 or query_error),
            }

        blocking_pending_symbols = self._non_monitor_pending_symbols(
            pending_symbols,
            monitor_symbols,
        )
        pending_symbol_details = [
            dict(pending_detail_by_symbol.get(symbol) or {"symbol": symbol})
            for symbol in blocking_pending_symbols
        ]

        next_last_completed_bucket_ms = 0
        if due_bucket_ms > 0 and not blocking_pending_symbols:
            next_last_completed_bucket_ms = due_bucket_ms
        elif (
            required_window_active
            and earliest_missing_ms > session_start_ms
            and step_ms > 0
        ):
            next_last_completed_bucket_ms = max(0, earliest_missing_ms - step_ms)

        self._set_official_5m_state(
            enabled=self._official_5m_enabled(),
            close_delay_sec=self._official_5m_close_delay_sec(),
            request_period=request_period,
            last_run=self._now_iso(),
            last_due_bucket_ms=due_bucket_ms,
            last_due_bucket_us=format_us_time(due_bucket_ms) if due_bucket_ms > 0 else "",
            last_completed_bucket_ms=next_last_completed_bucket_ms,
            last_completed_bucket_us=(
                format_us_time(next_last_completed_bucket_ms)
                if next_last_completed_bucket_ms > 0 else ""
            ),
            last_written_bars=0,
            written_symbols=[],
            written_symbols_total=0,
            pending_symbols=blocking_pending_symbols,
            pending_symbols_total=len(blocking_pending_symbols),
            pending_symbol_details=pending_symbol_details,
            sequence_gap_count=sum(1 for item in pending_symbol_details if bool(item.get("has_sequence_gap"))),
            missing_required_bars_total=sum(max(0, int(item.get("missing_count", 0) or 0)) for item in pending_symbol_details),
            last_error="; ".join(cycle_errors),
        )
        self._official_5m_last_cycle_at = time.time()

    def _official_5m_candidate_rows(
        self,
        fetched_rows: list[dict] | None,
        start_ms: int,
        end_ms: int,
    ) -> list[dict]:
        rows = [
            dict(row)
            for row in (fetched_rows or [])
            if start_ms <= int(row.get("bar_time_ms", 0) or 0) <= end_ms
        ]
        rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
        return rows

    def _write_official_5m_rows(
        self,
        rows: list[dict] | None,
        exchange: str,
        request_period: str,
    ) -> tuple[bool, int, int]:
        service_mod = _service_mod()
        wrote_symbol = False
        written_bars = 0
        max_written_ms = 0
        for row in (rows or []):
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
        return wrote_symbol, written_bars, max_written_ms

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

        snapshot = self._warmup_snapshot_from_subscriptions()
        override_set = {
            str(symbol or "").strip().upper()
            for symbol in (symbols_override or [])
            if str(symbol or "").strip()
        }
        snapshot_symbols = list(snapshot.get("symbols") or [])
        if override_set:
            symbols = [symbol for symbol in snapshot_symbols if symbol in override_set]
        elif "trade_symbols" in snapshot:
            trade_set = set(self._normalize_symbol_list(snapshot.get("trade_symbols") or []))
            symbols = [symbol for symbol in snapshot_symbols if symbol in trade_set]
        else:
            symbols = snapshot_symbols
        conid_map = dict(snapshot.get("conid_map") or {})
        symbol_meta = dict(snapshot.get("symbol_meta") or {})
        request_period = self._official_5m_request_period()

        if self._starting:
            self._restore_official_5m_state_from_storage(
                symbols,
                snapshot.get("monitor_symbols") or [],
                due_bucket_ms,
                request_period,
            )
            restored_state = self._copy_official_5m_state()
            restored_pending_symbols = sorted(
                {
                    str(symbol or "").strip().upper()
                    for symbol in (restored_state.get("pending_symbols") or [])
                    if str(symbol or "").strip()
                }
            )
            if not restored_pending_symbols:
                return
            last_completed_bucket_ms = int(restored_state.get("last_completed_bucket_ms", 0) or 0)
            pending_set = set(restored_pending_symbols)
            symbols = [symbol for symbol in symbols if symbol in pending_set]

        if not symbols:
            self._set_official_5m_state(
                enabled=self._official_5m_enabled(),
                close_delay_sec=self._official_5m_close_delay_sec(),
                request_period=request_period,
                last_run=self._now_iso(),
                last_due_bucket_ms=due_bucket_ms,
                last_due_bucket_us=format_us_time(due_bucket_ms),
                pending_symbols=[],
                pending_symbols_total=0,
                pending_symbol_details=[],
                sequence_gap_count=0,
                missing_required_bars_total=0,
                written_symbols=[],
                written_symbols_total=0,
                last_written_bars=0,
                last_error="",
            )
            return

        required_window_start_ms = self._official_5m_required_window_start_ms(
            last_completed_bucket_ms,
            due_bucket_ms,
        )
        written_symbols = []
        next_pending_symbols = []
        pending_detail_by_symbol = {}
        written_bars = 0
        cycle_errors = []

        for symbol in symbols:
            conid = int(conid_map.get(symbol) or 0)
            if conid <= 0:
                next_pending_symbols.append(symbol)
                pending_detail_by_symbol[symbol] = {
                    "symbol": symbol,
                    "missing_count": 1 if due_bucket_ms > 0 else 0,
                    "missing_us_times": [format_us_time(due_bucket_ms)] if due_bucket_ms > 0 else [],
                    "required_start_us": format_us_time(required_window_start_ms) if required_window_start_ms > 0 else "",
                    "due_bucket_us": format_us_time(due_bucket_ms) if due_bucket_ms > 0 else "",
                    "latest_stored_us": "",
                    "has_sequence_gap": False,
                }
                continue

            exchange = str((symbol_meta.get(symbol) or {}).get("exchange") or "").upper()
            latest_stored_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
            max_written_ms = latest_stored_ms
            wrote_symbol = False

            if latest_stored_ms < due_bucket_ms:
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

                candidate_rows = self._official_5m_candidate_rows(
                    fetched_rows,
                    latest_stored_ms + 1,
                    due_bucket_ms,
                )
                if fetched_rows and not candidate_rows:
                    fetched_times = sorted(int(row.get("bar_time_ms", 0) or 0) for row in fetched_rows)
                    service_mod.logger.warning(
                        "Official 5m close filtered all fetched rows for %s: latest_stored_ms=%d due_bucket_ms=%d fetched_first=%d(%s) fetched_last=%d(%s) fetched_count=%d",
                        symbol,
                        latest_stored_ms,
                        due_bucket_ms,
                        fetched_times[0],
                        format_us_time(fetched_times[0]),
                        fetched_times[-1],
                        format_us_time(fetched_times[-1]),
                        len(fetched_times),
                    )

                incremental_wrote, incremental_bars, incremental_max_ms = self._write_official_5m_rows(
                    candidate_rows,
                    exchange,
                    request_period,
                )
                wrote_symbol = wrote_symbol or incremental_wrote
                written_bars += incremental_bars
                max_written_ms = max(max_written_ms, incremental_max_ms)

            if wrote_symbol:
                written_symbols.append(symbol)

            sequence_status = {}
            if required_window_start_ms > 0 and required_window_start_ms <= due_bucket_ms:
                if wrote_symbol and not self.data_writer.flush():
                    cycle_errors.append(f"{symbol}:flush:incremental")
                sequence_status = self.data_backfill.get_required_sequence_snapshot(
                    symbol,
                    "5m",
                    start_ms=required_window_start_ms,
                    end_ms=due_bucket_ms,
                    example_limit=4,
                )
                if str(sequence_status.get("query_error") or "").strip():
                    cycle_errors.append(f"{symbol}:sequence:{sequence_status['query_error']}")

            missing_bar_times = list(sequence_status.get("missing_bar_times") or [])
            missing_us_times = list(sequence_status.get("missing_us_times") or [])
            missing_count = int(sequence_status.get("missing_count", 0) or 0)
            if missing_count > 0:
                repair_rows = self.data_backfill.fetch_history(
                    conid,
                    symbol,
                    interval="5m",
                    exchange=exchange,
                    repair=True,
                    request_period=request_period,
                )
                repair_candidate_rows = self._official_5m_candidate_rows(
                    repair_rows,
                    required_window_start_ms,
                    due_bucket_ms,
                )
                repair_wrote, repair_bars, repair_max_ms = self._write_official_5m_rows(
                    repair_candidate_rows,
                    exchange,
                    request_period,
                )
                wrote_symbol = wrote_symbol or repair_wrote
                written_bars += repair_bars
                max_written_ms = max(max_written_ms, repair_max_ms)
                if repair_wrote and symbol not in written_symbols:
                    written_symbols.append(symbol)
                if repair_wrote and not self.data_writer.flush():
                    cycle_errors.append(f"{symbol}:flush:repair")

                sequence_status = self.data_backfill.get_required_sequence_snapshot(
                    symbol,
                    "5m",
                    start_ms=required_window_start_ms,
                    end_ms=due_bucket_ms,
                    example_limit=4,
                )
                missing_bar_times = list(sequence_status.get("missing_bar_times") or [])
                missing_us_times = list(sequence_status.get("missing_us_times") or [])
                missing_count = int(sequence_status.get("missing_count", 0) or 0)
                if missing_count > 0:
                    service_mod.logger.warning(
                        "Official 5m close still missing required bars for %s: required_start_ms=%d(%s) due_bucket_ms=%d(%s) missing=%s latest_stored_ms=%d(%s)",
                        symbol,
                        required_window_start_ms,
                        format_us_time(required_window_start_ms),
                        due_bucket_ms,
                        format_us_time(due_bucket_ms),
                        ",".join(missing_us_times) or ",".join(format_us_time(ms) for ms in missing_bar_times[:4]),
                        max_written_ms,
                        format_us_time(max_written_ms) if max_written_ms > 0 else "",
                    )

            has_sequence_gap = missing_count > 0
            bucket_unfilled = max_written_ms < due_bucket_ms
            if has_sequence_gap or bucket_unfilled:
                next_pending_symbols.append(symbol)
                pending_detail_by_symbol[symbol] = {
                    "symbol": symbol,
                    "missing_count": max(missing_count, 1 if bucket_unfilled and due_bucket_ms > 0 else 0),
                    "missing_us_times": (
                        missing_us_times[:4]
                        if missing_us_times
                        else ([format_us_time(due_bucket_ms)] if bucket_unfilled and due_bucket_ms > 0 else [])
                    ),
                    "required_start_us": format_us_time(required_window_start_ms) if required_window_start_ms > 0 else "",
                    "due_bucket_us": format_us_time(due_bucket_ms) if due_bucket_ms > 0 else "",
                    "latest_stored_us": format_us_time(max_written_ms) if max_written_ms > 0 else "",
                    "has_sequence_gap": has_sequence_gap,
                }

        compute_symbols = [
            symbol for symbol in symbols
            if symbol not in next_pending_symbols
        ]
        due_compute_symbols = self._official_5m_due_compute_symbols(
            service_mod.ENVIRONMENT,
            compute_symbols,
            due_bucket_ms,
        )
        if due_compute_symbols:
            self._last_bar_close_at = time.time()
            self.data_writer.flush()
            self._queue_compute_event("canonical_close", bar_count=written_bars, symbols=due_compute_symbols)

        blocking_pending_symbols = self._non_monitor_pending_symbols(
            next_pending_symbols,
            snapshot.get("monitor_symbols") or [],
        )
        pending_symbol_details = [
            dict(pending_detail_by_symbol.get(symbol) or {"symbol": symbol})
            for symbol in blocking_pending_symbols
        ]
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
            pending_symbol_details=pending_symbol_details,
            sequence_gap_count=sum(1 for item in pending_symbol_details if bool(item.get("has_sequence_gap"))),
            missing_required_bars_total=sum(max(0, int(item.get("missing_count", 0) or 0)) for item in pending_symbol_details),
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
        while self._running or not self._compute_queue.empty():
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
                startup_compute = bool(self._starting)
                self._last_realtime_compute_started_at = time.time()
                self.data_writer.flush()
                result = self._trigger_realtime_compute(
                    source=str(merged_event.get("source") or "bar_close"),
                    symbols=list(merged_event.get("symbols") or []),
                    persist_signals=False if startup_compute else None,
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
