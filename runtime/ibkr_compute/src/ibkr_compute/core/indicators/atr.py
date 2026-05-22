"""ATR + VWAP indicator"""

from collections import deque
from datetime import datetime

from .base import BaseIndicator
from ..time_utils import ET


class ATRIndicator(BaseIndicator):

    def __init__(self, params: dict = None, max_history: int = None):
        super().__init__(params, max_history)
        self._atr_length: int = self.params.get("atr_length", 10)
        self._atr_smoothing: str = self.params.get("atr_smoothing", "RMA")
        self._atr_multiplier: float = self.params.get("atr_multiplier", 1.5)
        self._orb_bars: int = max(1, int(self.params.get("orb_bars", 6)))
        self._atr_pct_percentile_lookback: int = max(
            1,
            int(self.params.get("atr_pct_percentile_lookback", 100)),
        )

        # ATR running state
        self._atr_raw: float = 0.0
        self._tr_values: deque = deque(maxlen=self._atr_length)
        self._atr_pct_values: deque = deque(maxlen=self._atr_pct_percentile_lookback)

        # VWAP running state (reset daily)
        self._vwap_cum_vol: float = 0.0
        self._vwap_cum_pv: float = 0.0
        self._hlc3_values: deque = deque(maxlen=self._max)
        self._current_date: str | None = None
        self._regular_bar_index: int = 0
        self._orb_high: float | None = None
        self._orb_low: float | None = None
        self._rvol_values: deque = deque(maxlen=20)

    def is_ready(self) -> bool:
        return self.bar_count >= max(self._atr_length + 1, self._atr_pct_percentile_lookback)

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
        self._atr_pct_values.append(atr_pct)
        atr_pct_percentile = self._percent_rank_latest(self._atr_pct_values)

        # VWAP (reset on new trading day)
        bar_date = self._extract_date()
        if bar_date is not None and bar_date != self._current_date:
            self._vwap_cum_vol = 0.0
            self._vwap_cum_pv = 0.0
            self._hlc3_values.clear()
            self._regular_bar_index = 0
            self._orb_high = None
            self._orb_low = None
            self._rvol_values.clear()
            self._current_date = bar_date

        session_type = self._extract_session_type()
        is_regular = session_type == "regular"
        if is_regular:
            self._regular_bar_index += 1
            if self._regular_bar_index <= self._orb_bars:
                self._orb_high = high if self._orb_high is None else max(self._orb_high, high)
                self._orb_low = low if self._orb_low is None else min(self._orb_low, low)

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
        prev_vol_avg = (sum(self._rvol_values) / len(self._rvol_values)) if self._rvol_values else 0.0
        rvol_20 = (vol / prev_vol_avg) if prev_vol_avg > 0 else 1.0
        self._rvol_values.append(vol)
        dollar_volume = close * vol
        orb_complete = is_regular and self._regular_bar_index >= self._orb_bars
        orb_high = self._orb_high if self._orb_high is not None else high
        orb_low = self._orb_low if self._orb_low is not None else low
        orb_can_break = is_regular and self._regular_bar_index > self._orb_bars
        orb_breakout_up = bool(orb_can_break and close > orb_high)
        orb_breakout_down = bool(orb_can_break and close < orb_low)

        self._output = {
            "atr": atr_val,
            "atr_raw": self._atr_raw,
            "atr_pct": atr_pct,
            "atr_pct_percentile": atr_pct_percentile,
            "vwap": vwap,
            "vwap_dev": vwap_dev,
            "vwap_upper1": vwap_upper1,
            "vwap_lower1": vwap_lower1,
            "vwap_upper2": vwap_upper2,
            "vwap_lower2": vwap_lower2,
            "vwap_dist": vwap_dist,
            "vwap_bullish": vwap_bullish,
            "regular_bar_index": self._regular_bar_index,
            "orb_high": orb_high,
            "orb_low": orb_low,
            "orb_complete": orb_complete,
            "orb_breakout_up": orb_breakout_up,
            "orb_breakout_down": orb_breakout_down,
            "rvol_20": rvol_20,
            "dollar_volume": dollar_volume,
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

    @staticmethod
    def _percent_rank_latest(values: deque) -> float:
        window = list(values)
        if not window:
            return 0.0
        latest = window[-1]
        less = sum(1 for item in window if item < latest)
        equal = sum(1 for item in window if item == latest)
        return float((less + 0.5 * equal) / len(window) * 100.0)

    def _extract_date(self) -> str | None:
        """Extract the US market date from the last bar when available."""
        if not hasattr(self, "_last_bar"):
            return None
        for key in ("us_time", "timestamp", "time", "date"):
            ts = self._last_bar.get(key)
            if ts:
                return str(ts)[:10]
        bar_time_ms = int(self._last_bar.get("bar_time_ms", 0) or 0)
        if bar_time_ms > 0:
            try:
                return datetime.fromtimestamp(bar_time_ms / 1000.0, ET).strftime("%Y-%m-%d")
            except (OSError, TypeError, ValueError):
                return None
        ts = self._last_bar.get("cn_time")
        if ts is None:
            return None
        return str(ts)[:10]

    def _extract_session_type(self) -> str:
        if not hasattr(self, "_last_bar"):
            return "regular"
        return str(self._last_bar.get("session_type") or "regular").strip().lower()

    def _push_bar(self, bar: dict):
        self._last_bar = bar
        super()._push_bar(bar)

    def reset(self):
        super().reset()
        self._atr_raw = 0.0
        self._tr_values.clear()
        self._atr_pct_values.clear()
        self._vwap_cum_vol = 0.0
        self._vwap_cum_pv = 0.0
        self._hlc3_values.clear()
        self._current_date = None
        self._regular_bar_index = 0
        self._orb_high = None
        self._orb_low = None
        self._rvol_values.clear()
