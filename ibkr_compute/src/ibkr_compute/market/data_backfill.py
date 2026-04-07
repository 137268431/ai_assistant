"""
Historical bar backfill across the IBKR timeframes used by the pipeline.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import time
import logging
import threading
from typing import Dict, List, Optional, Sequence

import requests

from ibkr_compute.gateway.cookie_store import load_cookies, save_cookies

from .timeframe_utils import (
    build_runtime_timestamps,
    classify_session,
    format_cn_time,
    format_us_time,
    interval_to_ms,
    normalize_interval,
)

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
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
REQUEST_SPACING_SECONDS = max(0.0, float(os.environ.get("IBKR_HISTORY_REQUEST_SPACING", "0.20")))
INTERVAL_DELAY_SECONDS = max(0.0, float(os.environ.get("IBKR_HISTORY_INTERVAL_DELAY", "0.35")))
MAX_CONCURRENT_REQUESTS = max(
    1,
    min(5, int(os.environ.get("IBKR_HISTORY_MAX_CONCURRENCY", "4"))),
)
MAX_RETRIES = max(0, int(os.environ.get("IBKR_HISTORY_MAX_RETRIES", "4")))
RETRY_BASE_DELAY_SECONDS = max(0.5, float(os.environ.get("IBKR_HISTORY_RETRY_BASE_DELAY", "2.0")))
RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


class DataBackfill:
    def __init__(self, gateway_url: str = None, data_writer=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.data_writer = data_writer
        self.pb_client = getattr(data_writer, "pb_client", None) if data_writer else None
        self.default_intervals = list(DEFAULT_BACKFILL_INTERVALS)
        self.request_spacing = REQUEST_SPACING_SECONDS
        self.interval_delay = INTERVAL_DELAY_SECONDS
        self.max_concurrency = MAX_CONCURRENT_REQUESTS
        self.max_retries = MAX_RETRIES
        self.retry_base_delay = RETRY_BASE_DELAY_SECONDS
        self._backfill_count = 0
        self._request_count = 0
        self._retry_count = 0
        self._throttle_count = 0
        self._count_lock = threading.Lock()
        self._request_gate_lock = threading.Lock()
        self._next_request_at = 0.0

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        session.verify = False
        load_cookies(session)
        return session

    def _resolve_intervals(self, intervals: Optional[Sequence[str]]) -> List[str]:
        if intervals is None:
            return list(self.default_intervals)
        return _parse_intervals(",".join(str(item) for item in intervals), fallback=self.default_intervals)

    def _wait_for_request_slot(self):
        if self.request_spacing <= 0:
            return

        delay = 0.0
        with self._request_gate_lock:
            now = time.monotonic()
            if self._next_request_at > now:
                delay = self._next_request_at - now
                now = self._next_request_at
            self._next_request_at = now + self.request_spacing

        if delay > 0:
            time.sleep(delay)

    def _request_history_json(self, conid: int, symbol: str, interval: str, period: str, bar_size: str) -> Dict:
        params = {
            "conid": conid,
            "period": period,
            "bar": bar_size,
            "outsideRth": "true",
        }

        for attempt in range(self.max_retries + 1):
            self._wait_for_request_slot()
            session = self._create_session()
            try:
                with self._count_lock:
                    self._request_count += 1

                resp = session.get(
                    self._api_url("/iserver/marketdata/history"),
                    params=params,
                    timeout=30,
                )

                if resp.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    delay = self.retry_base_delay * (2 ** attempt)
                    with self._count_lock:
                        self._retry_count += 1
                        if resp.status_code == 429:
                            self._throttle_count += 1
                    logger.warning(
                        "History fetch retry for %s/%s (conid=%d, status=%d, attempt=%d/%d, sleep=%.1fs)",
                        symbol,
                        interval,
                        conid,
                        resp.status_code,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    time.sleep(delay)
                    continue

                resp.raise_for_status()
                payload = resp.json()
                save_cookies(session)
                return payload
            finally:
                session.close()

        raise RuntimeError(f"history_fetch_failed_after_retries:{symbol}:{interval}:{conid}")

    def _write_bars(self, bars: List[Dict]) -> int:
        written = 0
        for bar_data in bars:
            if self.data_writer and self.data_writer.write_bar(bar_data):
                written += 1
        with self._count_lock:
            self._backfill_count += written
        return written

    def _get_latest_stored_bar_ms(self, symbol: str, interval: str) -> int:
        if not self.pb_client:
            return 0

        normalized = normalize_interval(interval)
        safe_symbol = str(symbol or "").upper().replace('"', '\\"')
        safe_interval = normalized.replace('"', '\\"')
        safe_environment = str(ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        try:
            rows = self.pb_client.get_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{safe_symbol}" && '
                    f'interval = "{safe_interval}" && '
                    f'environment = "{safe_environment}"'
                ),
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
            "latest_stored_ms": 0,
            "oldest_loaded_ms": 0,
            "gap_count": 0,
            "gap_examples": [],
        }
        if not self.pb_client:
            return snapshot

        safe_symbol = snapshot["symbol"].replace('"', '\\"')
        safe_interval = normalized.replace('"', '\\"')
        safe_environment = str(ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        bars_needed = max(1, int(min_bars or 0), int(gap_lookback or 0))
        max_pages = max(1, min(5, (bars_needed + 199) // 200))

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
        snapshot["latest_stored_ms"] = int(rows[0].get("bar_time_ms", 0) or 0)
        snapshot["oldest_loaded_ms"] = int(rows[-1].get("bar_time_ms", 0) or 0)

        recent_rows = list(reversed(rows[: max(3, int(gap_lookback or 0))]))
        expected_ms = interval_to_ms(normalized)
        for index in range(1, len(recent_rows)):
            prev = recent_rows[index - 1]
            curr = recent_rows[index]
            if str(prev.get("session_type", "") or "") != "regular":
                continue
            if str(curr.get("session_type", "") or "") != "regular":
                continue

            prev_ms = int(prev.get("bar_time_ms", 0) or 0)
            curr_ms = int(curr.get("bar_time_ms", 0) or 0)
            delta_ms = curr_ms - prev_ms
            if delta_ms > expected_ms and delta_ms <= (6 * expected_ms):
                snapshot["gap_count"] += 1
                if len(snapshot["gap_examples"]) < 4:
                    snapshot["gap_examples"].append({
                        "prev_us_time": str(prev.get("us_time", "") or ""),
                        "next_us_time": str(curr.get("us_time", "") or ""),
                        "missing_points": max(int(round(delta_ms / expected_ms)) - 1, 1),
                    })

        return snapshot

    def fetch_history(
        self,
        conid: int,
        symbol: str,
        interval: str = "5m",
        exchange: str = "",
        repair: bool = False,
    ) -> List[Dict]:
        normalized = normalize_interval(interval)
        period, bar_size = PERIOD_MAP.get(normalized, PERIOD_MAP["5m"])

        try:
            data = self._request_history_json(conid, symbol, normalized, period, bar_size)
            bars = data.get("data", [])
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
            if data.get("mktDataDelay") is not None:
                fetch_meta["mkt_data_delay"] = data.get("mktDataDelay")
            if data.get("mdAvailability"):
                fetch_meta["md_availability"] = data.get("mdAvailability")
            if data.get("points") is not None:
                fetch_meta["points"] = data.get("points")

            for bar in bars:
                raw_bar_time = int(bar.get("t", 0) or 0)
                bar_time_ms = raw_bar_time if raw_bar_time > 1_000_000_000_000 else raw_bar_time * 1000
                us_time = format_us_time(bar_time_ms)
                payload = {
                    "symbol": symbol,
                    "conid": conid,
                    "environment": ENVIRONMENT,
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
    ) -> int:
        bars = self.fetch_history(conid, symbol, interval=interval, exchange=exchange, repair=repair)
        written = self._write_bars(bars)
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
    ) -> Dict[str, int]:
        results = {}
        for interval in self._resolve_intervals(intervals):
            written = self.backfill_symbol(conid, symbol, interval=interval, exchange=exchange, repair=repair)
            results[normalize_interval(interval)] = written
            if self.interval_delay > 0:
                time.sleep(self.interval_delay)
        return results

    def _fetch_symbol_all_intervals(
        self,
        conid: int,
        symbol: str,
        exchange: str,
        intervals: Sequence[str],
        repair: bool,
    ) -> Dict[str, List[Dict]]:
        fetched = {}
        for interval in self._resolve_intervals(intervals):
            fetched[interval] = self.fetch_history(conid, symbol, interval=interval, exchange=exchange, repair=repair)
            if self.interval_delay > 0:
                time.sleep(self.interval_delay)
        return fetched

    def backfill_all(
        self,
        conid_map: Dict[str, int],
        symbol_meta: Optional[Dict[str, Dict[str, str]]] = None,
        intervals: Optional[List[str]] = None,
        repair_symbols: Optional[Sequence[str]] = None,
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

        worker_count = min(self.max_concurrency, len(conid_map))
        logger.info(
            "Starting history backfill: symbols=%d, tasks=%d, intervals=%s, workers=%d",
            len(conid_map),
            len(conid_map) * len(interval_list),
            ",".join(interval_list),
            worker_count,
        )

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ibkr-backfill") as executor:
            future_map = {
                executor.submit(
                    self._fetch_symbol_all_intervals,
                    conid,
                    symbol,
                    str((metadata.get(symbol) or {}).get("exchange") or ""),
                    interval_list,
                    symbol in repair_set,
                ): symbol
                for symbol, conid in conid_map.items()
            }

            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    fetched = future.result()
                    for interval, bars in fetched.items():
                        written = self._write_bars(bars)
                        results[symbol][normalize_interval(interval)] = written
                        logger.info(
                            "Backfill %s/%s complete: %d/%d bars written",
                            symbol,
                            normalize_interval(interval),
                            written,
                            len(bars),
                        )
                except Exception as e:
                    logger.error("Backfill task failed for %s: %s", symbol, e)
        return results

    def status(self) -> dict:
        return {
            "total_backfilled": self._backfill_count,
            "request_count": self._request_count,
            "retry_count": self._retry_count,
            "throttle_count": self._throttle_count,
            "environment": ENVIRONMENT,
            "intervals": list(self.default_intervals),
            "max_concurrency": self.max_concurrency,
            "request_spacing_s": self.request_spacing,
        }
