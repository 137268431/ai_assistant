"""DTP — Deviation Trend Profile indicator"""

from collections import deque

from .base import BaseIndicator


class DTP(BaseIndicator):

    def __init__(self, params: dict = None, max_history: int = None):
        super().__init__(params, max_history)
        p = self.params
        self.dtp_sma_length: int = int(p.get("dtp_sma_length", 100))
        self.dtp_atr_length: int = int(p.get("dtp_atr_length", 200))
        self.dtp_mult1: float = float(p.get("dtp_mult1", 3.0))
        self.dtp_mult2: float = float(p.get("dtp_mult2", 6.0))
        self.dtp_mult3: float = float(p.get("dtp_mult3", 9.0))
        self.dtp_mult4: float = float(p.get("dtp_mult4", 12.0))
        self.dtp_signal_band: int = int(p.get("dtp_signal_band", 4))
        self.dtp_momentum_lookback: int = int(p.get("dtp_momentum_lookback", 12))
        self.dtp_trend_threshold: float = float(p.get("dtp_trend_threshold", 0.1))
        self.dtp_early_bars: int = int(p.get("dtp_early_bars", 12))
        self.dtp_mature_bars: int = int(p.get("dtp_mature_bars", 48))

        # ATR state (RMA smoothed)
        self._atr: float = 0.0
        self._atr_init: bool = False

        # SMA history for momentum lookback
        self._avg_hist: deque = deque(maxlen=max(self.dtp_momentum_lookback + 1, 500))
        # Avg-diff history for percentile
        self._avg_diff_hist: deque = deque(maxlen=500)

        # State machine
        self._dtp_trend: bool = False
        self._dtp_trend_init: bool = False
        self._prev_dtp_norm: float = 0.0

        # Phase tracking
        self._dtp_phase_bars: int = 0
        self._prev_dtp_dir: int = 0

        # External context (sd_trend, etc.)
        self._context: dict = {}

    # ── helpers ──

    def _band_to_mult(self, band: int) -> float:
        return {1: self.dtp_mult1, 2: self.dtp_mult2,
                3: self.dtp_mult3, 4: self.dtp_mult4}.get(band, self.dtp_mult1)

    def set_context(self, ctx: dict):
        self._context = ctx

    def is_ready(self) -> bool:
        return self.bar_count >= max(self.dtp_sma_length, self.dtp_atr_length)

    def update(self, bar: dict, context: dict = None) -> dict:
        if context is not None:
            self._context = context
        self._push_bar(bar)
        self._compute()
        return self._output

    # ── core ──

    def _compute(self):
        # True range for ATR
        cur_high = self.highs[-1]
        cur_low = self.lows[-1]
        cur_close = self.closes[-1]

        if self.bar_count >= 2:
            prev_close = self.closes[-2]
            tr = max(cur_high - cur_low,
                     abs(cur_high - prev_close),
                     abs(cur_low - prev_close))
        else:
            tr = cur_high - cur_low

        # ATR via RMA
        if not self._atr_init:
            self._atr = tr
            self._atr_init = True
        else:
            self._atr = self.rma_step(self._atr, tr, self.dtp_atr_length)

        # SMA of close
        dtp_avg = self.sma(self.closes, self.dtp_sma_length)
        if dtp_avg is None:
            return
        self._avg_hist.append(dtp_avg)

        dtp_atr = self._atr
        band_mult = self._band_to_mult(self.dtp_signal_band)

        # MR touch detection
        dtp_mr_bull = cur_low <= dtp_avg - band_mult * dtp_atr
        dtp_mr_bear = cur_high >= dtp_avg + band_mult * dtp_atr

        # Momentum: avg diff
        lookback = self.dtp_momentum_lookback
        if len(self._avg_hist) > lookback:
            dtp_avg_diff = dtp_avg - self._avg_hist[-lookback - 1]
        else:
            dtp_avg_diff = 0.0
        self._avg_diff_hist.append(dtp_avg_diff)

        # Normalise: 100th percentile = max of window
        pctl_period = min(len(self._avg_diff_hist), 500)
        window = list(self._avg_diff_hist)[-pctl_period:]
        denom = float(max(window)) if window else None
        if denom is not None and abs(denom) > 1e-12:
            dtp_norm = dtp_avg_diff / denom
        else:
            dtp_norm = 0.0

        # State machine: crossover / crossunder
        prev = self._prev_dtp_norm
        threshold = self.dtp_trend_threshold
        crossover = prev <= threshold and dtp_norm > threshold
        crossunder = prev >= -threshold and dtp_norm < -threshold

        if crossover and not self._dtp_trend:
            self._dtp_trend = True
            self._dtp_trend_init = True
        if crossunder and self._dtp_trend:
            self._dtp_trend = False
            self._dtp_trend_init = True
        self._prev_dtp_norm = dtp_norm

        # Direction
        dtp_bull_dir = self._dtp_trend_init and self._dtp_trend and cur_close > dtp_avg
        dtp_bear_dir = self._dtp_trend_init and (not self._dtp_trend) and cur_close < dtp_avg
        if dtp_bull_dir:
            dtp_dir = 1
        elif dtp_bear_dir:
            dtp_dir = -1
        else:
            dtp_dir = 0

        # Phase bars (reset on direction change)
        if dtp_dir != self._prev_dtp_dir:
            self._dtp_phase_bars = 0
        self._dtp_phase_bars += 1
        self._prev_dtp_dir = dtp_dir

        # Phase label
        sd_trend = self._context.get("sd_trend", 0)
        sd_confirms = (dtp_dir == 1 and sd_trend == 1) or (dtp_dir == -1 and sd_trend == -1)

        if dtp_dir == 0:
            dtp_phase = "neutral"
        elif self._dtp_phase_bars <= self.dtp_early_bars:
            dtp_phase = "early"
        elif self._dtp_phase_bars > self.dtp_mature_bars and sd_confirms:
            dtp_phase = "mature"
        elif sd_confirms:
            dtp_phase = "confirmed"
        else:
            dtp_phase = "weakening"

        self._output = {
            "dtp_avg": dtp_avg,
            "dtp_atr": dtp_atr,
            "dtp_dir": dtp_dir,
            "dtp_phase": dtp_phase,
            "dtp_phase_bars": self._dtp_phase_bars,
            "dtp_mr_bull": dtp_mr_bull,
            "dtp_mr_bear": dtp_mr_bear,
            "dtp_norm": dtp_norm,
            "dtp_trend_init": self._dtp_trend_init,
        }
