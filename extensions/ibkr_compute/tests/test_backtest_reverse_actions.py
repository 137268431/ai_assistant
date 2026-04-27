import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest.runtime_service import BacktestService


class BacktestReverseActionTests(unittest.TestCase):
    def setUp(self):
        self.service = BacktestService(None)
        self.request = {
            "source_environment": "live",
            "compare_with_tv": False,
            "compare_tv_signals": False,
            "strategy_tag": "TEST",
        }
        self.bar = {
            "bar_time_ms": 1775682300000,
            "us_time": "2026-04-08 17:05:00",
            "cn_time": "2026-04-09 05:05:00",
            "close": 108.0,
        }
        self.base_position = {
            "symbol": "AAPL",
            "direction": "long",
            "signal": "mr_L",
            "signal_id": "AAPL_20260408_1600_mr_L",
            "entry_bar_ms": 1775678400000,
            "entry_us_time": "2026-04-08 16:00:00",
            "entry_cn_time": "2026-04-09 04:00:00",
            "entry_price": 100.0,
            "target_price": 110.0,
            "stop_price": 95.0,
            "shares": 10,
            "bars_held": 4,
            "entry_commission": 0.05,
            "signal_bar_ms": 1775678100000,
            "signal_us_time": "2026-04-08 15:55:00",
            "signal_close": 100.2,
        }

    def test_indicator_conflict_near_target_adjusts_take_profit(self):
        snapshot = {
            "close": 108.0,
            "crsi": 55,
            "crsi_bear_div": True,
            "obv_rsi": 50,
            "vwap_dist": 0,
        }

        reverse_row = self.service._build_backtest_reverse_signal_row(
            self.request,
            "AAPL",
            self.bar,
            10,
            snapshot,
            {},
            dict(self.base_position),
            target_state="filled_position",
            reverse_kind="indicator_conflict",
            source="indicator",
        )

        self.assertIsNotNone(reverse_row)
        self.assertEqual(reverse_row["action_type"], "adjust_tp")
        extra = reverse_row["extra"]
        self.assertEqual(extra["old_tp"], 110.0)
        self.assertAlmostEqual(extra["new_tp"], 108.7)
        self.assertEqual(extra["progress_ratio"], 0.8)

        updated_position, trade = self.service._apply_backtest_position_reverse_action(
            dict(self.base_position),
            reverse_row,
            self.bar,
            commission_per_share=0.005,
            slippage_bps=2.0,
        )
        self.assertIsNone(trade)
        self.assertAlmostEqual(updated_position["target_price"], 108.7)

    def test_indicator_conflict_before_target_progress_keeps_adjusting_stop(self):
        snapshot = {
            "close": 104.0,
            "crsi": 55,
            "crsi_bear_div": True,
            "obv_rsi": 50,
            "vwap_dist": 0,
        }
        bar = dict(self.bar, close=104.0)

        reverse_row = self.service._build_backtest_reverse_signal_row(
            self.request,
            "AAPL",
            bar,
            10,
            snapshot,
            {},
            dict(self.base_position),
            target_state="filled_position",
            reverse_kind="indicator_conflict",
            source="indicator",
        )

        self.assertIsNotNone(reverse_row)
        self.assertEqual(reverse_row["action_type"], "adjust_sl")
        extra = reverse_row["extra"]
        self.assertEqual(extra["old_sl"], 95.0)
        self.assertGreater(extra["new_sl"], 95.0)
        self.assertEqual(extra["progress_ratio"], 0.4)

    def test_indicator_conflict_near_target_adjusts_short_take_profit(self):
        position = dict(
            self.base_position,
            direction="short",
            entry_price=255.7288,
            target_price=253.44,
            stop_price=257.34,
        )
        snapshot = {
            "close": 254.09,
            "crsi": 45,
            "crsi_bull_div": True,
            "obv_rsi": 50,
            "vwap_dist": 0,
        }
        bar = dict(self.bar, close=254.09)

        reverse_row = self.service._build_backtest_reverse_signal_row(
            self.request,
            "AAPL",
            bar,
            10,
            snapshot,
            {},
            position,
            target_state="filled_position",
            reverse_kind="indicator_conflict",
            source="indicator",
        )

        self.assertIsNotNone(reverse_row)
        self.assertEqual(reverse_row["action_type"], "adjust_tp")
        extra = reverse_row["extra"]
        self.assertEqual(extra["old_tp"], 253.44)
        self.assertLess(extra["new_tp"], 254.09)
        self.assertGreater(extra["new_tp"], 253.44)
        self.assertAlmostEqual(extra["progress_ratio"], 0.716, places=3)


if __name__ == "__main__":
    unittest.main()
