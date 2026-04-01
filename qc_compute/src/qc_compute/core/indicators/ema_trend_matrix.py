"""EMA Trend Matrix — ported from Pine Script EMA Trend Matrix indicator"""

from collections import deque

from .base import BaseIndicator


class EmaTrendMatrix(BaseIndicator):

    def __init__(self, params: dict = None, max_history: int = None):
        super().__init__(params, max_history)
        self._ema20: float = 0.0
        self._ema50: float = 0.0
        self._ema100: float = 0.0
        self._ema200: float = 0.0

        lookback = self.params.get("ema_slope_lookback", 12)
        self._ema20_hist: deque = deque(maxlen=lookback + 1)
        self._ema50_hist: deque = deque(maxlen=lookback + 1)
        self._ema100_hist: deque = deque(maxlen=lookback + 1)
        self._ema200_hist: deque = deque(maxlen=lookback + 1)

        self._prev_close: float = 0.0
        self._prev_ema_fast: float = 0.0
        self._prev_ema_slow: float = 0.0

    def is_ready(self) -> bool:
        lookback = self.params.get("ema_slope_lookback", 12)
        return self.bar_count >= 200 + lookback

    # ------------------------------------------------------------------ #

    def _compute(self):
        close = self.closes[-1]
        high = self.highs[-1]
        low = self.lows[-1]

        # --- EMA updates ---
        if self.bar_count == 1:
            self._ema20 = close
            self._ema50 = close
            self._ema100 = close
            self._ema200 = close
        else:
            self._ema20 = self.ema_step(self._ema20, close, 20)
            self._ema50 = self.ema_step(self._ema50, close, 50)
            self._ema100 = self.ema_step(self._ema100, close, 100)
            self._ema200 = self.ema_step(self._ema200, close, 200)

        self._ema20_hist.append(self._ema20)
        self._ema50_hist.append(self._ema50)
        self._ema100_hist.append(self._ema100)
        self._ema200_hist.append(self._ema200)

        # --- params ---
        lookback = self.params.get("ema_slope_lookback", 12)
        min_angle = self.params.get("ema_min_angle", 0.01)
        min_spacing = self.params.get("ema_min_spacing", 0.01)
        touch_type = self.params.get("ema_touch_type", "slow")

        ema_fast = self._ema20
        ema_slow = self._ema50
        ema_trend = self._ema100
        ema_longest = self._ema200

        # --- Slope: percentage change over lookback period ---
        slope_slow = slope_trend = slope_longest = 0.0
        if len(self._ema50_hist) > lookback:
            prev_val = self._ema50_hist[-(lookback + 1)]
            if prev_val != 0:
                slope_slow = (ema_slow - prev_val) / prev_val * 100
        if len(self._ema100_hist) > lookback:
            prev_val = self._ema100_hist[-(lookback + 1)]
            if prev_val != 0:
                slope_trend = (ema_trend - prev_val) / prev_val * 100
        if len(self._ema200_hist) > lookback:
            prev_val = self._ema200_hist[-(lookback + 1)]
            if prev_val != 0:
                slope_longest = (ema_longest - prev_val) / prev_val * 100

        # --- Spacing: percentage distance between EMAs ---
        spacing_fast_slow = (
            abs(ema_fast - ema_slow) / ema_slow * 100 if ema_slow != 0 else 0.0
        )
        spacing_slow_trend = (
            abs(ema_slow - ema_trend) / ema_trend * 100 if ema_trend != 0 else 0.0
        )
        spacing_trend_longest = (
            abs(ema_trend - ema_longest) / ema_longest * 100
            if ema_longest != 0
            else 0.0
        )

        spacing_ok = (
            spacing_fast_slow > min_spacing
            and spacing_slow_trend > min_spacing
            and spacing_trend_longest > min_spacing
        )

        # --- Alignment ---
        ema_bullish_align_only = ema_fast > ema_slow and ema_slow > ema_trend
        ema_bearish_align_only = ema_fast < ema_slow and ema_slow < ema_trend

        ema_bullish = (
            ema_fast > ema_slow
            and ema_slow > ema_trend
            and ema_trend > ema_longest
            and slope_slow > min_angle
            and slope_trend > min_angle
            and slope_longest > min_angle
            and spacing_ok
        )
        ema_bearish = (
            ema_fast < ema_slow
            and ema_slow < ema_trend
            and ema_trend < ema_longest
            and slope_slow < -min_angle
            and slope_trend < -min_angle
            and slope_longest < -min_angle
            and spacing_ok
        )

        trend_dir = 1 if ema_bullish else (-1 if ema_bearish else 0)

        # --- Touch detection ---
        bull_touch_fast = False
        bear_touch_fast = False
        bull_touch_slow = False
        bear_touch_slow = False

        if self.bar_count >= 2:
            bull_touch_fast = (
                ema_bullish_align_only
                and spacing_ok
                and self._prev_close > self._prev_ema_fast
                and low <= ema_fast
            )
            bear_touch_fast = (
                ema_bearish_align_only
                and spacing_ok
                and self._prev_close < self._prev_ema_fast
                and high >= ema_fast
            )
            bull_touch_slow = (
                ema_bullish_align_only
                and spacing_ok
                and self._prev_close > self._prev_ema_slow
                and low <= ema_slow
            )
            bear_touch_slow = (
                ema_bearish_align_only
                and spacing_ok
                and self._prev_close < self._prev_ema_slow
                and high >= ema_slow
            )

        # Filter by touch type
        if touch_type == "fast":
            ema_bull_touch = bull_touch_fast
            ema_bear_touch = bear_touch_fast
        elif touch_type == "both":
            ema_bull_touch = bull_touch_fast or bull_touch_slow
            ema_bear_touch = bear_touch_fast or bear_touch_slow
        else:  # "slow" (default)
            ema_bull_touch = bull_touch_slow
            ema_bear_touch = bear_touch_slow

        # Store previous values for next bar's touch detection
        self._prev_close = close
        self._prev_ema_fast = ema_fast
        self._prev_ema_slow = ema_slow

        self._output = {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "ema_trend": ema_trend,
            "ema_longest": ema_longest,
            "slope_slow": slope_slow,
            "slope_trend": slope_trend,
            "slope_longest": slope_longest,
            "spacing_fast_slow": spacing_fast_slow,
            "spacing_slow_trend": spacing_slow_trend,
            "spacing_trend_longest": spacing_trend_longest,
            "ema_bullish": ema_bullish,
            "ema_bearish": ema_bearish,
            "ema_bullish_align_only": ema_bullish_align_only,
            "ema_bearish_align_only": ema_bearish_align_only,
            "trend_dir": trend_dir,
            "ema_bull_touch": ema_bull_touch,
            "ema_bear_touch": ema_bear_touch,
            "bull_touch_fast": bull_touch_fast,
            "bear_touch_fast": bear_touch_fast,
            "bull_touch_slow": bull_touch_slow,
            "bear_touch_slow": bear_touch_slow,
        }
