"""ATR + VWAP indicator"""

from collections import deque

from .base import BaseIndicator


class ATRIndicator(BaseIndicator):

    def __init__(self, params: dict = None, max_history: int = None):
        super().__init__(params, max_history)
        self._atr_length: int = self.params.get("atr_length", 10)
        self._atr_smoothing: str = self.params.get("atr_smoothing", "RMA")
        self._atr_multiplier: float = self.params.get("atr_multiplier", 1.5)

        # ATR running state
        self._atr_raw: float = 0.0
        self._tr_values: deque = deque(maxlen=self._atr_length)

        # VWAP running state (reset daily)
        self._vwap_cum_vol: float = 0.0
        self._vwap_cum_pv: float = 0.0
        self._hlc3_values: deque = deque(maxlen=self._max)
        self._current_date: str | None = None

    def is_ready(self) -> bool:
        return self.bar_count >= self._atr_length + 1

    def _compute(self):
        close = self.closes[-1]
        high = self.highs[-1]
        low = self.lows[-1]
        vol = self.volumes[-1]
        hlc3 = (high + low + close) / 3.0

        # True Range
        if self.bar_count >= 2:
            prev_close = self.closes[-2]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        else:
            tr = high - low

        self._tr_values.append(tr)

        # Smoothed ATR
        if self.bar_count == 1:
            self._atr_raw = tr
        elif self.bar_count <= self._atr_length:
            # Seed: simple average until we have enough bars
            self._atr_raw = sum(self._tr_values) / len(self._tr_values)
        else:
            self._atr_raw = self._smooth_step(self._atr_raw, tr, self._atr_length)

        atr_val = self._atr_raw * self._atr_multiplier
        atr_pct = (self._atr_raw / close * 100.0) if close > 0 else 0.0

        # VWAP (reset on new trading day)
        bar_date = self._extract_date()
        if bar_date is not None and bar_date != self._current_date:
            self._vwap_cum_vol = 0.0
            self._vwap_cum_pv = 0.0
            self._hlc3_values.clear()
            self._current_date = bar_date

        self._vwap_cum_pv += hlc3 * vol
        self._vwap_cum_vol += vol
        self._hlc3_values.append(hlc3)

        vwap = (self._vwap_cum_pv / self._vwap_cum_vol) if self._vwap_cum_vol > 0 else close
        vwap_dev = self.stdev(self._hlc3_values, 20) or 0.0
        vwap_upper1 = vwap + 1.0 * vwap_dev
        vwap_lower1 = vwap - 1.0 * vwap_dev
        vwap_upper2 = vwap + 2.0 * vwap_dev
        vwap_lower2 = vwap - 2.0 * vwap_dev
        vwap_dist = ((close - vwap) / vwap * 100.0) if vwap != 0 else 0.0
        vwap_bullish = close > vwap

        self._output = {
            "atr": atr_val,
            "atr_raw": self._atr_raw,
            "atr_pct": atr_pct,
            "vwap": vwap,
            "vwap_dev": vwap_dev,
            "vwap_upper1": vwap_upper1,
            "vwap_lower1": vwap_lower1,
            "vwap_upper2": vwap_upper2,
            "vwap_lower2": vwap_lower2,
            "vwap_dist": vwap_dist,
            "vwap_bullish": vwap_bullish,
        }

    def _smooth_step(self, prev: float, value: float, period: int) -> float:
        if self._atr_smoothing == "RMA":
            return self.rma_step(prev, value, period)
        elif self._atr_smoothing == "EMA":
            return self.ema_step(prev, value, period)
        elif self._atr_smoothing == "SMA":
            return float(sum(self._tr_values) / len(self._tr_values))
        elif self._atr_smoothing == "WMA":
            return self.wma(self._tr_values, period) or prev
        return self.rma_step(prev, value, period)

    def _extract_date(self) -> str | None:
        """Extract date string from the last bar's timestamp if available."""
        if not hasattr(self, "_last_bar"):
            return None
        ts = self._last_bar.get("timestamp") or self._last_bar.get("time") or self._last_bar.get("date")
        if ts is None:
            return None
        return str(ts)[:10]

    def _push_bar(self, bar: dict):
        self._last_bar = bar
        super()._push_bar(bar)

    def reset(self):
        super().reset()
        self._atr_raw = 0.0
        self._tr_values.clear()
        self._vwap_cum_vol = 0.0
        self._vwap_cum_pv = 0.0
        self._hlc3_values.clear()
        self._current_date = None
