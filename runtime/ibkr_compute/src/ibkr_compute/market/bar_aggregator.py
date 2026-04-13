"""
实时 tick → 5min OHLCV K线聚合
- 按 ET 时间对齐 5 分钟窗口
- bar 关闭时触发回调 (写入PB + 指标计算)
- 支持多 symbol 并发聚合
"""

import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, Optional

from .timeframe_utils import classify_session, format_cn_time, format_us_time

logger = logging.getLogger(__name__)

ET = timezone(timedelta(hours=-4))
BAR_INTERVAL_SECONDS = 300  # 5 minutes


class LiveBar:
    __slots__ = (
        "symbol",
        "conid",
        "interval_start",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "tick_count",
        "last_update",
        "volume_update_count",
        "volume_source",
    )

    def __init__(self, symbol: str, conid: int, interval_start: datetime):
        self.symbol = symbol
        self.conid = conid
        self.interval_start = interval_start
        self.open = 0.0
        self.high = 0.0
        self.low = 0.0
        self.close = 0.0
        self.volume = 0.0
        self.tick_count = 0
        self.last_update = 0.0
        self.volume_update_count = 0
        self.volume_source = ""

    def update(self, price: float, volume: float = 0.0, volume_source: str = ""):
        if price <= 0:
            return

        if self.tick_count == 0:
            self.open = price
            self.high = price
            self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)

        self.close = price
        if volume > 0:
            self.volume += volume
            self.volume_update_count += 1
            if volume_source:
                self.volume_source = volume_source
        self.tick_count += 1
        self.last_update = time.time()

    def to_dict(self) -> dict:
        bar_time_ms = int(self.interval_start.timestamp() * 1000)
        us_time = format_us_time(bar_time_ms)

        return {
            "symbol": self.symbol,
            "conid": self.conid,
            "interval": "5m",
            "open": round(self.open, 4),
            "high": round(self.high, 4),
            "low": round(self.low, 4),
            "close": round(self.close, 4),
            "volume": round(self.volume, 2),
            "bar_time_ms": bar_time_ms,
            "us_time": us_time,
            "cn_time": format_cn_time(bar_time_ms),
            "session_type": classify_session(us_time, bar_time_ms),
            "tick_count": self.tick_count,
            "source": "ws",
            "extra": {
                "volume_source": self.volume_source or "",
                "volume_updates": self.volume_update_count,
            },
        }

    @property
    def is_valid(self) -> bool:
        return (self.tick_count > 0 and self.open > 0 and
                self.high >= max(self.open, self.close) and
                self.low <= min(self.open, self.close) and
                self.low > 0)


def get_bar_interval_start(et_now: datetime) -> datetime:
    minute_aligned = et_now.minute - (et_now.minute % 5)
    return et_now.replace(minute=minute_aligned, second=0, microsecond=0)


def _tick_time_to_et(tick_data: dict) -> datetime:
    updated_ms = tick_data.get("_updated")
    if updated_ms is not None:
        try:
            return datetime.fromtimestamp(int(updated_ms) / 1000, ET)
        except (TypeError, ValueError, OSError):
            pass
    return datetime.now(ET)


class BarAggregator:
    def __init__(self, conid_to_symbol: Dict[int, str] = None,
                 on_bar_close: Callable = None):
        self.conid_to_symbol = conid_to_symbol or {}
        self.on_bar_close = on_bar_close

        self._current_bars: Dict[int, LiveBar] = {}  # conid -> LiveBar
        self._bar_count = 0
        self._tick_count = 0
        self._lock = threading.Lock()

    def set_symbol_map(self, conid_to_symbol: Dict[int, str]):
        with self._lock:
            self.conid_to_symbol = conid_to_symbol

    def remove_conids(self, conids):
        with self._lock:
            for conid in list(conids or []):
                try:
                    self._current_bars.pop(int(conid), None)
                except (TypeError, ValueError):
                    continue

    def reset(self):
        with self._lock:
            self._current_bars.clear()

    def get_preview_bar(self, symbol: str) -> dict | None:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return None
        now_ts = time.time()
        with self._lock:
            items = list(self._current_bars.values())
        for bar in items:
            if str(bar.symbol or "").upper() != normalized_symbol:
                continue
            age_seconds = max(0.0, now_ts - float(bar.last_update or 0.0))
            if bar.last_update and age_seconds > (BAR_INTERVAL_SECONDS * 2):
                return None
            return {
                "symbol": normalized_symbol,
                "conid": int(bar.conid),
                "interval": "5m",
                "bar_time_ms": int(bar.interval_start.timestamp() * 1000),
                "us_time": format_us_time(int(bar.interval_start.timestamp() * 1000)),
                "cn_time": format_cn_time(int(bar.interval_start.timestamp() * 1000)),
                "session_type": classify_session(bar_time_ms=int(bar.interval_start.timestamp() * 1000)),
                "interval_start": bar.interval_start.strftime("%H:%M"),
                "open": round(float(bar.open), 4),
                "high": round(float(bar.high), 4),
                "low": round(float(bar.low), 4),
                "close": round(float(bar.close), 4),
                "volume": round(float(bar.volume), 2),
                "volume_updates": int(bar.volume_update_count),
                "volume_source": bar.volume_source or "",
                "tick_count": int(bar.tick_count),
                "last_update_age_s": round(age_seconds, 1),
            }
        return None

    def on_tick(self, tick_data: dict):
        conid = tick_data.get("conid") or tick_data.get("conidEx")
        if not conid:
            return

        conid = int(conid)
        symbol = self.conid_to_symbol.get(conid)
        if not symbol:
            return

        last_price = self._extract_price(tick_data)
        volume, volume_source = self._extract_volume(tick_data)

        if last_price is None or last_price <= 0:
            return

        et_now = _tick_time_to_et(tick_data)
        interval_start = get_bar_interval_start(et_now)
        bars_to_close = []

        with self._lock:
            self._tick_count += 1
            current = self._current_bars.get(conid)

            if current is None or current.interval_start != interval_start:
                if current is not None and current.is_valid:
                    bars_to_close.append(current)
                current = LiveBar(symbol, conid, interval_start)
                self._current_bars[conid] = current

            current.update(last_price, volume, volume_source)

        for bar in bars_to_close:
            self._close_bar(bar)

    def _extract_price(self, tick: dict) -> Optional[float]:
        for field in ("31", "last_price", "last"):
            val = tick.get(field)
            if val is not None:
                try:
                    price = float(str(val).replace("C", "").replace("H", ""))
                    if price > 0:
                        return price
                except (ValueError, TypeError):
                    continue
        return None

    def _coerce_numeric(self, value) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip().replace(",", "")
        if not text:
            return None

        multiplier = 1.0
        upper = text.upper()
        if upper.endswith("K"):
            multiplier = 1_000.0
            text = text[:-1]
        elif upper.endswith("M"):
            multiplier = 1_000_000.0
            text = text[:-1]
        elif upper.endswith("B"):
            multiplier = 1_000_000_000.0
            text = text[:-1]

        try:
            return float(text) * multiplier
        except (ValueError, TypeError):
            return None

    def _extract_volume(self, tick: dict) -> tuple[float, str]:
        # 7059 is IBKR's "last size" field for websocket market data. We treat
        # it as the per-trade size contribution for the current 5m bar volume.
        for field in ("7059", "last_size", "lastSize", "size", "87", "volume", "vol"):
            value = self._coerce_numeric(tick.get(field))
            if value is not None and value > 0:
                return float(value), field
        return 0.0, ""

    def _close_bar(self, bar: LiveBar):
        with self._lock:
            self._bar_count += 1
        bar_data = bar.to_dict()
        logger.info("Bar closed: %s %s O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f ticks=%d",
                     bar.symbol, bar_data["us_time"],
                     bar.open, bar.high, bar.low, bar.close,
                     bar.volume, bar.tick_count)

        if self.on_bar_close:
            try:
                self.on_bar_close(bar_data)
            except Exception as e:
                logger.error("on_bar_close callback error for %s: %s", bar.symbol, e)

    def force_close_due(self, et_now: Optional[datetime] = None) -> int:
        current_interval_start = get_bar_interval_start(et_now or datetime.now(ET))
        bars_to_close = []

        with self._lock:
            for conid, bar in list(self._current_bars.items()):
                if bar.interval_start >= current_interval_start:
                    continue
                self._current_bars.pop(conid, None)
                if bar.is_valid:
                    bars_to_close.append(bar)

        for bar in bars_to_close:
            self._close_bar(bar)
        return len(bars_to_close)

    def force_close_all(self):
        bars_to_close = []
        with self._lock:
            for _, bar in list(self._current_bars.items()):
                if bar.is_valid:
                    bars_to_close.append(bar)
            self._current_bars.clear()
        for bar in bars_to_close:
            self._close_bar(bar)

    def status(self) -> dict:
        active_bars = {}
        stale_symbols = 0
        now_ts = time.time()
        with self._lock:
            items = list(self._current_bars.items())
            total_bars_closed = self._bar_count
            total_ticks = self._tick_count
        for _, bar in items:
            age_seconds = max(0.0, now_ts - float(bar.last_update or 0.0))
            if bar.last_update and age_seconds > (BAR_INTERVAL_SECONDS * 2):
                stale_symbols += 1
                continue
            active_bars[bar.symbol] = {
                "bar_time_ms": int(bar.interval_start.timestamp() * 1000),
                "interval_start": bar.interval_start.strftime("%H:%M"),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": round(bar.volume, 2),
                "volume_updates": bar.volume_update_count,
                "volume_source": bar.volume_source or "",
                "tick_count": bar.tick_count,
                "last_update_age_s": round(age_seconds, 1),
            }

        return {
            "mode": "preview_only",
            "active_symbols": len(items),
            "active_symbols_visible": len(active_bars),
            "stale_symbols": stale_symbols,
            "total_bars_closed": total_bars_closed,
            "total_ticks": total_ticks,
            "active_bars": active_bars,
        }
