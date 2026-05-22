"""Lightweight incremental calculations for common technical indicators."""

from collections import deque

from .base import BaseIndicator


def _positive_int(params: dict, key: str, default: int) -> int:
    try:
        return max(1, int(params.get(key, default)))
    except (TypeError, ValueError):
        return default


class TechnicalIndicators(BaseIndicator):
    """ADX, MACD, PPO, MFI, and StochRSI in one incremental indicator pass."""

    def __init__(self, params: dict = None, max_history: int = None):
        p = params or {}
        adx_length = _positive_int(p, "adx_length", 14)
        adx_smoothing = _positive_int(p, "adx_smoothing", 14)
        macd_fast = _positive_int(p, "macd_fast_length", 12)
        macd_slow = _positive_int(p, "macd_slow_length", 26)
        macd_signal = _positive_int(p, "macd_signal_length", 9)
        ppo_fast = _positive_int(p, "ppo_fast_length", 12)
        ppo_slow = _positive_int(p, "ppo_slow_length", 26)
        ppo_signal = _positive_int(p, "ppo_signal_length", 9)
        mfi_length = _positive_int(p, "mfi_length", 14)
        stochrsi_rsi_length = _positive_int(p, "stochrsi_rsi_length", 14)
        stochrsi_length = _positive_int(p, "stochrsi_length", 14)
        stochrsi_k_smoothing = _positive_int(p, "stochrsi_k_smoothing", 3)
        stochrsi_d_smoothing = _positive_int(p, "stochrsi_d_smoothing", 3)
        history_needed = max(
            adx_length + adx_smoothing,
            max(macd_fast, macd_slow) + macd_signal,
            max(ppo_fast, ppo_slow) + ppo_signal,
            mfi_length + 1,
            stochrsi_rsi_length
            + stochrsi_length
            + stochrsi_k_smoothing
            + stochrsi_d_smoothing,
        )
        super().__init__(p, max_history or max(self.MAX_HISTORY, history_needed + 2))

        self._adx_length = adx_length
        self._adx_smoothing = adx_smoothing
        self._macd_fast_length = macd_fast
        self._macd_slow_length = macd_slow
        self._macd_signal_length = macd_signal
        self._ppo_fast_length = ppo_fast
        self._ppo_slow_length = ppo_slow
        self._ppo_signal_length = ppo_signal
        self._mfi_length = mfi_length
        self._stochrsi_rsi_length = stochrsi_rsi_length
        self._stochrsi_length = stochrsi_length
        self._stochrsi_k_smoothing = stochrsi_k_smoothing
        self._stochrsi_d_smoothing = stochrsi_d_smoothing

        self._tr_seed: list[float] = []
        self._plus_dm_seed: list[float] = []
        self._minus_dm_seed: list[float] = []
        self._dm_seeded = False
        self._smoothed_tr = 0.0
        self._smoothed_plus_dm = 0.0
        self._smoothed_minus_dm = 0.0
        self._dx_seed: list[float] = []
        self._adx_seeded = False
        self._adx = 0.0

        self._macd_fast_ema = 0.0
        self._macd_slow_ema = 0.0
        self._macd_signal_ema = 0.0
        self._macd_signal_seeded = False
        self._ppo_fast_ema = 0.0
        self._ppo_slow_ema = 0.0
        self._ppo_signal_ema = 0.0
        self._ppo_signal_seeded = False

        self._prev_typical_price: float | None = None
        self._mfi_positive: deque = deque(maxlen=self._mfi_length)
        self._mfi_negative: deque = deque(maxlen=self._mfi_length)

        self._stoch_rma_up = 0.0
        self._stoch_rma_down = 0.0
        self._stoch_rsi_seeded = False
        self._stoch_up_seed: list[float] = []
        self._stoch_down_seed: list[float] = []
        self._stoch_rsi_values: deque = deque(maxlen=self._stochrsi_length)
        self._stoch_raw_values: deque = deque(maxlen=self._stochrsi_k_smoothing)
        self._stoch_k_values: deque = deque(maxlen=self._stochrsi_d_smoothing)

    def is_ready(self) -> bool:
        return self.bar_count >= max(
            self._adx_length + self._adx_smoothing,
            max(self._macd_fast_length, self._macd_slow_length)
            + self._macd_signal_length,
            max(self._ppo_fast_length, self._ppo_slow_length) + self._ppo_signal_length,
            self._mfi_length + 1,
            self._stochrsi_rsi_length
            + self._stochrsi_length
            + self._stochrsi_k_smoothing
            + self._stochrsi_d_smoothing,
        )

    def _compute(self):
        close = self.closes[-1]
        adx, plus_di, minus_di = self._compute_adx()
        macd, macd_signal, macd_hist = self._compute_macd(close)
        ppo, ppo_signal, ppo_hist = self._compute_ppo(close)
        mfi = self._compute_mfi()
        stochrsi_rsi, stochrsi_raw, stochrsi_k, stochrsi_d = self._compute_stochrsi()

        self._output = {
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "macd": macd,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "ppo": ppo,
            "ppo_signal": ppo_signal,
            "ppo_hist": ppo_hist,
            "mfi": mfi,
            "stochrsi_rsi": stochrsi_rsi,
            "stochrsi": stochrsi_raw,
            "stochrsi_k": stochrsi_k,
            "stochrsi_d": stochrsi_d,
        }

    def _compute_adx(self) -> tuple[float, float, float]:
        if self.bar_count < 2:
            return self._adx, 0.0, 0.0

        high = self.highs[-1]
        low = self.lows[-1]
        prev_high = self.highs[-2]
        prev_low = self.lows[-2]
        prev_close = self.closes[-2]

        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm = up_move if up_move > down_move and up_move > 0.0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0.0 else 0.0
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))

        if not self._dm_seeded:
            self._tr_seed.append(tr)
            self._plus_dm_seed.append(plus_dm)
            self._minus_dm_seed.append(minus_dm)
            if len(self._tr_seed) >= self._adx_length:
                self._smoothed_tr = sum(self._tr_seed) / self._adx_length
                self._smoothed_plus_dm = sum(self._plus_dm_seed) / self._adx_length
                self._smoothed_minus_dm = sum(self._minus_dm_seed) / self._adx_length
                self._dm_seeded = True
        else:
            self._smoothed_tr = self.rma_step(self._smoothed_tr, tr, self._adx_length)
            self._smoothed_plus_dm = self.rma_step(
                self._smoothed_plus_dm,
                plus_dm,
                self._adx_length,
            )
            self._smoothed_minus_dm = self.rma_step(
                self._smoothed_minus_dm,
                minus_dm,
                self._adx_length,
            )

        plus_di = 0.0
        minus_di = 0.0
        if self._dm_seeded and self._smoothed_tr > 0.0:
            plus_di = 100.0 * self._smoothed_plus_dm / self._smoothed_tr
            minus_di = 100.0 * self._smoothed_minus_dm / self._smoothed_tr
            di_sum = plus_di + minus_di
            dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0.0 else 0.0
            if not self._adx_seeded:
                self._dx_seed.append(dx)
                if len(self._dx_seed) >= self._adx_smoothing:
                    self._adx = sum(self._dx_seed) / self._adx_smoothing
                    self._adx_seeded = True
            else:
                self._adx = self.rma_step(self._adx, dx, self._adx_smoothing)

        return self._adx, plus_di, minus_di

    def _compute_macd(self, close: float) -> tuple[float, float, float]:
        if self.bar_count == 1:
            self._macd_fast_ema = close
            self._macd_slow_ema = close
        else:
            self._macd_fast_ema = self.ema_step(
                self._macd_fast_ema,
                close,
                self._macd_fast_length,
            )
            self._macd_slow_ema = self.ema_step(
                self._macd_slow_ema,
                close,
                self._macd_slow_length,
            )

        macd = self._macd_fast_ema - self._macd_slow_ema
        if not self._macd_signal_seeded:
            self._macd_signal_ema = macd
            self._macd_signal_seeded = True
        else:
            self._macd_signal_ema = self.ema_step(
                self._macd_signal_ema,
                macd,
                self._macd_signal_length,
            )
        return macd, self._macd_signal_ema, macd - self._macd_signal_ema

    def _compute_ppo(self, close: float) -> tuple[float, float, float]:
        if self.bar_count == 1:
            self._ppo_fast_ema = close
            self._ppo_slow_ema = close
        else:
            self._ppo_fast_ema = self.ema_step(self._ppo_fast_ema, close, self._ppo_fast_length)
            self._ppo_slow_ema = self.ema_step(self._ppo_slow_ema, close, self._ppo_slow_length)

        ppo = (
            (self._ppo_fast_ema - self._ppo_slow_ema) / self._ppo_slow_ema * 100.0
            if self._ppo_slow_ema
            else 0.0
        )
        if not self._ppo_signal_seeded:
            self._ppo_signal_ema = ppo
            self._ppo_signal_seeded = True
        else:
            self._ppo_signal_ema = self.ema_step(
                self._ppo_signal_ema,
                ppo,
                self._ppo_signal_length,
            )
        return ppo, self._ppo_signal_ema, ppo - self._ppo_signal_ema

    def _compute_mfi(self) -> float:
        typical_price = (self.highs[-1] + self.lows[-1] + self.closes[-1]) / 3.0
        money_flow = typical_price * self.volumes[-1]
        positive_flow = 0.0
        negative_flow = 0.0
        if self._prev_typical_price is not None:
            if typical_price > self._prev_typical_price:
                positive_flow = money_flow
            elif typical_price < self._prev_typical_price:
                negative_flow = money_flow

        self._prev_typical_price = typical_price
        self._mfi_positive.append(positive_flow)
        self._mfi_negative.append(negative_flow)

        if len(self._mfi_positive) < self._mfi_length:
            return 50.0

        positive_sum = sum(self._mfi_positive)
        negative_sum = sum(self._mfi_negative)
        if positive_sum == 0.0 and negative_sum == 0.0:
            return 50.0
        if negative_sum == 0.0:
            return 100.0
        if positive_sum == 0.0:
            return 0.0
        money_ratio = positive_sum / negative_sum
        return 100.0 - 100.0 / (1.0 + money_ratio)

    def _compute_stochrsi(self) -> tuple[float, float, float, float]:
        if self.bar_count < 2:
            return 50.0, 50.0, 50.0, 50.0

        change = self.closes[-1] - self.closes[-2]
        up_val = max(change, 0.0)
        down_val = -min(change, 0.0)

        if not self._stoch_rsi_seeded:
            self._stoch_up_seed.append(up_val)
            self._stoch_down_seed.append(down_val)
            if len(self._stoch_up_seed) < self._stochrsi_rsi_length:
                return 50.0, 50.0, 50.0, 50.0
            self._stoch_rma_up = sum(self._stoch_up_seed) / self._stochrsi_rsi_length
            self._stoch_rma_down = sum(self._stoch_down_seed) / self._stochrsi_rsi_length
            self._stoch_rsi_seeded = True
        else:
            self._stoch_rma_up = self.rma_step(
                self._stoch_rma_up,
                up_val,
                self._stochrsi_rsi_length,
            )
            self._stoch_rma_down = self.rma_step(
                self._stoch_rma_down,
                down_val,
                self._stochrsi_rsi_length,
            )

        if self._stoch_rma_up == 0.0 and self._stoch_rma_down == 0.0:
            rsi = 50.0
        elif self._stoch_rma_down == 0.0:
            rsi = 100.0
        elif self._stoch_rma_up == 0.0:
            rsi = 0.0
        else:
            rsi = 100.0 - 100.0 / (1.0 + self._stoch_rma_up / self._stoch_rma_down)

        self._stoch_rsi_values.append(rsi)
        if len(self._stoch_rsi_values) < self._stochrsi_length:
            return rsi, 50.0, 50.0, 50.0

        lowest_rsi = self.lowest(self._stoch_rsi_values, self._stochrsi_length)
        highest_rsi = self.highest(self._stoch_rsi_values, self._stochrsi_length)
        if lowest_rsi is None or highest_rsi is None or highest_rsi == lowest_rsi:
            stochrsi = 50.0
        else:
            stochrsi = (rsi - lowest_rsi) / (highest_rsi - lowest_rsi) * 100.0

        self._stoch_raw_values.append(stochrsi)
        stochrsi_k = self.sma(self._stoch_raw_values, self._stochrsi_k_smoothing)
        if stochrsi_k is None:
            stochrsi_k = self._mean(self._stoch_raw_values)
        self._stoch_k_values.append(stochrsi_k)
        stochrsi_d = self.sma(self._stoch_k_values, self._stochrsi_d_smoothing)
        if stochrsi_d is None:
            stochrsi_d = self._mean(self._stoch_k_values)
        return rsi, stochrsi, stochrsi_k, stochrsi_d

    @staticmethod
    def _mean(values: deque) -> float:
        return float(sum(values) / len(values)) if values else 0.0

    def reset(self):
        super().reset()
        self._tr_seed = []
        self._plus_dm_seed = []
        self._minus_dm_seed = []
        self._dm_seeded = False
        self._smoothed_tr = 0.0
        self._smoothed_plus_dm = 0.0
        self._smoothed_minus_dm = 0.0
        self._dx_seed = []
        self._adx_seeded = False
        self._adx = 0.0
        self._macd_fast_ema = 0.0
        self._macd_slow_ema = 0.0
        self._macd_signal_ema = 0.0
        self._macd_signal_seeded = False
        self._ppo_fast_ema = 0.0
        self._ppo_slow_ema = 0.0
        self._ppo_signal_ema = 0.0
        self._ppo_signal_seeded = False
        self._prev_typical_price = None
        self._mfi_positive.clear()
        self._mfi_negative.clear()
        self._stoch_rma_up = 0.0
        self._stoch_rma_down = 0.0
        self._stoch_rsi_seeded = False
        self._stoch_up_seed = []
        self._stoch_down_seed = []
        self._stoch_rsi_values.clear()
        self._stoch_raw_values.clear()
        self._stoch_k_values.clear()
