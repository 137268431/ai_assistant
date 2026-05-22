"""Tracing helpers for historical data backfill."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Dict, Optional, Sequence

from ibkr_compute.core.large_operation_alert import emit_large_operation_alert

from .timeframe_utils import normalize_interval

logger = logging.getLogger("ibkr_compute.market.data_backfill")


class DataBackfillTracingMixin:
    def _new_trace(
        self,
        source: str,
        symbols: Sequence[str],
        intervals: Sequence[str],
        context: Optional[Dict] = None,
    ) -> Optional[Dict]:
        if not self._trace_enabled():
            return None
        normalized_symbols = [
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        ]
        normalized_intervals = [
            normalize_interval(interval)
            for interval in (intervals or [])
            if str(interval or "").strip()
        ]
        with self._count_lock:
            request_start = self._request_count
            retry_start = self._retry_count
            throttle_start = self._throttle_count
        trace = {
            "trace_id": uuid.uuid4().hex[:12],
            "source": str(source or "history").strip() or "history",
            "environment": self.environment,
            "symbols": normalized_symbols,
            "intervals": normalized_intervals,
            "started_at": time.time(),
            "request_count_start": request_start,
            "retry_count_start": retry_start,
            "throttle_count_start": throttle_start,
            "max_concurrency": self._max_concurrency(),
            "request_spacing_s": self._request_spacing(),
            "interval_delay_s": self._interval_delay(),
            "trace_context": dict(context or {}) if isinstance(context, dict) else {},
            "requests": [],
            "writes": [],
            "symbols_timing": [],
            "symbol_outcomes": {},
        }
        logger.info(
            "[HistoryTrace] stage=start trace=%s source=%s symbols=%d intervals=%s workers=%d spacing_s=%.3f",
            trace["trace_id"],
            trace["source"],
            len(normalized_symbols),
            ",".join(normalized_intervals) or "--",
            trace["max_concurrency"],
            trace["request_spacing_s"],
        )
        return trace

    def _active_request_enter(self, symbol: str) -> tuple[int, list[str]]:
        normalized_symbol = str(symbol or "").strip().upper()
        with self._trace_lock:
            self._active_requests += 1
            if normalized_symbol:
                self._active_symbol_counts[normalized_symbol] = (
                    int(self._active_symbol_counts.get(normalized_symbol, 0) or 0) + 1
                )
            return self._active_requests, sorted(self._active_symbol_counts.keys())

    def _active_request_exit(self, symbol: str) -> tuple[int, list[str]]:
        normalized_symbol = str(symbol or "").strip().upper()
        with self._trace_lock:
            self._active_requests = max(0, self._active_requests - 1)
            if normalized_symbol and normalized_symbol in self._active_symbol_counts:
                next_count = int(self._active_symbol_counts.get(normalized_symbol, 0) or 0) - 1
                if next_count > 0:
                    self._active_symbol_counts[normalized_symbol] = next_count
                else:
                    self._active_symbol_counts.pop(normalized_symbol, None)
            return self._active_requests, sorted(self._active_symbol_counts.keys())

    def _record_trace_request(
        self,
        trace: Optional[Dict],
        *,
        symbol: str,
        interval: str,
        attempt: int,
        wait_s: float,
        broker_s: float,
        rows: int = 0,
        error: str = "",
        active_at_start: int = 0,
    ) -> None:
        if not trace:
            return
        payload = {
            "symbol": str(symbol or "").strip().upper(),
            "interval": normalize_interval(interval),
            "attempt": int(attempt or 0),
            "request_wait_s": round(max(0.0, float(wait_s or 0.0)), 3),
            "broker_request_s": round(max(0.0, float(broker_s or 0.0)), 3),
            "rows": int(rows or 0),
            "error": str(error or ""),
            "active_at_start": int(active_at_start or 0),
        }
        with self._trace_lock:
            requests = trace.setdefault("requests", [])
            if len(requests) < 80:
                requests.append(payload)
            outcomes = trace.setdefault("symbol_outcomes", {})
            outcome = outcomes.setdefault(
                payload["symbol"],
                {
                    "symbol": payload["symbol"],
                    "intervals": [],
                    "request_count": 0,
                    "rows": 0,
                    "errors": [],
                    "last_error": "",
                    "hmds_no_data": False,
                    "terminal_no_data": False,
                    "max_attempt": 0,
                },
            )
            if payload["interval"] and payload["interval"] not in outcome["intervals"]:
                outcome["intervals"].append(payload["interval"])
            outcome["request_count"] = int(outcome.get("request_count", 0) or 0) + 1
            outcome["rows"] = int(outcome.get("rows", 0) or 0) + int(payload["rows"] or 0)
            outcome["max_attempt"] = max(int(outcome.get("max_attempt", 0) or 0), payload["attempt"])
            if payload["error"]:
                outcome["last_error"] = payload["error"]
                errors = outcome.setdefault("errors", [])
                if payload["error"] not in errors and len(errors) < 5:
                    errors.append(payload["error"])
                error_text = payload["error"].lower()
                if "hmds query returned no data" in error_text:
                    outcome["hmds_no_data"] = True
                    outcome["terminal_no_data"] = True
        should_log = self._trace_log_all_requests() or payload["broker_request_s"] >= self._trace_slow_seconds()
        if should_log:
            logger.info(
                "[HistoryTrace] stage=request_done trace=%s symbol=%s interval=%s active=%d wait_s=%.3f broker_s=%.3f rows=%d attempt=%d error=%s",
                trace.get("trace_id", ""),
                payload["symbol"],
                payload["interval"],
                payload["active_at_start"],
                payload["request_wait_s"],
                payload["broker_request_s"],
                payload["rows"],
                payload["attempt"],
                payload["error"] or "--",
            )
        self._maybe_emit_large_operation_progress_alert(trace)

    def _record_trace_write(
        self,
        trace: Optional[Dict],
        *,
        symbol: str,
        interval: str,
        rows: int,
        written: int,
        write_s: float,
    ) -> None:
        if not trace:
            return
        payload = {
            "symbol": str(symbol or "").strip().upper(),
            "interval": normalize_interval(interval),
            "rows": int(rows or 0),
            "written": int(written or 0),
            "write_s": round(max(0.0, float(write_s or 0.0)), 3),
        }
        with self._trace_lock:
            writes = trace.setdefault("writes", [])
            if len(writes) < 80:
                writes.append(payload)
        if payload["write_s"] >= self._trace_slow_seconds():
            logger.info(
                "[HistoryTrace] stage=write_done trace=%s symbol=%s interval=%s rows=%d written=%d write_s=%.3f",
                trace.get("trace_id", ""),
                payload["symbol"],
                payload["interval"],
                payload["rows"],
                payload["written"],
                payload["write_s"],
            )

    def _record_trace_symbol(
        self,
        trace: Optional[Dict],
        *,
        symbol: str,
        fetch_s: float,
        intervals: Sequence[str],
        rows_by_interval: Dict[str, int],
    ) -> None:
        if not trace:
            return
        payload = {
            "symbol": str(symbol or "").strip().upper(),
            "fetch_s": round(max(0.0, float(fetch_s or 0.0)), 3),
            "intervals": [normalize_interval(interval) for interval in intervals or []],
            "rows_by_interval": {
                normalize_interval(interval): int(count or 0)
                for interval, count in (rows_by_interval or {}).items()
            },
        }
        with self._trace_lock:
            symbols_timing = trace.setdefault("symbols_timing", [])
            if len(symbols_timing) < 80:
                symbols_timing.append(payload)
        if payload["fetch_s"] >= self._trace_slow_seconds():
            logger.info(
                "[HistoryTrace] stage=symbol_fetch_done trace=%s symbol=%s fetch_s=%.3f rows=%s",
                trace.get("trace_id", ""),
                payload["symbol"],
                payload["fetch_s"],
                payload["rows_by_interval"],
            )

    def _slowest_trace_stage(self, trace: Dict) -> dict:
        slowest = {"stage": "", "duration_s": 0.0, "symbol": "", "interval": ""}
        for request in trace.get("requests") or []:
            for stage_key in ("request_wait_s", "broker_request_s"):
                duration = float(request.get(stage_key, 0) or 0)
                if duration > slowest["duration_s"]:
                    slowest = {
                        "stage": stage_key.replace("_s", ""),
                        "duration_s": round(duration, 3),
                        "symbol": str(request.get("symbol") or ""),
                        "interval": str(request.get("interval") or ""),
                    }
        for write in trace.get("writes") or []:
            duration = float(write.get("write_s", 0) or 0)
            if duration > slowest["duration_s"]:
                slowest = {
                    "stage": "write",
                    "duration_s": round(duration, 3),
                    "symbol": str(write.get("symbol") or ""),
                    "interval": str(write.get("interval") or ""),
                }
        for item in trace.get("symbols_timing") or []:
            duration = float(item.get("fetch_s", 0) or 0)
            if duration > slowest["duration_s"]:
                slowest = {
                    "stage": "symbol_fetch",
                    "duration_s": round(duration, 3),
                    "symbol": str(item.get("symbol") or ""),
                    "interval": ",".join(item.get("intervals") or []),
                }
        return slowest

    def _finish_trace(self, trace: Optional[Dict], *, total_written: int = 0, error: str = "") -> Dict:
        if not trace:
            return {}
        finished_at = time.time()
        with self._count_lock:
            request_delta = self._request_count - int(trace.get("request_count_start", 0) or 0)
            retry_delta = self._retry_count - int(trace.get("retry_count_start", 0) or 0)
            throttle_delta = self._throttle_count - int(trace.get("throttle_count_start", 0) or 0)
        summary = {
            "trace_id": trace.get("trace_id", ""),
            "source": trace.get("source", ""),
            "environment": self.environment,
            "symbols_total": len(trace.get("symbols") or []),
            "intervals": list(trace.get("intervals") or []),
            "duration_s": round(max(0.0, finished_at - float(trace.get("started_at", finished_at) or finished_at)), 3),
            "request_count": int(request_delta or 0),
            "retry_count": int(retry_delta or 0),
            "throttle_count": int(throttle_delta or 0),
            "written": int(total_written or 0),
            "max_concurrency": trace.get("max_concurrency", self._max_concurrency()),
            "request_spacing_s": trace.get("request_spacing_s", self._request_spacing()),
            "trace_context": dict(trace.get("trace_context") or {}),
            "slowest_stage": self._slowest_trace_stage(trace),
            "error": str(error or ""),
            "request_samples": list((trace.get("requests") or [])[-8:]),
            "symbol_timings": list((trace.get("symbols_timing") or [])[-12:]),
            "finished_at_ms": int(finished_at * 1000),
        }
        symbol_outcomes = list((trace.get("symbol_outcomes") or {}).values())
        hmds_no_data_symbols = sorted(
            str((item or {}).get("symbol") or "")
            for item in symbol_outcomes
            if (item or {}).get("hmds_no_data")
        )
        request_error_symbols = sorted(
            str((item or {}).get("symbol") or "")
            for item in symbol_outcomes
            if str((item or {}).get("last_error") or "")
        )
        non_hmds_error_symbols = sorted(
            str((item or {}).get("symbol") or "")
            for item in symbol_outcomes
            if str((item or {}).get("last_error") or "")
            and not (item or {}).get("hmds_no_data")
        )
        summary.update(
            {
                "symbol_outcomes": symbol_outcomes,
                "hmds_no_data_symbols": hmds_no_data_symbols,
                "hmds_no_data_count": len(hmds_no_data_symbols),
                "request_error_symbols": request_error_symbols,
                "request_error_count": len(request_error_symbols),
                "non_hmds_error_symbols": non_hmds_error_symbols,
                "non_hmds_error_count": len(non_hmds_error_symbols),
            }
        )
        with self._trace_lock:
            self._last_trace = dict(summary)
            self._recent_traces.append(dict(summary))
        logger.info(
            "[HistoryTrace] stage=done trace=%s source=%s total_s=%.3f requests=%d retry=%d throttle=%d written=%d slowest=%s/%s %.3fs error=%s",
            summary["trace_id"],
            summary["source"],
            summary["duration_s"],
            summary["request_count"],
            summary["retry_count"],
            summary["throttle_count"],
            summary["written"],
            summary["slowest_stage"].get("stage") or "--",
            summary["slowest_stage"].get("symbol") or "--",
            float(summary["slowest_stage"].get("duration_s", 0) or 0),
            summary["error"] or "--",
        )
        self._emit_large_operation_terminal_alert(summary)
        return summary

    def _maybe_emit_large_operation_progress_alert(self, trace: Optional[Dict]) -> None:
        if not trace:
            return
        with self._count_lock:
            request_delta = self._request_count - int(trace.get("request_count_start", 0) or 0)
            retry_delta = self._retry_count - int(trace.get("retry_count_start", 0) or 0)
            throttle_delta = self._throttle_count - int(trace.get("throttle_count_start", 0) or 0)
        trace_context = dict(trace.get("trace_context") or {})
        emit_large_operation_alert(
            self.pb_client,
            {
                **trace_context,
                "operation_id": str(trace.get("trace_id") or ""),
                "operation_type": "history_backfill",
                "job_id": str(trace.get("source") or "history_backfill"),
                "source": str(trace.get("source") or "history_backfill"),
                "symbols": list(trace.get("symbols") or []),
                "symbols_total": len(trace.get("symbols") or []),
                "intervals": list(trace.get("intervals") or []),
                "task_count": len(trace.get("symbols") or []) * max(1, len(trace.get("intervals") or [])),
                "duration_s": max(0.0, time.time() - float(trace.get("started_at", time.time()) or time.time())),
                "request_count": int(request_delta or 0),
                "retry_count": int(retry_delta or 0),
                "throttle_count": int(throttle_delta or 0),
                "max_concurrency": trace.get("max_concurrency", self._max_concurrency()),
                "request_spacing_s": trace.get("request_spacing_s", self._request_spacing()),
                "data_environment": self.environment,
            },
            config=self.config,
            stage="progress",
            environment=self.environment,
        )

    def _emit_large_operation_terminal_alert(self, summary: Dict) -> None:
        trace_context = dict(summary.get("trace_context") or {})
        source = str(summary.get("source") or "history_backfill")
        symbols_total = int(summary.get("symbols_total", 0) or 0)
        hmds_no_data_count = int(summary.get("hmds_no_data_count", 0) or 0)
        non_hmds_error_count = int(summary.get("non_hmds_error_count", 0) or 0)
        written = int(summary.get("written", 0) or 0)
        market_session = str(trace_context.get("market_session") or "").strip().lower()
        extended_no_data_only = bool(
            source == "watchlist_idle_topup"
            and symbols_total > 0
            and hmds_no_data_count >= symbols_total
            and non_hmds_error_count == 0
            and written <= 0
            and market_session in {"premarket", "afterhours", "closed", "close_transition"}
        )
        emit_large_operation_alert(
            self.pb_client,
            {
                **trace_context,
                "operation_id": str(summary.get("trace_id") or ""),
                "operation_type": "history_backfill",
                "job_id": source,
                "source": source,
                "trigger_source": source,
                "symbols": list(summary.get("symbols") or []),
                "symbols_total": symbols_total,
                "intervals": list(summary.get("intervals") or []),
                "task_count": symbols_total * max(1, len(summary.get("intervals") or [])),
                "duration_s": float(summary.get("duration_s", 0) or 0),
                "request_count": int(summary.get("request_count", 0) or 0),
                "retry_count": int(summary.get("retry_count", 0) or 0),
                "throttle_count": int(summary.get("throttle_count", 0) or 0),
                "written": written,
                "slowest_stage": dict(summary.get("slowest_stage") or {}),
                "hmds_no_data_symbols": list(summary.get("hmds_no_data_symbols") or []),
                "hmds_no_data_count": hmds_no_data_count,
                "request_error_count": int(summary.get("request_error_count", 0) or 0),
                "non_hmds_error_count": non_hmds_error_count,
                "explained_no_data_only": extended_no_data_only,
                "explained_no_data_reason": "extended_hours_no_data" if extended_no_data_only else "",
                "explained_no_data_cn": (
                    "盘前/盘后或闭市时标的不活跃，IBKR HMDS 可能没有可返回的 5m bars；这类完成事件不代表系统故障。"
                    if extended_no_data_only
                    else ""
                ),
                "explained_no_data_en": (
                    "During premarket/afterhours/closed sessions, inactive symbols may have no IBKR HMDS 5m bars; this does not indicate a system failure."
                    if extended_no_data_only
                    else ""
                ),
                "error": str(summary.get("error") or ""),
                "data_environment": self.environment,
            },
            config=self.config,
            stage="failed" if str(summary.get("error") or "") else "completed",
            environment=self.environment,
        )

    def status(self) -> dict:
        with self._trace_lock:
            recent_limit = self._trace_recent_limit()
            recent_traces = list(self._recent_traces)[-recent_limit:]
            active_symbols = sorted(self._active_symbol_counts.keys())
            last_trace = dict(self._last_trace)
            active_requests = int(self._active_requests or 0)
        slowest_recent = {"stage": "", "duration_s": 0.0, "symbol": "", "interval": ""}
        for trace in recent_traces:
            candidate = trace.get("slowest_stage") if isinstance(trace, dict) else {}
            duration = float((candidate or {}).get("duration_s", 0) or 0)
            if duration > float(slowest_recent.get("duration_s", 0) or 0):
                slowest_recent = dict(candidate or {})
        return {
            "total_backfilled": self._backfill_count,
            "request_count": self._request_count,
            "retry_count": self._retry_count,
            "throttle_count": self._throttle_count,
            "environment": self.environment,
            "intervals": list(self.default_intervals),
            "max_concurrency": self._max_concurrency(),
            "request_spacing_s": self._request_spacing(),
            "interval_delay_s": self._interval_delay(),
            "active_requests": active_requests,
            "active_symbols": active_symbols,
            "active_symbols_total": len(active_symbols),
            "trace_enabled": self._trace_enabled(),
            "last_trace": last_trace,
            "recent_traces": recent_traces,
            "recent_traces_total": len(recent_traces),
            "slowest_recent_stage": slowest_recent,
        }
