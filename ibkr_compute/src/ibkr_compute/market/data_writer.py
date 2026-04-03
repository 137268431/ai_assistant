"""
Write validated bar records into PocketBase via the IBKR custom hook.
"""

from __future__ import annotations

import os
import logging
from typing import Dict

from .timeframe_utils import build_runtime_timestamps, classify_session, normalize_interval

logger = logging.getLogger(__name__)

DEFAULT_ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")


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

        payload = self._build_payload(bar_data)
        try:
            result = self.pb_client.upsert_bars([payload])
            if not result.get("ok", False):
                raise RuntimeError(result.get("error") or "bar_upsert_failed")

            if int(result.get("created", 0) or 0) > 0 or int(result.get("updated", 0) or 0) > 0:
                self._write_count += 1
            else:
                self._skip_count += 1
            return True
        except Exception as e:
            logger.error(
                "Failed to write bar %s %s: %s",
                bar_data.get("symbol"),
                bar_data.get("us_time"),
                e,
            )
            self._error_count += 1
            return False

    def _build_payload(self, bar: Dict) -> Dict:
        base_extra = dict(bar.get("extra") or {})
        base_extra.setdefault("source", bar.get("source", "ibkr_compute"))
        base_extra.setdefault("tick_count", int(bar.get("tick_count", 0) or 0))
        base_extra.setdefault("conid", int(bar.get("conid", 0) or 0))
        base_extra.setdefault("interval", normalize_interval(bar.get("interval", "5m")))
        base_extra.setdefault("session_type", classify_session(bar.get("us_time", ""), bar.get("bar_time_ms")))
        base_extra.update(build_runtime_timestamps())

        return {
            "symbol": str(bar["symbol"]).upper(),
            "environment": str(bar.get("environment") or DEFAULT_ENVIRONMENT).strip().lower() or DEFAULT_ENVIRONMENT,
            "exchange": str(bar.get("exchange") or "").strip().upper(),
            "interval": normalize_interval(bar.get("interval", "5m")),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar.get("volume", 0) or 0),
            "session_type": str(bar.get("session_type") or base_extra["session_type"]),
            "us_time": str(bar.get("us_time") or ""),
            "cn_time": str(bar.get("cn_time") or ""),
            "bar_time_ms": int(bar["bar_time_ms"]),
            "extra": base_extra,
        }

    def _validate_bar(self, bar: dict) -> bool:
        required = ["symbol", "bar_time_ms", "open", "high", "low", "close"]
        for field in required:
            if field not in bar or bar[field] is None:
                return False

        try:
            o = float(bar["open"])
            h = float(bar["high"])
            l = float(bar["low"])
            c = float(bar["close"])
        except (TypeError, ValueError):
            return False

        if any(v <= 0 for v in (o, h, l, c)):
            return False

        if h < max(o, c) or l > min(o, c):
            logger.warning(
                "OHLC relationship invalid: %s O=%.2f H=%.2f L=%.2f C=%.2f",
                bar.get("symbol"),
                o,
                h,
                l,
                c,
            )
            return False

        if l > 0:
            spread_pct = (h - l) / l * 100
            if spread_pct > 50:
                logger.warning("Abnormal spread %.1f%% for %s", spread_pct, bar.get("symbol"))
                return False

        return True

    def status(self) -> dict:
        return {
            "writes": self._write_count,
            "skips_dedup": self._skip_count,
            "errors": self._error_count,
            "collection": self.collection,
        }
