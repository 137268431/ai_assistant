"""Fractal Pivot Scanner — ported from Pine Script Williams Fractal + Donchian filter"""

from collections import deque

from .base import BaseIndicator


class FractalPivot(BaseIndicator):

    def __init__(self, params: dict = None, max_history: int = None):
        super().__init__(params, max_history)
        self._baseline_hist: deque = deque(maxlen=self._max)

    def is_ready(self) -> bool:
        fp = self.params.get("fractal_period", 4)
        dp = self.params.get("donchian_period", 20)
        return self.bar_count >= max(2 * fp + 5, dp)

    # ------------------------------------------------------------------ #

    def _compute(self):
        fp = self.params.get("fractal_period", 4)
        dp = self.params.get("donchian_period", 20)

        # --- Donchian channel ---
        upper_donch = self.highest(self.highs, dp)
        lower_donch = self.lowest(self.lows, dp)

        if upper_donch is not None and lower_donch is not None:
            baseline = (upper_donch + lower_donch) / 2.0
        else:
            baseline = 0.0

        self._baseline_hist.append(baseline)

        # Minimum bars required for fractal detection (including relaxation)
        min_bars = 2 * fp + 5
        if len(self.highs) < min_bars:
            self._output = {
                "fractal_bull": False,
                "fractal_bear": False,
                "upper_donch": upper_donch if upper_donch is not None else 0.0,
                "lower_donch": lower_donch if lower_donch is not None else 0.0,
                "baseline": baseline,
            }
            return

        highs = self.highs
        lows = self.lows

        # Center of the fractal window is at fractalPeriod bars back
        # Pine: high[fp]  →  deque: highs[-(fp+1)]
        center_high = highs[-(fp + 1)]
        center_low = lows[-(fp + 1)]

        # --- Up fractal (local high) ---
        up_flag_down = True   # right side (newer bars must be lower)
        up_flag_up0 = True    # left side strict
        up_flag_up1 = True    # left: 1 equal allowed
        up_flag_up2 = True    # left: 2 equal allowed
        up_flag_up3 = True    # left: 3 equal allowed
        up_flag_up4 = True    # left: 4 equal allowed

        # --- Down fractal (local low) ---
        dn_flag_down = True
        dn_flag_up0 = True
        dn_flag_up1 = True
        dn_flag_up2 = True
        dn_flag_up3 = True
        dn_flag_up4 = True

        for i in range(1, fp + 1):
            # Right side: Pine high[fp - i] → highs[-(fp - i + 1)]
            h_r = highs[-(fp - i + 1)]
            l_r = lows[-(fp - i + 1)]
            up_flag_down = up_flag_down and (h_r < center_high)
            dn_flag_down = dn_flag_down and (l_r > center_low)

            # Left side strict: Pine high[fp + i] → highs[-(fp + i + 1)]
            h_l = highs[-(fp + i + 1)]
            l_l = lows[-(fp + i + 1)]
            up_flag_up0 = up_flag_up0 and (h_l < center_high)
            dn_flag_up0 = dn_flag_up0 and (l_l > center_low)

            # Relaxed 1: first left bar may equal, rest strict
            h_eq1 = highs[-(fp + 2)]
            l_eq1 = lows[-(fp + 2)]
            up_flag_up1 = (
                up_flag_up1
                and (h_eq1 <= center_high)
                and (highs[-(fp + i + 2)] < center_high)
            )
            dn_flag_up1 = (
                dn_flag_up1
                and (l_eq1 >= center_low)
                and (lows[-(fp + i + 2)] > center_low)
            )

            # Relaxed 2: first two left bars may equal
            h_eq2 = highs[-(fp + 3)]
            l_eq2 = lows[-(fp + 3)]
            up_flag_up2 = (
                up_flag_up2
                and (h_eq1 <= center_high)
                and (h_eq2 <= center_high)
                and (highs[-(fp + i + 3)] < center_high)
            )
            dn_flag_up2 = (
                dn_flag_up2
                and (l_eq1 >= center_low)
                and (l_eq2 >= center_low)
                and (lows[-(fp + i + 3)] > center_low)
            )

            # Relaxed 3: first three left bars may equal
            h_eq3 = highs[-(fp + 4)]
            l_eq3 = lows[-(fp + 4)]
            up_flag_up3 = (
                up_flag_up3
                and (h_eq1 <= center_high)
                and (h_eq2 <= center_high)
                and (h_eq3 <= center_high)
                and (highs[-(fp + i + 4)] < center_high)
            )
            dn_flag_up3 = (
                dn_flag_up3
                and (l_eq1 >= center_low)
                and (l_eq2 >= center_low)
                and (l_eq3 >= center_low)
                and (lows[-(fp + i + 4)] > center_low)
            )

            # Relaxed 4: first four left bars may equal
            h_eq4 = highs[-(fp + 5)]
            l_eq4 = lows[-(fp + 5)]
            up_flag_up4 = (
                up_flag_up4
                and (h_eq1 <= center_high)
                and (h_eq2 <= center_high)
                and (h_eq3 <= center_high)
                and (h_eq4 <= center_high)
                and (highs[-(fp + i + 5)] < center_high)
            )
            dn_flag_up4 = (
                dn_flag_up4
                and (l_eq1 >= center_low)
                and (l_eq2 >= center_low)
                and (l_eq3 >= center_low)
                and (l_eq4 >= center_low)
                and (lows[-(fp + i + 5)] > center_low)
            )

        up_fractal = up_flag_down and (
            up_flag_up0 or up_flag_up1 or up_flag_up2 or up_flag_up3 or up_flag_up4
        )
        down_fractal = dn_flag_down and (
            dn_flag_up0 or dn_flag_up1 or dn_flag_up2 or dn_flag_up3 or dn_flag_up4
        )

        # Donchian filter: confirm fractal against baseline at fractalPeriod delay
        fractal_bear = False
        fractal_bull = False
        if len(self._baseline_hist) > fp:
            baseline_at_fp = self._baseline_hist[-(fp + 1)]
            fractal_bear = up_fractal and (center_high > baseline_at_fp)
            fractal_bull = down_fractal and (center_low < baseline_at_fp)

        self._output = {
            "fractal_bull": fractal_bull,
            "fractal_bear": fractal_bear,
            "upper_donch": upper_donch if upper_donch is not None else 0.0,
            "lower_donch": lower_donch if lower_donch is not None else 0.0,
            "baseline": baseline,
        }
