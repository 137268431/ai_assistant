"""Historical bar request and row-building helpers."""

from __future__ import annotations

import logging
import math
import sys
import time
from typing import Dict, List, Optional, Sequence

from ibkr_compute.observability.prometheus import record_history_event

from .data_backfill_support import (
    PERIOD_MAP,
    _format_ib_end_datetime,
    _parse_period_days,
    _period_from_days,
    _to_ib_bar_size,
    _to_ib_duration,
)
from .pocketbase_sqlite import fetch_latest_bar, fetch_latest_bars_by_symbol, open_pb_sqlite
from .timeframe_utils import (
    build_runtime_timestamps,
    classify_session,
    format_cn_time,
    format_us_time,
    interval_to_ms,
    normalize_interval,
)

logger = logging.getLogger("ibkr_compute.market.data_backfill")


def _facade_attr(name: str, fallback):
    facade = sys.modules.get("ibkr_compute.market.data_backfill")
    return getattr(facade, name, fallback)


class DataBackfillHistoryMixin:
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

    def _is_broker_blocked_history_error(self, error: Exception | str | None) -> bool:
        text = str(error or "").strip().lower()
        if not text:
            return False
        return (
            ("client id" in text and "in use" in text)
            or "last_error_code=326" in text
            or "code=326" in text
            or "broker_not_ready" in text
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
            record_history_event(
                environment=getattr(self, "environment", ""),
                source="data_backfill",
                operation="throttle",
                result="wait",
                duration_s=delay,
            )
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
                record_history_event(
                    environment=getattr(self, "environment", ""),
                    source=str((trace or {}).get("source") or "data_backfill"),
                    interval=interval,
                    operation="request",
                    result="ok",
                    duration_s=broker_s,
                    rows=len(bars or []),
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
                    record_history_event(
                        environment=getattr(self, "environment", ""),
                        source=str((trace or {}).get("source") or "data_backfill"),
                        interval=interval,
                        operation="request",
                        result="error",
                        duration_s=broker_s,
                        error_class="terminal",
                    )
                    raise RuntimeError(
                        f"history_fetch_terminal:{symbol}:{interval}:{conid}:{exc}"
                    ) from exc
                if self._is_broker_blocked_history_error(exc):
                    record_history_event(
                        environment=getattr(self, "environment", ""),
                        source=str((trace or {}).get("source") or "data_backfill"),
                        interval=interval,
                        operation="request",
                        result="blocked",
                        duration_s=broker_s,
                        error_class="broker_blocked",
                    )
                    raise RuntimeError(
                        f"history_fetch_broker_blocked:{symbol}:{interval}:{conid}:{exc}"
                    ) from exc
                if attempt >= max_retries:
                    record_history_event(
                        environment=getattr(self, "environment", ""),
                        source=str((trace or {}).get("source") or "data_backfill"),
                        interval=interval,
                        operation="request",
                        result="error",
                        duration_s=broker_s,
                        error_class=exc.__class__.__name__,
                    )
                    raise RuntimeError(
                        f"history_fetch_failed_after_retries:{symbol}:{interval}:{conid}:{exc}"
                    ) from exc
                delay = retry_base_delay * (2 ** attempt)
                with self._count_lock:
                    self._retry_count += 1
                record_history_event(
                    environment=getattr(self, "environment", ""),
                    source=str((trace or {}).get("source") or "data_backfill"),
                    interval=interval,
                    operation="retry",
                    result="retry",
                    duration_s=broker_s,
                    error_class=exc.__class__.__name__,
                )
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

    def _get_latest_stored_bar_ms(self, symbol: str, interval: str) -> int:
        normalized = normalize_interval(interval)
        safe_symbol = str(symbol or "").upper().replace('"', '\\"')
        safe_interval = normalized.replace('"', '\\"')
        safe_environment = str(self.environment or "live").strip().lower().replace('"', '\\"')
        safe_upper_ms = self._safe_history_upper_bound_ms(normalized)

        if self._direct_sqlite_read_enabled():
            try:
                open_sqlite = _facade_attr("open_pb_sqlite", open_pb_sqlite)
                fetch_latest = _facade_attr("fetch_latest_bar", fetch_latest_bar)
                with open_sqlite(readonly=True, timeout=self._direct_sqlite_read_timeout()) as conn:
                    row = fetch_latest(
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

    def get_latest_stored_bar_ms_map(self, symbols: Sequence[str], interval: str = "5m") -> Dict[str, int]:
        normalized = normalize_interval(interval)
        normalized_symbols = sorted(
            {
                str(symbol or "").strip().upper()
                for symbol in (symbols or [])
                if str(symbol or "").strip()
            }
        )
        if not normalized_symbols:
            return {}
        safe_upper_ms = self._safe_history_upper_bound_ms(normalized)
        if self._direct_sqlite_read_enabled():
            try:
                open_sqlite = _facade_attr("open_pb_sqlite", open_pb_sqlite)
                fetch_latest_map = _facade_attr("fetch_latest_bars_by_symbol", fetch_latest_bars_by_symbol)
                with open_sqlite(readonly=True, timeout=self._direct_sqlite_read_timeout()) as conn:
                    rows_by_symbol = fetch_latest_map(
                        conn,
                        normalized_symbols,
                        normalized,
                        self.environment,
                        safe_upper_ms=safe_upper_ms,
                        include_legacy_empty=False,
                    )
                return {
                    symbol: int((rows_by_symbol.get(symbol) or {}).get("bar_time_ms", 0) or 0)
                    for symbol in normalized_symbols
                }
            except Exception as exc:
                if not self._direct_sqlite_read_fallback_api_enabled():
                    logger.warning(
                        "Failed to query latest stored bars via SQLite for %d symbols/%s: %s",
                        len(normalized_symbols),
                        normalized,
                        exc,
                    )
                    return {symbol: 0 for symbol in normalized_symbols}
                logger.debug(
                    "Direct SQLite latest bars map lookup failed for %d symbols/%s, falling back to per-symbol lookup: %s",
                    len(normalized_symbols),
                    normalized,
                    exc,
                )

        return {
            symbol: self._get_latest_stored_bar_ms(symbol, normalized)
            for symbol in normalized_symbols
        }

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
