"""EMA-DTP 联动过滤器 + 震荡市场检测"""

from collections import deque
from typing import Optional


class SignalFilters:
    """EMA-DTP linkage filters + oscillation market detection"""

    def __init__(self, params: dict = None):
        p = params or {}
        self.enable_advanced = bool(p.get("enable_advanced_filter", True))
        self.ema_strength_lookback = int(p.get("ema_strength_lookback", 20))
        self.ema_weak_threshold = float(p.get("ema_weak_threshold", 0.005))
        self.ema_min_angle = float(p.get("ema_min_angle", 0.01))
        self.dtp_switch_lookback = int(p.get("dtp_switch_lookback", 10))
        self.dtp_max_switches = int(p.get("dtp_max_switches", 3))
        self.dtp_early_bars = int(p.get("dtp_early_bars", 12))
        self.oscillation_lookback = int(p.get("oscillation_lookback", 20))
        self.oscillation_threshold = float(p.get("oscillation_threshold", 0.4))

        # EMA history deques for slope calculation
        self._ema_fast: deque = deque(maxlen=self.ema_strength_lookback + 1)
        self._ema_slow: deque = deque(maxlen=self.ema_strength_lookback + 1)
        self._ema_trend: deque = deque(maxlen=self.ema_strength_lookback + 1)

        # DTP direction history for switch counting
        self._dtp_dirs: deque = deque(maxlen=self.dtp_switch_lookback + 1)
        self._prev_dtp_dir: Optional[int] = None

        # Component mismatch tracking for oscillation detection
        self._mismatch_flags: deque = deque(maxlen=self.oscillation_lookback)

    def _calc_ema_slopes(self) -> tuple:
        lb = self.ema_strength_lookback
        if len(self._ema_fast) <= lb:
            return 0.0, 0.0, 0.0
        fast_list = list(self._ema_fast)
        slow_list = list(self._ema_slow)
        trend_list = list(self._ema_trend)
        prev_fast = fast_list[-(lb + 1)]
        prev_slow = slow_list[-(lb + 1)]
        prev_trend = trend_list[-(lb + 1)]
        cur_fast = fast_list[-1]
        cur_slow = slow_list[-1]
        cur_trend = trend_list[-1]
        slope_fast = (cur_fast - prev_fast) / prev_fast * 100 if prev_fast != 0 else 0.0
        slope_slow = (cur_slow - prev_slow) / prev_slow * 100 if prev_slow != 0 else 0.0
        slope_trend = (cur_trend - prev_trend) / prev_trend * 100 if prev_trend != 0 else 0.0
        return slope_fast, slope_slow, slope_trend

    def _count_dtp_switches(self) -> int:
        dirs = list(self._dtp_dirs)
        if len(dirs) < 2:
            return 0
        count = 0
        for i in range(1, len(dirs)):
            if dirs[i] != dirs[i - 1] and dirs[i] != 0 and dirs[i - 1] != 0:
                count += 1
        return count

    def update(self, snapshot: dict) -> dict:
        """Called each bar with full indicator snapshot.

        Expects keys: ema_fast, ema_slow, ema_trend, dtp_dir, dtp_phase_bars,
                      fractal_bull, fractal_bear, crsi_bull_div, crsi_bear_div, etc.
        """
        ema_fast = snapshot.get("ema_fast", 0.0)
        ema_slow = snapshot.get("ema_slow", 0.0)
        ema_trend = snapshot.get("ema_trend", 0.0)
        dtp_dir = snapshot.get("dtp_dir", 0)
        dtp_phase_bars = snapshot.get("dtp_phase_bars", 0)
        fractal_bull = snapshot.get("fractal_bull", False)
        fractal_bear = snapshot.get("fractal_bear", False)

        # Push EMA values
        self._ema_fast.append(ema_fast)
        self._ema_slow.append(ema_slow)
        self._ema_trend.append(ema_trend)

        # Push DTP direction
        self._dtp_dirs.append(dtp_dir)

        # EMA strength
        slope_fast, slope_slow, slope_trend = self._calc_ema_slopes()
        ema_strong_bull = (
            slope_fast > self.ema_min_angle
            and slope_slow > self.ema_min_angle
            and slope_trend > self.ema_min_angle
        )
        ema_strong_bear = (
            slope_fast < -self.ema_min_angle
            and slope_slow < -self.ema_min_angle
            and slope_trend < -self.ema_min_angle
        )
        slopes_agree = (
            (slope_fast > 0 and slope_slow > 0 and slope_trend > 0)
            or (slope_fast < 0 and slope_slow < 0 and slope_trend < 0)
        )
        any_weak = (
            abs(slope_fast) < self.ema_weak_threshold
            or abs(slope_slow) < self.ema_weak_threshold
            or abs(slope_trend) < self.ema_weak_threshold
        )
        ema_weak = any_weak or not slopes_agree

        # DTP switch frequency
        dtp_frequent_switch = self._count_dtp_switches() > self.dtp_max_switches

        # DTP turns
        dtp_red_to_blue = self._prev_dtp_dir == -1 and dtp_dir == 1
        dtp_blue_to_red = self._prev_dtp_dir == 1 and dtp_dir == -1
        self._prev_dtp_dir = dtp_dir

        # DTP early phase
        dtp_blue_early = dtp_dir == 1 and dtp_phase_bars <= self.dtp_early_bars
        dtp_red_early = dtp_dir == -1 and dtp_phase_bars <= self.dtp_early_bars

        # Oscillation market detection: both bull and bear components present
        has_mismatch = bool(fractal_bull) and bool(fractal_bear)
        self._mismatch_flags.append(1 if has_mismatch else 0)
        if len(self._mismatch_flags) > 0:
            mismatch_ratio = sum(self._mismatch_flags) / len(self._mismatch_flags)
        else:
            mismatch_ratio = 0.0
        is_oscillation = mismatch_ratio > self.oscillation_threshold

        # Filter rules
        if self.enable_advanced:
            block_mr_short = (
                (ema_strong_bull and dtp_blue_early) or dtp_red_to_blue
            )
            block_mr_long = (
                (ema_strong_bear and dtp_red_early) or dtp_blue_to_red
            )
            block_ema_trend = ema_weak or dtp_frequent_switch
        else:
            block_mr_short = False
            block_mr_long = False
            block_ema_trend = False

        block_all_signals = is_oscillation

        return {
            "block_mr_short": block_mr_short,
            "block_mr_long": block_mr_long,
            "block_ema_trend": block_ema_trend,
            "block_all_signals": block_all_signals,
            "ema_strong_bull": ema_strong_bull,
            "ema_strong_bear": ema_strong_bear,
            "ema_weak": ema_weak,
            "dtp_frequent_switch": dtp_frequent_switch,
            "is_oscillation_market": is_oscillation,
        }

    def reset(self):
        self._ema_fast.clear()
        self._ema_slow.clear()
        self._ema_trend.clear()
        self._dtp_dirs.clear()
        self._prev_dtp_dir = None
        self._mismatch_flags.clear()
