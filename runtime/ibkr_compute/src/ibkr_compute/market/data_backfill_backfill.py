"""Backfill orchestration methods."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import time
from typing import Dict, List, Optional, Sequence

from ibkr_compute.core.large_operation_alert import emit_large_operation_alert
from ibkr_compute.observability.prometheus import record_history_event

from .data_backfill_support import PERIOD_MAP
from .timeframe_utils import normalize_interval

logger = logging.getLogger("ibkr_compute.market.data_backfill")


class BackfillResult(dict):
    """Dict-compatible result with optional out-of-band metadata."""

    def __init__(self, *args, meta: Optional[Dict] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.meta = dict(meta or {})

    def get(self, key, default=None):
        if key in {"_meta", "meta"}:
            return self.meta or default
        return super().get(key, default)


class DataBackfillBackfillMixin:
    def _legacy_bar_pipeline_enabled(self) -> bool:
        config = self.config or getattr(self.data_writer, "config", None)
        if not config or not hasattr(config, "get_bool_for_environment"):
            return False
        try:
            return bool(
                config.get_bool_for_environment(
                    "ibkr_legacy_bar_pipeline_enabled",
                    self.environment,
                    False,
                )
            )
        except Exception:
            return False

    def _legacy_bar_pipeline_status(self) -> dict:
        enabled = self._legacy_bar_pipeline_enabled()
        return {
            "enabled": enabled,
            "status": "enabled" if enabled else "disabled_tv_primary",
            "reason": "" if enabled else "legacy_bar_pipeline_disabled",
        }

    def _record_skipped_backfill_trace(
        self,
        *,
        source: str,
        symbols: Sequence[str],
        intervals: Sequence[str],
        reason: str,
        context: Optional[Dict] = None,
    ) -> Dict:
        summary = {
            "trace_id": "",
            "source": str(source or "history_backfill"),
            "environment": self.environment,
            "symbols_total": len(symbols or []),
            "symbols": list(symbols or []),
            "intervals": list(intervals or []),
            "duration_s": 0.0,
            "request_count": 0,
            "retry_count": 0,
            "throttle_count": 0,
            "written": 0,
            "ok": True,
            "skipped": True,
            "reason": reason,
            "status": "disabled_tv_primary",
            "trace_context": dict(context or {}) if isinstance(context, dict) else {},
            "slowest_stage": {"stage": "", "duration_s": 0.0, "symbol": "", "interval": ""},
            "error": "",
            "request_samples": [],
            "symbol_timings": [],
            "symbol_outcomes": [
                {
                    "symbol": str(symbol or "").strip().upper(),
                    "intervals": list(intervals or []),
                    "ok": True,
                    "skipped": True,
                    "reason": reason,
                    "request_count": 0,
                    "rows": 0,
                    "errors": [],
                    "last_error": "",
                    "hmds_no_data": False,
                    "terminal_no_data": False,
                    "max_attempt": 0,
                }
                for symbol in (symbols or [])
            ],
            "hmds_no_data_symbols": [],
            "hmds_no_data_count": 0,
            "request_error_symbols": [],
            "request_error_count": 0,
            "non_hmds_error_symbols": [],
            "non_hmds_error_count": 0,
            "finished_at_ms": int(time.time() * 1000),
        }
        with self._trace_lock:
            self._last_trace = dict(summary)
            self._recent_traces.append(dict(summary))
        logger.info(
            "History backfill skipped: source=%s symbols=%d intervals=%s reason=%s",
            summary["source"],
            summary["symbols_total"],
            ",".join(summary["intervals"]) or "--",
            reason,
        )
        return summary

    def _write_bars(self, bars: List[Dict]) -> int:
        return self._write_bars_with_trace(bars)

    def _write_bars_with_trace(
        self,
        bars: List[Dict],
        *,
        trace: Optional[Dict] = None,
        symbol: str = "",
        interval: str = "",
    ) -> int:
        started = time.perf_counter()
        written = 0
        for bar_data in bars:
            if self.data_writer and self.data_writer.write_bar(bar_data):
                written += 1
        with self._count_lock:
            self._backfill_count += written
        self._record_trace_write(
            trace,
            symbol=symbol or (str((bars[0] or {}).get("symbol") or "") if bars else ""),
            interval=interval or (str((bars[0] or {}).get("interval") or "") if bars else ""),
            rows=len(bars or []),
            written=written,
            write_s=time.perf_counter() - started,
        )
        record_history_event(
            environment=getattr(self, "environment", ""),
            source=str((trace or {}).get("source") or "data_backfill"),
            interval=interval or (str((bars[0] or {}).get("interval") or "") if bars else ""),
            operation="write",
            result="ok",
            duration_s=time.perf_counter() - started,
            rows=written,
        )
        return written

    def backfill_symbol(
        self,
        conid: int,
        symbol: str,
        interval: str = "5m",
        exchange: str = "",
        repair: bool = False,
        request_period: Optional[str] = None,
        trace: Optional[Dict] = None,
    ) -> int:
        bars = self.fetch_history(
            conid,
            symbol,
            interval=interval,
            exchange=exchange,
            repair=repair,
            request_period=request_period,
            trace=trace,
        )
        written = self._write_bars_with_trace(bars, trace=trace, symbol=symbol, interval=interval)
        logger.info(
            "Backfill %s/%s (%s): %d/%d bars written",
            symbol,
            normalize_interval(interval),
            "repair" if repair else "incremental",
            written,
            len(bars),
        )
        return written

    def backfill_symbol_all_intervals(
        self,
        conid: int,
        symbol: str,
        exchange: str = "",
        intervals: Optional[List[str]] = None,
        repair: bool = False,
        period_overrides: Optional[Dict[str, str]] = None,
        trace: Optional[Dict] = None,
    ) -> Dict[str, int]:
        results = {}
        for interval in self._resolve_intervals(intervals):
            normalized = normalize_interval(interval)
            written = self.backfill_symbol(
                conid,
                symbol,
                interval=normalized,
                exchange=exchange,
                repair=repair,
                request_period=str((period_overrides or {}).get(normalized) or "").strip() or None,
                trace=trace,
            )
            results[normalized] = written
            interval_delay = self._interval_delay()
            if interval_delay > 0:
                time.sleep(interval_delay)
        return results

    def _fetch_symbol_all_intervals(
        self,
        conid: int,
        symbol: str,
        exchange: str,
        intervals: Sequence[str],
        repair: bool,
        period_overrides: Optional[Dict[str, str]] = None,
        trace: Optional[Dict] = None,
    ) -> Dict[str, List[Dict]]:
        fetched = {}
        started = time.perf_counter()
        for interval in self._resolve_intervals(intervals):
            normalized = normalize_interval(interval)
            fetched[normalized] = self.fetch_history(
                conid,
                symbol,
                interval=normalized,
                exchange=exchange,
                repair=repair,
                request_period=str((period_overrides or {}).get(normalized) or "").strip() or None,
                trace=trace,
            )
            interval_delay = self._interval_delay()
            if interval_delay > 0:
                time.sleep(interval_delay)
        self._record_trace_symbol(
            trace,
            symbol=symbol,
            fetch_s=time.perf_counter() - started,
            intervals=list(fetched.keys()),
            rows_by_interval={interval: len(rows or []) for interval, rows in fetched.items()},
        )
        return fetched

    def backfill_all(
        self,
        conid_map: Dict[str, int],
        symbol_meta: Optional[Dict[str, Dict[str, str]]] = None,
        intervals: Optional[List[str]] = None,
        repair_symbols: Optional[Sequence[str]] = None,
        period_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        trace_source: str = "backfill_all",
        trace_context: Optional[Dict] = None,
    ) -> Dict[str, Dict[str, int]]:
        backfill_started = time.perf_counter()
        results = {}
        metadata = symbol_meta or {}
        interval_list = self._resolve_intervals(intervals)
        repair_set = {str(symbol or "").upper() for symbol in (repair_symbols or []) if str(symbol or "").strip()}
        for symbol, conid in conid_map.items():
            exchange = str((metadata.get(symbol) or {}).get("exchange") or "")
            results[symbol] = {interval: 0 for interval in interval_list}

        if not conid_map:
            record_history_event(
                environment=getattr(self, "environment", ""),
                source=trace_source,
                operation="backfill_all",
                result="skipped",
                duration_s=time.perf_counter() - backfill_started,
            )
            return results

        if not self._legacy_bar_pipeline_enabled():
            reason = "legacy_bar_pipeline_disabled"
            symbols = sorted(str(symbol or "").strip().upper() for symbol in conid_map.keys())
            trace_context_payload = dict(trace_context or {}) if isinstance(trace_context, dict) else {}
            summary = self._record_skipped_backfill_trace(
                source=trace_source,
                symbols=symbols,
                intervals=interval_list,
                reason=reason,
                context=trace_context_payload,
            )
            record_history_event(
                environment=getattr(self, "environment", ""),
                source=trace_source,
                operation="backfill_all",
                result="skipped",
                duration_s=time.perf_counter() - backfill_started,
                error_class=reason,
            )
            return BackfillResult(
                results,
                meta={
                    "ok": True,
                    "skipped": True,
                    "reason": reason,
                    "status": "disabled_tv_primary",
                    "trace": summary,
                    "symbols_total": len(symbols),
                    "intervals": interval_list,
                    "per_symbol": {
                        symbol: {"ok": True, "skipped": True, "reason": reason}
                        for symbol in symbols
                    },
                },
            )

        worker_count = min(self._max_concurrency(), len(conid_map))
        trace_context_payload = dict(trace_context or {}) if isinstance(trace_context, dict) else {}
        trace = self._new_trace(trace_source, list(conid_map.keys()), interval_list, context=trace_context_payload)
        operation_id = str((trace or {}).get("trace_id") or "")
        periods_by_symbol = {
            symbol: {
                interval: str(((period_overrides or {}).get(symbol) or {}).get(interval) or PERIOD_MAP.get(interval, ("", ""))[0])
                for interval in interval_list
            }
            for symbol in conid_map.keys()
        }
        if operation_id:
            emit_large_operation_alert(
                self.pb_client,
                {
                    **trace_context_payload,
                    "operation_id": operation_id,
                    "operation_type": "history_backfill",
                    "job_id": trace_source,
                    "source": trace_source,
                    "trigger_source": trace_source,
                    "symbols": sorted(conid_map.keys()),
                    "symbols_total": len(conid_map),
                    "intervals": interval_list,
                    "task_count": len(conid_map) * len(interval_list),
                    "periods": periods_by_symbol,
                    "max_concurrency": worker_count,
                    "request_spacing_s": self._request_spacing(),
                    "data_environment": self.environment,
                },
                config=self.config,
                stage="start",
                environment=self.environment,
            )
        logger.info(
            "Starting history backfill: symbols=%d, tasks=%d, intervals=%s, workers=%d",
            len(conid_map),
            len(conid_map) * len(interval_list),
            ",".join(interval_list),
            worker_count,
        )

        total_written = 0
        trace_error = ""
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ibkr-backfill") as executor:
            future_map = {
                executor.submit(
                    self._fetch_symbol_all_intervals,
                    conid,
                    symbol,
                    str((metadata.get(symbol) or {}).get("exchange") or ""),
                    interval_list,
                    symbol in repair_set,
                    dict((period_overrides or {}).get(symbol) or {}),
                    trace,
                ): symbol
                for symbol, conid in conid_map.items()
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    fetched = future.result()
                    for interval, bars in fetched.items():
                        written = self._write_bars_with_trace(
                            bars,
                            trace=trace,
                            symbol=symbol,
                            interval=interval,
                        )
                        total_written += written
                        results[symbol][normalize_interval(interval)] = written
                        logger.info(
                            "Backfill %s/%s complete: %d/%d bars written",
                            symbol,
                            normalize_interval(interval),
                            written,
                            len(bars),
                        )
                except Exception as e:
                    trace_error = str(e)
                    logger.error("Backfill task failed for %s: %s", symbol, e)
        self._finish_trace(trace, total_written=sum(
            int(count or 0)
            for per_symbol in results.values()
            for count in per_symbol.values()
        ), error=trace_error)
        record_history_event(
            environment=getattr(self, "environment", ""),
            source=trace_source,
            operation="backfill_all",
            result="error" if trace_error else "ok",
            duration_s=time.perf_counter() - backfill_started,
            rows=sum(int(count or 0) for per_symbol in results.values() for count in per_symbol.values()),
            error_class="error" if trace_error else "",
        )
        return results
