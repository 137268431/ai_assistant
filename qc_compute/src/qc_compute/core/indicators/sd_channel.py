"""SD Channel — Standard Deviation Channel indicator"""

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

        self._prev_sd_lower: bool = False
        self._prev_sd_upper: bool = False

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
        if rt_slope is not None and rt_slope > 0:
            sd_trend = 1
        elif rt_slope is not None and rt_slope < 0:
            sd_trend = -1
        else:
            sd_trend = 0

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
        }
