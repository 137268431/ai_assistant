import sys
import json
import tempfile
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

    def test_quantity_for_target_notional_sizes_from_entry_price(self):
        quantity, exposure = probe.quantity_for_target_notional(155.57, 5000.0, 1)

        self.assertEqual(33, quantity)
        self.assertAlmostEqual(5133.81, exposure)

    def test_select_probe_plan_skips_dirty_symbol_and_keeps_clean_ones(self):
        args = Namespace(
            symbols="TSLA,AAPL,MSFT",
            orders=2,
            quantity=1,
            direction="long",
            target_notional_per_order=5000.0,
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
        self.assertEqual(2, len([plan for plan in plans if plan.requested_exposure >= 5000.0]))
        self.assertGreaterEqual(summary["total_requested_exposure"], 10000.0)
        self.assertEqual("TSLA", excluded[0]["symbol"])
        self.assertIn("pre_existing_position", excluded[0]["reason"])

    def test_load_probe_plan_reuses_dry_run_summary_without_fetching_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "summary.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "plan_summary": {"target_notional_per_order": 5000.0},
                        "plan": [
                            {
                                "symbol": "AAPL",
                                "direction": "long",
                                "quantity": 33,
                                "reference_price": 311.14,
                                "entry_price": 155.57,
                                "take_profit_price": 178.91,
                                "stop_loss_price": 132.23,
                                "target_notional": 5000.0,
                                "requested_exposure": 5133.81,
                            }
                        ],
                    }
                )
            )
            args = Namespace(
                plan_path=str(plan_path),
                orders=1,
                quantity=1,
                direction="long",
                target_notional_per_order=0.0,
            )
            snapshot = {"ok": True, "environment": "paper", "broker_mode": "paper", "positions": [], "orders": [], "live_open_orders": []}

            with mock.patch.object(probe, "get_snapshot", return_value=snapshot), mock.patch.object(
                probe.fee_probe,
                "fetch_latest_reference_price",
            ) as fetch_price:
                plans, excluded, summary = probe.select_probe_plan(args)

        fetch_price.assert_not_called()
        self.assertFalse(excluded)
        self.assertEqual(["AAPL"], [plan.symbol for plan in plans])
        self.assertEqual(33, plans[0].quantity)
        self.assertEqual(5133.81, plans[0].requested_exposure)
        self.assertEqual(str(plan_path), summary["plan_path"])

    def test_action_params_force_paper(self):
        self.assertEqual({"environment": "paper", "broker_mode": "paper"}, probe.action_params())

    def test_account_lock_stress_preset_holds_forty_five_pending_orders(self):
        args = probe.parse_args(["--account-lock-stress"])

        self.assertTrue(args.gateway_stress)
        self.assertTrue(args.account_lock_stress)
        self.assertGreaterEqual(args.orders, 45)
        self.assertGreaterEqual(args.min_orders, 45)
        self.assertGreaterEqual(args.burst_workers, 45)
        self.assertGreaterEqual(args.target_notional_per_order, 5000.0)
        self.assertGreaterEqual(args.pending_hold_seconds, 45.0)
        self.assertGreaterEqual(args.min_pending_hold_samples, 3)
        self.assertGreaterEqual(args.min_pending_visible_orders, 45)
        self.assertGreaterEqual(args.submit_timeout_sec, 90.0)
        self.assertGreaterEqual(len(probe.split_symbols(args.symbols)), 45)

    def test_account_lock_stress_can_scale_to_forty_five_orders(self):
        args = probe.parse_args(["--account-lock-stress", "--orders", "45", "--min-orders", "45"])

        self.assertEqual(45, args.orders)
        self.assertEqual(45, args.min_orders)
        self.assertEqual(45, args.burst_workers)
        self.assertEqual(45, args.min_pending_visible_orders)

    def test_observe_pending_account_access_requires_visible_orders_and_latency(self):
        args = Namespace(
            pending_hold_seconds=0.0,
            post_place_sleep_seconds=0.0,
            min_pending_hold_samples=2,
            pending_hold_sample_interval_sec=0.25,
            min_pending_visible_orders=2,
            max_pending_snapshot_elapsed_sec=10.0,
        )
        snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "service_running": True,
            "session_authenticated": True,
            "websocket_ready": True,
            "summary_available": True,
            "positions": [],
            "orders": [
                {"symbol": "AAPL", "status": "Submitted", "order_id": "1"},
                {"symbol": "MSFT", "status": "Submitted", "order_id": "2"},
            ],
            "live_open_orders": [],
        }

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot), mock.patch.object(probe.time, "sleep", return_value=None):
            result = probe.observe_pending_account_access(args, ["AAPL", "MSFT"])

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["sample_count"])
        self.assertEqual(2, result["max_selected_open_order_count_observed"])
        self.assertFalse(result["failures"])

    def test_observe_pending_account_access_fails_when_orders_are_not_visible(self):
        args = Namespace(
            pending_hold_seconds=0.0,
            post_place_sleep_seconds=0.0,
            min_pending_hold_samples=1,
            pending_hold_sample_interval_sec=0.25,
            min_pending_visible_orders=1,
            max_pending_snapshot_elapsed_sec=10.0,
        )
        snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "service_running": True,
            "session_authenticated": True,
            "websocket_ready": True,
            "summary_available": True,
            "positions": [],
            "orders": [],
            "live_open_orders": [],
        }

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot):
            result = probe.observe_pending_account_access(args, ["AAPL"])

        self.assertFalse(result["ok"])
        self.assertIn("visible_pending_orders", {item["name"] for item in result["failures"]})

    def test_submit_burst_marks_unfinished_symbols_timed_out(self):
        args = Namespace(
            api_base_url="https://example.test",
            http_timeout_sec=30.0,
            burst_workers=2,
            burst_spacing_seconds=0.0,
            submit_timeout_sec=0.05,
        )
        plans = [
            probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5),
            probe.OrderProbePlan("MSFT", "long", 1, 200.0, 100.0, 115.0, 85.0),
        ]

        def fake_submit(_client, plan, *, delay_s=0.0):
            if plan.symbol == "MSFT":
                probe.time.sleep(0.2)
            return {"ok": True, "symbol": plan.symbol, "order_ids": [f"{plan.symbol}-1"]}

        with mock.patch.object(probe.fee_probe, "ApiClient", return_value=object()), mock.patch.object(
            probe,
            "submit_one",
            side_effect=fake_submit,
        ):
            results = probe.submit_burst(args, plans)

        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertTrue(results[1]["timed_out"])
        self.assertEqual("submit_timeout", results[1]["error"])


if __name__ == "__main__":
    unittest.main()
