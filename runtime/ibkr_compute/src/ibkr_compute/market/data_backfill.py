"""
Historical bar backfill across the IBKR timeframes used by the pipeline.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from ibkr_compute.core.time_utils import ET
import math
import os
import time
import logging
import threading
import uuid
from typing import Dict, List, Optional, Sequence

from ibkr_compute.broker import BrokerAdapter

from .pocketbase_sqlite import (
    fetch_bar_times_in_range,
    fetch_latest_bar,
    fetch_recent_bars,
    open_pb_sqlite,
)
from .timeframe_utils import (
    build_runtime_timestamps,
    latest_safe_closed_bucket_ms,
    classify_session,
    format_cn_time,
    format_us_time,
    interval_to_ms,
    normalize_interval,
)

logger = logging.getLogger(__name__)

ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")

PERIOD_MAP = {
    "5m": ("4d", "5min"),
    "15m": ("10d", "15min"),
    "30m": ("20d", "30min"),
    "1h": ("40d", "1h"),
    "4h": ("120d", "4h"),
    "1d": ("2y", "1d"),
}


def _parse_intervals(value: str, fallback: Sequence[str]) -> List[str]:
    parsed = []
    for raw in str(value or "").split(","):
        normalized = normalize_interval(raw)
        if normalized and normalized not in parsed:
            parsed.append(normalized)
    return parsed or list(fallback)


DEFAULT_BACKFILL_INTERVALS = _parse_intervals(
    os.environ.get("IBKR_BACKFILL_INTERVALS", "5m"),
    fallback=("5m",),
)
REQUEST_SPACING_SECONDS = max(0.0, float(os.environ.get("IBKR_HISTORY_REQUEST_SPACING", "0.15")))
INTERVAL_DELAY_SECONDS = max(0.0, float(os.environ.get("IBKR_HISTORY_INTERVAL_DELAY", "0.10")))
MAX_CONCURRENT_REQUESTS = max(
    1,
    min(10, int(os.environ.get("IBKR_HISTORY_MAX_CONCURRENCY", "10"))),
)
MAX_RETRIES = max(0, int(os.environ.get("IBKR_HISTORY_MAX_RETRIES", "4")))
RETRY_BASE_DELAY_SECONDS = max(0.5, float(os.environ.get("IBKR_HISTORY_RETRY_BASE_DELAY", "2.0")))
RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
DEFAULT_HISTORY_CLOSE_DELAY_SECONDS = max(
    1,
    int(os.environ.get("IBKR_OFFICIAL_5M_CLOSE_DELAY_SEC", "8")),
)
TRACE_RECENT_LIMIT = max(1, int(os.environ.get("IBKR_HISTORY_TRACE_RECENT_LIMIT", "20")))
TRACE_SLOW_SECONDS = max(0.1, float(os.environ.get("IBKR_HISTORY_TRACE_SLOW_SEC", "2.0")))

IB_DURATION_SUFFIX = {
    "s": "S",
    "d": "D",
    "w": "W",
    "m": "M",
    "y": "Y",
}
IB_BAR_SIZE_MAP = {
    "5min": "5 mins",
    "15min": "15 mins",
    "30min": "30 mins",
    "1h": "1 hour",
    "4h": "4 hours",
    "1d": "1 day",
}
DEFAULT_CHUNK_DAYS = {
    "5m": 4,
    "15m": 14,
    "30m": 30,
    "1h": 60,
    "4h": 120,
}


def _to_ib_duration(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "1 D"
    if " " in text:
        return text.upper()
    digits = "".join(ch for ch in text if ch.isdigit())
    suffix = "".join(ch for ch in text if ch.isalpha())
    if digits and suffix in IB_DURATION_SUFFIX:
        return f"{int(digits)} {IB_DURATION_SUFFIX[suffix]}"
    return text.upper()


def _parse_period_days(value: str) -> Optional[int]:
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    suffix = "".join(ch for ch in text if ch.isalpha())
    if not digits:
        return None
    amount = max(1, int(digits))
    if suffix == "d":
        return amount
    if suffix == "w":
        return amount * 7
    if suffix == "m":
        return amount * 30
    if suffix == "y":
        return amount * 365
    return None


def _period_from_days(days: int) -> str:
    return f"{max(1, int(days or 0))}d"


def _format_ib_end_datetime(bar_time_ms: int) -> str:
    if int(bar_time_ms or 0) <= 0:
        return ""
    return datetime.fromtimestamp(int(bar_time_ms) / 1000, ET).strftime("%Y%m%d %H:%M:%S US/Eastern")


def _to_ib_bar_size(value: str) -> str:
    text = str(value or "").strip().lower()
    return IB_BAR_SIZE_MAP.get(text, value)


def _regular_session_gap_summary(
    rows: Sequence[Dict],
    interval: str,
    *,
    same_day_only: bool = False,
    example_limit: int = 4,
) -> Dict[str, object]:
    expected_ms = interval_to_ms(interval)
    gap_count = 0
    gap_examples: List[Dict[str, object]] = []

    def market_date(row: Dict) -> str:
        bar_time_ms = int(row.get("bar_time_ms", 0) or 0)
        if bar_time_ms > 0:
            try:
                return datetime.fromtimestamp(bar_time_ms / 1000, ET).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                pass
        us_time = str(row.get("us_time", "") or "").strip()
        return us_time.split(" ", 1)[0] if us_time else ""

    for index in range(1, len(rows)):
        prev = rows[index - 1]
        curr = rows[index]
        if str(prev.get("session_type", "") or "").strip().lower() != "regular":
            continue
        if str(curr.get("session_type", "") or "").strip().lower() != "regular":
            continue

        prev_ms = int(prev.get("bar_time_ms", 0) or 0)
        curr_ms = int(curr.get("bar_time_ms", 0) or 0)
        if prev_ms <= 0 or curr_ms <= 0:
            continue
        if same_day_only and market_date(prev) != market_date(curr):
            continue

        delta_ms = curr_ms - prev_ms
        if delta_ms <= expected_ms:
            continue

        gap_count += 1
        if len(gap_examples) < example_limit:
            gap_examples.append({
                "prev_us_time": str(prev.get("us_time", "") or ""),
                "next_us_time": str(curr.get("us_time", "") or ""),
                "missing_points": max(int(round(delta_ms / expected_ms)) - 1, 1),
            })

    return {
        "gap_count": gap_count,
        "gap_examples": gap_examples,
    }


def _regular_session_expected_bar_times(
    start_ms: int,
    end_ms: int,
    interval: str,
) -> List[int]:
    expected_ms = interval_to_ms(interval)
    if expected_ms <= 0 or start_ms <= 0 or end_ms <= 0 or start_ms > end_ms:
        return []

    expected_times: List[int] = []
    current_ms = start_ms
    while current_ms <= end_ms:
        if classify_session(format_us_time(current_ms), current_ms).strip().lower() == "regular":
            expected_times.append(current_ms)
        current_ms += expected_ms
    return expected_times


class DataBackfill:
    def __init__(
        self,
        gateway_url: str = None,
        data_writer=None,
        config=None,
        environment: str = ENVIRONMENT,
        broker: BrokerAdapter | None = None,
    ):
        self.data_writer = data_writer
        self.pb_client = getattr(data_writer, "pb_client", None) if data_writer else None
        self.config = config
        self.environment = str(environment or ENVIRONMENT).strip().lower() or ENVIRONMENT
        self.broker = broker or BrokerAdapter()
        self.default_intervals = list(DEFAULT_BACKFILL_INTERVALS)
        self._backfill_count = 0
        self._request_count = 0
        self._retry_count = 0
        self._throttle_count = 0
        self._count_lock = threading.Lock()
        self._request_gate_lock = threading.Lock()
        self._next_request_at = 0.0
        self._trace_lock = threading.RLock()
        self._recent_traces = deque(maxlen=max(TRACE_RECENT_LIMIT, 100))
        self._last_trace: Dict = {}
        self._active_requests = 0
        self._active_symbol_counts: Dict[str, int] = {}

    def _resolve_intervals(self, intervals: Optional[Sequence[str]]) -> List[str]:
        if intervals is None:
            return list(self.default_intervals)
        return _parse_intervals(",".join(str(item) for item in intervals), fallback=self.default_intervals)

    def _get_int_setting(self, key: str, fallback: int) -> int:
        if not self.config:
            return fallback
        return self.config.get_int_for_environment(key, self.environment, fallback)

    def _get_float_setting(self, key: str, fallback: float) -> float:
        if not self.config:
            return fallback
        return self.config.get_float_for_environment(key, self.environment, fallback)

    def _get_bool_setting(self, key: str, fallback: bool) -> bool:
        if not self.config or not hasattr(self.config, "get_bool_for_environment"):
            return bool(fallback)
        return bool(self.config.get_bool_for_environment(key, self.environment, fallback))

    def _request_spacing(self) -> float:
        return max(0.0, self._get_float_setting("ibkr_history_request_spacing", REQUEST_SPACING_SECONDS))

    def _interval_delay(self) -> float:
        return max(0.0, self._get_float_setting("ibkr_history_interval_delay", INTERVAL_DELAY_SECONDS))

    def _max_concurrency(self) -> int:
        return max(1, min(10, self._get_int_setting("ibkr_history_max_concurrency", MAX_CONCURRENT_REQUESTS)))

    def _trace_enabled(self) -> bool:
        return self._get_bool_setting("ibkr_history_trace_enabled", True)

    def _trace_log_all_requests(self) -> bool:
        return self._get_bool_setting("ibkr_history_trace_log_all_requests", True)

    def _trace_recent_limit(self) -> int:
        return max(1, min(100, self._get_int_setting("ibkr_history_trace_recent_limit", TRACE_RECENT_LIMIT)))

    def _trace_slow_seconds(self) -> float:
        return max(0.1, self._get_float_setting("ibkr_history_trace_slow_sec", TRACE_SLOW_SECONDS))

    def _direct_sqlite_read_enabled(self) -> bool:
        return self._get_bool_setting("ibkr_bar_direct_sqlite_read_enabled", True)

    def _direct_sqlite_read_fallback_api_enabled(self) -> bool:
        return self._get_bool_setting("ibkr_bar_direct_sqlite_read_fallback_api_enabled", True)

    def _direct_sqlite_read_timeout(self) -> float:
        fallback = self._get_float_setting("ibkr_bar_direct_sqlite_timeout_sec", 30.0)
        return max(0.5, self._get_float_setting("ibkr_bar_direct_sqlite_read_timeout_sec", fallback))

    def _new_trace(self, source: str, symbols: Sequence[str], intervals: Sequence[str]) -> Optional[Dict]:
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
            "requests": [],
            "writes": [],
            "symbols_timing": [],
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
            "slowest_stage": self._slowest_trace_stage(trace),
            "error": str(error or ""),
            "request_samples": list((trace.get("requests") or [])[-8:]),
            "symbol_timings": list((trace.get("symbols_timing") or [])[-12:]),
            "finished_at_ms": int(finished_at * 1000),
        }
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
        return summary

    def _max_retries(self) -> int:
        return max(0, self._get_int_setting("ibkr_history_max_retries", MAX_RETRIES))

    def _retry_base_delay(self) -> float:
        return max(0.5, self._get_float_setting("ibkr_history_retry_base_delay", RETRY_BASE_DELAY_SECONDS))

    def _chunked_backfill_enabled(self) -> bool:
        if not self.config:
            return True
        return self.config.get_bool_for_environment(
            "ibkr_history_chunked_backfill_enabled",
            self.environment,
            True,
        )

    def _history_chunk_days(self, interval: str) -> int:
        normalized = normalize_interval(interval)
        fallback = DEFAULT_CHUNK_DAYS.get(normalized, 0)
        if fallback <= 0:
            return 0
        return max(
            1,
            self._get_int_setting(
                f"ibkr_history_chunk_days_{normalized}",
                fallback,
            ),
        )

    def _is_terminal_history_error(self, error: Exception | str | None) -> bool:
        text = str(error or "").strip().lower()
        if not text:
            return False
        terminal_markers = (
            "contract_not_found",
            "no security definition has been found for the request",
            "hmds query returned no data",
        )
        return any(marker in text for marker in terminal_markers)

    def _close_delay_seconds(self) -> int:
        return max(
            0,
            self._get_int_setting(
                "ibkr_official_5m_close_delay_sec",
                DEFAULT_HISTORY_CLOSE_DELAY_SECONDS,
            ),
        )

    def _safe_history_upper_bound_ms(self, interval: str, *, now_ts: float | None = None) -> int:
        normalized = normalize_interval(interval)
        return latest_safe_closed_bucket_ms(
            normalized,
            delay_seconds=self._close_delay_seconds(),
            now_ms=int((now_ts or time.time()) * 1000),
        )

    def _wait_for_request_slot(self) -> float:
        request_spacing = self._request_spacing()
        if request_spacing <= 0:
            return 0.0

        delay = 0.0
        with self._request_gate_lock:
            now = time.monotonic()
            if self._next_request_at > now:
                delay = self._next_request_at - now
                now = self._next_request_at
            self._next_request_at = now + request_spacing

        if delay > 0:
            with self._count_lock:
                self._throttle_count += 1
            time.sleep(delay)
        return delay

    def _request_history_json(
        self,
        conid: int,
        symbol: str,
        interval: str,
        period: str,
        bar_size: str,
        start_time: str = "",
        exchange: str = "",
        trace: Optional[Dict] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> Dict:
        max_retries = self._max_retries() if max_retries is None else max(0, int(max_retries or 0))
        request_timeout = max(1, int(timeout or 30))
        retry_base_delay = self._retry_base_delay()
        for attempt in range(max_retries + 1):
            wait_s = self._wait_for_request_slot()
            with self._count_lock:
                self._request_count += 1

            active_at_start, _ = self._active_request_enter(symbol)
            request_started = time.perf_counter()
            try:
                bars = self.broker.request_historical_bars(
                    conid=int(conid or 0),
                    symbol=str(symbol or "").upper(),
                    exchange=str(exchange or "").upper(),
                    duration=_to_ib_duration(period),
                    bar_size=_to_ib_bar_size(bar_size),
                    end_datetime=str(start_time or ""),
                    use_rth=False,
                    timeout=request_timeout,
                )
                broker_s = time.perf_counter() - request_started
                self._record_trace_request(
                    trace,
                    symbol=symbol,
                    interval=interval,
                    attempt=attempt + 1,
                    wait_s=wait_s,
                    broker_s=broker_s,
                    rows=len(bars or []),
                    active_at_start=active_at_start,
                )
                return {
                    "serverId": "ib_gateway_socket",
                    "symbol": str(symbol or "").upper(),
                    "text": str(symbol or "").upper(),
                    "data": list(bars or []),
                    "points": len(bars or []),
                    "mdAvailability": "IBGW",
                }
            except Exception as exc:
                broker_s = time.perf_counter() - request_started
                self._record_trace_request(
                    trace,
                    symbol=symbol,
                    interval=interval,
                    attempt=attempt + 1,
                    wait_s=wait_s,
                    broker_s=broker_s,
                    rows=0,
                    error=str(exc),
                    active_at_start=active_at_start,
                )
                if self._is_terminal_history_error(exc):
                    raise RuntimeError(
                        f"history_fetch_terminal:{symbol}:{interval}:{conid}:{exc}"
                    ) from exc
                if attempt >= max_retries:
                    raise RuntimeError(
                        f"history_fetch_failed_after_retries:{symbol}:{interval}:{conid}:{exc}"
                    ) from exc
                delay = retry_base_delay * (2 ** attempt)
                with self._count_lock:
                    self._retry_count += 1
                logger.warning(
                    "History fetch retry for %s/%s (conid=%d, attempt=%d/%d, sleep=%.1fs): %s",
                    symbol,
                    interval,
                    conid,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    exc,
                )
                time.sleep(delay)
            finally:
                self._active_request_exit(symbol)
        raise RuntimeError(f"history_fetch_failed_after_retries:{symbol}:{interval}:{conid}")

    def _build_history_rows(
        self,
        *,
        conid: int,
        symbol: str,
        interval: str,
        period: str,
        bar_size: str,
        exchange: str,
        bars: Sequence[Dict],
        repair: bool,
        chunk_period: str = "",
        chunk_end_datetime: str = "",
        fetch_meta_extra: Optional[Dict] = None,
    ) -> List[Dict]:
        normalized = normalize_interval(interval)
        result = []
        fetch_meta = {
            "source": "ibkr_history_backfill",
            "conid": conid,
            "exchange": exchange,
            "interval": normalized,
            "outside_rth": True,
            "request_period": period,
            "request_bar": bar_size,
        }
        if chunk_period:
            fetch_meta["request_chunk_period"] = chunk_period
        if chunk_end_datetime:
            fetch_meta["request_end_datetime"] = chunk_end_datetime
        if fetch_meta_extra:
            fetch_meta.update(fetch_meta_extra)

        for bar in bars:
            raw_bar_time = int(bar.get("t", 0) or 0)
            bar_time_ms = raw_bar_time if raw_bar_time > 1_000_000_000_000 else raw_bar_time * 1000
            us_time = format_us_time(bar_time_ms)
            payload = {
                "symbol": symbol,
                "conid": conid,
                "environment": self.environment,
                "exchange": exchange,
                "interval": normalized,
                "open": float(bar.get("o", 0) or 0),
                "high": float(bar.get("h", 0) or 0),
                "low": float(bar.get("l", 0) or 0),
                "close": float(bar.get("c", 0) or 0),
                "volume": float(bar.get("v", 0) or 0),
                "bar_time_ms": bar_time_ms,
                "us_time": us_time,
                "cn_time": format_cn_time(bar_time_ms),
                "session_type": classify_session(us_time, bar_time_ms),
                "source": "backfill",
                "extra": {
                    **fetch_meta,
                    "repair_mode": bool(repair),
                    **build_runtime_timestamps(),
                },
            }
            result.append(payload)
        return result

    def _fetch_history_rows_once(
        self,
        *,
        conid: int,
        symbol: str,
        interval: str,
        period: str,
        bar_size: str,
        exchange: str,
        repair: bool,
        end_datetime: str = "",
        parent_period: str = "",
        trace: Optional[Dict] = None,
    ) -> List[Dict]:
        data = self._request_history_json(
            conid,
            symbol,
            interval,
            period,
            bar_size,
            end_datetime,
            exchange=exchange,
            trace=trace,
        )
        fetch_meta_extra = {}
        if data.get("mktDataDelay") is not None:
            fetch_meta_extra["mkt_data_delay"] = data.get("mktDataDelay")
        if data.get("mdAvailability"):
            fetch_meta_extra["md_availability"] = data.get("mdAvailability")
        if data.get("points") is not None:
            fetch_meta_extra["points"] = data.get("points")
        return self._build_history_rows(
            conid=conid,
            symbol=symbol,
            interval=interval,
            period=parent_period or period,
            bar_size=bar_size,
            exchange=exchange,
            bars=data.get("data", []),
            repair=repair,
            chunk_period=period if parent_period else "",
            chunk_end_datetime=end_datetime,
            fetch_meta_extra=fetch_meta_extra,
        )

    def _fetch_history_rows_chunked(
        self,
        *,
        conid: int,
        symbol: str,
        interval: str,
        period: str,
        period_days: int,
        chunk_days: int,
        bar_size: str,
        exchange: str,
        repair: bool,
        trace: Optional[Dict] = None,
    ) -> List[Dict]:
        normalized = normalize_interval(interval)
        interval_ms = max(1, interval_to_ms(normalized))
        rows_by_time: Dict[int, Dict] = {}
        remaining_days = max(1, int(period_days or 0))
        end_datetime = ""
        previous_oldest_ms = 0
        chunks_requested = 0
        max_chunks = max(1, int(math.ceil(remaining_days / max(1, int(chunk_days or 0)))))
        logger.info(
            "Chunked history fetch started for %s/%s: period=%s chunk_days=%d max_chunks=%d",
            symbol,
            normalized,
            period,
            chunk_days,
            max_chunks,
        )

        while remaining_days > 0 and chunks_requested < max_chunks:
            current_chunk_days = min(chunk_days, remaining_days)
            current_period = _period_from_days(current_chunk_days)
            chunk_rows = self._fetch_history_rows_once(
                conid=conid,
                symbol=symbol,
                interval=normalized,
                period=current_period,
                bar_size=bar_size,
                exchange=exchange,
                repair=repair,
                end_datetime=end_datetime,
                parent_period=period,
                trace=trace,
            )
            chunks_requested += 1
            remaining_days -= current_chunk_days

            if not chunk_rows:
                break
            for row in chunk_rows:
                bar_time_ms = int(row.get("bar_time_ms", 0) or 0)
                if bar_time_ms > 0:
                    rows_by_time[bar_time_ms] = row
            oldest_ms = min(int(row.get("bar_time_ms", 0) or 0) for row in chunk_rows if int(row.get("bar_time_ms", 0) or 0) > 0)
            if oldest_ms <= 0 or oldest_ms == previous_oldest_ms:
                break
            previous_oldest_ms = oldest_ms
            end_datetime = _format_ib_end_datetime(oldest_ms - interval_ms)

        rows = [rows_by_time[key] for key in sorted(rows_by_time.keys())]
        logger.info(
            "Chunked history fetch finished for %s/%s: period=%s chunks=%d rows=%d",
            symbol,
            normalized,
            period,
            chunks_requested,
            len(rows),
        )
        return rows

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

    def _get_latest_stored_bar_ms(self, symbol: str, interval: str) -> int:
        normalized = normalize_interval(interval)
        safe_symbol = str(symbol or "").upper().replace('"', '\\"')
        safe_interval = normalized.replace('"', '\\"')
        safe_environment = str(self.environment or "live").strip().lower().replace('"', '\\"')
        safe_upper_ms = self._safe_history_upper_bound_ms(normalized)

        if self._direct_sqlite_read_enabled():
            try:
                with open_pb_sqlite(readonly=True, timeout=self._direct_sqlite_read_timeout()) as conn:
                    row = fetch_latest_bar(
                        conn,
                        safe_symbol,
                        normalized,
                        self.environment,
                        safe_upper_ms=safe_upper_ms,
                        include_legacy_empty=False,
                    )
                return int((row or {}).get("bar_time_ms", 0) or 0)
            except Exception as exc:
                if not self._direct_sqlite_read_fallback_api_enabled():
                    logger.warning(
                        "Failed to query latest stored bar via SQLite for %s/%s: %s",
                        symbol,
                        normalized,
                        exc,
                    )
                    return 0
                logger.debug(
                    "Direct SQLite latest bar lookup failed for %s/%s, falling back to PocketBase API: %s",
                    symbol,
                    normalized,
                    exc,
                )

        if not self.pb_client:
            return 0

        try:
            filter_parts = [
                f'symbol = "{safe_symbol}"',
                f'interval = "{safe_interval}"',
                f'environment = "{safe_environment}"',
            ]
            if safe_upper_ms > 0:
                filter_parts.append(f"bar_time_ms <= {safe_upper_ms}")
            rows = self.pb_client.get_records(
                "ibkr_bars",
                filter=" && ".join(filter_parts),
                sort="-bar_time_ms",
                per_page=1,
                page=1,
            )
            if not rows:
                return 0
            return int(rows[0].get("bar_time_ms", 0) or 0)
        except Exception as exc:
            logger.warning(
                "Failed to query latest stored bar for %s/%s: %s",
                symbol,
                normalized,
                exc,
            )
            return 0

    def get_latest_stored_bar_ms(self, symbol: str, interval: str = "5m") -> int:
        return self._get_latest_stored_bar_ms(symbol, interval)

    def get_integrity_snapshot(
        self,
        symbol: str,
        interval: str = "5m",
        min_bars: int = 0,
        gap_lookback: int = 80,
    ) -> Dict:
        normalized = normalize_interval(interval)
        snapshot = {
            "symbol": str(symbol or "").upper(),
            "interval": normalized,
            "stored_bar_count": 0,
            "scanned_row_count": 0,
            "latest_stored_ms": 0,
            "oldest_loaded_ms": 0,
            "gap_count": 0,
            "gap_examples": [],
            "duplicate_count": 0,
            "duplicate_examples": [],
            "bad_ohlc_count": 0,
            "bad_ohlc_examples": [],
        }
        if not self.pb_client and not self._direct_sqlite_read_enabled():
            return snapshot

        safe_symbol = snapshot["symbol"].replace('"', '\\"')
        safe_interval = normalized.replace('"', '\\"')
        safe_environment = str(self.environment or "live").strip().lower().replace('"', '\\"')
        bars_needed = max(1, int(min_bars or 0), int(gap_lookback or 0), 400)
        max_pages = max(1, min(8, (bars_needed + 199) // 200))

        rows = []
        sqlite_read_ok = False
        if self._direct_sqlite_read_enabled():
            try:
                with open_pb_sqlite(readonly=True, timeout=self._direct_sqlite_read_timeout()) as conn:
                    rows = fetch_recent_bars(
                        conn,
                        snapshot["symbol"],
                        normalized,
                        self.environment,
                        limit=max_pages * 200,
                        include_legacy_empty=False,
                    )
                sqlite_read_ok = True
            except Exception as exc:
                if not self._direct_sqlite_read_fallback_api_enabled():
                    logger.warning(
                        "Failed to inspect stored bars via SQLite for %s/%s: %s",
                        symbol,
                        normalized,
                        exc,
                    )
                    return snapshot
                logger.debug(
                    "Direct SQLite stored bar inspection failed for %s/%s, falling back to PocketBase API: %s",
                    symbol,
                    normalized,
                    exc,
                )

        if not rows and not sqlite_read_ok:
            if not self.pb_client:
                return snapshot
            try:
                rows = self.pb_client.get_all_records(
                    "ibkr_bars",
                    filter=(
                        f'symbol = "{safe_symbol}" && '
                        f'interval = "{safe_interval}" && '
                        f'environment = "{safe_environment}"'
                    ),
                    sort="-bar_time_ms",
                    max_pages=max_pages,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to inspect stored bars for %s/%s: %s",
                    symbol,
                    normalized,
                    exc,
                )
                return snapshot

        if not rows:
            return snapshot

        snapshot["stored_bar_count"] = len(rows)
        snapshot["scanned_row_count"] = len(rows)
        snapshot["latest_stored_ms"] = int(rows[0].get("bar_time_ms", 0) or 0)
        snapshot["oldest_loaded_ms"] = int(rows[-1].get("bar_time_ms", 0) or 0)

        seen_bar_times = set()
        for row in rows:
            bar_time_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_time_ms > 0:
                if bar_time_ms in seen_bar_times:
                    snapshot["duplicate_count"] += 1
                    if len(snapshot["duplicate_examples"]) < 4:
                        snapshot["duplicate_examples"].append({
                            "bar_time_ms": bar_time_ms,
                            "us_time": str(row.get("us_time", "") or ""),
                        })
                else:
                    seen_bar_times.add(bar_time_ms)

            try:
                open_px = float(row.get("open", 0) or 0)
                high_px = float(row.get("high", 0) or 0)
                low_px = float(row.get("low", 0) or 0)
                close_px = float(row.get("close", 0) or 0)
            except Exception:
                open_px = high_px = low_px = close_px = 0.0

            invalid_ohlc = (
                open_px <= 0
                or high_px <= 0
                or low_px <= 0
                or close_px <= 0
                or high_px < max(open_px, close_px, low_px)
                or low_px > min(open_px, close_px, high_px)
            )
            if invalid_ohlc:
                snapshot["bad_ohlc_count"] += 1
                if len(snapshot["bad_ohlc_examples"]) < 4:
                    snapshot["bad_ohlc_examples"].append({
                        "bar_time_ms": bar_time_ms,
                        "us_time": str(row.get("us_time", "") or ""),
                        "open": open_px,
                        "high": high_px,
                        "low": low_px,
                        "close": close_px,
                    })

        recent_rows = list(reversed(rows[: max(3, int(gap_lookback or 0))]))
        gap_summary = _regular_session_gap_summary(
            recent_rows,
            normalized,
            same_day_only=True,
            example_limit=4,
        )
        snapshot["gap_count"] = int(gap_summary.get("gap_count", 0) or 0)
        snapshot["gap_examples"] = list(gap_summary.get("gap_examples") or [])

        return snapshot

    def get_required_sequence_snapshot(
        self,
        symbol: str,
        interval: str = "5m",
        start_ms: int = 0,
        end_ms: int = 0,
        example_limit: int = 4,
    ) -> Dict:
        normalized = normalize_interval(interval)
        normalized_symbol = str(symbol or "").strip().upper()
        range_start_ms = int(start_ms or 0)
        range_end_ms = int(end_ms or 0)
        snapshot = {
            "symbol": normalized_symbol,
            "interval": normalized,
            "start_ms": range_start_ms,
            "start_us": format_us_time(range_start_ms) if range_start_ms > 0 else "",
            "end_ms": range_end_ms,
            "end_us": format_us_time(range_end_ms) if range_end_ms > 0 else "",
            "expected_count": 0,
            "stored_count": 0,
            "missing_count": 0,
            "missing_bar_times": [],
            "missing_us_times": [],
            "query_error": "",
        }
        if not self.pb_client and not self._direct_sqlite_read_enabled():
            return snapshot
        if range_start_ms <= 0 or range_end_ms <= 0 or range_start_ms > range_end_ms:
            return snapshot

        expected_bar_times = _regular_session_expected_bar_times(range_start_ms, range_end_ms, normalized)
        snapshot["expected_count"] = len(expected_bar_times)
        if not expected_bar_times:
            return snapshot

        safe_symbol = normalized_symbol.replace('"', '\\"')
        safe_interval = normalized.replace('"', '\\"')
        safe_environment = str(self.environment or "live").strip().lower().replace('"', '\\"')
        max_pages = max(1, min(8, (len(expected_bar_times) + 199) // 200))

        stored_bar_times = []
        sqlite_read_ok = False
        if self._direct_sqlite_read_enabled():
            try:
                with open_pb_sqlite(readonly=True, timeout=self._direct_sqlite_read_timeout()) as conn:
                    stored_bar_times = fetch_bar_times_in_range(
                        conn,
                        normalized_symbol,
                        normalized,
                        self.environment,
                        start_ms=range_start_ms,
                        end_ms=range_end_ms,
                        session_type="regular",
                        include_legacy_empty=False,
                    )
                sqlite_read_ok = True
            except Exception as exc:
                if not self._direct_sqlite_read_fallback_api_enabled():
                    snapshot["query_error"] = str(exc)
                    logger.warning(
                        "Failed to inspect required sequence via SQLite for %s/%s (%s -> %s): %s",
                        normalized_symbol,
                        normalized,
                        snapshot["start_us"] or range_start_ms,
                        snapshot["end_us"] or range_end_ms,
                        exc,
                    )
                    return snapshot
                logger.debug(
                    "Direct SQLite sequence inspection failed for %s/%s, falling back to PocketBase API: %s",
                    normalized_symbol,
                    normalized,
                    exc,
                )

        if not stored_bar_times and not sqlite_read_ok:
            if not self.pb_client:
                return snapshot
            try:
                rows = self.pb_client.get_all_records(
                    "ibkr_bars",
                    filter=(
                        f'symbol = "{safe_symbol}" && '
                        f'interval = "{safe_interval}" && '
                        f'environment = "{safe_environment}" && '
                        f'session_type = "regular" && '
                        f'bar_time_ms >= {range_start_ms} && '
                        f'bar_time_ms <= {range_end_ms}'
                    ),
                    sort="bar_time_ms",
                    max_pages=max_pages,
                )
            except Exception as exc:
                snapshot["query_error"] = str(exc)
                logger.warning(
                    "Failed to inspect required sequence for %s/%s (%s -> %s): %s",
                    normalized_symbol,
                    normalized,
                    snapshot["start_us"] or range_start_ms,
                    snapshot["end_us"] or range_end_ms,
                    exc,
                )
                return snapshot

            stored_bar_times = sorted({
                int(row.get("bar_time_ms", 0) or 0)
                for row in (rows or [])
                if int(row.get("bar_time_ms", 0) or 0) > 0
            })
        snapshot["stored_count"] = len(stored_bar_times)

        stored_set = set(stored_bar_times)
        missing_bar_times = [bar_time_ms for bar_time_ms in expected_bar_times if bar_time_ms not in stored_set]
        snapshot["missing_count"] = len(missing_bar_times)
        snapshot["missing_bar_times"] = missing_bar_times
        snapshot["missing_us_times"] = [
            format_us_time(bar_time_ms)
            for bar_time_ms in missing_bar_times[: max(1, int(example_limit or 0))]
        ]
        return snapshot

    def fetch_history(
        self,
        conid: int,
        symbol: str,
        interval: str = "5m",
        exchange: str = "",
        repair: bool = False,
        request_period: Optional[str] = None,
        trace: Optional[Dict] = None,
    ) -> List[Dict]:
        normalized = normalize_interval(interval)
        period, bar_size = PERIOD_MAP.get(normalized, PERIOD_MAP["5m"])
        if str(request_period or "").strip():
            period = str(request_period).strip()

        try:
            period_days = _parse_period_days(period)
            chunk_days = self._history_chunk_days(normalized)
            if (
                self._chunked_backfill_enabled()
                and period_days is not None
                and chunk_days > 0
                and period_days > chunk_days
            ):
                result = self._fetch_history_rows_chunked(
                    conid=conid,
                    symbol=symbol,
                    interval=normalized,
                    period=period,
                    period_days=period_days,
                    chunk_days=chunk_days,
                    bar_size=bar_size,
                    exchange=exchange,
                    repair=repair,
                    trace=trace,
                )
            else:
                result = self._fetch_history_rows_once(
                    conid=conid,
                    symbol=symbol,
                    interval=normalized,
                    period=period,
                    bar_size=bar_size,
                    exchange=exchange,
                    repair=repair,
                    trace=trace,
                )

            safe_upper_ms = self._safe_history_upper_bound_ms(normalized)
            if safe_upper_ms > 0:
                future_rows = [
                    row for row in result
                    if int(row.get("bar_time_ms", 0) or 0) > safe_upper_ms
                ]
                if future_rows:
                    future_rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
                    logger.warning(
                        "Dropped %d unsafe future %s bars for %s: safe_upper_ms=%d(%s) first=%d(%s) last=%d(%s) repair=%s period=%s",
                        len(future_rows),
                        normalized,
                        symbol,
                        safe_upper_ms,
                        format_us_time(safe_upper_ms),
                        int(future_rows[0].get("bar_time_ms", 0) or 0),
                        future_rows[0].get("us_time", ""),
                        int(future_rows[-1].get("bar_time_ms", 0) or 0),
                        future_rows[-1].get("us_time", ""),
                        bool(repair),
                        period,
                    )
                    result = [
                        row for row in result
                        if int(row.get("bar_time_ms", 0) or 0) <= safe_upper_ms
                    ]

            latest_stored_ms = self._get_latest_stored_bar_ms(symbol, normalized)
            if latest_stored_ms > 0 and not repair:
                result = [row for row in result if int(row.get("bar_time_ms", 0) or 0) > latest_stored_ms]

            logger.info(
                "Fetched %d %s bars for %s/%s (period=%s, latest_stored_ms=%d)",
                len(result),
                "repair" if repair else "new",
                symbol,
                normalized,
                period,
                latest_stored_ms,
            )
            return result
        except Exception as e:
            logger.error(
                "History fetch failed for %s (conid=%d, interval=%s): %s",
                symbol,
                conid,
                normalized,
                e,
            )
            return []

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
