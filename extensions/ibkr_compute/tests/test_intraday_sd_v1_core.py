import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.compute.runtime_state import universe as universe_mod
from ibkr_compute.api.compute.runtime_state import engines as engines_mod
from ibkr_compute.core.indicator_engine import IndicatorEngine
from ibkr_compute.core.indicators.atr import ATRIndicator
from ibkr_compute.core.indicators.sd_channel import SDChannel
from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.market.timeframe_utils import build_signal_id
from ibkr_compute.universe.target_execution import target_row_execution_metadata


def intraday_breakout_snapshot(**overrides):
    snapshot = {
        "close": 101.0,
        "open": 100.2,
        "high": 101.4,
        "low": 100.6,
        "atr": 1.5,
        "atr_raw": 1.0,
        "atr_pct": 1.0,
        "sd_zone": 1,
        "sd_trend": 1,
        "sd_regime": "breakout_up",
        "sd_squeeze_active": True,
        "sd_breakout_up": True,
        "sd_breakout_down": False,
        "sd_trend_walk_up": False,
        "sd_trend_walk_down": False,
        "vwap": 100.0,
        "vwap_upper1": 100.8,
        "vwap_lower1": 99.2,
        "vwap_bullish": True,
        "orb_breakout_up": True,
        "orb_breakout_down": False,
        "rvol_20": 1.4,
        "dollar_volume": 1_010_000.0,
        "session_type": "regular",
        "dtp_dir": 0,
        "dtp_phase": "neutral",
        "dtp_phase_bars": 99,
    }
    snapshot.update(overrides)
    return snapshot


def legacy_mr_long_snapshot(**overrides):
    snapshot = intraday_breakout_snapshot(
        sd_lower=True,
        sd_upper=False,
        sd_zone=-1,
        sd_regime="flat",
        sd_squeeze_active=False,
        sd_breakout_up=False,
        sd_breakout_down=False,
        sd_trend_walk_up=False,
        sd_trend_walk_down=False,
        fractal_bull=True,
        fractal_bear=False,
        crsi_bull_div=True,
        crsi_bear_div=False,
        obv_bull_div=False,
        obv_bear_div=False,
        block_mr_long=False,
        block_mr_short=False,
        block_ema_trend=False,
        block_all_signals=False,
    )
    snapshot.update(overrides)
    return snapshot


class IntradaySdV1CoreTest(unittest.TestCase):
    def test_sd_channel_outputs_derived_fields(self):
        indicator = SDChannel(
            {
                "sd_length": 5,
                "sd_signal_band": 2,
                "sd_filter_band": 1,
                "sd_squeeze_lookback": 3,
                "sd_breakout_confirm_bars": 1,
                "sd_trend_walk_min_bars": 2,
            }
        )

        output = {}
        for idx, close in enumerate([100, 101, 100.5, 101.5, 102, 103, 104]):
            output = indicator.update(
                {
                    "open": close - 0.2,
                    "high": close + 0.4,
                    "low": close - 0.4,
                    "close": close,
                    "volume": 1000 + idx,
                }
            )

        for key in (
            "sd_width_pct",
            "sd_width_rank",
            "sd_slope_pct",
            "sd_close_z",
            "sd_squeeze_active",
            "sd_breakout_up",
            "sd_breakout_down",
            "sd_trend_walk_up",
            "sd_trend_walk_down",
            "sd_regime",
        ):
            self.assertIn(key, output)

    def test_legacy_profile_does_not_emit_intraday_setups(self):
        gen = SignalGenerator("SPY", "5m", {})

        signal = gen.update(intraday_breakout_snapshot())

        self.assertIsNone(signal)
        trace = gen.get_trace_snapshot()
        self.assertEqual(trace["setup_state"]["strategy_profile"], "legacy")
        self.assertFalse(trace["setup_state"]["enabled"])

    def test_intraday_sd_v1_emits_squeeze_breakout_long(self):
        gen = SignalGenerator("SPY", "5m", {"signal_strategy_profile": "intraday_sd_v1"})

        signal = gen.update(intraday_breakout_snapshot())

        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal"], "sd_squeeze_breakout_long")
        self.assertEqual(signal["direction"], "long")
        self.assertEqual(signal["entry"], 100.7)
        self.assertLess(signal["entry"], 101.0)
        extra = signal["extra"]
        for key in (
            "strategy_profile",
            "setup",
            "sd_regime",
            "entry_order_type",
            "entry_limit_mode",
            "entry_price_plan",
            "validity_minutes",
            "trigger_checks",
            "filter_checks",
            "technical_description",
        ):
            self.assertIn(key, extra)
        self.assertEqual(extra["strategy_profile"], "intraday_sd_v1")
        self.assertEqual(extra["entry_order_type"], "limit")
        self.assertEqual(extra["entry_limit_mode"], "passive_limit_dynamic")
        self.assertEqual(extra["entry_price_plan"], "passive_limit_dynamic")
        self.assertEqual(extra["setup"], "sd_squeeze_breakout_long")
        self.assertEqual(extra["setup_family"], "breakout")
        self.assertEqual(extra["setup_label"], "SD Squeeze Breakout Long")
        self.assertEqual(extra["signal_mode"], "breakout")
        self.assertEqual(extra["entry_window_start_time"], "09:35")
        self.assertEqual(extra["entry_window_end_time"], "10:30")

        trace = gen.get_trace_snapshot()
        self.assertEqual(trace["signal_state"]["setup"], "sd_squeeze_breakout_long")
        self.assertEqual(trace["signal_state"]["entry_order_type"], "limit")
        self.assertEqual(trace["signal_state"]["entry_limit_mode"], "passive_limit_dynamic")
        self.assertEqual(trace["signal_state"]["entry_price_plan"], "passive_limit_dynamic")
        self.assertEqual(trace["setup_state"]["selected_setup"], "sd_squeeze_breakout_long")

    def test_intraday_sd_v1_blocks_signal_against_target_direction(self):
        gen = SignalGenerator(
            "AVGO",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_require_target_direction_alignment": True,
                "target_direction_bias": "long",
            },
        )

        signal = gen.update(
            intraday_breakout_snapshot(
                close=450.10,
                open=449.0,
                high=451.2,
                low=449.8,
                atr=3.0,
                sd_regime="breakout_down",
                sd_breakout_up=False,
                sd_breakout_down=True,
                vwap=451.0,
                vwap_bullish=False,
                orb_breakout_up=False,
                orb_breakout_down=True,
            )
        )

        self.assertIsNone(signal)
        trace = gen.get_trace_snapshot()
        self.assertEqual(trace["signal_state"]["stage"], "blocked")
        self.assertFalse(trace["signal_state"]["filter_checks"]["target_direction_alignment"])

    def test_intraday_sd_v1_blocks_late_new_setups_by_default(self):
        gen = SignalGenerator("SPY", "5m", {"signal_strategy_profile": "intraday_sd_v1"})

        signal = gen.update(intraday_breakout_snapshot(us_time="2026-05-01 15:05:00"))

        self.assertIsNone(signal)
        trace = gen.get_trace_snapshot()
        self.assertEqual(trace["signal_state"]["stage"], "blocked")
        self.assertEqual(trace["signal_state"]["setup"], "sd_squeeze_breakout_long")
        self.assertIn("intraday_entry_window", trace["signal_state"]["filter_reason"])

    def test_intraday_sd_v1_includes_legacy_sd_signals_by_default(self):
        gen = SignalGenerator("SPY", "5m", {"signal_strategy_profile": "intraday_sd_v1"})

        signal = gen.update(legacy_mr_long_snapshot())

        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal"], "sd_mr_reversal_long")
        trace = gen.get_trace_snapshot()
        self.assertTrue(trace["component_flags"]["legacy_signals_enabled"])
        self.assertTrue(trace["component_flags"]["buy_raw"])
        self.assertEqual(trace["signal_state"]["stage"], "confirmed")

    def test_intraday_sd_v1_can_opt_out_of_legacy_sd_signals(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_include_legacy_signals": False,
            },
        )

        signal = gen.update(legacy_mr_long_snapshot())

        self.assertIsNone(signal)
        trace = gen.get_trace_snapshot()
        self.assertFalse(trace["component_flags"]["legacy_signals_enabled"])

    def test_intraday_sd_v1_legacy_sd_uses_unified_entry_plan(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_include_legacy_signals": True,
            },
        )

        signal = gen.update(legacy_mr_long_snapshot())

        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal"], "sd_mr_reversal_long")
        self.assertEqual(signal["extra"]["entry_order_type"], "limit")
        self.assertEqual(signal["extra"]["entry_limit_mode"], "passive_limit_dynamic")
        self.assertEqual(signal["extra"]["entry_price_plan"], "passive_limit_dynamic")
        trace = gen.get_trace_snapshot()
        legacy_candidate = next(
            item for item in trace["setup_state"]["candidates"]
            if item["source"] == "legacy_sd" and item["setup"] == "sd_mr_reversal_long"
        )
        self.assertTrue(legacy_candidate["triggered"])
        self.assertEqual(signal["extra"]["setup_family"], "mean_reversion")
        self.assertEqual(signal["extra"]["setup_label"], "SD MR Reversal Long")

    def test_intraday_sd_v1_canonical_setups_have_distinct_signal_id_suffixes(self):
        bar_ms = 1777660200000
        setups = [
            "sd_squeeze_breakout_long",
            "sd_squeeze_breakout_short",
            "vwap_trend_pullback_long",
            "vwap_trend_pullback_short",
            "sd_trend_continuation_long",
            "sd_trend_continuation_short",
            "sd_mr_reversal_long",
            "sd_mr_reversal_short",
        ]

        signal_ids = {build_signal_id("SPY", bar_ms, setup) for setup in setups}

        self.assertEqual(len(signal_ids), len(setups))
        self.assertTrue(all(signal_id.startswith("SPY_") for signal_id in signal_ids))

    def test_intraday_entry_window_can_be_extended_by_params(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_entry_window_end_time": "11:00",
            },
        )

        signal = gen.update(intraday_breakout_snapshot(us_time="2026-05-01 10:35:00"))

        self.assertIsNotNone(signal)
        self.assertEqual(signal["extra"]["entry_window_end_time"], "11:00")

    def test_intraday_liquidity_and_atr_filters_block_new_setups(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_min_rvol_20": 0.5,
                "intraday_min_atr_pct": 0.8,
                "intraday_max_atr_pct": 1.6,
            },
        )

        signal = gen.update(intraday_breakout_snapshot(rvol_20=0.4, atr_pct=1.0))

        self.assertIsNone(signal)
        trace = gen.get_trace_snapshot()
        self.assertEqual(trace["signal_state"]["stage"], "blocked")
        self.assertFalse(trace["signal_state"]["filter_checks"]["rvol_20_min"])
        self.assertIn("rvol_20_min", trace["signal_state"]["filter_reason"])

    def test_intraday_liquidity_and_atr_filters_allow_qualified_new_setups(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_min_rvol_20": 0.5,
                "intraday_min_atr_pct": 0.8,
                "intraday_max_atr_pct": 1.6,
            },
        )

        signal = gen.update(intraday_breakout_snapshot(rvol_20=0.8, atr_pct=1.1))

        self.assertIsNotNone(signal)
        self.assertTrue(signal["extra"]["filter_checks"]["rvol_20_min"])
        self.assertTrue(signal["extra"]["filter_checks"]["atr_pct_min"])
        self.assertTrue(signal["extra"]["filter_checks"]["atr_pct_max"])

    def test_intraday_directional_day_change_filter_blocks_extended_move(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_max_directional_day_change_pct": 3.0,
            },
        )

        signal = gen.update(intraday_breakout_snapshot(day_change_pct=3.1))

        self.assertIsNone(signal)
        trace = gen.get_trace_snapshot()
        self.assertEqual(trace["signal_state"]["stage"], "blocked")
        self.assertFalse(trace["signal_state"]["filter_checks"]["directional_day_change_max"])

    def test_intraday_vwap_pullback_long_can_require_trend_walk_regime(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_vwap_pullback_long_require_trend_walk": True,
            },
        )

        signal = gen.update(
            intraday_breakout_snapshot(
                sd_regime="breakout_up",
                sd_squeeze_active=False,
                sd_breakout_up=False,
                sd_trend_walk_up=True,
                low=100.2,
            )
        )

        self.assertIsNone(signal)
        trace = gen.get_trace_snapshot()
        self.assertEqual(trace["signal_state"]["stage"], "none")
        long_candidate = next(
            item for item in trace["setup_state"]["candidates"]
            if item["setup"] == "vwap_trend_pullback_long"
        )
        self.assertFalse(long_candidate["trigger_checks"]["trend_walk_regime"])

    def test_intraday_vwap_pullback_long_requires_real_vwap_touch(self):
        gen = SignalGenerator("SPY", "5m", {"signal_strategy_profile": "intraday_sd_v1"})

        signal = gen.update(
            intraday_breakout_snapshot(
                sd_regime="trend_walk_up",
                sd_squeeze_active=False,
                sd_breakout_up=False,
                sd_trend_walk_up=True,
                close=100.35,
                low=100.25,
                vwap=100.0,
                vwap_upper1=100.8,
                atr_raw=1.0,
            )
        )

        self.assertIsNone(signal)
        trace = gen.get_trace_snapshot()
        long_candidate = next(
            item for item in trace["setup_state"]["candidates"]
            if item["setup"] == "vwap_trend_pullback_long"
        )
        self.assertFalse(long_candidate["trigger_checks"]["pullback_touched_vwap"])

    def test_intraday_vwap_pullback_long_confirms_after_touch_and_reclaim(self):
        gen = SignalGenerator("SPY", "5m", {"signal_strategy_profile": "intraday_sd_v1"})

        signal = gen.update(
            intraday_breakout_snapshot(
                sd_regime="trend_walk_up",
                sd_squeeze_active=False,
                sd_breakout_up=False,
                sd_trend_walk_up=True,
                close=100.35,
                low=100.05,
                vwap=100.0,
                vwap_upper1=100.8,
                atr_raw=1.0,
            )
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal"], "vwap_trend_pullback_long")
        self.assertTrue(signal["extra"]["trigger_checks"]["pullback_touched_vwap"])
        self.assertTrue(signal["extra"]["trigger_checks"]["close_not_extended_above_vwap_band"])

    def test_intraday_vwap_pullback_long_rejects_extended_close_above_band(self):
        gen = SignalGenerator("SPY", "5m", {"signal_strategy_profile": "intraday_sd_v1"})

        signal = gen.update(
            intraday_breakout_snapshot(
                sd_regime="trend_walk_up",
                sd_squeeze_active=False,
                sd_breakout_up=False,
                sd_trend_walk_up=True,
                close=101.2,
                low=100.05,
                vwap=100.0,
                vwap_upper1=100.8,
                atr_raw=1.0,
            )
        )

        self.assertIsNone(signal)
        trace = gen.get_trace_snapshot()
        long_candidate = next(
            item for item in trace["setup_state"]["candidates"]
            if item["setup"] == "vwap_trend_pullback_long"
        )
        self.assertFalse(long_candidate["trigger_checks"]["close_not_extended_above_vwap_band"])

    def test_intraday_vwap_pullback_short_requires_trend_walk_and_touch(self):
        gen = SignalGenerator("SPY", "5m", {"signal_strategy_profile": "intraday_sd_v1"})

        signal = gen.update(
            intraday_breakout_snapshot(
                sd_regime="trend_walk_down",
                sd_squeeze_active=False,
                sd_breakout_up=False,
                sd_breakout_down=False,
                sd_trend_walk_up=False,
                sd_trend_walk_down=True,
                close=99.65,
                high=99.95,
                vwap=100.0,
                vwap_lower1=99.2,
                vwap_bullish=False,
                atr_raw=1.0,
            )
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal"], "vwap_trend_pullback_short")
        self.assertTrue(signal["extra"]["trigger_checks"]["pullback_touched_vwap"])
        self.assertTrue(signal["extra"]["trigger_checks"]["trend_walk_regime"])

    def test_intraday_setup_daily_limit_blocks_repeat_same_day(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_reentry_policy": "single_setup",
            },
        )

        first = gen.update(
            intraday_breakout_snapshot(
                us_time="2026-05-01 09:40:00",
                sd_regime="trend_walk_up",
                sd_squeeze_active=False,
                sd_breakout_up=False,
                sd_trend_walk_up=True,
                close=100.35,
                low=100.05,
                vwap=100.0,
                vwap_upper1=100.8,
                atr_raw=1.0,
            )
        )
        second = gen.update(
            intraday_breakout_snapshot(
                us_time="2026-05-01 09:45:00",
                sd_regime="trend_walk_up",
                sd_squeeze_active=False,
                sd_breakout_up=False,
                sd_trend_walk_up=True,
                close=100.35,
                low=100.05,
                vwap=100.0,
                vwap_upper1=100.8,
                atr_raw=1.0,
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        trace = gen.get_trace_snapshot()
        self.assertEqual(trace["signal_state"]["stage"], "blocked")
        self.assertFalse(trace["signal_state"]["filter_checks"]["setup_daily_limit"])
        self.assertFalse(trace["signal_state"]["filter_checks"]["setup_cooldown"])

    def test_intraday_controlled_reentry_allows_second_and_blocks_third_symbol_signal(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_reentry_policy": "controlled",
                "intraday_symbol_daily_entry_limit": 2,
                "intraday_setup_cooldown_bars": 1,
            },
        )

        first = gen.update(intraday_breakout_snapshot(us_time="2026-05-01 09:40:00"))
        self.assertIsNotNone(first)

        self.assertIsNone(
            gen.update(
                intraday_breakout_snapshot(
                    us_time="2026-05-01 09:45:00",
                    sd_squeeze_active=False,
                    sd_breakout_up=False,
                    sd_trend_walk_up=False,
                )
            )
        )
        second = gen.update(intraday_breakout_snapshot(us_time="2026-05-01 09:50:00"))
        self.assertIsNotNone(second)
        self.assertTrue(second["extra"]["filter_checks"]["setup_daily_limit"])
        self.assertTrue(second["extra"]["filter_checks"]["symbol_daily_entry_limit"])

        self.assertIsNone(
            gen.update(
                intraday_breakout_snapshot(
                    us_time="2026-05-01 09:55:00",
                    sd_squeeze_active=False,
                    sd_breakout_up=False,
                    sd_trend_walk_up=False,
                )
            )
        )
        third = gen.update(intraday_breakout_snapshot(us_time="2026-05-01 10:00:00"))

        self.assertIsNone(third)
        trace = gen.get_trace_snapshot()
        self.assertEqual(trace["signal_state"]["stage"], "blocked")
        self.assertFalse(trace["signal_state"]["filter_checks"]["symbol_daily_entry_limit"])
        self.assertEqual(trace["signal_state"]["filter_reason"], "symbol_daily_entry_limit_reached")

    def test_intraday_passive_dynamic_short_entry_prices_above_close_amd_style(self):
        gen = SignalGenerator(
            "AMD",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "entry_limit_mode": "marketable_limit_dynamic",
                "marketable_limit_bps": 10,
            },
        )

        signal = gen.update(
            intraday_breakout_snapshot(
                close=450.10,
                open=449.0,
                high=451.2,
                low=449.8,
                atr=3.0,
                sd_regime="breakout_down",
                sd_breakout_up=False,
                sd_breakout_down=True,
                vwap=451.0,
                vwap_bullish=False,
                orb_breakout_up=False,
                orb_breakout_down=True,
            )
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal"], "sd_squeeze_breakout_short")
        self.assertEqual(signal["direction"], "short")
        self.assertEqual(signal["entry"], 451.0)
        self.assertGreater(signal["entry"], 450.10)
        self.assertAlmostEqual(signal["extra"]["entry_limit_offset"], 0.90)
        self.assertEqual(signal["extra"]["entry_order_type"], "limit")
        self.assertEqual(signal["extra"]["entry_limit_mode"], "passive_limit_dynamic")
        self.assertEqual(signal["extra"]["entry_price_plan"], "passive_limit_dynamic")
        self.assertGreater(signal["stop_loss"], signal["entry"])
        self.assertLess(signal["take_profit"], signal["entry"])

    def test_marketable_limit_bps_mode_remains_backwards_compatible(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "entry_limit_mode": "marketable_limit_bps",
                "marketable_limit_bps": 10,
            },
        )

        signal = gen.update(
            intraday_breakout_snapshot(
                close=100.0,
                sd_regime="breakout_down",
                sd_breakout_up=False,
                sd_breakout_down=True,
                vwap=101.0,
                vwap_bullish=False,
                orb_breakout_up=False,
                orb_breakout_down=True,
            )
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal["entry"], 99.9)
        self.assertEqual(signal["extra"]["entry_order_type"], "limit")
        self.assertEqual(signal["extra"]["entry_limit_mode"], "marketable_limit_bps")
        self.assertEqual(signal["extra"]["entry_price_plan"], "marketable_limit_bps")

    def test_opposite_direction_candidates_are_blocked_as_ambiguous(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "intraday_include_legacy_signals": True,
            },
        )

        signal = gen.update(
            legacy_mr_long_snapshot(
                sd_squeeze_active=True,
                sd_breakout_down=True,
                vwap=102.0,
                orb_breakout_up=False,
            )
        )

        self.assertIsNone(signal)
        trace = gen.get_trace_snapshot()
        self.assertEqual(trace["signal_state"]["stage"], "blocked")
        self.assertEqual(trace["signal_state"]["filter_reason"], "ambiguous_opposite_directions")
        self.assertTrue(trace["setup_state"]["ambiguous_opposite_directions"])

    def test_atr_resets_vwap_context_by_us_market_date(self):
        indicator = ATRIndicator({"orb_bars": 2, "atr_length": 1})

        first = indicator.update(
            {
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
                "session_type": "regular",
                "us_time": "2026-05-01 09:30:00",
            }
        )
        second = indicator.update(
            {
                "open": 102,
                "high": 103,
                "low": 101,
                "close": 102,
                "volume": 2000,
                "session_type": "regular",
                "us_time": "2026-05-04 09:30:00",
            }
        )

        self.assertEqual(first["regular_bar_index"], 1)
        self.assertEqual(second["regular_bar_index"], 1)
        self.assertEqual(second["orb_high"], 103)
        self.assertEqual(second["orb_low"], 101)

    def test_indicator_engine_snapshot_carries_bar_time_fields(self):
        engine = IndicatorEngine("SPY", "5m", {"atr_length": 1})
        engine.update(
            {
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
                "session_type": "regular",
                "bar_time_ms": 1777631400000,
                "us_time": "2026-05-01 09:30:00",
                "cn_time": "2026-05-01 21:30:00",
            }
        )
        snapshot = engine.update(
            {
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 101,
                "volume": 1200,
                "session_type": "regular",
                "bar_time_ms": 1777631700000,
                "us_time": "2026-05-01 09:35:00",
                "cn_time": "2026-05-01 21:35:00",
            }
        )

        self.assertEqual(snapshot["bar_time_ms"], 1777631700000)
        self.assertEqual(snapshot["us_time"], "2026-05-01 09:35:00")
        self.assertEqual(snapshot["cn_time"], "2026-05-01 21:35:00")

    def test_runtime_signal_params_include_strategy_config(self):
        timeframe_profiles_json = json.dumps({"5m": {"sd_length": 64}, "1d": {"sd_length": 220}})

        class FakeCfg:
            def get_for_environment(self, key, environment, default=None):
                if key == "signal_strategy_profile":
                    return "intraday_sd_v1"
                if key == "intraday_entry_window_start_time":
                    return "09:40"
                if key == "intraday_entry_window_end_time":
                    return "10:20"
                if key == "ibkr_timeframe_param_profiles_json":
                    return timeframe_profiles_json
                return default

            def get_int_for_environment(self, key, environment, default=0):
                if key == "intraday_signal_validity_minutes":
                    return 7
                if key == "signal_window_max_bars":
                    return 9
                return default

            def get_float_for_environment(self, key, environment, default=0.0):
                if key == "marketable_limit_bps":
                    return 8.0
                if key == "intraday_min_rvol_20":
                    return 0.5
                if key == "intraday_min_atr_pct":
                    return 0.8
                if key == "intraday_max_atr_pct":
                    return 1.6
                if key == "intraday_max_directional_day_change_pct":
                    return 3.0
                if key == "intraday_trend_mismatch_max_abs_day_change_pct":
                    return 2.0
                return default

            def get_bool_for_environment(self, key, environment, default=False):
                if key == "ibkr_market_ws_enabled":
                    return False
                if key == "intraday_vwap_pullback_long_require_trend_walk":
                    return True
                if key == "intraday_include_legacy_signals":
                    return False
                return default

        fake_app = SimpleNamespace(
            WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR="market_monitor",
            pb=SimpleNamespace(
                get_all_records=lambda collection, **kwargs: [
                    {
                        "symbol": "AAPL",
                        "status": "active",
                        "direction_bias": "long",
                        "extra": {
                            "source": "daily_scan",
                            "active_gate_passed": True,
                            "strategy_policy": {
                                "recommended_signal_profile": "intraday_sd_v1",
                                "recommended_exit_policy": {"sl_atr_mult": 1.8, "tp_rr": 2.5},
                            },
                            "symbol_profile": {"threshold_profile": "large_liquid"},
                        },
                    },
                    {"symbol": "SPY", "status": "active", "extra": {}},
                ]
                if collection == "ibkr_targets"
                else []
            ),
            cfg=FakeCfg(),
            current_market_date=lambda: "2026-05-01",
            normalize_symbol_csv=lambda text: [item.strip().upper() for item in str(text or "").split(",") if item.strip()],
            normalize_watchlist_symbol_role=lambda role: str(role or "").strip().lower(),
        )

        with mock.patch.object(universe_mod, "_api_app", return_value=fake_app), mock.patch.object(
            universe_mod,
            "load_effective_watchlist",
            return_value={"SPY": {"symbol": "SPY", "symbol_role": "market_monitor"}},
        ):
            params = universe_mod.get_signal_generator_params("live")

        self.assertEqual(params["signal_strategy_profile"], "intraday_sd_v1")
        self.assertEqual(params["intraday_signal_validity_minutes"], 7)
        self.assertEqual(params["intraday_entry_window_start_time"], "09:40")
        self.assertEqual(params["intraday_entry_window_end_time"], "10:20")
        self.assertEqual(params["signal_window_max_bars"], 9)
        self.assertEqual(params["entry_limit_mode"], "passive_limit_dynamic")
        self.assertEqual(params["marketable_limit_bps"], 8.0)
        self.assertEqual(params["intraday_min_rvol_20"], 0.5)
        self.assertEqual(params["intraday_min_atr_pct"], 0.8)
        self.assertEqual(params["intraday_max_atr_pct"], 1.6)
        self.assertEqual(params["intraday_max_directional_day_change_pct"], 3.0)
        self.assertEqual(params["intraday_trend_mismatch_max_abs_day_change_pct"], 2.0)
        self.assertTrue(params["intraday_vwap_pullback_long_require_trend_walk"])
        self.assertFalse(params["intraday_include_legacy_signals"])
        self.assertEqual(params["ibkr_timeframe_param_profiles_json"], timeframe_profiles_json)
        self.assertEqual(universe_mod.signal_generator_params_for_interval(params, "5m")["sd_length"], 64)
        self.assertEqual(params["market_monitor_symbols"], "SPY")
        self.assertEqual(params["signal_enabled_symbols"], "AAPL")
        strategy_map = json.loads(params["target_strategy_policy_by_symbol"])
        profile_map = json.loads(params["target_symbol_profile_by_symbol"])
        self.assertEqual(strategy_map["AAPL"]["recommended_exit_policy"]["tp_rr"], 2.5)
        self.assertEqual(profile_map["AAPL"]["threshold_profile"], "large_liquid")

    def test_market_monitor_symbols_do_not_generate_signals_even_if_active(self):
        gen = SignalGenerator(
            "SPY",
            "5m",
            {"market_monitor_symbols": "SPY,QQQ,VIX", "signal_enabled_symbols": "SPY"},
        )
        self.assertTrue(gen.symbol_is_market_monitor)

        diagnostic_gen = SignalGenerator(
            "SPY",
            "5m",
            {
                "market_monitor_symbols": "SPY,QQQ,VIX",
                "signal_enabled_symbols": "SPY",
                "allow_market_monitor_signals": True,
            },
        )
        self.assertFalse(diagnostic_gen.symbol_is_market_monitor)

    def test_active_target_direction_biases_match_selected_trade_rows(self):
        class FakeCfg:
            def get_for_environment(self, key, environment, default=None):
                if key == "ibkr_market_ws_symbols":
                    return "SPY,QQQ,VIX"
                return default

            def get_int_for_environment(self, key, environment, default=0):
                if key == "ibkr_target_subscription_limit":
                    return 2
                if key == "ibkr_total_subscription_limit":
                    return 5
                return default

            def get_bool_for_environment(self, key, environment, default=False):
                return default

        fake_app = SimpleNamespace(
            WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR="market_monitor",
            pb=SimpleNamespace(
                get_all_records=lambda collection, **kwargs: [
                    {"symbol": "SPY", "status": "active", "direction_bias": "long", "extra": {}},
                    {
                        "symbol": "APP",
                        "status": "active",
                        "direction_bias": "short",
                        "extra": {"source": "daily_scan", "active_gate_passed": True},
                    },
                    {"symbol": "AMZN", "status": "active", "direction_bias": "long", "extra": {"source": "manual_page"}},
                    {
                        "symbol": "DDOG",
                        "status": "active",
                        "direction_bias": "long",
                        "extra": {"source": "daily_scan", "active_gate_passed": True},
                    },
                ]
                if collection == "ibkr_targets"
                else []
            ),
            cfg=FakeCfg(),
            current_market_date=lambda: "2026-05-01",
            normalize_symbol_csv=lambda text: [item.strip().upper() for item in str(text or "").split(",") if item.strip()],
            normalize_watchlist_symbol_role=lambda role: str(role or "").strip().lower(),
        )

        with mock.patch.object(universe_mod, "_api_app", return_value=fake_app), mock.patch.object(
            universe_mod,
            "load_effective_watchlist",
            return_value={"SPY": {"symbol": "SPY", "symbol_role": "market_monitor"}},
        ):
            biases = universe_mod.get_active_target_direction_biases("live")
            symbols = universe_mod.get_active_trade_symbols("live")

        self.assertEqual(biases, {"APP": "short", "DDOG": "long"})
        self.assertEqual(symbols, {"APP", "DDOG"})

    def test_paper_active_targets_read_shared_live_target_rows(self):
        filters = []

        class FakeCfg:
            def get_for_environment(self, key, environment, default=None):
                if key == "ibkr_market_ws_symbols":
                    return "SPY,QQQ,VIX"
                return default

            def get_int_for_environment(self, key, environment, default=0):
                return default

            def get_bool_for_environment(self, key, environment, default=False):
                return default

        def get_all_records(collection, **kwargs):
            if collection != "ibkr_targets":
                return []
            filters.append(str(kwargs.get("filter") or ""))
            return [
                {
                    "symbol": "APP",
                    "status": "active",
                    "direction_bias": "short",
                    "extra": {"source": "daily_scan", "active_gate_passed": True},
                }
            ]

        fake_app = SimpleNamespace(
            WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR="market_monitor",
            pb=SimpleNamespace(get_all_records=get_all_records),
            cfg=FakeCfg(),
            current_market_date=lambda: "2026-05-18",
            normalize_symbol_csv=lambda text: [item.strip().upper() for item in str(text or "").split(",") if item.strip()],
            normalize_watchlist_symbol_role=lambda role: str(role or "").strip().lower(),
        )

        with mock.patch.dict(os.environ, {"IBKR_MARKET_DATA_MODE": "live"}, clear=False), mock.patch.object(
            universe_mod,
            "_api_app",
            return_value=fake_app,
        ), mock.patch.object(universe_mod, "load_effective_watchlist", return_value={}):
            symbols = universe_mod.get_active_trade_symbols("paper")

        self.assertEqual(symbols, {"APP"})
        self.assertTrue(filters)
        self.assertIn('environment = "live"', filters[0])
        self.assertNotIn('environment = "paper"', filters[0])

    def test_signal_params_include_unsubscribed_active_target_policy(self):
        class FakeCfg:
            def get_for_environment(self, key, environment, default=None):
                if key == "ibkr_market_ws_symbols":
                    return "SPY,QQQ,VIX"
                return default

            def get_int_for_environment(self, key, environment, default=0):
                if key == "ibkr_target_subscription_limit":
                    return 1
                if key == "ibkr_total_subscription_limit":
                    return 5
                return default

            def get_bool_for_environment(self, key, environment, default=False):
                if key == "ibkr_target_strategy_policy_enabled":
                    return True
                return default

            def get_float_for_environment(self, key, environment, default=0.0):
                return default

        rows = [
            {
                "symbol": "APP",
                "status": "active",
                "direction_bias": "short",
                "extra": {
                    "source": "daily_scan",
                    "active_gate_passed": True,
                    "strategy_policy": {"recommended_signal_profile": "intraday_sd_v1"},
                    "symbol_profile": {"threshold_profile": "large_liquid"},
                },
            },
            {
                "symbol": "DDOG",
                "status": "active",
                "direction_bias": "long",
                "extra": {
                    "source": "daily_scan",
                    "context_gate_passed": True,
                    "strategy_policy": {"recommended_exit_policy": {"tp_rr": 2.1}},
                },
            },
        ]
        fake_app = SimpleNamespace(
            WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR="market_monitor",
            pb=SimpleNamespace(
                get_all_records=lambda collection, **kwargs: rows
                if collection == "ibkr_targets"
                else []
            ),
            cfg=FakeCfg(),
            current_market_date=lambda: "2026-05-01",
            normalize_symbol_csv=lambda text: [item.strip().upper() for item in str(text or "").split(",") if item.strip()],
            normalize_watchlist_symbol_role=lambda role: str(role or "").strip().lower(),
        )

        with mock.patch.object(universe_mod, "_api_app", return_value=fake_app), mock.patch.object(
            universe_mod,
            "load_effective_watchlist",
            return_value={"SPY": {"symbol": "SPY", "symbol_role": "market_monitor"}},
        ):
            params = universe_mod.get_signal_generator_params("live")
            selected_symbols = universe_mod.get_active_trade_symbols("live")

        self.assertEqual(selected_symbols, {"APP"})
        self.assertEqual(params["signal_enabled_symbols"], "APP,DDOG")
        self.assertTrue(params["intraday_require_target_direction_alignment"])
        bias_map = json.loads(params["target_direction_bias_by_symbol"])
        strategy_map = json.loads(params["target_strategy_policy_by_symbol"])
        profile_map = json.loads(params["target_symbol_profile_by_symbol"])
        self.assertEqual(bias_map, {"APP": "short", "DDOG": "long"})
        self.assertEqual(strategy_map["APP"]["recommended_signal_profile"], "intraday_sd_v1")
        self.assertEqual(strategy_map["DDOG"]["recommended_exit_policy"]["tp_rr"], 2.1)
        self.assertEqual(profile_map["APP"]["threshold_profile"], "large_liquid")

    def test_signal_params_exclude_watch_only_execution_layer(self):
        class FakeCfg:
            def get_for_environment(self, key, environment, default=None):
                if key == "ibkr_market_ws_symbols":
                    return "SPY,QQQ,VIX"
                return default

            def get_int_for_environment(self, key, environment, default=0):
                return default

            def get_bool_for_environment(self, key, environment, default=False):
                if key == "ibkr_target_strategy_policy_enabled":
                    return True
                return default

            def get_float_for_environment(self, key, environment, default=0.0):
                return default

        rows = [
            {
                "symbol": "APP",
                "status": "active",
                "direction_bias": "short",
                "extra": {
                    "source": "daily_scan",
                    "active_gate_passed": True,
                    "strategy_policy": {"allowed_sides": ["short"]},
                },
            },
            {
                "symbol": "TOST",
                "status": "active",
                "direction_bias": "long",
                "extra": {
                    "source": "daily_scan",
                    "active_gate_passed": True,
                    "strategy_policy": {"setup_type": "watch_only", "allowed_sides": ["long"]},
                },
            },
        ]
        fake_app = SimpleNamespace(
            WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR="market_monitor",
            pb=SimpleNamespace(
                get_all_records=lambda collection, **kwargs: rows
                if collection == "ibkr_targets"
                else []
            ),
            cfg=FakeCfg(),
            current_market_date=lambda: "2026-05-01",
            normalize_symbol_csv=lambda text: [item.strip().upper() for item in str(text or "").split(",") if item.strip()],
            normalize_watchlist_symbol_role=lambda role: str(role or "").strip().lower(),
        )

        with mock.patch.object(universe_mod, "_api_app", return_value=fake_app), mock.patch.object(
            universe_mod,
            "load_effective_watchlist",
            return_value={},
        ):
            params = universe_mod.get_signal_generator_params("live")
            selected_symbols = universe_mod.get_active_trade_symbols("live")
            biases = universe_mod.get_active_target_direction_biases("live")

        self.assertEqual(selected_symbols, {"APP", "TOST"})
        self.assertEqual(params["signal_enabled_symbols"], "APP")
        self.assertEqual(biases, {"APP": "short"})
        strategy_map = json.loads(params["target_strategy_policy_by_symbol"])
        self.assertEqual(set(strategy_map), {"APP"})

    def test_execution_metadata_recomputes_stale_persisted_flag(self):
        metadata = target_row_execution_metadata(
            {
                "symbol": "TOST",
                "status": "active",
                "direction_bias": "long",
                "extra": {
                    "source": "daily_scan",
                    "active_gate_passed": True,
                    "execution_eligible": True,
                    "target_layer": "execution",
                    "strategy_policy": {"setup_type": "watch_only", "allowed_sides": ["long"]},
                },
            }
        )

        self.assertFalse(metadata["execution_eligible"])
        self.assertEqual("observe", metadata["target_layer"])
        self.assertIn("watch_only", metadata["execution_blockers"])

    def test_live_signal_params_apply_per_symbol_target_policy(self):
        params = engines_mod._signal_params_for_symbol(
            {
                "exit_policy_profile": "fixed_atr_rr",
                "sl_atr_mult": 2.0,
                "rr_ratio": 1.5,
                "signal_strategy_profile": "legacy",
                "target_strategy_policy_enabled": True,
                "target_strategy_policy_by_symbol": json.dumps(
                    {
                        "APP": {
                            "recommended_signal_profile": "intraday_sd_v1",
                            "recommended_exit_policy": {
                                "exit_policy_profile": "signal_mode_adaptive_v1",
                                "sl_atr_mult": 1.8,
                                "tp_rr": 2.5,
                            },
                        }
                    }
                ),
                "target_direction_bias_by_symbol": json.dumps({"APP": "short"}),
                "target_symbol_profile_by_symbol": json.dumps({"APP": {"threshold_profile": "large_liquid"}}),
            },
            "APP",
        )

        self.assertEqual(params["exit_policy_profile"], "signal_mode_adaptive_v1")
        self.assertEqual(params["sl_atr_mult"], 1.8)
        self.assertEqual(params["rr_ratio"], 2.5)
        self.assertEqual(params["signal_strategy_profile"], "intraday_sd_v1")
        self.assertEqual(params["target_direction_bias"], "short")
        self.assertEqual(params["target_symbol_profile"]["threshold_profile"], "large_liquid")

    def test_live_signal_params_apply_target_direction_without_strategy_policy(self):
        params = engines_mod._signal_params_for_symbol(
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "target_strategy_policy_enabled": False,
                "target_direction_bias_by_symbol": json.dumps({"APP": "short"}),
                "target_strategy_policy_by_symbol": json.dumps(
                    {"APP": {"recommended_signal_profile": "legacy"}}
                ),
            },
            "APP",
        )

        self.assertEqual(params["target_direction_bias"], "short")
        self.assertEqual(params["signal_strategy_profile"], "intraday_sd_v1")
        self.assertNotIn("target_strategy_policy", params)

    def test_timeframe_param_profiles_apply_only_to_matching_interval(self):
        from ibkr_compute.core.indicator_engine import IndicatorEngine
        from ibkr_compute.core.signal_generator import SignalGenerator

        base_params = {
            "sd_length": 128,
            "atr_length": 10,
            "signal_strategy_profile": "legacy",
            "ibkr_timeframe_param_profiles_json": json.dumps(
                {
                    "5m": {
                        "sd_length": 64,
                        "atr_length": 8,
                        "signal_strategy_profile": "intraday_sd_v1",
                    },
                    "1d": {
                        "sd_length": 220,
                        "atr_length": 21,
                        "signal_strategy_profile": "daily_sd_v1",
                    },
                }
            ),
        }

        five_min_params = universe_mod.signal_generator_params_for_interval(base_params, "5m")
        daily_params = universe_mod.signal_generator_params_for_interval(base_params, "1d")

        self.assertEqual(five_min_params["sd_length"], 64)
        self.assertEqual(five_min_params["atr_length"], 8)
        self.assertEqual(five_min_params["signal_strategy_profile"], "intraday_sd_v1")
        self.assertEqual(daily_params["sd_length"], 220)
        self.assertEqual(daily_params["atr_length"], 21)
        self.assertEqual(daily_params["signal_strategy_profile"], "daily_sd_v1")

        self.assertEqual(IndicatorEngine("APP", "1d", base_params).params["sd_length"], 220)
        self.assertEqual(SignalGenerator("APP", "5m", base_params).params["sd_length"], 64)

    def test_timeframe_profile_applies_before_symbol_strategy_policy(self):
        from ibkr_compute.core.signal_generator import SignalGenerator

        base_params = {
            "exit_policy_profile": "fixed_atr_rr",
            "sl_atr_mult": 2.0,
            "rr_ratio": 1.5,
            "signal_strategy_profile": "legacy",
            "target_strategy_policy_enabled": True,
            "target_strategy_policy_by_symbol": json.dumps(
                {
                    "APP": {
                        "recommended_signal_profile": "policy_signal",
                        "recommended_exit_policy": {
                            "exit_policy_profile": "policy_exit",
                            "sl_atr_mult": 1.8,
                            "tp_rr": 2.5,
                        },
                    }
                }
            ),
            "ibkr_timeframe_param_profiles_json": json.dumps(
                {
                    "5m": {
                        "exit_policy_profile": "profile_exit",
                        "sl_atr_mult": 1.1,
                        "rr_ratio": 1.2,
                        "signal_strategy_profile": "profile_signal",
                    },
                    "1d": {
                        "exit_policy_profile": "daily_profile_exit",
                        "sl_atr_mult": 3.0,
                        "rr_ratio": 4.0,
                        "signal_strategy_profile": "daily_profile_signal",
                    },
                }
            ),
        }

        interval_params = universe_mod.signal_generator_params_for_interval(base_params, "5")
        effective_params = engines_mod._signal_params_for_symbol(interval_params, "APP")
        daily_params = universe_mod.signal_generator_params_for_interval(base_params, "D")

        self.assertEqual(effective_params["exit_policy_profile"], "policy_exit")
        self.assertEqual(effective_params["sl_atr_mult"], 1.8)
        self.assertEqual(effective_params["rr_ratio"], 2.5)
        self.assertEqual(effective_params["signal_strategy_profile"], "policy_signal")
        self.assertEqual(daily_params["exit_policy_profile"], "daily_profile_exit")
        self.assertEqual(daily_params["sl_atr_mult"], 3.0)
        self.assertEqual(SignalGenerator("APP", "5m", effective_params).params["signal_strategy_profile"], "policy_signal")
        self.assertEqual(SignalGenerator("APP", "5m", effective_params).params["sl_atr_mult"], 1.8)

    def test_get_or_create_engine_passes_interval_specific_params(self):
        created_engines = []
        created_signal_generators = []

        class FakeIndicatorEngine:
            def __init__(self, symbol, interval, params=None):
                self.symbol = symbol
                self.interval = interval
                self.params = dict(params or {})
                created_engines.append(self)

        class FakeSignalGenerator:
            def __init__(self, symbol, interval, params=None):
                self.symbol = symbol
                self.interval = interval
                self.params = dict(params or {})
                created_signal_generators.append(self)

            def set_params(self, params):
                self.params = dict(params or {})

        fake_app = SimpleNamespace(
            compute_lock=mock.MagicMock(),
            engines={},
            signal_gens={},
        )
        fake_app.compute_lock.__enter__.return_value = None
        fake_app.compute_lock.__exit__.return_value = None
        signal_params = {
            "sd_length": 128,
            "atr_length": 10,
            "target_strategy_policy_enabled": False,
            "ibkr_timeframe_param_profiles_json": json.dumps(
                {
                    "5m": {"sd_length": 64, "atr_length": 8},
                    "1d": {"sd_length": 220, "atr_length": 21},
                }
            ),
        }

        with mock.patch.object(engines_mod, "_api_app", return_value=fake_app), mock.patch.object(
            engines_mod,
            "IndicatorEngine",
            FakeIndicatorEngine,
        ), mock.patch.object(engines_mod, "SignalGenerator", FakeSignalGenerator):
            engines_mod.get_or_create_engine("live", "APP", "5m", signal_params=signal_params)
            engines_mod.get_or_create_engine("live", "APP", "1d", signal_params=signal_params)

        self.assertEqual(created_engines[0].params["sd_length"], 64)
        self.assertEqual(created_engines[0].params["atr_length"], 8)
        self.assertEqual(created_signal_generators[0].params["sd_length"], 64)
        self.assertEqual(created_engines[1].params["sd_length"], 220)
        self.assertEqual(created_engines[1].params["atr_length"], 21)
        self.assertEqual(created_signal_generators[1].params["atr_length"], 21)


if __name__ == "__main__":
    unittest.main()
