"""
实时 tick → 5min OHLCV K线聚合
- 按 ET 时间对齐 5 分钟窗口
- bar 关闭时触发回调 (写入PB + 指标计算)
- 支持多 symbol 并发聚合
"""

import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

ET = timezone(timedelta(hours=-4))
BAR_INTERVAL_SECONDS = 300  # 5 minutes


class LiveBar:
    __slots__ = ("symbol", "conid", "interval_start", "open", "high", "low",
                 "close", "volume", "tick_count", "last_update")

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

    def update(self, price: float, volume: float = 0.0):
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
        self.volume = volume
        self.tick_count += 1
        self.last_update = time.time()

    def to_dict(self) -> dict:
        us_time = self.interval_start.strftime("%Y-%m-%d %H:%M:%S")
        cn_time = (self.interval_start + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
        bar_time_ms = int(self.interval_start.timestamp() * 1000)

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
            "cn_time": cn_time,
            "tick_count": self.tick_count,
            "source": "ws",
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


class BarAggregator:
    def __init__(self, conid_to_symbol: Dict[int, str] = None,
                 on_bar_close: Callable = None):
        self.conid_to_symbol = conid_to_symbol or {}
        self.on_bar_close = on_bar_close

        self._current_bars: Dict[int, LiveBar] = {}  # conid -> LiveBar
        self._bar_count = 0
        self._tick_count = 0

    def set_symbol_map(self, conid_to_symbol: Dict[int, str]):
        self.conid_to_symbol = conid_to_symbol

    def on_tick(self, tick_data: dict):
        conid = tick_data.get("conid") or tick_data.get("conidEx")
        if not conid:
            return

        conid = int(conid)
        symbol = self.conid_to_symbol.get(conid)
        if not symbol:
            return

        last_price = self._extract_price(tick_data)
        volume = self._extract_volume(tick_data)

        if last_price is None or last_price <= 0:
            return

        self._tick_count += 1

        et_now = datetime.now(ET)
        interval_start = get_bar_interval_start(et_now)

        current = self._current_bars.get(conid)

        if current is None or current.interval_start != interval_start:
            if current is not None and current.is_valid:
                self._close_bar(current)
            current = LiveBar(symbol, conid, interval_start)
            self._current_bars[conid] = current

        current.update(last_price, volume)

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

    def _extract_volume(self, tick: dict) -> float:
        for field in ("87", "volume", "vol"):
            val = tick.get(field)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return 0.0

    def _close_bar(self, bar: LiveBar):
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

    def force_close_all(self):
        for conid, bar in list(self._current_bars.items()):
            if bar.is_valid:
                self._close_bar(bar)
        self._current_bars.clear()

    def status(self) -> dict:
        active_bars = {}
        for conid, bar in self._current_bars.items():
            active_bars[bar.symbol] = {
                "interval_start": bar.interval_start.strftime("%H:%M"),
                "open": bar.open,
                "close": bar.close,
                "tick_count": bar.tick_count,
            }

        return {
            "active_symbols": len(self._current_bars),
            "total_bars_closed": self._bar_count,
            "total_ticks": self._tick_count,
            "active_bars": active_bars,
        }
