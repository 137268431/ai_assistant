import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest import request_utils
from ibkr_compute.backtest.execution_cost import (
    apply_execution_slippage,
    build_execution_cost_profile,
    calculate_execution_commission,
    summarize_execution_costs,
)
from ibkr_compute.backtest.runtime_service import BacktestService


class BacktestExecutionCostTests(unittest.TestCase):
    def test_legacy_flat_per_share_matches_existing_defaults(self):
        profile = build_execution_cost_profile({"commission_per_share": 0.005, "slippage_bps": 2.0})

        commission = calculate_execution_commission(shares=100, price=25.0, side="buy", profile=profile)
        fill = apply_execution_slippage(
            price=100.0,
            direction="long",
            is_entry=True,
            profile=profile,
            shares=100,
        )

        self.assertEqual(profile["fee_model"], "legacy_flat_per_share_v1")
        self.assertAlmostEqual(commission["commission"], 0.5)
        self.assertAlmostEqual(fill["fill_price"], 100.02)

    def test_ibkr_fixed_model_applies_minimum_and_one_percent_cap(self):
        profile = build_execution_cost_profile({"fee_model": "ibkr_us_equity_fixed_v1"})

        minimum = calculate_execution_commission(shares=100, price=25.0, side="buy", profile=profile)
        normal = calculate_execution_commission(shares=1000, price=25.0, side="buy", profile=profile)
        capped = calculate_execution_commission(shares=100, price=0.25, side="buy", profile=profile)

        self.assertAlmostEqual(minimum["commission"], 1.0)
        self.assertAlmostEqual(normal["commission"], 5.0)
        self.assertAlmostEqual(capped["commission"], 0.25)

    def test_bar_capped_slippage_respects_limit_price(self):
        profile = build_execution_cost_profile(
            {
                "slippage_model": "bar_capped_bps_v1",
                "slippage_bps": 200,
                "slippage_cap_to_bar": True,
                "limit_price_protection": True,
            }
        )

        fill = apply_execution_slippage(
            price=101.0,
            direction="long",
            is_entry=True,
            profile=profile,
            bar={"high": 104.0, "low": 100.0, "close": 102.0, "volume": 10000, "session_type": "regular"},
            shares=10,
            limit_price=102.0,
            order_type="marketable_limit",
        )

        self.assertAlmostEqual(fill["fill_price"], 102.0)
        self.assertTrue(fill["limit_cap_applied"])

    def test_request_normalizes_new_execution_fields(self):
        request = request_utils.normalize_request(
            {
                "symbols": "AAPL",
                "date_from": "2026-04-01",
                "date_to": "2026-04-01",
                "account_model_mode": "current_snapshot",
                "fee_model": "ibkr_us_equity_fixed_v1",
                "slippage_model": "volume_share_v1",
            }
        )

        self.assertEqual(request["account_model_mode"], "current_snapshot")
        self.assertEqual(request["fee_model"], "ibkr_us_equity_fixed_v1")
        self.assertEqual(request["slippage_model"], "volume_share_v1")
        self.assertTrue(request["slippage_cap_to_bar"])

    def test_trade_extra_contains_gross_net_and_cost_summary(self):
        service = BacktestService(None)
        request = request_utils.normalize_request(
            {
                "symbols": "AAPL",
                "date_from": "2026-04-01",
                "date_to": "2026-04-01",
                "fee_model": "ibkr_us_equity_fixed_v1",
                "slippage_model": "bar_capped_bps_v1",
                "slippage_bps": 0,
            }
        )
        profile = build_execution_cost_profile(request)
        position = service._open_position(
            "AAPL",
            {"open": 100.0, "high": 101.0, "low": 99.0, "bar_time_ms": 1, "us_time": "2026-04-01 09:35:00"},
            {"direction": "long", "shares": 100, "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0},
            request["commission_per_share"],
            request["slippage_bps"],
            execution_profile=profile,
        )
        trade = service._close_position(
            position,
            {"close": 101.0, "high": 101.0, "low": 100.0, "bar_time_ms": 2, "us_time": "2026-04-01 09:40:00"},
            request["commission_per_share"],
            request["slippage_bps"],
            "last_bar",
            profile,
        )
        summary = summarize_execution_costs([trade], profile)

        self.assertAlmostEqual(trade["extra"]["gross_pnl"], 100.0)
        self.assertAlmostEqual(trade["extra"]["total_commission"], 2.0)
        self.assertAlmostEqual(trade["pnl"], 98.0)
        self.assertAlmostEqual(summary["total_commission"], 2.0)


if __name__ == "__main__":
    unittest.main()

