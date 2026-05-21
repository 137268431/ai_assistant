"""Backfill orchestration methods."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import time
from typing import Dict, List, Optional, Sequence

from ibkr_compute.core.large_operation_alert import emit_large_operation_alert

from .data_backfill_support import PERIOD_MAP
from .timeframe_utils import normalize_interval

logger = logging.getLogger("ibkr_compute.market.data_backfill")


class DataBackfillBackfillMixin:
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
    ) -> Dict[str, Dict[str, int]]:
        results = {}
        metadata = symbol_meta or {}
        interval_list = self._resolve_intervals(intervals)
        repair_set = {str(symbol or "").upper() for symbol in (repair_symbols or []) if str(symbol or "").strip()}
        for symbol, conid in conid_map.items():
            exchange = str((metadata.get(symbol) or {}).get("exchange") or "")
            results[symbol] = {interval: 0 for interval in interval_list}

        if not conid_map:
            return results

        worker_count = min(self._max_concurrency(), len(conid_map))
        trace = self._new_trace(trace_source, list(conid_map.keys()), interval_list)
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
        return results
