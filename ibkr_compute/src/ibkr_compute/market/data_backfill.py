"""
Historical bar backfill across the IBKR timeframes used by the pipeline.
"""

from __future__ import annotations

import os
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional

import requests

from .timeframe_utils import (
    COMPUTE_INTERVALS,
    build_runtime_timestamps,
    classify_session,
    format_cn_time,
    format_us_time,
    normalize_interval,
)

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000")
ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")

PERIOD_MAP = {
    "5m": ("4d", "5min"),
    "15m": ("10d", "15min"),
    "30m": ("20d", "30min"),
    "1h": ("40d", "1h"),
    "4h": ("120d", "4h"),
    "1d": ("2y", "1d"),
}

REQUEST_DELAY = 0.35


class DataBackfill:
    def __init__(self, gateway_url: str = None, data_writer=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.data_writer = data_writer
        self._session = requests.Session()
        self._session.verify = False
        self._backfill_count = 0

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def fetch_history(
        self,
        conid: int,
        symbol: str,
        interval: str = "5m",
        exchange: str = "",
    ) -> List[Dict]:
        normalized = normalize_interval(interval)
        period, bar_size = PERIOD_MAP.get(normalized, PERIOD_MAP["5m"])

        try:
            resp = self._session.get(
                self._api_url("/iserver/marketdata/history"),
                params={
                    "conid": conid,
                    "period": period,
                    "bar": bar_size,
                    "outsideRth": "true",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            bars = data.get("data", [])
            result = []
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
                        "source": "ibkr_history_backfill",
                        "conid": conid,
                        "exchange": exchange,
                        "interval": normalized,
                        "outside_rth": True,
                        **build_runtime_timestamps(),
                    },
                }
                result.append(payload)

            logger.info(
                "Fetched %d bars for %s/%s (period=%s)",
                len(result),
                symbol,
                normalized,
                period,
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
    ) -> int:
        bars = self.fetch_history(conid, symbol, interval=interval, exchange=exchange)
        written = 0
        for bar_data in bars:
            if self.data_writer and self.data_writer.write_bar(bar_data):
                written += 1
        self._backfill_count += written
        logger.info(
            "Backfill %s/%s: %d/%d bars written",
            symbol,
            normalize_interval(interval),
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
    ) -> Dict[str, int]:
        results = {}
        for interval in intervals or COMPUTE_INTERVALS:
            written = self.backfill_symbol(conid, symbol, interval=interval, exchange=exchange)
            results[normalize_interval(interval)] = written
            time.sleep(REQUEST_DELAY)
        return results

    def backfill_all(
        self,
        conid_map: Dict[str, int],
        symbol_meta: Optional[Dict[str, Dict[str, str]]] = None,
        intervals: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, int]]:
        results = {}
        metadata = symbol_meta or {}
        for symbol, conid in conid_map.items():
            exchange = str((metadata.get(symbol) or {}).get("exchange") or "")
            results[symbol] = self.backfill_symbol_all_intervals(
                conid=conid,
                symbol=symbol,
                exchange=exchange,
                intervals=intervals,
            )
        return results

    def status(self) -> dict:
        return {
            "total_backfilled": self._backfill_count,
            "environment": ENVIRONMENT,
            "intervals": list(COMPUTE_INTERVALS),
        }
