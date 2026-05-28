"""
指标计算引擎 — 编排所有子指标, 返回统一快照

每个 IndicatorEngine 实例对应一个 (symbol, interval) 组合。
每根 bar 调用 update() 按顺序更新全部子指标, 合并为快照 dict。
"""

from collections import deque
import json

from .indicators.ema_trend_matrix import EmaTrendMatrix
from .indicators.fractal_pivot import FractalPivot
from .indicators.sd_channel import SDChannel
from .indicators.dtp import DTP
from .indicators.atr import ATRIndicator
from .indicators.crsi import CyclicRSI
from .indicators.obv_rsi import OBVRsi
from .indicators.technical import TechnicalIndicators
from .indicators.divergence import DivergenceDetector
from .indicators.filters import SignalFilters


DEFAULT_PARAMS = {
    "ema_slope_lookback": 12, "ema_min_angle": 0.01, "ema_min_spacing": 0.01,
    "ema_touch_type": "slow",
    "fractal_period": 4, "donchian_period": 20,
    "sd_length": 128, "sd_mult1": 1.0, "sd_mult2": 2.0, "sd_mult3": 3.0, "sd_mult4": 4.0,
    "sd_signal_band": 3, "sd_filter_band": 2,
    "sd_squeeze_lookback": 120, "sd_squeeze_rank_max": 0.25,
    "sd_flat_slope_pct": 0.03, "sd_breakout_confirm_bars": 1,
    "sd_trend_walk_min_bars": 2,
    "dtp_sma_length": 100, "dtp_atr_length": 200,
    "dtp_mult1": 3.0, "dtp_mult2": 6.0, "dtp_mult3": 9.0, "dtp_mult4": 12.0,
    "dtp_signal_band": 4, "dtp_momentum_lookback": 12, "dtp_trend_threshold": 0.1,
    "dtp_early_bars": 12, "dtp_mature_bars": 48,
    "atr_length": 10, "atr_smoothing": "RMA", "atr_multiplier": 1.5,
    "atr_pct_percentile_lookback": 100,
    "adx_length": 14, "adx_smoothing": 14,
    "macd_fast_length": 12, "macd_slow_length": 26, "macd_signal_length": 9,
    "ppo_fast_length": 12, "ppo_slow_length": 26, "ppo_signal_length": 9,
    "mfi_length": 14,
    "stochrsi_rsi_length": 14, "stochrsi_length": 14,
    "stochrsi_k_smoothing": 3, "stochrsi_d_smoothing": 3,
    "orb_bars": 6,
    "crsi_domcycle": 20, "crsi_vibration": 6, "crsi_leveling": 10.0,
    "crsi_div_lookback": 4, "crsi_div_max_bars": 30,
    "obv_rsi_len": 10, "obv_div_lookback": 4, "obv_div_max_bars": 30,
    "div_type": "both",
    "position_amount": 10000, "max_loss_per_trade": 150,
    "entry_atr_mult": 1.0, "sl_atr_mult": 2.0, "rr_ratio": 1.5,
    "exit_policy_profile": "setup_aware_hybrid_v1", "exit_policy_overrides": "",
    "entry_limit_mode": "passive_limit_dynamic",
    "entry_limit_atr_mult": 0.30,
    "entry_limit_floor_bps": 15.0,
    "entry_limit_cap_bps": 30.0,
    "marketable_limit_bps": 10.0,
    "signal_window_max_bars": 12,
    "enable_advanced_filter": True,
    "ema_strength_lookback": 20, "ema_weak_threshold": 0.005,
    "dtp_switch_lookback": 10, "dtp_max_switches": 3,
    "oscillation_lookback": 20, "oscillation_threshold": 0.4,
    "signal_strategy_profile": "intraday_sd_v1",
    "ibkr_signal_strategy_profile": "",
    "intraday_signal_validity_minutes": 15,
    "intraday_entry_window_start_time": "09:35",
    "intraday_entry_window_end_time": "10:30",
    "intraday_min_rvol_20": 1.2,
    "intraday_min_atr_pct": 0.08,
    "intraday_max_atr_pct": 1.20,
    "intraday_max_directional_day_change_pct": 4.0,
    "intraday_trend_mismatch_max_abs_day_change_pct": 0.0,
    "intraday_min_signal_quality_score": 70.0,
    "intraday_candidate_observation_min_quality_score": 60.0,
    "entry_plan_version": "entry_plan_v2",
    "entry_breakout_marketable_quality_min": 80.0,
    "entry_reprice_policy": "single_reprice_then_cancel",
    "intraday_vwap_pullback_atr_mult": 0.15,
    "intraday_vwap_pullback_max_bps": 10.0,
    "intraday_vwap_pullback_long_require_trend_walk": True,
    "intraday_setup_daily_limit": 1,
    "intraday_setup_cooldown_bars": 6,
    "intraday_reentry_policy": "controlled",
    "intraday_symbol_daily_entry_limit": 2,
    "intraday_include_legacy_signals": True,
}


def indicator_ready_bar_count(params: dict | None = None) -> int:
    effective = {**DEFAULT_PARAMS, **(params or {})}

    ema_slope_lookback = int(effective.get("ema_slope_lookback", 12))
    sd_length = int(effective.get("sd_length", 128))
    dtp_sma_length = int(effective.get("dtp_sma_length", 100))
    dtp_atr_length = int(effective.get("dtp_atr_length", 200))
    atr_length = int(effective.get("atr_length", 10))
    atr_pct_percentile_lookback = int(effective.get("atr_pct_percentile_lookback", 100))
    adx_length = int(effective.get("adx_length", 14))
    adx_smoothing = int(effective.get("adx_smoothing", 14))
    macd_fast_length = int(effective.get("macd_fast_length", 12))
    macd_slow_length = int(effective.get("macd_slow_length", 26))
    macd_signal_length = int(effective.get("macd_signal_length", 9))
    ppo_fast_length = int(effective.get("ppo_fast_length", 12))
    ppo_slow_length = int(effective.get("ppo_slow_length", 26))
    ppo_signal_length = int(effective.get("ppo_signal_length", 9))
    mfi_length = int(effective.get("mfi_length", 14))
    stochrsi_rsi_length = int(effective.get("stochrsi_rsi_length", 14))
    stochrsi_length = int(effective.get("stochrsi_length", 14))
    stochrsi_k_smoothing = int(effective.get("stochrsi_k_smoothing", 3))
    stochrsi_d_smoothing = int(effective.get("stochrsi_d_smoothing", 3))
    crsi_domcycle = int(effective.get("crsi_domcycle", 20))
    crsi_vibration = int(effective.get("crsi_vibration", 6))

    crsi_cycle_len = crsi_domcycle // 2
    crsi_phasing_lag = round((crsi_vibration - 1) / 2.0)

    return max(
        200 + ema_slope_lookback,
        sd_length,
        max(dtp_sma_length, dtp_atr_length),
        max(atr_length + 1, atr_pct_percentile_lookback),
        adx_length + adx_smoothing,
        max(macd_fast_length, macd_slow_length) + macd_signal_length,
        max(ppo_fast_length, ppo_slow_length) + ppo_signal_length,
        mfi_length + 1,
        stochrsi_rsi_length + stochrsi_length + stochrsi_k_smoothing + stochrsi_d_smoothing,
        crsi_cycle_len + crsi_phasing_lag + 2,
    )


def _normalize_param_interval(value: str) -> str:
    text = str(value or "").strip().lower()
    return {
        "5": "5m",
        "5m": "5m",
        "15": "15m",
        "15m": "15m",
        "30": "30m",
        "30m": "30m",
        "60": "1h",
        "1h": "1h",
        "240": "4h",
        "4h": "4h",
        "d": "1d",
        "1d": "1d",
    }.get(text, text or "5m")


def _parse_timeframe_param_profiles(raw) -> dict[str, dict]:
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
    else:
        return {}
    if not isinstance(parsed, dict):
        return {}
    profiles: dict[str, dict] = {}
    for interval, profile in parsed.items():
        if not isinstance(profile, dict):
            continue
        profile_params = profile.get("params") if isinstance(profile.get("params"), dict) else profile
        profiles[_normalize_param_interval(interval)] = dict(profile_params)
    return profiles


def params_for_interval(params: dict | None, interval: str) -> dict:
    effective = dict(params or {})
    normalized_interval = _normalize_param_interval(interval)
    if str(effective.get("_timeframe_profile_applied_interval") or "") == normalized_interval:
        return effective
    profile = _parse_timeframe_param_profiles(effective.get("ibkr_timeframe_param_profiles_json")).get(
        normalized_interval,
        {},
    )
    if profile:
        effective.update(profile)
    effective["_timeframe_profile_applied_interval"] = normalized_interval
    return effective


class IndicatorEngine:
    MAX_HISTORY = 300

    def __init__(self, symbol: str, interval: str, params: dict = None):
        self.symbol = symbol
        self.interval = interval
        self.params = {**DEFAULT_PARAMS, **params_for_interval(params, interval)}

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
        self.technical = TechnicalIndicators(self.params)
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

        # ⑥ Common technical indicators
        technical_out = self.technical.update(bar)

        # ⑦ cRSI
        crsi_out = self.crsi.update(bar)

        # ⑧ OBV RSI
        obv_out = self.obv.update(bar)

        # ⑨ Divergence (uses crsi and obv_rsi values)
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
            "bar_time_ms": int(bar.get("bar_time_ms", 0) or 0),
            "us_time": str(bar.get("us_time", "") or ""),
            "cn_time": str(bar.get("cn_time", "") or ""),
            "bar_count": self.bar_count,
            "session_type": bar.get("session_type", "regular"),
            "source": "ibkr_compute",
        }
        snapshot.update(ema_out)
        snapshot.update(frac_out)
        snapshot.update(sd_out)
        snapshot.update(dtp_out)
        snapshot.update(atr_out)
        snapshot.update(technical_out)
        snapshot.update(crsi_out)
        snapshot.update(obv_out)
        snapshot.update(div_out)

        # ⑩ Filters (needs merged snapshot)
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
            and self.technical.is_ready()
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
