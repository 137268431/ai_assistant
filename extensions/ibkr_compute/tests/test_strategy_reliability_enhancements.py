import sys
import unittest
from datetime import datetime
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest import request_utils
from ibkr_compute.backtest.runtime_service import BacktestService
from ibkr_compute.core.risk_management import compute_atr_tightened_stop
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
