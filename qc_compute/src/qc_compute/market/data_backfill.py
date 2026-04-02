"""
历史数据回补
- 启动时补全缺失 K 线
- WebSocket 断线恢复后补全缺口
- 通过 IBKR Client Portal REST API 拉取历史 bars
"""

import os
import time
import logging
import requests
from typing import List, Dict, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000")
ET = timezone(timedelta(hours=-4))

PERIOD_MAP = {
    "5m": ("2d", "5min"),
    "15m": ("5d", "15min"),
    "30m": ("10d", "30min"),
    "1h": ("14d", "1h"),
    "4h": ("14d", "4h"),
    "1d": ("14d", "1d"),
}

REQUEST_DELAY = 0.5


class DataBackfill:
    def __init__(self, gateway_url: str = None, data_writer=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.data_writer = data_writer
        self._session = requests.Session()
        self._session.verify = False
        self._backfill_count = 0

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def fetch_history(self, conid: int, symbol: str, interval: str = "5m") -> List[Dict]:
        period, bar_size = PERIOD_MAP.get(interval, ("2d", "5min"))

        try:
            resp = self._session.get(
                self._api_url("/iserver/marketdata/history"),
                params={
                    "conid": conid,
                    "period": period,
                    "bar": bar_size,
                    "outsideRth": "false",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            bars = data.get("data", [])
            result = []
            for bar in bars:
                bar_time = bar.get("t", 0)
                if isinstance(bar_time, str):
                    bar_time = int(bar_time)

                bar_data = {
                    "symbol": symbol,
                    "conid": conid,
                    "interval": interval,
                    "open": bar.get("o", 0),
                    "high": bar.get("h", 0),
                    "low": bar.get("l", 0),
                    "close": bar.get("c", 0),
                    "volume": bar.get("v", 0),
                    "bar_time_ms": bar_time,
                    "us_time": datetime.fromtimestamp(bar_time / 1000, ET).strftime("%Y-%m-%d %H:%M:%S")
                    if bar_time > 1e12 else datetime.fromtimestamp(bar_time, ET).strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "backfill",
                }

                cn_time_offset = timedelta(hours=12)
                if bar_time > 1e12:
                    et_dt = datetime.fromtimestamp(bar_time / 1000, ET)
                else:
                    et_dt = datetime.fromtimestamp(bar_time, ET)
                bar_data["cn_time"] = (et_dt + cn_time_offset).strftime("%Y-%m-%d %H:%M:%S")

                if bar_time > 1e12:
                    bar_data["bar_time_ms"] = bar_time
                else:
                    bar_data["bar_time_ms"] = bar_time * 1000

                result.append(bar_data)

            logger.info("Fetched %d bars for %s/%s (period=%s)", len(result), symbol, interval, period)
            return result

        except Exception as e:
            logger.error("History fetch failed for %s (conid=%d, interval=%s): %s",
                         symbol, conid, interval, e)
            return []

    def backfill_symbol(self, conid: int, symbol: str, interval: str = "5m") -> int:
        bars = self.fetch_history(conid, symbol, interval)
        written = 0
        for bar_data in bars:
            if self.data_writer and self.data_writer.write_bar(bar_data):
                written += 1
        self._backfill_count += written
        logger.info("Backfill %s/%s: %d/%d bars written", symbol, interval, written, len(bars))
        return written

    def backfill_all(self, conid_map: Dict[str, int], interval: str = "5m") -> Dict[str, int]:
        results = {}
        for symbol, conid in conid_map.items():
            written = self.backfill_symbol(conid, symbol, interval)
            results[symbol] = written
            time.sleep(REQUEST_DELAY)
        return results

    def status(self) -> dict:
        return {
            "total_backfilled": self._backfill_count,
        }
