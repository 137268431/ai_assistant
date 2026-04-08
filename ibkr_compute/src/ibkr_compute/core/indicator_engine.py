"""
指标计算引擎 — 编排所有子指标, 返回统一快照

每个 IndicatorEngine 实例对应一个 (symbol, interval) 组合。
每根 bar 调用 update() 按顺序更新全部子指标, 合并为快照 dict。
"""

from collections import deque

from .indicators.ema_trend_matrix import EmaTrendMatrix
from .indicators.fractal_pivot import FractalPivot
from .indicators.sd_channel import SDChannel
from .indicators.dtp import DTP
from .indicators.atr import ATRIndicator
from .indicators.crsi import CyclicRSI
from .indicators.obv_rsi import OBVRsi
from .indicators.divergence import DivergenceDetector
from .indicators.filters import SignalFilters


DEFAULT_PARAMS = {
    "ema_slope_lookback": 12, "ema_min_angle": 0.01, "ema_min_spacing": 0.01,
    "ema_touch_type": "slow",
    "fractal_period": 4, "donchian_period": 20,
    "sd_length": 128, "sd_mult1": 1.0, "sd_mult2": 2.0, "sd_mult3": 3.0, "sd_mult4": 4.0,
    "sd_signal_band": 3, "sd_filter_band": 2,
    "dtp_sma_length": 100, "dtp_atr_length": 200,
    "dtp_mult1": 3.0, "dtp_mult2": 6.0, "dtp_mult3": 9.0, "dtp_mult4": 12.0,
    "dtp_signal_band": 4, "dtp_momentum_lookback": 12, "dtp_trend_threshold": 0.1,
    "dtp_early_bars": 12, "dtp_mature_bars": 48,
    "atr_length": 10, "atr_smoothing": "RMA", "atr_multiplier": 1.5,
    "crsi_domcycle": 20, "crsi_vibration": 6, "crsi_leveling": 10.0,
    "crsi_div_lookback": 4, "crsi_div_max_bars": 30,
    "obv_rsi_len": 10, "obv_div_lookback": 4, "obv_div_max_bars": 30,
    "div_type": "both",
    "position_amount": 10000, "max_loss_per_trade": 150,
    "entry_atr_mult": 1.0, "sl_atr_mult": 2.0, "rr_ratio": 1.5,
    "enable_advanced_filter": True,
    "ema_strength_lookback": 20, "ema_weak_threshold": 0.005,
    "dtp_switch_lookback": 10, "dtp_max_switches": 3,
    "oscillation_lookback": 20, "oscillation_threshold": 0.4,
    "market_index_symbols": "SPY,QQQ,VIX",
}


class IndicatorEngine:
    MAX_HISTORY = 300

    def __init__(self, symbol: str, interval: str, params: dict = None):
        self.symbol = symbol
        self.interval = interval
        self.params = {**DEFAULT_PARAMS, **(params or {})}

        self.bars = deque(maxlen=self.MAX_HISTORY)
        self.bar_count = 0
        self.last_bar_time_ms = 0
        self._snapshot = {}

        # 子指标
        self.ema = EmaTrendMatrix(self.params)
        self.fractal = FractalPivot(self.params)
        self.sd = SDChannel(self.params)
        self.dtp = DTP(self.params)
        self.atr_ind = ATRIndicator(self.params)
        self.crsi = CyclicRSI(self.params)
        self.obv = OBVRsi(self.params)
        self.divergence = DivergenceDetector(self.params)
        self.filters = SignalFilters(self.params)

    def update(self, bar: dict) -> dict:
        bar_time_ms = bar.get("bar_time_ms", 0)
        if bar_time_ms <= self.last_bar_time_ms:
            return self._snapshot

        self.bars.append(bar)
        self.bar_count += 1
        self.last_bar_time_ms = bar_time_ms

        if len(self.bars) < 2:
            return self._snapshot

        # ① EMA Trend Matrix
        ema_out = self.ema.update(bar)

        # ② Fractal Pivot
        frac_out = self.fractal.update(bar)

        # ③ SD Channel
        sd_out = self.sd.update(bar)

        # ④ DTP (needs sd_trend from SD Channel)
        dtp_ctx = {"sd_trend": sd_out.get("sd_trend", 0)}
        dtp_out = self.dtp.update(bar, context=dtp_ctx)

        # ⑤ ATR + VWAP
        atr_out = self.atr_ind.update(bar)

        # ⑥ cRSI
        crsi_out = self.crsi.update(bar)

        # ⑦ OBV RSI
        obv_out = self.obv.update(bar)

        # ⑧ Divergence (uses crsi and obv_rsi values)
        crsi_val = crsi_out.get("crsi", 50.0)
        obv_val = obv_out.get("obv_rsi", 50.0)
        div_out = self.divergence.update(
            bar_index=self.bar_count,
            high=float(bar.get("high", 0)),
            low=float(bar.get("low", 0)),
            crsi=crsi_val,
            obv_rsi=obv_val,
        )

        # 合并快照
        snapshot = {
            "close": float(bar.get("close", 0)),
            "open": float(bar.get("open", 0)),
            "high": float(bar.get("high", 0)),
            "low": float(bar.get("low", 0)),
            "volume": float(bar.get("volume", 0)),
            "bar_count": self.bar_count,
            "session_type": bar.get("session_type", "regular"),
            "source": "ibkr_compute",
        }
        snapshot.update(ema_out)
        snapshot.update(frac_out)
        snapshot.update(sd_out)
        snapshot.update(dtp_out)
        snapshot.update(atr_out)
        snapshot.update(crsi_out)
        snapshot.update(obv_out)
        snapshot.update(div_out)

        # ⑨ Filters (needs merged snapshot)
        filter_out = self.filters.update(snapshot)
        snapshot.update(filter_out)

        self._snapshot = snapshot
        return self._snapshot

    def get_snapshot(self) -> dict:
        return self._snapshot.copy()

    def is_ready(self) -> bool:
        return (
            self.ema.is_ready()
            and self.sd.is_ready()
            and self.dtp.is_ready()
            and self.atr_ind.is_ready()
            and self.crsi.is_ready()
        )

    def reset(self):
        self.bars.clear()
        self.bar_count = 0
        self.last_bar_time_ms = 0
        self._snapshot = {}
        self.ema.reset()
        self.fractal.reset()
        self.sd.reset()
        self.dtp.reset()
        self.atr_ind.reset()
        self.crsi.reset()
        self.obv.reset()
        self.divergence.reset()
        self.filters.reset()
