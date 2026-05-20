"""
Roll higher-timeframe bars from closed 5-minute bars.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .timeframe_utils import (
    HIGHER_INTERVALS,
    bar_close_ms,
    bucket_start_ms,
    build_runtime_timestamps,
    classify_session,
    format_cn_time,
    format_us_time,
    interval_to_ms,
)


class TimeframeBarBuilder:
    def __init__(self, target_intervals=None):
        self.target_intervals = tuple(target_intervals or HIGHER_INTERVALS)
        self._current: Dict[Tuple[str, str], dict] = {}

    def consume(self, base_bar: dict) -> List[dict]:
        normalized_base = str(base_bar.get("interval") or "").strip().lower()
        if normalized_base != "5m":
            return []

        symbol = str(base_bar.get("symbol") or "").upper()
        if not symbol:
            return []

        completed = []
        for interval in self.target_intervals:
            closed = self._consume_interval(interval, base_bar)
            if closed:
                completed.append(closed)
        return completed

    def reset(self):
        self._current.clear()

    def _consume_interval(self, interval: str, base_bar: dict):
        symbol = str(base_bar.get("symbol") or "").upper()
        bucket_ms = bucket_start_ms(int(base_bar["bar_time_ms"]), interval)
        key = (symbol, interval)
        current = self._current.get(key)

        if current is None:
            current = self._init_bucket(interval, bucket_ms, base_bar)
            self._current[key] = current
            return self._finalize_if_closed(key, current, base_bar)

        if int(current["bar_time_ms"]) != bucket_ms:
            closed = self._finalize_bucket(current, base_bar)
            self._current[key] = self._init_bucket(interval, bucket_ms, base_bar)
            return closed

        self._merge_bucket(current, base_bar)
        return self._finalize_if_closed(key, current, base_bar)

    def _init_bucket(self, interval: str, bucket_ms: int, base_bar: dict) -> dict:
        extra = dict(base_bar.get("extra") or {})
        return {
            "symbol": base_bar["symbol"],
            "environment": base_bar.get("environment", "live"),
            "exchange": base_bar.get("exchange", ""),
            "interval": interval,
            "open": float(base_bar["open"]),
            "high": float(base_bar["high"]),
            "low": float(base_bar["low"]),
            "close": float(base_bar["close"]),
            "volume": float(base_bar.get("volume", 0) or 0),
            "session_type": classify_session(bar_time_ms=bucket_ms),
            "us_time": format_us_time(bucket_ms),
            "cn_time": format_cn_time(bucket_ms),
            "bar_time_ms": bucket_ms,
            "extra": {
                "source": "ibkr_5m_rollup",
                "component_interval": "5m",
                "component_count": 1,
                "last_component_bar_time_ms": int(base_bar["bar_time_ms"]),
                "last_component_close": float(base_bar["close"]),
                "base_session_type": base_bar.get("session_type", ""),
                "base_extra_source": extra.get("source", ""),
            },
        }

    def _merge_bucket(self, target: dict, base_bar: dict):
        target["high"] = max(float(target["high"]), float(base_bar["high"]))
        target["low"] = min(float(target["low"]), float(base_bar["low"]))
        target["close"] = float(base_bar["close"])
        target["volume"] = float(target["volume"]) + float(base_bar.get("volume", 0) or 0)
        extra = target.setdefault("extra", {})
        extra["component_count"] = int(extra.get("component_count", 0)) + 1
        extra["last_component_bar_time_ms"] = int(base_bar["bar_time_ms"])
        extra["last_component_close"] = float(base_bar["close"])

    def _finalize_if_closed(self, key: Tuple[str, str], current: dict, base_bar: dict):
        interval = str(current.get("interval") or key[1])
        bucket_end_ms = (
            bar_close_ms(int(current["bar_time_ms"]), interval)
            if interval == "1d"
            else int(current["bar_time_ms"]) + interval_to_ms(interval)
        )
        base_close_ms = int(base_bar["bar_time_ms"]) + interval_to_ms("5m")
        if base_close_ms < bucket_end_ms:
            return None
        self._current.pop(key, None)
        return self._finalize_bucket(current, base_bar, closed_by_bar_time_ms=bucket_end_ms)

    def _finalize_bucket(self, current: dict, closing_bar: dict, *, closed_by_bar_time_ms: int | None = None) -> dict:
        extra = dict(current.get("extra") or {})
        extra.update(build_runtime_timestamps())
        extra["source"] = "ibkr_5m_rollup"
        extra["closed_by_bar_time_ms"] = int(closed_by_bar_time_ms or closing_bar["bar_time_ms"])
        current["extra"] = extra
        current["session_type"] = current.get("session_type") or classify_session(
            us_time=current.get("us_time", "")
        )
        return current
