"""SD Channel — Standard Deviation Channel indicator"""

from collections import deque

from .base import BaseIndicator


class SDChannel(BaseIndicator):

    def __init__(self, params: dict = None, max_history: int = None):
        super().__init__(params, max_history)
        p = self.params
        self.sd_length: int = int(p.get("sd_length", 128))
        self.sd_mult1: float = float(p.get("sd_mult1", 1.0))
        self.sd_mult2: float = float(p.get("sd_mult2", 2.0))
        self.sd_mult3: float = float(p.get("sd_mult3", 3.0))
        self.sd_mult4: float = float(p.get("sd_mult4", 4.0))
        self.sd_signal_band: int = int(p.get("sd_signal_band", 3))
        self.sd_filter_band: int = int(p.get("sd_filter_band", 2))
        self.sd_squeeze_lookback: int = int(p.get("sd_squeeze_lookback", 120))
        self.sd_squeeze_rank_max: float = float(p.get("sd_squeeze_rank_max", 0.25))
        self.sd_flat_slope_pct: float = float(p.get("sd_flat_slope_pct", 0.03))
        self.sd_breakout_confirm_bars: int = max(1, int(p.get("sd_breakout_confirm_bars", 1)))
        self.sd_trend_walk_min_bars: int = max(1, int(p.get("sd_trend_walk_min_bars", 2)))

        self._prev_sd_lower: bool = False
        self._prev_sd_upper: bool = False
        self._width_pct_values: deque = deque(maxlen=max(1, self.sd_squeeze_lookback))
        self._breakout_up_bars: int = 0
        self._breakout_down_bars: int = 0
        self._trend_walk_up_bars: int = 0
        self._trend_walk_down_bars: int = 0

    # ── helpers ──

    def _band_to_mult(self, band: int) -> float:
        return {1: self.sd_mult1, 2: self.sd_mult2,
                3: self.sd_mult3, 4: self.sd_mult4}.get(band, self.sd_mult1)

    def is_ready(self) -> bool:
        return self.bar_count >= self.sd_length

    # ── core ──

    def _compute(self):
        n = self.sd_length
        if len(self.closes) < n:
            return

        rt_reg = self.linreg(self.closes, n)
        rt_std_dev = self.stdev(self.closes, n)

        signal_mult = self._band_to_mult(self.sd_signal_band)
        filter_mult = self._band_to_mult(self.sd_filter_band)

        signal_upper = rt_reg + signal_mult * rt_std_dev
        signal_lower = rt_reg - signal_mult * rt_std_dev

        cur_low = self.lows[-1]
        cur_high = self.highs[-1]
        cur_close = self.closes[-1]

        # Touch detection (edge: new touch)
        raw_lower = cur_low <= signal_lower
        raw_upper = cur_high >= signal_upper
        sd_lower = raw_lower and not self._prev_sd_lower
        sd_upper = raw_upper and not self._prev_sd_upper
        self._prev_sd_lower = raw_lower
        self._prev_sd_upper = raw_upper

        # Filter zone
        filter_upper = rt_reg + filter_mult * rt_std_dev
        filter_lower = rt_reg - filter_mult * rt_std_dev
        if cur_close > filter_upper:
            sd_zone = 1
        elif cur_close < filter_lower:
            sd_zone = -1
        else:
            sd_zone = 0

        # Trend via regression slope
        rt_slope = self.linreg_slope(self.closes, n)
        sd_slope_pct = (rt_slope / rt_reg * 100.0) if rt_reg not in (None, 0) and rt_slope is not None else 0.0
        if rt_slope is not None and rt_slope > 0:
            sd_trend = 1
        elif rt_slope is not None and rt_slope < 0:
            sd_trend = -1
        else:
            sd_trend = 0

        sd_width_pct = ((signal_upper - signal_lower) / rt_reg * 100.0) if rt_reg else 0.0
        self._width_pct_values.append(sd_width_pct)
        sd_width_rank = self._rank_latest(self._width_pct_values)
        sd_close_z = ((cur_close - rt_reg) / rt_std_dev) if rt_std_dev else 0.0

        sd_squeeze_active = (
            len(self._width_pct_values) >= min(self.sd_squeeze_lookback, self.sd_length)
            and sd_width_rank <= self.sd_squeeze_rank_max
            and abs(sd_slope_pct) <= self.sd_flat_slope_pct
        )

        if cur_close > signal_upper:
            self._breakout_up_bars += 1
        else:
            self._breakout_up_bars = 0
        if cur_close < signal_lower:
            self._breakout_down_bars += 1
        else:
            self._breakout_down_bars = 0
        sd_breakout_up = self._breakout_up_bars >= self.sd_breakout_confirm_bars
        sd_breakout_down = self._breakout_down_bars >= self.sd_breakout_confirm_bars

        if cur_close >= filter_upper and sd_trend >= 0:
            self._trend_walk_up_bars += 1
        else:
            self._trend_walk_up_bars = 0
        if cur_close <= filter_lower and sd_trend <= 0:
            self._trend_walk_down_bars += 1
        else:
            self._trend_walk_down_bars = 0
        sd_trend_walk_up = self._trend_walk_up_bars >= self.sd_trend_walk_min_bars
        sd_trend_walk_down = self._trend_walk_down_bars >= self.sd_trend_walk_min_bars

        if sd_breakout_up:
            sd_regime = "breakout_up"
        elif sd_breakout_down:
            sd_regime = "breakout_down"
        elif sd_trend_walk_up:
            sd_regime = "trend_walk_up"
        elif sd_trend_walk_down:
            sd_regime = "trend_walk_down"
        elif sd_squeeze_active:
            sd_regime = "squeeze"
        elif abs(sd_slope_pct) <= self.sd_flat_slope_pct:
            sd_regime = "flat"
        elif sd_trend > 0:
            sd_regime = "trend_up"
        elif sd_trend < 0:
            sd_regime = "trend_down"
        else:
            sd_regime = "neutral"

        self._output = {
            "sd_reg": rt_reg,
            "sd_std_dev": rt_std_dev,
            "sd_signal_upper": signal_upper,
            "sd_signal_lower": signal_lower,
            "sd_lower": sd_lower,
            "sd_upper": sd_upper,
            "sd_zone": sd_zone,
            "sd_trend": sd_trend,
            "sd_filter_upper": filter_upper,
            "sd_filter_lower": filter_lower,
            "sd_width_pct": sd_width_pct,
            "sd_width_rank": sd_width_rank,
            "sd_slope_pct": sd_slope_pct,
            "sd_close_z": sd_close_z,
            "sd_squeeze_active": sd_squeeze_active,
            "sd_breakout_up": sd_breakout_up,
            "sd_breakout_down": sd_breakout_down,
            "sd_trend_walk_up": sd_trend_walk_up,
            "sd_trend_walk_down": sd_trend_walk_down,
            "sd_regime": sd_regime,
        }

    @staticmethod
    def _rank_latest(values) -> float:
        window = list(values)
        if not window:
            return 0.0
        latest = window[-1]
        if len(window) == 1 or max(window) == min(window):
            return 0.0
        return float(sum(1 for item in window if item < latest) / (len(window) - 1))

    def reset(self):
        super().reset()
        self._prev_sd_lower = False
        self._prev_sd_upper = False
        self._width_pct_values.clear()
        self._breakout_up_bars = 0
        self._breakout_down_bars = 0
        self._trend_walk_up_bars = 0
        self._trend_walk_down_bars = 0
