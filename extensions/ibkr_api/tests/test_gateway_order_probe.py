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
            probe,
            "fetch_latest_reference_context",
            side_effect=lambda _args, symbol: (prices[symbol], {"AAPL": 265598, "MSFT": 272093}[symbol]),
        ):
            plans, excluded, summary = probe.select_probe_plan(args)

        self.assertEqual(["AAPL", "MSFT"], [plan.symbol for plan in plans])
        self.assertEqual([265598, 272093], [plan.conid for plan in plans])
        self.assertEqual(265598, plans[0].payload()["conid"])
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

    def test_load_probe_plan_can_rescale_notional_and_preserve_conid(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "summary.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "plan_summary": {"target_notional_per_order": 5000.0},
                        "plan": [
                            {
                                "symbol": "AAPL",
                                "conid": 265598,
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
                target_notional_per_order=6000.0,
            )
            snapshot = {"ok": True, "environment": "paper", "broker_mode": "paper", "positions": [], "orders": [], "live_open_orders": []}

            with mock.patch.object(probe, "get_snapshot", return_value=snapshot):
                plans, excluded, summary = probe.select_probe_plan(args)

        self.assertFalse(excluded)
        self.assertEqual(265598, plans[0].conid)
        self.assertEqual(39, plans[0].quantity)
        self.assertAlmostEqual(6067.23, plans[0].requested_exposure)
        self.assertEqual(6000.0, summary["target_notional_per_order"])

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
            pending_hold_seconds=0.5,
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
        self.assertEqual(3, result["sample_count"])
        self.assertEqual(2, result["max_selected_open_order_count_observed"])
        self.assertFalse(result["failures"])

    def test_observe_pending_account_access_fails_when_orders_are_not_visible(self):
        args = Namespace(
            pending_hold_seconds=0.5,
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

    def test_pending_account_access_warns_but_passes_when_summary_is_unavailable(self):
        args = Namespace(
            pending_hold_seconds=0.5,
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
            "summary_available": False,
            "stale": False,
            "positions": [],
            "orders": [{"symbol": "AAPL", "status": "Submitted", "order_id": "1"}],
            "live_open_orders": [],
        }

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot):
            result = probe.observe_pending_account_access(args, ["AAPL"])

        self.assertTrue(result["ok"])
        self.assertFalse(result["failures"])
        self.assertEqual("account_summary_unavailable", result["warnings"][0]["name"])

    def test_pending_account_access_allows_extra_failed_samples_when_min_successes_pass(self):
        args = Namespace(
            pending_hold_seconds=0.5,
            post_place_sleep_seconds=0.0,
            min_pending_hold_samples=2,
            pending_hold_sample_interval_sec=0.25,
            min_pending_visible_orders=1,
            max_pending_snapshot_elapsed_sec=10.0,
        )
        good_snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "service_running": True,
            "session_authenticated": True,
            "websocket_ready": True,
            "summary_available": True,
            "stale": False,
            "positions": [],
            "orders": [{"symbol": "AAPL", "status": "Submitted", "order_id": "1"}],
            "live_open_orders": [],
        }

        with mock.patch.object(
            probe,
            "sample_account_access",
            side_effect=[
                {"ok": True, "elapsed_s": 0.1, "summary": probe.summarize_account_snapshot(good_snapshot, ["AAPL"])},
                {"ok": False, "elapsed_s": 0.1, "error": "HTTP 502"},
                {"ok": True, "elapsed_s": 0.1, "summary": probe.summarize_account_snapshot(good_snapshot, ["AAPL"])},
            ],
        ), mock.patch.object(probe.time, "sleep", return_value=None), mock.patch.object(
            probe.time,
            "time",
            side_effect=[0.0, 0.0, 0.25, 0.5],
        ):
            result = probe.observe_pending_account_access(args, ["AAPL"])

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["ok_sample_count"])
        self.assertFalse(result["failures"])
        self.assertEqual("account_snapshot_sample_failures", result["warnings"][0]["name"])

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
        self.assertEqual(["MSFT-1"], results[1]["order_ids"])
        self.assertTrue(results[1]["late_after_submit_timeout"])
        self.assertTrue(results[1]["late_ok"])

    def test_place_acceptance_allows_expected_buying_power_blocks(self):
        args = Namespace(expect_buying_power_blocks=True, min_buying_power_blocks=1)
        plans = [
            probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5),
            probe.OrderProbePlan("MSFT", "long", 1, 200.0, 100.0, 115.0, 85.0),
        ]
        results = [
            {"ok": True, "symbol": "AAPL", "order_ids": ["1", "2", "3"]},
            {
                "ok": False,
                "symbol": "MSFT",
                "response": {"ok": False, "result": {"ok": False, "error": "buying_power_blocked"}},
            },
        ]

        summary = probe.summarize_place_acceptance(args, results, plans)

        self.assertTrue(summary["ok"])
        self.assertEqual(2, summary["accepted"])
        self.assertEqual(1, summary["buying_power_blocked"])
        self.assertEqual(["MSFT"], summary["blocked_symbols"])

    def test_place_acceptance_rejects_unexpected_place_failures(self):
        args = Namespace(expect_buying_power_blocks=True, min_buying_power_blocks=1)
        plans = [probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5)]
        results = [{"ok": False, "symbol": "AAPL", "response": {"ok": False, "result": {"error": "order_rejected"}}}]

        summary = probe.summarize_place_acceptance(args, results, plans)

        self.assertFalse(summary["ok"])
        self.assertEqual("order_rejected", summary["unexpected_failures"][0]["error"])

    def test_get_snapshot_can_request_orders_fast_profile(self):
        args = Namespace()
        calls = []

        class _Client:
            def get(self, path, params=None):
                calls.append((path, dict(params or {})))
                return {"ok": True}

        with mock.patch.object(probe, "account_snapshot_client", return_value=(_Client(), "/ibkr/account")), mock.patch.object(
            probe.fee_probe,
            "account_params",
            return_value={"environment": "paper", "broker_mode": "paper", "cache_bust": 123},
        ):
            payload = probe.get_snapshot(args, orders_fast=True)

        self.assertTrue(payload["ok"])
        self.assertEqual("/ibkr/account", calls[0][0])
        self.assertEqual("1", calls[0][1]["orders_fast"])
        self.assertEqual("orders_fast", calls[0][1]["snapshot_profile"])
        self.assertEqual("0", calls[0][1]["include_pnl"])

    def test_build_stop_loss_modify_items_targets_submitted_stop_legs(self):
        plans = [
            probe.OrderProbePlan("AAPL", "long", 10, 100.0, 50.0, 57.5, 42.5),
            probe.OrderProbePlan("MSFT", "long", 5, 200.0, 100.0, 115.0, 85.0),
        ]
        place_results = [
            {"ok": True, "symbol": "AAPL", "order_ids": ["101", "102", "103"]},
            {"ok": False, "symbol": "MSFT", "order_ids": []},
        ]

        items = probe.build_stop_loss_modify_items(plans, place_results, repeat=1)

        self.assertEqual(1, len(items))
        self.assertEqual("103", items[0]["order_id"])
        self.assertEqual("AAPL", items[0]["symbol"])
        self.assertGreater(items[0]["new_stop_loss_price"], 42.5)
        self.assertLess(items[0]["new_stop_loss_price"], 50.0)

    def test_build_exit_cancel_items_defaults_to_entry_legs(self):
        items = probe.build_exit_cancel_items(
            [
                {"ok": True, "symbol": "AAPL", "order_ids": ["101", "102", "103"]},
                {"ok": True, "symbol": "MSFT", "order_ids": ["201", "202", "203"]},
            ],
            scope="entry",
        )

        self.assertEqual(["101", "201"], [item["order_id"] for item in items])
        self.assertEqual(["entry", "entry"], [item["role"] for item in items])

    def test_cleanup_symbols_skips_per_symbol_work_when_already_flat(self):
        args = Namespace(
            api_base_url="https://example.test",
            account_base_url="",
            account_snapshot_path="",
            http_timeout_sec=1200.0,
            cleanup_http_timeout_sec=30.0,
            cleanup_timeout_sec=1200.0,
            symbol_cleanup_timeout_sec=0.0,
            poll_interval_sec=2.0,
            cancel_spacing_seconds=0.0,
        )
        plans = [
            probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5),
            probe.OrderProbePlan("MSFT", "long", 1, 200.0, 100.0, 115.0, 85.0),
        ]
        snapshot = {"ok": True, "positions": [], "orders": [], "live_open_orders": []}

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot) as get_snapshot, mock.patch.object(
            probe.fee_probe,
            "cleanup_symbol",
        ) as cleanup_symbol:
            results = probe.cleanup_symbols(args, plans, [])

        get_snapshot.assert_called_once_with(args, timeout_sec=30.0)
        cleanup_symbol.assert_not_called()
        self.assertEqual(2, len(results))
        self.assertTrue(all(item["ok"] for item in results))
        self.assertTrue(all(item["skipped"] for item in results))

    def test_cancel_all_orders_retries_timeout_before_success(self):
        args = Namespace(
            api_base_url="https://example.test",
            http_timeout_sec=1200.0,
            cleanup_http_timeout_sec=30.0,
            cancel_all_http_timeout_sec=0.0,
            cancel_all_attempts=2,
            cancel_all_retry_delay_sec=0.0,
        )

        class FakeClient:
            calls = 0
            timeouts = []

            def __init__(self, _base_url, timeout=0):
                self.timeout = timeout
                self.__class__.timeouts.append(timeout)

            def post(self, path, payload, params):
                self.__class__.calls += 1
                self.last_payload = payload
                if self.__class__.calls == 1:
                    return {
                        "ok": False,
                        "result": {"ok": False, "errors": ["open_orders_all_timeout"]},
                    }
                return {"ok": True, "result": {"ok": True, "cancelled": 3}}

        with mock.patch.object(probe.fee_probe, "ApiClient", FakeClient), mock.patch.object(probe.time, "sleep", return_value=None):
            results = probe.cancel_all_orders(args)

        self.assertEqual(1, len(results))
        self.assertTrue(results[0]["ok"])
        self.assertEqual(3, results[0]["cancelled"])
        self.assertEqual(2, len(results[0]["attempts"]))
        self.assertEqual("open_orders_all_timeout", results[0]["attempts"][0]["errors"][0])
        self.assertEqual(2, FakeClient.calls)
        self.assertEqual([240.0, 240.0], FakeClient.timeouts)

    def test_cancel_all_timeout_can_be_overridden(self):
        args = Namespace(
            http_timeout_sec=1200.0,
            cleanup_http_timeout_sec=45.0,
            cancel_all_http_timeout_sec=300.0,
        )

        self.assertEqual(300.0, probe.cancel_all_http_timeout(args))


if __name__ == "__main__":
    unittest.main()
