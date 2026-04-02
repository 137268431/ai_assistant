"""
K线数据写入 PocketBase
- 去重: 复合键 (symbol, interval, bar_time_ms)
- 验证: OHLC 关系校验, 价格合理性检查
"""

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class DataWriter:
    def __init__(self, pb_client, collection: str = "ibkr_bars"):
        self.pb_client = pb_client
        self.collection = collection
        self._write_count = 0
        self._skip_count = 0
        self._error_count = 0

    def write_bar(self, bar_data: dict) -> bool:
        if not self._validate_bar(bar_data):
            logger.warning("Bar validation failed: %s %s", bar_data.get("symbol"), bar_data.get("us_time"))
            self._error_count += 1
            return False

        if self._is_duplicate(bar_data):
            self._skip_count += 1
            return False

        try:
            self.pb_client.create_record(self.collection, {
                "symbol": bar_data["symbol"],
                "interval": bar_data.get("interval", "5m"),
                "bar_time_ms": bar_data["bar_time_ms"],
                "open": bar_data["open"],
                "high": bar_data["high"],
                "low": bar_data["low"],
                "close": bar_data["close"],
                "volume": bar_data.get("volume", 0),
                "us_time": bar_data.get("us_time", ""),
                "cn_time": bar_data.get("cn_time", ""),
                "source": bar_data.get("source", "ws"),
                "tick_count": bar_data.get("tick_count", 0),
            })
            self._write_count += 1
            return True
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                self._skip_count += 1
                return False
            logger.error("Failed to write bar %s %s: %s",
                         bar_data.get("symbol"), bar_data.get("us_time"), e)
            self._error_count += 1
            return False

    def _validate_bar(self, bar: dict) -> bool:
        required = ["symbol", "bar_time_ms", "open", "high", "low", "close"]
        for field in required:
            if field not in bar or bar[field] is None:
                return False

        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]

        if any(v <= 0 for v in [o, h, l, c]):
            return False

        if h < max(o, c) or l > min(o, c):
            logger.warning("OHLC relationship invalid: %s O=%.2f H=%.2f L=%.2f C=%.2f",
                           bar.get("symbol"), o, h, l, c)
            return False

        if h > 0 and l > 0:
            spread_pct = (h - l) / l * 100
            if spread_pct > 50:
                logger.warning("Abnormal spread %.1f%% for %s", spread_pct, bar.get("symbol"))
                return False

        return True

    def _is_duplicate(self, bar: dict) -> bool:
        try:
            symbol = bar["symbol"]
            interval = bar.get("interval", "5m")
            bar_time_ms = bar["bar_time_ms"]

            existing = self.pb_client.get_records(
                self.collection,
                filter=f'symbol = "{symbol}" && interval = "{interval}" && bar_time_ms = {bar_time_ms}',
                per_page=1,
            )
            return len(existing) > 0
        except Exception as e:
            logger.debug("Dedup check failed: %s", e)
            return False

    def status(self) -> dict:
        return {
            "writes": self._write_count,
            "skips_dedup": self._skip_count,
            "errors": self._error_count,
            "collection": self.collection,
        }
