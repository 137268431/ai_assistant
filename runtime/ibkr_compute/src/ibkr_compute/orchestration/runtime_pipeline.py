from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import queue
import threading
import time
import uuid

from ibkr_compute.market.timeframe_utils import ET, bucket_start_ms, format_us_time, interval_to_ms, normalize_interval
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

    def _official_5m_parallel_enabled(self) -> bool:
        service_mod = _service_mod()
        return self.config.get_bool_for_environment(
            "ibkr_official_5m_parallel_enabled",
            service_mod.ENVIRONMENT,
            True,
        )

    def _official_5m_max_concurrency(self) -> int:
        service_mod = _service_mod()
        return max(
            1,
            min(
                10,
                self.config.get_int_for_environment(
                    "ibkr_official_5m_max_concurrency",
                    service_mod.ENVIRONMENT,
                    8,
                ),
            ),
        )

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
            "last_trace_id": "",
            "last_duration_s": 0.0,
            "last_symbol_timings": [],
            "fetch_workers": 1,
            "slowest_stage": {},
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

    def _runtime_direct_topup_enabled(self) -> bool:
        service_mod = _service_mod()
        return self.config.get_bool_for_environment(
            "ibkr_runtime_direct_topup_enabled",
            service_mod.ENVIRONMENT,
            True,
        )

    def _runtime_direct_topup_intervals(self) -> list[str]:
        service_mod = _service_mod()
        raw_value = str(
            self.config.get_for_environment(
                "ibkr_runtime_direct_topup_intervals",
                service_mod.ENVIRONMENT,
                ",".join(service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS),
            )
            or ""
        )
        allowed = {normalize_interval(interval) for interval in service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS}
        intervals = []
        for item in raw_value.replace(";", ",").split(","):
            normalized = normalize_interval(item)
            if normalized in {"all", "*"}:
                return list(service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS)
            if normalized in allowed and normalized not in intervals:
                intervals.append(normalized)
        return intervals or list(service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS)

    def _runtime_direct_topup_close_delay_sec(self) -> int:
        service_mod = _service_mod()
        return max(
            1,
            self.config.get_int_for_environment(
                "ibkr_runtime_direct_topup_close_delay_sec",
                service_mod.ENVIRONMENT,
                service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_CLOSE_DELAY_SECONDS,
            ),
        )

    def _runtime_direct_topup_loop_interval_sec(self) -> float:
        service_mod = _service_mod()
        return max(
            1.0,
            self.config.get_float_for_environment(
                "ibkr_runtime_direct_topup_loop_interval_sec",
                service_mod.ENVIRONMENT,
                service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_LOOP_INTERVAL_SECONDS,
            ),
        )

    def _runtime_direct_topup_request_period(self, interval: str) -> str:
        service_mod = _service_mod()
        normalized = normalize_interval(interval)
        fallback = service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_PERIODS.get(normalized, "2d")
        return str(
            self.config.get_for_environment(
                f"ibkr_runtime_direct_topup_period_{normalized}",
                service_mod.ENVIRONMENT,
                fallback,
            )
            or fallback
        ).strip() or fallback

    def _initial_direct_topup_state(self) -> dict:
        service_mod = _service_mod()
        return {
            "enabled": True,
            "driver": "ibkr_history_direct_topup",
            "intervals": list(service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS),
            "close_delay_sec": service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_CLOSE_DELAY_SECONDS,
            "loop_interval_s": service_mod.DEFAULT_RUNTIME_DIRECT_TOPUP_LOOP_INTERVAL_SECONDS,
            "last_run": "",
            "last_error": "",
            "total_written_bars": 0,
            "last_completed_interval": "",
            "intervals_state": {},
        }

    def _copy_direct_topup_state(self, source: dict | None = None) -> dict:
        payload = source if source is not None else self._direct_topup_state
        copied = {}
        for key, value in payload.items():
            if isinstance(value, dict):
                copied[key] = {
                    nested_key: dict(nested_value) if isinstance(nested_value, dict) else list(nested_value) if isinstance(nested_value, list) else nested_value
                    for nested_key, nested_value in value.items()
                }
            elif isinstance(value, list):
                copied[key] = [dict(item) if isinstance(item, dict) else item for item in value]
            else:
                copied[key] = value
        return copied

    def _set_direct_topup_state(self, **updates) -> dict:
        with self._direct_topup_lock:
            next_state = self._copy_direct_topup_state()
            for key, value in updates.items():
                if isinstance(value, dict):
                    next_state[key] = {
                        nested_key: dict(nested_value) if isinstance(nested_value, dict) else list(nested_value) if isinstance(nested_value, list) else nested_value
                        for nested_key, nested_value in value.items()
                    }
                elif isinstance(value, list):
                    next_state[key] = [dict(item) if isinstance(item, dict) else item for item in value]
                else:
                    next_state[key] = value
            self._direct_topup_state = next_state
            return self._copy_direct_topup_state(next_state)

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
        intervals: list[str] | None = None,
        rollup_intervals: list[str] | None = None,
    ) -> dict:
        service_mod = _service_mod()
        try:
            payload = {"source": source, "environments": [service_mod.ENVIRONMENT]}
            if persist_signals is not None:
                payload["persist_signals"] = bool(persist_signals)
            if intervals is not None:
                payload["intervals"] = [
                    normalize_interval(interval)
                    for interval in intervals
                    if str(interval or "").strip()
                ]
            if rollup_intervals is not None:
                payload["rollup_intervals"] = [
                    normalize_interval(interval)
                    for interval in rollup_intervals
                    if str(interval or "").strip()
                ]
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

    def _official_5m_slowest_stage(self, timings: list[dict]) -> dict:
        slowest = {"stage": "", "duration_s": 0.0, "symbol": ""}
        for item in timings or []:
            symbol = str((item or {}).get("symbol") or "")
            for key in (
                "latest_query_s",
                "fetch_s",
                "write_s",
                "flush_s",
                "verify_s",
                "repair_fetch_s",
                "repair_write_s",
                "repair_flush_s",
                "repair_verify_s",
                "repair_total_s",
                "total_s",
            ):
                value = float((item or {}).get(key, 0) or 0)
                if value > float(slowest.get("duration_s", 0) or 0):
                    slowest = {
                        "stage": key.replace("_s", ""),
                        "duration_s": round(value, 3),
                        "symbol": symbol,
                    }
        return slowest

    def _official_5m_fetch_workers(self, symbol_count: int) -> int:
        if not self._official_5m_parallel_enabled():
            return 1
        return max(1, min(self._official_5m_max_concurrency(), max(1, int(symbol_count or 0))))

    def _official_5m_fetch_incremental_symbol(
        self,
        *,
        symbol: str,
        conid: int,
        exchange: str,
        due_bucket_ms: int,
        request_period: str,
        trace: dict | None = None,
    ) -> dict:
        started = time.perf_counter()
        timing = {"symbol": symbol}
        latest_started = time.perf_counter()
        latest_stored_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
        timing["latest_query_s"] = round(time.perf_counter() - latest_started, 3)
        fetched_rows = []
        fetch_error = ""
        fetch_started = 0.0
        if latest_stored_ms < due_bucket_ms:
            try:
                fetch_started = time.perf_counter()
                fetched_rows = self.data_backfill.fetch_history(
                    conid,
                    symbol,
                    interval="5m",
                    exchange=exchange,
                    repair=False,
                    request_period=request_period,
                    trace=trace,
                )
            except Exception as exc:
                fetch_error = str(exc)
                fetched_rows = []
            finally:
                timing["fetch_s"] = round(time.perf_counter() - fetch_started, 3) if fetch_started else 0.0
        else:
            timing["fetch_s"] = 0.0
        candidate_rows = self._official_5m_candidate_rows(
            fetched_rows,
            latest_stored_ms + 1,
            due_bucket_ms,
        )
        timing["total_s"] = round(time.perf_counter() - started, 3)
        return {
            "symbol": symbol,
            "conid": int(conid or 0),
            "exchange": exchange,
            "latest_stored_ms": int(latest_stored_ms or 0),
            "candidate_rows": candidate_rows,
            "fetched_rows_count": len(fetched_rows or []),
            "fetched_first_ms": min([int(row.get("bar_time_ms", 0) or 0) for row in fetched_rows] or [0]),
            "fetched_last_ms": max([int(row.get("bar_time_ms", 0) or 0) for row in fetched_rows] or [0]),
            "error": fetch_error,
            "timing": timing,
        }

    def _official_5m_fetch_repair_symbol(
        self,
        *,
        symbol: str,
        conid: int,
        exchange: str,
        required_window_start_ms: int,
        due_bucket_ms: int,
        request_period: str,
        trace: dict | None = None,
    ) -> dict:
        started = time.perf_counter()
        timing = {"symbol": symbol}
        repair_rows = []
        error = ""
        try:
            repair_rows = self.data_backfill.fetch_history(
                conid,
                symbol,
                interval="5m",
                exchange=exchange,
                repair=True,
                request_period=request_period,
                trace=trace,
            )
        except Exception as exc:
            error = str(exc)
            repair_rows = []
        timing["repair_fetch_s"] = round(time.perf_counter() - started, 3)
        timing["total_s"] = timing["repair_fetch_s"]
        return {
            "symbol": symbol,
            "conid": int(conid or 0),
            "exchange": exchange,
            "candidate_rows": self._official_5m_candidate_rows(
                repair_rows,
                required_window_start_ms,
                due_bucket_ms,
            ),
            "error": error,
            "timing": timing,
        }

    def _official_5m_run_parallel(self, jobs: list[dict], worker_fn, workers: int) -> list[dict]:
        if not jobs:
            return []
        if max(1, int(workers or 1)) <= 1 or len(jobs) <= 1:
            results = []
            for job in jobs:
                try:
                    results.append(worker_fn(**job))
                except Exception as exc:
                    results.append({
                        "symbol": str(job.get("symbol") or "").strip().upper(),
                        "conid": int(job.get("conid") or 0),
                        "exchange": str(job.get("exchange") or ""),
                        "candidate_rows": [],
                        "error": str(exc),
                        "timing": {
                            "symbol": str(job.get("symbol") or "").strip().upper(),
                            "total_s": 0.0,
                        },
                    })
            return results
        results = []
        with ThreadPoolExecutor(max_workers=max(1, int(workers or 1)), thread_name_prefix="official-5m") as executor:
            future_map = {executor.submit(worker_fn, **job): job for job in jobs}
            for future in as_completed(future_map):
                job = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({
                        "symbol": str(job.get("symbol") or "").strip().upper(),
                        "conid": int(job.get("conid") or 0),
                        "exchange": str(job.get("exchange") or ""),
                        "candidate_rows": [],
                        "error": str(exc),
                        "timing": {
                            "symbol": str(job.get("symbol") or "").strip().upper(),
                            "total_s": 0.0,
                        },
                    })
        return results

    def _run_official_5m_close_cycle(self, symbols_override: list[str] | None = None):
        service_mod = _service_mod()
        cycle_started_perf = time.perf_counter()
        trace_id = f"official5m_{uuid.uuid4().hex[:10]}"
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
                last_trace_id=trace_id,
                last_duration_s=round(max(0.0, time.perf_counter() - cycle_started_perf), 3),
                last_symbol_timings=[],
                fetch_workers=0,
                slowest_stage={},
            )
            return

        required_window_start_ms = self._official_5m_required_window_start_ms(
            last_completed_bucket_ms,
            due_bucket_ms,
        )
        written_symbol_set = set()
        next_pending_symbols = []
        pending_detail_by_symbol = {}
        written_bars = 0
        cycle_errors = []
        symbol_timings_by_symbol: dict[str, dict] = {}
        max_written_ms_by_symbol: dict[str, int] = {}
        sequence_status_by_symbol: dict[str, dict] = {}
        fetch_jobs = []

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
                symbol_timings_by_symbol[symbol] = {
                    "symbol": symbol,
                    "error": "missing_conid",
                    "total_s": 0.0,
                }
                continue

            exchange = str((symbol_meta.get(symbol) or {}).get("exchange") or "").upper()
            fetch_jobs.append(
                {
                    "symbol": symbol,
                    "conid": conid,
                    "exchange": exchange,
                    "due_bucket_ms": due_bucket_ms,
                    "request_period": request_period,
                }
            )

        history_trace = None
        if fetch_jobs and hasattr(self.data_backfill, "_new_trace"):
            history_trace = self.data_backfill._new_trace(
                "official_5m_close",
                [job["symbol"] for job in fetch_jobs],
                ["5m"],
            )
            for job in fetch_jobs:
                job["trace"] = history_trace

        fetch_workers = self._official_5m_fetch_workers(len(fetch_jobs)) if fetch_jobs else 0
        self._set_official_5m_state(
            enabled=self._official_5m_enabled(),
            close_delay_sec=self._official_5m_close_delay_sec(),
            request_period=request_period,
            last_run=self._now_iso(),
            last_due_bucket_ms=due_bucket_ms,
            last_due_bucket_us=format_us_time(due_bucket_ms),
            last_trace_id=trace_id,
            fetch_workers=fetch_workers,
            last_error="",
        )
        service_mod.logger.info(
            "Official 5m close cycle started: trace=%s due=%s symbols=%d fetch_jobs=%d workers=%d last_completed=%s",
            trace_id,
            format_us_time(due_bucket_ms),
            len(symbols),
            len(fetch_jobs),
            fetch_workers,
            format_us_time(last_completed_bucket_ms) if last_completed_bucket_ms > 0 else "--",
        )

        fetch_results = self._official_5m_run_parallel(
            fetch_jobs,
            self._official_5m_fetch_incremental_symbol,
            fetch_workers,
        )
        fetch_results_by_symbol = {
            str((result or {}).get("symbol") or "").strip().upper(): (result or {})
            for result in fetch_results
            if str((result or {}).get("symbol") or "").strip()
        }

        incremental_wrote_symbols = set()
        for symbol in [job["symbol"] for job in fetch_jobs]:
            result = fetch_results_by_symbol.get(symbol) or {}
            timing = dict(result.get("timing") or {})
            timing["symbol"] = symbol
            timing["fetched_rows"] = int(result.get("fetched_rows_count", 0) or 0)
            timing["candidate_rows"] = len(result.get("candidate_rows") or [])
            symbol_timings_by_symbol[symbol] = timing

            error_text = str(result.get("error") or "").strip()
            if error_text:
                cycle_errors.append(f"{symbol}:{error_text}")

            latest_stored_ms = int(result.get("latest_stored_ms", 0) or 0)
            max_written_ms_by_symbol[symbol] = latest_stored_ms
            candidate_rows = list(result.get("candidate_rows") or [])
            fetched_count = int(result.get("fetched_rows_count", 0) or 0)
            if fetched_count and not candidate_rows:
                fetched_first_ms = int(result.get("fetched_first_ms", 0) or 0)
                fetched_last_ms = int(result.get("fetched_last_ms", 0) or 0)
                service_mod.logger.warning(
                    "Official 5m close filtered all fetched rows for %s: latest_stored_ms=%d due_bucket_ms=%d fetched_first=%d(%s) fetched_last=%d(%s) fetched_count=%d",
                    symbol,
                    latest_stored_ms,
                    due_bucket_ms,
                    fetched_first_ms,
                    format_us_time(fetched_first_ms),
                    fetched_last_ms,
                    format_us_time(fetched_last_ms),
                    fetched_count,
                )

            write_started = time.perf_counter()
            incremental_wrote, incremental_bars, incremental_max_ms = self._write_official_5m_rows(
                candidate_rows,
                str(result.get("exchange") or ""),
                request_period,
            )
            timing["write_s"] = round(max(0.0, time.perf_counter() - write_started), 3)
            timing["written_bars"] = int(incremental_bars or 0)
            if incremental_wrote:
                written_symbol_set.add(symbol)
                incremental_wrote_symbols.add(symbol)
            written_bars += int(incremental_bars or 0)
            max_written_ms_by_symbol[symbol] = max(
                int(max_written_ms_by_symbol.get(symbol, 0) or 0),
                int(incremental_max_ms or 0),
            )

        if incremental_wrote_symbols:
            flush_started = time.perf_counter()
            if not self.data_writer.flush():
                cycle_errors.append("batch:flush:incremental")
            flush_s = round(max(0.0, time.perf_counter() - flush_started), 3)
            for symbol in incremental_wrote_symbols:
                symbol_timings_by_symbol.setdefault(symbol, {"symbol": symbol})["flush_s"] = flush_s

        repair_jobs = []
        for symbol in [job["symbol"] for job in fetch_jobs]:
            timing = symbol_timings_by_symbol.setdefault(symbol, {"symbol": symbol})
            sequence_status = {}
            if required_window_start_ms > 0 and required_window_start_ms <= due_bucket_ms:
                verify_started = time.perf_counter()
                sequence_status = self.data_backfill.get_required_sequence_snapshot(
                    symbol,
                    "5m",
                    start_ms=required_window_start_ms,
                    end_ms=due_bucket_ms,
                    example_limit=4,
                )
                timing["verify_s"] = round(max(0.0, time.perf_counter() - verify_started), 3)
                if str(sequence_status.get("query_error") or "").strip():
                    cycle_errors.append(f"{symbol}:sequence:{sequence_status['query_error']}")
            sequence_status_by_symbol[symbol] = dict(sequence_status or {})
            missing_count = int(sequence_status.get("missing_count", 0) or 0)
            timing["missing_count"] = missing_count
            if missing_count > 0:
                job = next((item for item in fetch_jobs if item.get("symbol") == symbol), None)
                if job:
                    repair_jobs.append(
                        {
                            "symbol": symbol,
                            "conid": int(job.get("conid") or 0),
                            "exchange": str(job.get("exchange") or ""),
                            "required_window_start_ms": required_window_start_ms,
                            "due_bucket_ms": due_bucket_ms,
                            "request_period": request_period,
                            "trace": history_trace,
                        }
                    )

        repair_wrote_symbols = set()
        repair_workers = self._official_5m_fetch_workers(len(repair_jobs)) if repair_jobs else 0
        repair_results = self._official_5m_run_parallel(
            repair_jobs,
            self._official_5m_fetch_repair_symbol,
            repair_workers,
        )
        for result in repair_results:
            symbol = str((result or {}).get("symbol") or "").strip().upper()
            if not symbol:
                continue
            timing = symbol_timings_by_symbol.setdefault(symbol, {"symbol": symbol})
            repair_timing = dict((result or {}).get("timing") or {})
            for key, value in repair_timing.items():
                if key == "total_s":
                    timing["repair_total_s"] = value
                elif key != "symbol":
                    timing[key] = value
            error_text = str((result or {}).get("error") or "").strip()
            if error_text:
                cycle_errors.append(f"{symbol}:repair:{error_text}")
            repair_rows = list((result or {}).get("candidate_rows") or [])
            timing["repair_candidate_rows"] = len(repair_rows)
            write_started = time.perf_counter()
            repair_wrote, repair_bars, repair_max_ms = self._write_official_5m_rows(
                repair_rows,
                str((result or {}).get("exchange") or ""),
                request_period,
            )
            timing["repair_write_s"] = round(max(0.0, time.perf_counter() - write_started), 3)
            timing["repair_written_bars"] = int(repair_bars or 0)
            if repair_wrote:
                written_symbol_set.add(symbol)
                repair_wrote_symbols.add(symbol)
            written_bars += int(repair_bars or 0)
            max_written_ms_by_symbol[symbol] = max(
                int(max_written_ms_by_symbol.get(symbol, 0) or 0),
                int(repair_max_ms or 0),
            )

        if repair_wrote_symbols:
            flush_started = time.perf_counter()
            if not self.data_writer.flush():
                cycle_errors.append("batch:flush:repair")
            repair_flush_s = round(max(0.0, time.perf_counter() - flush_started), 3)
            for symbol in repair_wrote_symbols:
                symbol_timings_by_symbol.setdefault(symbol, {"symbol": symbol})["repair_flush_s"] = repair_flush_s

        for repair_job in repair_jobs:
            symbol = str(repair_job.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            timing = symbol_timings_by_symbol.setdefault(symbol, {"symbol": symbol})
            sequence_status = sequence_status_by_symbol.get(symbol) or {}
            if required_window_start_ms > 0 and required_window_start_ms <= due_bucket_ms:
                verify_started = time.perf_counter()
                sequence_status = self.data_backfill.get_required_sequence_snapshot(
                    symbol,
                    "5m",
                    start_ms=required_window_start_ms,
                    end_ms=due_bucket_ms,
                    example_limit=4,
                )
                timing["repair_verify_s"] = round(max(0.0, time.perf_counter() - verify_started), 3)
                if str(sequence_status.get("query_error") or "").strip():
                    cycle_errors.append(f"{symbol}:sequence:{sequence_status['query_error']}")
            sequence_status_by_symbol[symbol] = dict(sequence_status or {})
            missing_count = int(sequence_status.get("missing_count", 0) or 0)
            timing["missing_count"] = missing_count
            if missing_count > 0:
                missing_bar_times = list(sequence_status.get("missing_bar_times") or [])
                missing_us_times = list(sequence_status.get("missing_us_times") or [])
                max_written_ms = int(max_written_ms_by_symbol.get(symbol, 0) or 0)
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

        for symbol in [job["symbol"] for job in fetch_jobs]:
            sequence_status = sequence_status_by_symbol.get(symbol) or {}
            missing_bar_times = list(sequence_status.get("missing_bar_times") or [])
            missing_us_times = list(sequence_status.get("missing_us_times") or [])
            missing_count = int(sequence_status.get("missing_count", 0) or 0)
            has_sequence_gap = missing_count > 0
            max_written_ms = int(max_written_ms_by_symbol.get(symbol, 0) or 0)
            bucket_unfilled = max_written_ms < due_bucket_ms
            if has_sequence_gap or bucket_unfilled:
                if symbol not in next_pending_symbols:
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

        written_symbols = [symbol for symbol in symbols if symbol in written_symbol_set]
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

        symbol_timings = []
        for symbol in symbols:
            timing = dict(symbol_timings_by_symbol.get(symbol) or {"symbol": symbol})
            timing["symbol"] = symbol
            timing["total_s"] = round(max(0.0, float(timing.get("total_s", 0) or 0)), 3)
            symbol_timings.append(timing)
        slowest_stage = self._official_5m_slowest_stage(symbol_timings)
        duration_s = round(max(0.0, time.perf_counter() - cycle_started_perf), 3)
        if history_trace and hasattr(self.data_backfill, "_finish_trace"):
            self.data_backfill._finish_trace(
                history_trace,
                total_written=written_bars,
                error="; ".join(cycle_errors),
            )

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
            last_trace_id=trace_id,
            last_duration_s=duration_s,
            last_symbol_timings=symbol_timings[:80],
            fetch_workers=fetch_workers,
            slowest_stage=slowest_stage,
        )
        service_mod.logger.info(
            "Official 5m close cycle finished: trace=%s due=%s total_s=%.3f written=%d symbols=%d pending=%d slowest=%s/%s %.3fs errors=%s",
            trace_id,
            format_us_time(due_bucket_ms),
            duration_s,
            written_bars,
            len(written_symbols),
            len(blocking_pending_symbols),
            slowest_stage.get("stage") or "--",
            slowest_stage.get("symbol") or "--",
            float(slowest_stage.get("duration_s", 0) or 0),
            "; ".join(cycle_errors) or "--",
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

    def _runtime_direct_topup_latest_due_ms(self, interval: str) -> int:
        return latest_safe_closed_bucket_ms(
            normalize_interval(interval),
            delay_seconds=self._runtime_direct_topup_close_delay_sec(),
            now_ms=int(time.time() * 1000),
        )

    def _runtime_direct_topup_5m_is_current(self) -> bool:
        official_5m = self._copy_official_5m_state()
        due_bucket_ms = self._latest_safe_closed_5m_ms()
        completed_bucket_ms = int(official_5m.get("last_completed_bucket_ms", 0) or 0)
        if due_bucket_ms > 0 and completed_bucket_ms < due_bucket_ms:
            return False
        if int(official_5m.get("pending_symbols_total", 0) or 0) > 0:
            return False
        return True

    def _runtime_direct_topup_symbols(self) -> tuple[list[str], dict, dict]:
        snapshot = self._warmup_snapshot_from_subscriptions()
        snapshot_symbols = list(snapshot.get("symbols") or [])
        if "trade_symbols" in snapshot:
            trade_set = set(self._normalize_symbol_list(snapshot.get("trade_symbols") or []))
            symbols = [symbol for symbol in snapshot_symbols if symbol in trade_set]
        else:
            symbols = snapshot_symbols
        normalized_symbols = self._normalize_symbol_list(symbols)
        return (
            normalized_symbols,
            dict(snapshot.get("conid_map") or {}),
            dict(snapshot.get("symbol_meta") or {}),
        )

    def _run_runtime_direct_topup_interval(
        self,
        interval: str,
        symbols: list[str],
        conid_map: dict,
        symbol_meta: dict,
        state: dict,
    ) -> dict:
        service_mod = _service_mod()
        normalized_interval = normalize_interval(interval)
        due_bucket_ms = self._runtime_direct_topup_latest_due_ms(normalized_interval)
        interval_states = dict(state.get("intervals_state") or {})
        interval_state = dict(interval_states.get(normalized_interval) or {})
        last_completed_bucket_ms = int(interval_state.get("last_completed_bucket_ms", 0) or 0)
        request_period = self._runtime_direct_topup_request_period(normalized_interval)

        base_update = {
            **interval_state,
            "enabled": True,
            "request_period": request_period,
            "last_due_bucket_ms": due_bucket_ms,
            "last_due_bucket_us": format_us_time(due_bucket_ms) if due_bucket_ms > 0 else "",
        }
        if due_bucket_ms <= 0:
            base_update["status"] = "waiting"
            base_update["reason"] = "no_safe_bucket"
            return base_update
        if due_bucket_ms <= last_completed_bucket_ms:
            base_update["status"] = "idle"
            base_update["reason"] = "already_completed"
            return base_update
        if not symbols:
            base_update.update(
                {
                    "status": "skipped",
                    "reason": "no_trade_symbols",
                    "last_run": self._now_iso(),
                    "last_completed_bucket_ms": due_bucket_ms,
                    "last_completed_bucket_us": format_us_time(due_bucket_ms),
                    "last_written_bars": 0,
                    "written_symbols": [],
                    "written_symbols_total": 0,
                    "pending_symbols": [],
                    "pending_symbols_total": 0,
                    "last_error": "",
                }
            )
            return base_update

        runnable_conids = {
            symbol: int(conid_map.get(symbol) or 0)
            for symbol in symbols
            if int(conid_map.get(symbol) or 0) > 0
        }
        missing_conid_symbols = [symbol for symbol in symbols if symbol not in runnable_conids]
        if not runnable_conids:
            base_update.update(
                {
                    "status": "pending",
                    "reason": "missing_conids",
                    "last_run": self._now_iso(),
                    "last_written_bars": 0,
                    "written_symbols": [],
                    "written_symbols_total": 0,
                    "pending_symbols": missing_conid_symbols,
                    "pending_symbols_total": len(missing_conid_symbols),
                    "last_error": "missing_conids",
                }
            )
            return base_update

        started_at = time.time()
        base_update.update(
            {
                "status": "running",
                "reason": "",
                "last_run": self._now_iso(),
                "symbol_count": len(symbols),
                "missing_conid_symbols": missing_conid_symbols,
                "last_error": "",
            }
        )
        interval_states[normalized_interval] = base_update
        self._set_direct_topup_state(
            enabled=self._runtime_direct_topup_enabled(),
            intervals=self._runtime_direct_topup_intervals(),
            close_delay_sec=self._runtime_direct_topup_close_delay_sec(),
            loop_interval_s=self._runtime_direct_topup_loop_interval_sec(),
            last_run=self._now_iso(),
            intervals_state=interval_states,
        )

        written_bars = 0
        written_symbols = []
        last_error = ""
        try:
            period_overrides = {
                symbol: {normalized_interval: request_period}
                for symbol in runnable_conids
            }
            results = self.data_backfill.backfill_all(
                runnable_conids,
                symbol_meta=symbol_meta,
                intervals=[normalized_interval],
                repair_symbols=[],
                period_overrides=period_overrides,
            )
            self.data_writer.flush()
            for symbol, payload in (results or {}).items():
                count = int((payload or {}).get(normalized_interval, 0) or 0)
                if count > 0:
                    written_symbols.append(symbol)
                    written_bars += count
            compute_result = {}
            if runnable_conids:
                compute_result = self._trigger_realtime_compute(
                    source="direct_history_topup",
                    symbols=list(runnable_conids.keys()),
                    persist_signals=False,
                    intervals=[normalized_interval],
                    rollup_intervals=[],
                )
                if compute_result.get("ok") is False:
                    last_error = str(compute_result.get("error") or "compute_failed")
        except Exception as exc:
            service_mod.logger.warning(
                "Runtime direct %s top-up failed: %s",
                normalized_interval,
                exc,
            )
            last_error = str(exc)

        verified_symbols = []
        pending_freshness_symbols = []
        freshness_by_symbol = {}
        if not last_error:
            planner = getattr(self, "bar_freshness_planner", None)
            if planner is None:
                verified_symbols = list(runnable_conids.keys())
                pending_freshness_symbols = []
                freshness_by_symbol = {symbol: {"status": "not_checked", "reason": "planner_unavailable"} for symbol in runnable_conids.keys()}
            else:
                for symbol in runnable_conids.keys():
                    try:
                        freshness = planner.plan_symbol(
                            symbol,
                            [normalized_interval],
                            environment=service_mod.ENVIRONMENT,
                            required_bars=0,
                        )
                        interval_payload = (freshness.get("intervals") or {}).get(normalized_interval) or {}
                        latest_ms = int(interval_payload.get("latest_stored_ms", 0) or 0)
                        freshness_by_symbol[symbol] = interval_payload
                        if latest_ms >= due_bucket_ms:
                            verified_symbols.append(symbol)
                        else:
                            pending_freshness_symbols.append(symbol)
                    except Exception as exc:
                        freshness_by_symbol[symbol] = {"status": "verify_failed", "error": str(exc)}
                        pending_freshness_symbols.append(symbol)
            if pending_freshness_symbols:
                last_error = "freshness_pending_after_topup"

        completed = not last_error
        base_update.update(
            {
                "status": "completed" if completed else ("pending" if pending_freshness_symbols else "failed"),
                "reason": "" if completed else str(last_error or "topup_failed"),
                "last_completed_bucket_ms": due_bucket_ms if completed else last_completed_bucket_ms,
                "last_completed_bucket_us": format_us_time(due_bucket_ms) if completed else str(interval_state.get("last_completed_bucket_us") or ""),
                "last_written_bars": written_bars,
                "written_symbols": sorted(written_symbols),
                "written_symbols_total": len(written_symbols),
                "verified_symbols": sorted(verified_symbols),
                "verified_symbols_total": len(verified_symbols),
                "pending_symbols": [] if completed else sorted(pending_freshness_symbols or runnable_conids.keys()),
                "pending_symbols_total": 0 if completed else len(pending_freshness_symbols or runnable_conids),
                "freshness_by_symbol": freshness_by_symbol,
                "duration_s": round(max(0.0, time.time() - started_at), 3),
                "last_error": last_error,
            }
        )
        return base_update

    def _run_runtime_direct_topup_cycle(self, intervals_override: list[str] | None = None):
        service_mod = _service_mod()
        enabled = self._runtime_direct_topup_enabled()
        intervals = [
            normalize_interval(interval)
            for interval in (intervals_override or self._runtime_direct_topup_intervals())
            if str(interval or "").strip()
        ]
        state = self._copy_direct_topup_state()
        state.update(
            {
                "enabled": enabled,
                "intervals": intervals,
                "close_delay_sec": self._runtime_direct_topup_close_delay_sec(),
                "loop_interval_s": self._runtime_direct_topup_loop_interval_sec(),
            }
        )
        if not enabled:
            self._set_direct_topup_state(**state)
            return
        if not intervals:
            state["last_error"] = "no_direct_topup_intervals"
            self._set_direct_topup_state(**state)
            return
        if not self._runtime_direct_topup_5m_is_current():
            state["last_error"] = "canonical_5m_not_current"
            self._set_direct_topup_state(**state)
            return

        symbols, conid_map, symbol_meta = self._runtime_direct_topup_symbols()
        interval_states = dict(state.get("intervals_state") or {})
        ran_interval = ""
        total_written = int(state.get("total_written_bars", 0) or 0)
        last_error = ""

        # Keep this low priority: run at most one due higher interval per loop tick.
        for interval in intervals:
            normalized_interval = normalize_interval(interval)
            interval_state = self._run_runtime_direct_topup_interval(
                normalized_interval,
                symbols,
                conid_map,
                symbol_meta,
                state,
            )
            interval_states[normalized_interval] = interval_state
            if str(interval_state.get("status") or "") in {"completed", "failed", "skipped", "pending"}:
                previous_state = (state.get("intervals_state") or {}).get(normalized_interval) or {}
                if int(interval_state.get("last_due_bucket_ms", 0) or 0) > int(previous_state.get("last_completed_bucket_ms", 0) or 0):
                    ran_interval = normalized_interval
                    total_written += int(interval_state.get("last_written_bars", 0) or 0)
                    last_error = str(interval_state.get("last_error") or "")
                    break

        self._set_direct_topup_state(
            enabled=enabled,
            intervals=intervals,
            close_delay_sec=self._runtime_direct_topup_close_delay_sec(),
            loop_interval_s=self._runtime_direct_topup_loop_interval_sec(),
            last_run=self._now_iso(),
            last_error=last_error,
            total_written_bars=total_written,
            last_completed_interval=ran_interval,
            intervals_state=interval_states,
        )

    def _runtime_direct_topup_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Runtime direct higher-timeframe top-up loop started")
        while self._running:
            try:
                self._run_runtime_direct_topup_cycle()
            except Exception as exc:
                service_mod.logger.error("Runtime direct top-up loop error: %s", exc)
                self._set_direct_topup_state(
                    enabled=self._runtime_direct_topup_enabled(),
                    intervals=self._runtime_direct_topup_intervals(),
                    close_delay_sec=self._runtime_direct_topup_close_delay_sec(),
                    loop_interval_s=self._runtime_direct_topup_loop_interval_sec(),
                    last_run=self._now_iso(),
                    last_error=str(exc),
                )
            time.sleep(self._runtime_direct_topup_loop_interval_sec())

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
