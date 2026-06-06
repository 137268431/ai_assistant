import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.validate import run_gateway_order_probe as probe  # noqa: E402


class GatewayOrderProbeTest(unittest.TestCase):
    def test_build_non_marketable_long_bracket_stays_below_reference(self):
        entry, take_profit, stop_loss = probe.build_non_marketable_bracket(
            100.0,
            direction="long",
            entry_distance_pct=0.50,
            protection_gap_pct=0.15,
        )

        self.assertEqual(50.0, entry)
        self.assertEqual(57.5, take_profit)
        self.assertEqual(42.5, stop_loss)
        self.assertLess(take_profit, 100.0)
        self.assertLess(stop_loss, entry)

    def test_build_non_marketable_short_bracket_stays_above_reference(self):
        entry, take_profit, stop_loss = probe.build_non_marketable_bracket(
            100.0,
            direction="short",
            entry_distance_pct=0.50,
            protection_gap_pct=0.15,
        )

        self.assertEqual(150.0, entry)
        self.assertEqual(127.5, take_profit)
        self.assertEqual(172.5, stop_loss)
        self.assertGreater(take_profit, 100.0)
        self.assertLess(take_profit, entry)

    def test_select_probe_plan_skips_dirty_symbol_and_keeps_clean_ones(self):
        args = Namespace(
            symbols="TSLA,AAPL,MSFT",
            orders=2,
            quantity=1,
            direction="long",
            entry_distance_pct=0.5,
            protection_gap_pct=0.15,
            reference_price=0.0,
            sqlite_script=str(REPO_ROOT / "ops" / "db" / "remote_pb_sqlite.sh"),
            host="host",
            db_path="db",
            sqlite_timeout_sec=1,
        )
        snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "positions": [{"symbol": "TSLA", "quantity": 1}],
            "live_open_orders": [],
            "orders": [],
        }
        prices = {"AAPL": 200.0, "MSFT": 400.0}

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot), mock.patch.object(
            probe.fee_probe,
            "fetch_latest_reference_price",
            side_effect=lambda _args, symbol: prices[symbol],
        ):
            plans, excluded, summary = probe.select_probe_plan(args)

        self.assertEqual(["AAPL", "MSFT"], [plan.symbol for plan in plans])
        self.assertEqual(2, summary["selected_orders"])
        self.assertEqual("TSLA", excluded[0]["symbol"])
        self.assertIn("pre_existing_position", excluded[0]["reason"])

    def test_action_params_force_paper(self):
        self.assertEqual({"environment": "paper", "broker_mode": "paper"}, probe.action_params())


if __name__ == "__main__":
    unittest.main()
