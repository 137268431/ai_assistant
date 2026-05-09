import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest import request_utils, runtime_service
from ibkr_compute.backtest.runtime_service import BacktestService
from ibkr_compute.core.exit_policy import normalize_exit_policy_profile, resolve_exit_policy
from ibkr_compute.core.risk_management import (
    compute_atr_tightened_stop,
    compute_exit_policy_stop_update,
    compute_exit_policy_target_update,
    compute_exit_policy_time_exit,
)
from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.core.time_utils import ET
from ibkr_compute.signal.signal_processor import SignalProcessor


class FakeConfig:
    def get_bool_for_environment(self, key, environment, default=False):
        return bool(default)

    def get_int_for_environment(self, key, environment, default=0):
        values = {
            "signal_validity_minutes": 30,
            "cooldown_bars_after_sl": 6,
            "cooldown_bars_after_reverse": 3,
        }
        return int(values.get(key, default))

    def get_for_environment(self, key, environment, default=""):
        values = {
            "trade_window_start_time": "00:00",
            "trade_window_end_time": "23:59",
            "order_window_end_time": "23:59",
        }
        return values.get(key, default)


class StrategyReliabilityEnhancementTests(unittest.TestCase):
    def test_backtest_default_scan_cutoff_matches_live_scan_time(self):
        request = request_utils.normalize_request(
            {
                "name": "default cutoff",
                "symbol_source": "daily_scan_replay",
                "symbols": "AAPL",
                "date_from": "2026-04-01",
                "date_to": "2026-04-01",
            }
        )

        self.assertEqual(request["premarket_cutoff_time"], "09:20")

    def test_backtest_indicator_persistence_defaults_off(self):
        request = request_utils.normalize_request(
            {
                "name": "memory only indicators",
                "symbol_source": "manual",
                "symbols": "AAPL",
                "date_from": "2026-04-01",
                "date_to": "2026-04-01",
            }
        )

        self.assertFalse(request["persist_backtest_indicators"])
        self.assertFalse(request["params"]["persist_backtest_indicators"])

    def test_backtest_indicator_persistence_can_be_enabled(self):
        request = request_utils.normalize_request(
            {
                "name": "debug indicator persistence",
                "symbol_source": "manual",
                "symbols": "AAPL",
                "date_from": "2026-04-01",
                "date_to": "2026-04-01",
                "persist_backtest_indicators": True,
            }
        )

        self.assertTrue(request["persist_backtest_indicators"])
        self.assertTrue(request["params"]["persist_backtest_indicators"])

    def test_backtest_date_to_is_clamped_to_latest_complete_et_date(self):
        real_datetime = request_utils.datetime
        with mock.patch.object(request_utils, "datetime") as mock_datetime:
            mock_datetime.now.return_value = real_datetime(2026, 5, 6, 10, 0, tzinfo=ET)
            request = request_utils.normalize_request(
                {
                    "name": "no current day",
                    "symbol_source": "manual",
                    "symbols": "AAPL",
                    "date_from": "2026-05-06",
                    "date_to": "2026-05-06",
                }
            )

        self.assertEqual(request["date_from"], "2026-05-05")
        self.assertEqual(request["date_to"], "2026-05-05")
        self.assertEqual(request["latest_complete_date"], "2026-05-05")
        self.assertTrue(request["date_to_clamped"])
        self.assertTrue(request["params"]["date_to_clamped"])

    def test_backtest_backfill_limits_are_normalized(self):
        request = request_utils.normalize_request(
            {
                "name": "bounded preflight",
                "symbol_source": "manual",
                "symbols": "AAPL",
                "date_from": "2026-05-01",
                "date_to": "2026-05-01",
                "backfill_concurrency": 99,
                "backfill_symbol_timeout_s": 999999,
                "backfill_max_batches": 999999,
                "backfill_history_timeout_s": 999999,
                "backfill_history_max_retries": 999999,
            }
        )

        self.assertEqual(request["backfill_concurrency"], 5)
        self.assertEqual(request["backfill_symbol_timeout_s"], 3600)
        self.assertEqual(request["backfill_max_batches"], 240)
        self.assertEqual(request["backfill_history_timeout_s"], 60)
        self.assertEqual(request["backfill_history_max_retries"], 5)
        self.assertEqual(request["params"]["backfill_max_batches"], 240)

    def test_backtest_machine_profile_is_recorded(self):
        request = request_utils.normalize_request(
            {
                "name": "4c8g safe",
                "symbol_source": "manual",
                "symbols": "AAPL",
                "date_from": "2026-05-01",
                "date_to": "2026-05-01",
                "machine_profile": "safe_4c8g",
            }
        )

        self.assertEqual(request["machine_profile"], "safe_4c8g")
        self.assertEqual(request["params"]["machine_profile"], "safe_4c8g")

    def test_portfolio_bar_compaction_round_trips_required_fields(self):
        service = BacktestService(None)
        bar = {
            "symbol": "AAPL",
            "exchange": "NASDAQ",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 1234,
            "session_type": "regular",
            "bar_time_ms": 1778155800000,
        }

        compact = service._compact_portfolio_bar(bar, is_last_bar=True, next_day="2026-05-06")
        inflated = service._inflate_portfolio_bar("AAPL", compact)

        self.assertIsInstance(compact, tuple)
        self.assertEqual(inflated["symbol"], "AAPL")
        self.assertEqual(inflated["exchange"], "NASDAQ")
        self.assertEqual(inflated["close"], 10.5)
        self.assertEqual(inflated["volume"], 1234.0)
        self.assertEqual(inflated["bar_time_ms"], 1778155800000)
        self.assertTrue(inflated["_backtest_is_last_bar"])
        self.assertEqual(inflated["_backtest_next_day"], "2026-05-06")

    def test_backtest_repair_windows_scope_to_missing_segments(self):
        service = BacktestService(None)
        interval_ms = 5 * 60 * 1000
        rows = [
            {"bar_time_ms": 100 * interval_ms, "us_time": "2026-05-05 09:30:00"},
            {"bar_time_ms": 101 * interval_ms, "us_time": "2026-05-05 09:35:00"},
            {"bar_time_ms": 106 * interval_ms, "us_time": "2026-05-05 10:00:00"},
            {"bar_time_ms": 107 * interval_ms, "us_time": "2026-05-05 10:05:00"},
        ]

        windows = service._build_backfill_repair_windows(
            rows,
            requested_start_ms=100 * interval_ms,
            requested_end_ms=107 * interval_ms,
        )

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["start_ms"], 102 * interval_ms)
        self.assertEqual(windows[0]["end_ms"], 105 * interval_ms)
        self.assertEqual(windows[0]["reason"], "internal_gap")

    def test_backtest_coverage_summary_uses_sqlite_window_diagnostics(self):
        service = BacktestService(None)
        bar_times = [
            datetime(2026, 5, 5, 9, 30, tzinfo=ET),
            datetime(2026, 5, 5, 9, 35, tzinfo=ET),
            datetime(2026, 5, 5, 10, 0, tzinfo=ET),
            datetime(2026, 5, 5, 10, 5, tzinfo=ET),
        ]
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with sqlite3.connect(tmp.name) as conn:
                conn.execute(
                    """
                    CREATE TABLE ibkr_bars (
                        symbol TEXT,
                        interval TEXT,
                        environment TEXT,
                        bar_time_ms INTEGER,
                        us_time TEXT
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO ibkr_bars(symbol, interval, environment, bar_time_ms, us_time) VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            "AAPL",
                            "5m",
                            "live",
                            int(item.timestamp() * 1000),
                            item.strftime("%Y-%m-%d %H:%M:%S"),
                        )
                        for item in bar_times
                    ],
                )
                conn.commit()

            old_path = runtime_service.BACKTEST_SQLITE_PATH
            runtime_service.BACKTEST_SQLITE_PATH = tmp.name
            try:
                summary = service._symbol_range_coverage_summary(
                    "AAPL",
                    "live",
                    "2026-05-05",
                    "2026-05-05",
                    warmup_bars=0,
                )
            finally:
                runtime_service.BACKTEST_SQLITE_PATH = old_path

        self.assertEqual(summary["diagnostic_source"], "sqlite_window")
        self.assertTrue(summary["needs_backfill"])
        self.assertEqual(summary["gap_count"], 1)
        self.assertEqual(summary["repair_window_count"], 1)
        self.assertEqual(summary["repair_windows"][0]["reason"], "internal_gap")
        self.assertEqual(summary["repair_windows"][0]["start_us"], "2026-05-05 09:40:00")
        self.assertEqual(summary["repair_windows"][0]["end_us"], "2026-05-05 09:55:00")

    def test_signal_window_expires_after_configured_bar_count(self):
        gen = SignalGenerator("AAPL", "5m", {"signal_window_max_bars": 1})

        gen.update({"sd_lower": True, "close": 100, "atr": 1})
        self.assertTrue(gen.last_trace["window_flags"]["sd_lower_active"])

        gen.update({"sd_lower": False, "close": 100, "atr": 1})
        self.assertTrue(gen.last_trace["window_flags"]["sd_lower_active"])

        gen.update({"sd_lower": False, "close": 100, "atr": 1})
        self.assertFalse(gen.last_trace["window_flags"]["sd_lower_active"])
        self.assertTrue(any("窗口超过1根K线" in item for item in gen.last_trace["events"]))

    def test_atr_stop_helper_only_tightens_risk(self):
        position = {
            "direction": "long",
            "entry_price": 100.0,
            "stop_price": 95.0,
            "target_price": 110.0,
            "original_stop_loss": 95.0,
            "last_stop_atr": 2.0,
        }

        tightened = compute_atr_tightened_stop(
            position,
            current_price=103.0,
            current_atr=1.0,
            sl_atr_mult=2.0,
            min_profit_r=0.3,
            deviation_threshold=0.3,
            min_change=0.01,
        )
        widened = compute_atr_tightened_stop(
            position,
            current_price=103.0,
            current_atr=3.0,
            sl_atr_mult=2.0,
            min_profit_r=0.3,
            deviation_threshold=0.3,
            min_change=0.01,
        )

        self.assertTrue(tightened["should_update"])
        self.assertGreater(tightened["new_sl"], position["stop_price"])
        self.assertFalse(widened["should_update"])

    def test_signal_mode_exit_policy_reprices_intraday_breakout(self):
        gen = SignalGenerator(
            "AAPL",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "exit_policy_profile": "signal_mode_adaptive_v1",
                "position_amount": 10000,
                "max_loss_per_trade": 1000,
            },
        )
        snapshot = {
            "close": 100.0,
            "open": 99.5,
            "high": 100.5,
            "low": 99.4,
            "atr": 1.0,
            "atr_pct": 1.0,
            "sd_squeeze_active": True,
            "sd_breakout_up": True,
            "sd_breakout_down": False,
            "sd_trend_walk_up": False,
            "sd_trend_walk_down": False,
            "vwap": 99.0,
            "vwap_bullish": True,
            "orb_breakout_down": False,
            "session_type": "regular",
            "rvol_20": 1.0,
            "dtp_dir": 0,
            "dtp_phase": "neutral",
            "dtp_phase_bars": 99,
        }

        signal = gen.update(snapshot)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["extra"]["exit_policy_profile"], "signal_mode_adaptive_v1")
        self.assertEqual(signal["extra"]["exit_policy_type"], "breakout")
        self.assertEqual(signal["extra"]["exit_policy"], "breakout_runner")
        self.assertAlmostEqual(signal["rr"], 2.5)
        self.assertGreater(signal["risk_r"], 0)
        self.assertGreater(signal["take_profit"], signal["entry"])

    def test_exit_policy_aliases_normalize_to_strategy_names(self):
        self.assertEqual(normalize_exit_policy_profile({"exit_policy_profile": "legacy"}), "fixed_atr_rr")
        self.assertEqual(
            normalize_exit_policy_profile({"exit_policy_profile": "setup_aware_v1"}),
            "signal_mode_adaptive_v1",
        )

        mr_policy = resolve_exit_policy(
            {"exit_policy_profile": "signal_mode_adaptive_v1"},
            setup="mr_sdUpper",
            signal_mode="mr",
        )
        trend_policy = resolve_exit_policy(
            {"exit_policy_profile": "signal_mode_adaptive_v1"},
            setup="trend_sdLower",
            signal_mode="trend",
        )

        self.assertEqual(mr_policy["name"], "fixed_atr_rr")
        self.assertEqual(mr_policy["tp_rr"], 1.5)
        self.assertEqual(trend_policy["name"], "chandelier_runner")
        self.assertEqual(trend_policy["tp_rr"], 2.0)

    def test_signal_mode_adaptive_v2_resolves_stateful_target_modes(self):
        self.assertEqual(
            normalize_exit_policy_profile({"exit_policy_profile": "signal_mode_adaptive_v2"}),
            "signal_mode_adaptive_v2",
        )
        mr_policy = resolve_exit_policy(
            {"exit_policy_profile": "signal_mode_adaptive_v2"},
            setup="mr_sdUpper",
            signal_mode="mr",
        )
        trend_policy = resolve_exit_policy(
            {"exit_policy_profile": "signal_mode_adaptive_v2"},
            setup="trend_sdLower",
            signal_mode="trend",
        )
        breakout_policy = resolve_exit_policy(
            {"exit_policy_profile": "signal_mode_adaptive_v2"},
            setup="squeeze_breakout_long",
            signal_mode="breakout",
        )

        self.assertEqual(mr_policy["target_mode"], "hard_rr")
        self.assertTrue(mr_policy["target_is_hard"])
        self.assertEqual(trend_policy["target_mode"], "checkpoint_then_trail")
        self.assertTrue(trend_policy["target_is_hard"])
        self.assertEqual(trend_policy["tp_rr"], 3.0)
        self.assertEqual(breakout_policy["target_mode"], "checkpoint_then_trail")
        self.assertTrue(breakout_policy["target_is_hard"])
        self.assertTrue(breakout_policy["failure_exit_enabled"])

    def test_signal_mode_adaptive_v2_signal_metadata_marks_breakout_checkpoint_target(self):
        gen = SignalGenerator(
            "AAPL",
            "5m",
            {
                "signal_strategy_profile": "intraday_sd_v1",
                "exit_policy_profile": "signal_mode_adaptive_v2",
                "position_amount": 10000,
                "max_loss_per_trade": 1000,
            },
        )
        snapshot = {
            "close": 100.0,
            "open": 99.5,
            "high": 100.5,
            "low": 99.4,
            "atr": 1.0,
            "atr_pct": 1.0,
            "sd_squeeze_active": True,
            "sd_breakout_up": True,
            "sd_breakout_down": False,
            "sd_trend_walk_up": False,
            "sd_trend_walk_down": False,
            "vwap": 99.0,
            "vwap_bullish": True,
            "orb_breakout_down": False,
            "session_type": "regular",
            "rvol_20": 1.0,
            "dtp_dir": 0,
            "dtp_phase": "neutral",
            "dtp_phase_bars": 99,
        }

        signal = gen.update(snapshot)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["extra"]["exit_policy_profile"], "signal_mode_adaptive_v2")
        self.assertEqual(signal["extra"]["exit_policy_type"], "breakout")
        self.assertEqual(signal["extra"]["exit_policy_settings"]["target_mode"], "checkpoint_then_trail")
        self.assertTrue(signal["extra"]["exit_policy_settings"]["target_is_hard"])
        self.assertEqual(signal["extra"]["target_state"]["target_mode"], "checkpoint_then_trail")

    def test_signal_mode_policy_trail_uses_chandelier_without_widening(self):
        position = {
            "direction": "long",
            "entry_price": 100.0,
            "stop_price": 98.0,
            "target_price": 110.0,
            "original_stop_loss": 98.0,
            "risk_r": 2.0,
            "exit_policy_profile": "signal_mode_adaptive_v1",
            "exit_policy_settings": {
                "trail_type": "chandelier",
                "trail_activation_r": 1.0,
                "chandelier_atr_mult": 2.0,
            },
            "trail_state": {"high_water": 100.0, "max_favorable_r": 0.0, "adjust_count": 0},
        }

        result = compute_exit_policy_stop_update(
            position,
            current_price=104.0,
            current_atr=1.0,
            bar_high=104.5,
            bar_low=103.0,
        )

        self.assertTrue(result["should_update"])
        self.assertGreater(result["new_sl"], position["stop_price"])
        self.assertLess(result["new_sl"], 104.0)
        self.assertEqual(result["trail_state"]["adjust_count"], 1)

    def test_soft_runner_target_touch_does_not_close_backtest_position(self):
        service = BacktestService(None)
        position = {
            "symbol": "AAPL",
            "direction": "long",
            "signal": "trend_sdLower",
            "signal_id": "trend",
            "reason": "",
            "entry_bar_ms": 1,
            "entry_us_time": "2026-04-08 10:00:00",
            "entry_cn_time": "",
            "entry_price": 100.0,
            "target_price": 104.0,
            "stop_price": 98.0,
            "shares": 10,
            "entry_commission": 0.05,
            "bars_held": 0,
            "exit_policy_settings": {"target_mode": "soft_runner", "target_is_hard": False},
        }
        bar = {"bar_time_ms": 2, "us_time": "2026-04-08 10:05:00", "high": 105.0, "low": 101.0, "close": 104.5}

        self.assertIsNone(service._check_exit(position, bar, commission_per_share=0.005, slippage_bps=0.0))

        hard_position = dict(position, bars_held=0, exit_policy_settings={"target_mode": "hard_rr", "target_is_hard": True})
        hard_close = service._check_exit(hard_position, bar, commission_per_share=0.005, slippage_bps=0.0)
        self.assertIsNotNone(hard_close)
        self.assertEqual(hard_close["exit_reason"], "take_profit")

    def test_checkpoint_target_tightens_stop_without_widening(self):
        position = {
            "direction": "long",
            "entry_price": 100.0,
            "stop_price": 98.0,
            "target_price": 110.0,
            "risk_r": 2.0,
            "mfe": 0.0,
            "exit_policy_settings": {
                "target_mode": "checkpoint_then_trail",
                "target_is_hard": True,
                "checkpoint_r": 1.0,
                "checkpoint_lock_r": 0.1,
            },
            "target_state": {},
        }

        result = compute_exit_policy_target_update(
            position,
            current_price=102.4,
            bar_high=102.5,
            bar_low=101.5,
        )

        self.assertTrue(result["should_update_stop"])
        self.assertAlmostEqual(result["new_sl"], 100.2)
        self.assertTrue(result["target_state"]["checkpoint_hit"])
        self.assertTrue(result["target_state"]["target_is_hard"])
        self.assertGreater(result["new_sl"], position["stop_price"])

        protected = dict(position, stop_price=101.0)
        no_widen = compute_exit_policy_target_update(
            protected,
            current_price=102.4,
            bar_high=102.5,
            bar_low=101.5,
        )
        self.assertFalse(no_widen["should_update_stop"])
        self.assertTrue(no_widen["target_state"]["checkpoint_hit"])

    def test_breakout_failure_time_exit_requires_v2_failure_flag(self):
        position = {
            "direction": "long",
            "entry_price": 100.0,
            "stop_price": 98.0,
            "risk_r": 2.0,
            "mfe": 0.6,
            "bars_held": 6,
            "exit_policy_settings": {
                "failure_exit_enabled": True,
                "failure_exit_bars": 6,
                "failure_exit_min_mfe_r": 0.5,
            },
        }

        failed = compute_exit_policy_time_exit(position)
        self.assertTrue(failed["should_exit"])
        self.assertEqual(failed["reason"], "exit_policy_breakout_failure")

        disabled = compute_exit_policy_time_exit(
            {
                **position,
                "exit_policy_settings": {
                    "failure_exit_enabled": False,
                    "failure_exit_bars": 6,
                    "failure_exit_min_mfe_r": 0.5,
                },
            }
        )
        self.assertFalse(disabled["should_exit"])

    def test_signal_processor_blocks_symbol_during_cooldown_and_active_target(self):
        processor = SignalProcessor(FakeConfig(), environment="live")
        signal = {
            "symbol": "AAPL",
            "direction": "short",
            "entry": 100.0,
            "stop_loss": 105.0,
            "take_profit": 90.0,
            "shares": 10,
        }

        processor.register_pending_entry("AAPL", {"direction": "long"})
        valid, reason = processor.validate_signal(signal)
        self.assertFalse(valid)
        self.assertEqual(reason, "direction_conflict")

        processor.remove_position("AAPL")
        processor.start_cooldown("AAPL", 3, "cooldown_after_reverse_close", now=datetime.now(ET))
        valid, reason = processor.validate_signal(signal)
        self.assertFalse(valid)
        self.assertEqual(reason, "cooldown_after_reverse_close")

    def test_backtest_opposite_signal_conflict_closes_instead_of_adjusting(self):
        service = BacktestService(None)
        reverse_row = service._build_backtest_reverse_signal_row(
            {
                "source_environment": "live",
                "compare_with_tv": False,
                "compare_tv_signals": False,
                "strategy_tag": "TEST",
            },
            "AAPL",
            {"bar_time_ms": 1775682300000, "us_time": "2026-04-08 17:05:00", "cn_time": "", "close": 108.0},
            10,
            {"close": 108.0},
            {},
            {
                "symbol": "AAPL",
                "direction": "long",
                "signal_id": "old",
                "entry_price": 100.0,
                "target_price": 110.0,
                "stop_price": 95.0,
            },
            target_state="filled_position",
            reverse_kind="signal_conflict",
            source="signal",
            origin_signal_payload={"signal_id": "new", "direction": "short"},
        )

        self.assertIsNotNone(reverse_row)
        self.assertEqual(reverse_row["action_type"], "close")


if __name__ == "__main__":
    unittest.main()
