"""统一背离检测框架 — cRSI 与 OBV RSI 背离检测"""

from collections import deque
from typing import Optional, Tuple


class DivergenceDetector:
    """Detects strict and sensitive divergences for cRSI and OBV RSI oscillators"""

    DEQUE_LEN = 100

    def __init__(self, params: dict = None):
        p = params or {}
        self.crsi_lookback = int(p.get("crsi_div_lookback", 4))
        self.crsi_max_bars = int(p.get("crsi_div_max_bars", 30))
        self.obv_lookback = int(p.get("obv_div_lookback", 4))
        self.obv_max_bars = int(p.get("obv_div_max_bars", 30))
        self.div_type = p.get("div_type", "both")  # "regular", "hidden", "both"

        self._highs: deque = deque(maxlen=self.DEQUE_LEN)
        self._lows: deque = deque(maxlen=self.DEQUE_LEN)
        self._crsi_values: deque = deque(maxlen=self.DEQUE_LEN)
        self._obv_values: deque = deque(maxlen=self.DEQUE_LEN)
        self._bar_indices: deque = deque(maxlen=self.DEQUE_LEN)

        # Previous pivot tracking — cRSI
        self._prev_crsi_ph_price: Optional[float] = None
        self._prev_crsi_ph_rsi: Optional[float] = None
        self._prev_crsi_ph_bar: Optional[int] = None
        self._prev_crsi_pl_price: Optional[float] = None
        self._prev_crsi_pl_rsi: Optional[float] = None
        self._prev_crsi_pl_bar: Optional[int] = None

        # Previous pivot tracking — OBV RSI
        self._prev_obv_ph_price: Optional[float] = None
        self._prev_obv_ph_rsi: Optional[float] = None
        self._prev_obv_ph_bar: Optional[int] = None
        self._prev_obv_pl_price: Optional[float] = None
        self._prev_obv_pl_rsi: Optional[float] = None
        self._prev_obv_pl_bar: Optional[int] = None

    # ── pivot helpers (inline, not inherited) ──

    @staticmethod
    def _pivothigh(data, left: int, right: int) -> Tuple[Optional[float], Optional[int]]:
        total = left + right + 1
        if len(data) < total:
            return None, None
        window = list(data)[-total:]
        mid_val = window[left]
        for i in range(total):
            if i != left and window[i] >= mid_val:
                return None, None
        return mid_val, right

    @staticmethod
    def _pivotlow(data, left: int, right: int) -> Tuple[Optional[float], Optional[int]]:
        total = left + right + 1
        if len(data) < total:
            return None, None
        window = list(data)[-total:]
        mid_val = window[left]
        for i in range(total):
            if i != left and window[i] <= mid_val:
                return None, None
        return mid_val, right

    @staticmethod
    def _highest(data, period: int) -> Optional[float]:
        if len(data) < period:
            return None
        return float(max(list(data)[-period:]))

    @staticmethod
    def _lowest(data, period: int) -> Optional[float]:
        if len(data) < period:
            return None
        return float(min(list(data)[-period:]))

    # ── core detection for one oscillator ──

    def _detect_strict(
        self,
        highs: deque,
        lows: deque,
        osc: deque,
        lookback: int,
        max_bars: int,
        prev_ph_price: Optional[float],
        prev_ph_rsi: Optional[float],
        prev_ph_bar: Optional[int],
        prev_pl_price: Optional[float],
        prev_pl_rsi: Optional[float],
        prev_pl_bar: Optional[int],
        bar_index: int,
    ) -> dict:
        bull_div = bear_div = hid_bull = hid_bear = False

        # Detect current pivots
        ph_price, ph_off = self._pivothigh(highs, lookback, lookback)
        ph_rsi, _ = self._pivothigh(osc, lookback, lookback)
        pl_price, pl_off = self._pivotlow(lows, lookback, lookback)
        pl_rsi, _ = self._pivotlow(osc, lookback, lookback)

        new_ph_price = prev_ph_price
        new_ph_rsi = prev_ph_rsi
        new_ph_bar = prev_ph_bar
        new_pl_price = prev_pl_price
        new_pl_rsi = prev_pl_rsi
        new_pl_bar = prev_pl_bar

        # Check pivot highs for bearish / hidden bearish divergence
        if ph_price is not None and ph_rsi is not None:
            cur_bar = bar_index - lookback  # pivot is at offset=lookback from end
            if prev_ph_price is not None and prev_ph_bar is not None:
                bar_dist = cur_bar - prev_ph_bar
                if 0 < bar_dist <= max_bars:
                    # Regular bearish: price higher high, oscillator lower high
                    if ph_price > prev_ph_price and ph_rsi < prev_ph_rsi:
                        bear_div = True
                    # Hidden bearish: price lower high, oscillator higher high
                    if ph_price < prev_ph_price and ph_rsi > prev_ph_rsi:
                        hid_bear = True
            new_ph_price = ph_price
            new_ph_rsi = ph_rsi
            new_ph_bar = cur_bar

        # Check pivot lows for bullish / hidden bullish divergence
        if pl_price is not None and pl_rsi is not None:
            cur_bar = bar_index - lookback
            if prev_pl_price is not None and prev_pl_bar is not None:
                bar_dist = cur_bar - prev_pl_bar
                if 0 < bar_dist <= max_bars:
                    # Regular bullish: price lower low, oscillator higher low
                    if pl_price < prev_pl_price and pl_rsi > prev_pl_rsi:
                        bull_div = True
                    # Hidden bullish: price higher low, oscillator lower low
                    if pl_price > prev_pl_price and pl_rsi < prev_pl_rsi:
                        hid_bull = True
            new_pl_price = pl_price
            new_pl_rsi = pl_rsi
            new_pl_bar = cur_bar

        return {
            "bull_div": bull_div,
            "bear_div": bear_div,
            "hid_bull": hid_bull,
            "hid_bear": hid_bear,
            "new_ph_price": new_ph_price,
            "new_ph_rsi": new_ph_rsi,
            "new_ph_bar": new_ph_bar,
            "new_pl_price": new_pl_price,
            "new_pl_rsi": new_pl_rsi,
            "new_pl_bar": new_pl_bar,
        }

    def _detect_sensitive(
        self,
        highs: deque,
        lows: deque,
        osc: deque,
        lookback: int,
        max_bars: int,
        prev_ph_price: Optional[float],
        prev_ph_rsi: Optional[float],
        prev_ph_bar: Optional[int],
        prev_pl_price: Optional[float],
        prev_pl_rsi: Optional[float],
        prev_pl_bar: Optional[int],
        bar_index: int,
    ) -> dict:
        bull_div = bear_div = hid_bull = hid_bear = False
        window_size = lookback * 2 + 1

        new_ph_price = prev_ph_price
        new_ph_rsi = prev_ph_rsi
        new_ph_bar = prev_ph_bar
        new_pl_price = prev_pl_price
        new_pl_rsi = prev_pl_rsi
        new_pl_bar = prev_pl_bar

        # Sensitive: only price needs a pivot; take windowed extreme of oscillator
        ph_price, _ = self._pivothigh(highs, lookback, lookback)
        if ph_price is not None:
            osc_high = self._highest(osc, window_size)
            if osc_high is not None:
                cur_bar = bar_index - lookback
                if prev_ph_price is not None and prev_ph_bar is not None:
                    bar_dist = cur_bar - prev_ph_bar
                    if 0 < bar_dist <= max_bars:
                        if ph_price > prev_ph_price and osc_high < prev_ph_rsi:
                            bear_div = True
                        if ph_price < prev_ph_price and osc_high > prev_ph_rsi:
                            hid_bear = True
                new_ph_price = ph_price
                new_ph_rsi = osc_high
                new_ph_bar = cur_bar

        pl_price, _ = self._pivotlow(lows, lookback, lookback)
        if pl_price is not None:
            osc_low = self._lowest(osc, window_size)
            if osc_low is not None:
                cur_bar = bar_index - lookback
                if prev_pl_price is not None and prev_pl_bar is not None:
                    bar_dist = cur_bar - prev_pl_bar
                    if 0 < bar_dist <= max_bars:
                        if pl_price < prev_pl_price and osc_low > prev_pl_rsi:
                            bull_div = True
                        if pl_price > prev_pl_price and osc_low < prev_pl_rsi:
                            hid_bull = True
                new_pl_price = pl_price
                new_pl_rsi = osc_low
                new_pl_bar = cur_bar

        return {
            "bull_div": bull_div,
            "bear_div": bear_div,
            "hid_bull": hid_bull,
            "hid_bear": hid_bear,
            "new_ph_price": new_ph_price,
            "new_ph_rsi": new_ph_rsi,
            "new_ph_bar": new_ph_bar,
            "new_pl_price": new_pl_price,
            "new_pl_rsi": new_pl_rsi,
            "new_pl_bar": new_pl_bar,
        }

    def _apply_type_filter(self, bull: bool, bear: bool, hid_bull: bool, hid_bear: bool):
        if self.div_type == "regular":
            return bull, bear, False, False
        if self.div_type == "hidden":
            return False, False, hid_bull, hid_bear
        return bull, bear, hid_bull, hid_bear

    # ── public API ──

    def update(self, bar_index: int, high: float, low: float,
               crsi: float, obv_rsi: float) -> dict:
        """Called each bar with current values. Returns divergence flags."""
        self._highs.append(high)
        self._lows.append(low)
        self._crsi_values.append(crsi)
        self._obv_values.append(obv_rsi)
        self._bar_indices.append(bar_index)

        result = {
            "crsi_bull_div": False, "crsi_bear_div": False,
            "crsi_hid_bull": False, "crsi_hid_bear": False,
            "crsi_reg_bull_div": False, "crsi_reg_bear_div": False,
            "crsi_wide_bull_div": False, "crsi_wide_bear_div": False,
            "crsi_reg_hid_bull": False, "crsi_reg_hid_bear": False,
            "crsi_wide_hid_bull": False, "crsi_wide_hid_bear": False,
            "obv_bull_div": False, "obv_bear_div": False,
            "obv_hid_bull": False, "obv_hid_bear": False,
            "obv_reg_bull_div": False, "obv_reg_bear_div": False,
            "obv_wide_bull_div": False, "obv_wide_bear_div": False,
            "obv_reg_hid_bull": False, "obv_reg_hid_bear": False,
            "obv_wide_hid_bull": False, "obv_wide_hid_bear": False,
            "any_bull_div": False, "any_bear_div": False,
        }

        # ── cRSI divergence ──
        lb = self.crsi_lookback
        mb = self.crsi_max_bars
        min_len = lb * 2 + 1
        if len(self._highs) >= min_len:
            strict = self._detect_strict(
                self._highs, self._lows, self._crsi_values,
                lb, mb,
                self._prev_crsi_ph_price, self._prev_crsi_ph_rsi, self._prev_crsi_ph_bar,
                self._prev_crsi_pl_price, self._prev_crsi_pl_rsi, self._prev_crsi_pl_bar,
                bar_index,
            )
            wide = self._detect_sensitive(
                self._highs, self._lows, self._crsi_values,
                lb, mb,
                self._prev_crsi_ph_price, self._prev_crsi_ph_rsi, self._prev_crsi_ph_bar,
                self._prev_crsi_pl_price, self._prev_crsi_pl_rsi, self._prev_crsi_pl_bar,
                bar_index,
            )
            # Update prev pivots (strict takes priority for tracking)
            if strict["new_ph_price"] != self._prev_crsi_ph_price:
                self._prev_crsi_ph_price = strict["new_ph_price"]
                self._prev_crsi_ph_rsi = strict["new_ph_rsi"]
                self._prev_crsi_ph_bar = strict["new_ph_bar"]
            elif wide["new_ph_price"] != self._prev_crsi_ph_price:
                self._prev_crsi_ph_price = wide["new_ph_price"]
                self._prev_crsi_ph_rsi = wide["new_ph_rsi"]
                self._prev_crsi_ph_bar = wide["new_ph_bar"]

            if strict["new_pl_price"] != self._prev_crsi_pl_price:
                self._prev_crsi_pl_price = strict["new_pl_price"]
                self._prev_crsi_pl_rsi = strict["new_pl_rsi"]
                self._prev_crsi_pl_bar = strict["new_pl_bar"]
            elif wide["new_pl_price"] != self._prev_crsi_pl_price:
                self._prev_crsi_pl_price = wide["new_pl_price"]
                self._prev_crsi_pl_rsi = wide["new_pl_rsi"]
                self._prev_crsi_pl_bar = wide["new_pl_bar"]

            strict_bull = strict["bull_div"]
            strict_bear = strict["bear_div"]
            strict_hid_bull = strict["hid_bull"]
            strict_hid_bear = strict["hid_bear"]
            wide_bull = wide["bull_div"]
            wide_bear = wide["bear_div"]
            wide_hid_bull = wide["hid_bull"]
            wide_hid_bear = wide["hid_bear"]
            if self.div_type == "regular":
                strict_hid_bull = strict_hid_bear = False
                wide_hid_bull = wide_hid_bear = False
            elif self.div_type == "hidden":
                strict_bull = strict_bear = False
                wide_bull = wide_bear = False
            bull = strict_bull or wide_bull
            bear = strict_bear or wide_bear
            hid_bull = strict_hid_bull or wide_hid_bull
            hid_bear = strict_hid_bear or wide_hid_bear
            bull, bear, hid_bull, hid_bear = self._apply_type_filter(
                bull, bear, hid_bull, hid_bear,
            )
            result["crsi_bull_div"] = bull
            result["crsi_bear_div"] = bear
            result["crsi_hid_bull"] = hid_bull
            result["crsi_hid_bear"] = hid_bear
            result["crsi_reg_bull_div"] = strict_bull
            result["crsi_reg_bear_div"] = strict_bear
            result["crsi_wide_bull_div"] = wide_bull
            result["crsi_wide_bear_div"] = wide_bear
            result["crsi_reg_hid_bull"] = strict_hid_bull
            result["crsi_reg_hid_bear"] = strict_hid_bear
            result["crsi_wide_hid_bull"] = wide_hid_bull
            result["crsi_wide_hid_bear"] = wide_hid_bear

        # ── OBV RSI divergence ──
        lb = self.obv_lookback
        mb = self.obv_max_bars
        min_len = lb * 2 + 1
        if len(self._highs) >= min_len:
            strict = self._detect_strict(
                self._highs, self._lows, self._obv_values,
                lb, mb,
                self._prev_obv_ph_price, self._prev_obv_ph_rsi, self._prev_obv_ph_bar,
                self._prev_obv_pl_price, self._prev_obv_pl_rsi, self._prev_obv_pl_bar,
                bar_index,
            )
            wide = self._detect_sensitive(
                self._highs, self._lows, self._obv_values,
                lb, mb,
                self._prev_obv_ph_price, self._prev_obv_ph_rsi, self._prev_obv_ph_bar,
                self._prev_obv_pl_price, self._prev_obv_pl_rsi, self._prev_obv_pl_bar,
                bar_index,
            )
            if strict["new_ph_price"] != self._prev_obv_ph_price:
                self._prev_obv_ph_price = strict["new_ph_price"]
                self._prev_obv_ph_rsi = strict["new_ph_rsi"]
                self._prev_obv_ph_bar = strict["new_ph_bar"]
            elif wide["new_ph_price"] != self._prev_obv_ph_price:
                self._prev_obv_ph_price = wide["new_ph_price"]
                self._prev_obv_ph_rsi = wide["new_ph_rsi"]
                self._prev_obv_ph_bar = wide["new_ph_bar"]

            if strict["new_pl_price"] != self._prev_obv_pl_price:
                self._prev_obv_pl_price = strict["new_pl_price"]
                self._prev_obv_pl_rsi = strict["new_pl_rsi"]
                self._prev_obv_pl_bar = strict["new_pl_bar"]
            elif wide["new_pl_price"] != self._prev_obv_pl_price:
                self._prev_obv_pl_price = wide["new_pl_price"]
                self._prev_obv_pl_rsi = wide["new_pl_rsi"]
                self._prev_obv_pl_bar = wide["new_pl_bar"]

            strict_bull = strict["bull_div"]
            strict_bear = strict["bear_div"]
            strict_hid_bull = strict["hid_bull"]
            strict_hid_bear = strict["hid_bear"]
            wide_bull = wide["bull_div"]
            wide_bear = wide["bear_div"]
            wide_hid_bull = wide["hid_bull"]
            wide_hid_bear = wide["hid_bear"]
            if self.div_type == "regular":
                strict_hid_bull = strict_hid_bear = False
                wide_hid_bull = wide_hid_bear = False
            elif self.div_type == "hidden":
                strict_bull = strict_bear = False
                wide_bull = wide_bear = False
            bull = strict_bull or wide_bull
            bear = strict_bear or wide_bear
            hid_bull = strict_hid_bull or wide_hid_bull
            hid_bear = strict_hid_bear or wide_hid_bear
            bull, bear, hid_bull, hid_bear = self._apply_type_filter(
                bull, bear, hid_bull, hid_bear,
            )
            result["obv_bull_div"] = bull
            result["obv_bear_div"] = bear
            result["obv_hid_bull"] = hid_bull
            result["obv_hid_bear"] = hid_bear
            result["obv_reg_bull_div"] = strict_bull
            result["obv_reg_bear_div"] = strict_bear
            result["obv_wide_bull_div"] = wide_bull
            result["obv_wide_bear_div"] = wide_bear
            result["obv_reg_hid_bull"] = strict_hid_bull
            result["obv_reg_hid_bear"] = strict_hid_bear
            result["obv_wide_hid_bull"] = wide_hid_bull
            result["obv_wide_hid_bear"] = wide_hid_bear

        # Aggregate
        result["any_bull_div"] = (
            result["crsi_bull_div"] or result["obv_bull_div"]
            or result["crsi_hid_bull"] or result["obv_hid_bull"]
        )
        result["any_bear_div"] = (
            result["crsi_bear_div"] or result["obv_bear_div"]
            or result["crsi_hid_bear"] or result["obv_hid_bear"]
        )

        return result

    def reset(self):
        self._highs.clear()
        self._lows.clear()
        self._crsi_values.clear()
        self._obv_values.clear()
        self._bar_indices.clear()
        self._prev_crsi_ph_price = None
        self._prev_crsi_ph_rsi = None
        self._prev_crsi_ph_bar = None
        self._prev_crsi_pl_price = None
        self._prev_crsi_pl_rsi = None
        self._prev_crsi_pl_bar = None
        self._prev_obv_ph_price = None
        self._prev_obv_ph_rsi = None
        self._prev_obv_ph_bar = None
        self._prev_obv_pl_price = None
        self._prev_obv_pl_rsi = None
        self._prev_obv_pl_bar = None
