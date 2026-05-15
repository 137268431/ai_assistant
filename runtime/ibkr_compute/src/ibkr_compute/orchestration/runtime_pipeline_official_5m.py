from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import time
import uuid

from ibkr_compute.market.timeframe_utils import ET, format_us_time, interval_to_ms, latest_safe_closed_bucket_ms

from .runtime_pipeline_support import _service_mod


class RuntimePipelineOfficial5mMixin:
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

    def _official_5m_error_result(self, job: dict, exc: Exception) -> dict:
            return {
                "symbol": str(job.get("symbol") or "").strip().upper(),
                "conid": int(job.get("conid") or 0),
                "exchange": str(job.get("exchange") or ""),
                "candidate_rows": [],
                "error": str(exc),
                "timing": {
                    "symbol": str(job.get("symbol") or "").strip().upper(),
                    "total_s": 0.0,
                },
            }

    def _official_5m_stream_parallel(self, jobs: list[dict], worker_fn, workers: int):
            if not jobs:
                return
            if max(1, int(workers or 1)) <= 1 or len(jobs) <= 1:
                for job in jobs:
                    try:
                        yield worker_fn(**job)
                    except Exception as exc:
                        yield self._official_5m_error_result(job, exc)
                return
            with ThreadPoolExecutor(max_workers=max(1, int(workers or 1)), thread_name_prefix="official-5m") as executor:
                future_map = {executor.submit(worker_fn, **job): job for job in jobs}
                for future in as_completed(future_map):
                    job = future_map[future]
                    try:
                        yield future.result()
                    except Exception as exc:
                        yield self._official_5m_error_result(job, exc)

    def _official_5m_run_parallel(self, jobs: list[dict], worker_fn, workers: int) -> list[dict]:
            return list(self._official_5m_stream_parallel(jobs, worker_fn, workers))

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
            trade_symbols_declared = "trade_symbols" in snapshot
            trade_set = (
                set(self._normalize_symbol_list(snapshot.get("trade_symbols") or []))
                if trade_symbols_declared
                else set(self._normalize_symbol_list(snapshot_symbols))
            )
            monitor_set = set(self._normalize_symbol_list(snapshot.get("monitor_symbols") or []))
            if override_set:
                symbols = [symbol for symbol in snapshot_symbols if symbol in override_set]
            elif trade_symbols_declared:
                refresh_set = trade_set | monitor_set
                symbols = [symbol for symbol in snapshot_symbols if symbol in refresh_set]
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
            queued_compute_symbols: set[str] = set()

            def queue_due_compute_symbols(symbol_candidates, *, bar_count: int = 0) -> None:
                candidates = [
                    symbol
                    for symbol in self._normalize_symbol_list(symbol_candidates)
                    if symbol in trade_set and symbol not in queued_compute_symbols
                ]
                if not candidates:
                    return
                due_symbols = [
                    symbol
                    for symbol in self._official_5m_due_compute_symbols(
                        service_mod.ENVIRONMENT,
                        candidates,
                        due_bucket_ms,
                    )
                    if symbol not in queued_compute_symbols
                ]
                if not due_symbols:
                    return
                self._last_bar_close_at = time.time()
                self._queue_compute_event(
                    "canonical_close",
                    bar_count=max(0, int(bar_count or 0)),
                    symbols=due_symbols,
                )
                queued_compute_symbols.update(due_symbols)

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

            repair_jobs = []
            fetch_job_by_symbol = {
                str(job.get("symbol") or "").strip().upper(): job
                for job in fetch_jobs
                if str(job.get("symbol") or "").strip()
            }

            def verify_symbol_ready(symbol: str, *, repair_phase: bool = False) -> None:
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
                    timing["repair_verify_s" if repair_phase else "verify_s"] = round(
                        max(0.0, time.perf_counter() - verify_started),
                        3,
                    )
                    if str(sequence_status.get("query_error") or "").strip():
                        cycle_errors.append(f"{symbol}:sequence:{sequence_status['query_error']}")
                sequence_status_by_symbol[symbol] = dict(sequence_status or {})
                missing_count = int(sequence_status.get("missing_count", 0) or 0)
                timing["missing_count"] = missing_count
                if missing_count > 0:
                    if repair_phase:
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
                    else:
                        job = fetch_job_by_symbol.get(symbol)
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
                elif int(max_written_ms_by_symbol.get(symbol, 0) or 0) >= due_bucket_ms:
                    queue_due_compute_symbols(
                        [symbol],
                        bar_count=written_bars,
                    )

            def process_stream_result(result: dict, *, repair_phase: bool = False) -> None:
                nonlocal written_bars
                symbol = str((result or {}).get("symbol") or "").strip().upper()
                if not symbol:
                    return
                timing = symbol_timings_by_symbol.setdefault(symbol, {"symbol": symbol})
                result_timing = dict((result or {}).get("timing") or {})
                for key, value in result_timing.items():
                    if key == "symbol":
                        continue
                    if repair_phase and key == "total_s":
                        timing["repair_total_s"] = value
                    elif repair_phase:
                        timing[key] = value
                    else:
                        timing[key] = value
                timing["symbol"] = symbol
                timing["fetched_rows"] = int((result or {}).get("fetched_rows_count", 0) or 0)

                error_text = str((result or {}).get("error") or "").strip()
                if error_text:
                    cycle_errors.append(f"{symbol}:repair:{error_text}" if repair_phase else f"{symbol}:{error_text}")

                rows = list((result or {}).get("candidate_rows") or [])
                if repair_phase:
                    timing["repair_candidate_rows"] = len(rows)
                    write_key = "repair_write_s"
                    written_key = "repair_written_bars"
                    flush_key = "repair_flush_s"
                    flush_error = f"{symbol}:flush:repair"
                else:
                    latest_stored_ms = int((result or {}).get("latest_stored_ms", 0) or 0)
                    max_written_ms_by_symbol[symbol] = latest_stored_ms
                    timing["candidate_rows"] = len(rows)
                    fetched_count = int((result or {}).get("fetched_rows_count", 0) or 0)
                    if fetched_count and not rows:
                        fetched_first_ms = int((result or {}).get("fetched_first_ms", 0) or 0)
                        fetched_last_ms = int((result or {}).get("fetched_last_ms", 0) or 0)
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
                    write_key = "write_s"
                    written_key = "written_bars"
                    flush_key = "flush_s"
                    flush_error = f"{symbol}:flush:incremental"

                write_started = time.perf_counter()
                wrote, written_count, max_written_ms = self._write_official_5m_rows(
                    rows,
                    str((result or {}).get("exchange") or ""),
                    request_period,
                )
                timing[write_key] = round(max(0.0, time.perf_counter() - write_started), 3)
                timing[written_key] = int(written_count or 0)
                if wrote:
                    written_symbol_set.add(symbol)
                    written_bars += int(written_count or 0)
                    max_written_ms_by_symbol[symbol] = max(
                        int(max_written_ms_by_symbol.get(symbol, 0) or 0),
                        int(max_written_ms or 0),
                    )
                    flush_started = time.perf_counter()
                    if not self.data_writer.flush():
                        cycle_errors.append(flush_error)
                    timing[flush_key] = round(max(0.0, time.perf_counter() - flush_started), 3)

                verify_symbol_ready(symbol, repair_phase=repair_phase)

            for result in self._official_5m_stream_parallel(
                fetch_jobs,
                self._official_5m_fetch_incremental_symbol,
                fetch_workers,
            ):
                process_stream_result(result, repair_phase=False)

            repair_workers = self._official_5m_fetch_workers(len(repair_jobs)) if repair_jobs else 0
            for result in self._official_5m_stream_parallel(
                repair_jobs,
                self._official_5m_fetch_repair_symbol,
                repair_workers,
            ):
                process_stream_result(result, repair_phase=True)

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
                if symbol in trade_set
                and symbol not in next_pending_symbols
                and symbol not in queued_compute_symbols
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
            request_watchlist_topup_now = bool(written_symbols and not blocking_pending_symbols)

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
            if request_watchlist_topup_now:
                try:
                    self._request_watchlist_idle_topup_now(ttl_s=20.0)
                except Exception:
                    pass
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
