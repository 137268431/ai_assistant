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
            "fetch_latest_reference_contexts",
            return_value={symbol: (price, {"AAPL": 265598, "MSFT": 272093}[symbol]) for symbol, price in prices.items()},
        ), mock.patch.object(
            probe,
            "fetch_latest_reference_context",
        ) as fallback_context:
            plans, excluded, summary = probe.select_probe_plan(args)

        fallback_context.assert_not_called()
        self.assertEqual(["AAPL", "MSFT"], [plan.symbol for plan in plans])
        self.assertEqual([265598, 272093], [plan.conid for plan in plans])
        self.assertEqual(265598, plans[0].payload()["conid"])
        self.assertEqual(2, summary["selected_orders"])
        self.assertEqual(2, len([plan for plan in plans if plan.requested_exposure >= 5000.0]))
        self.assertGreaterEqual(summary["total_requested_exposure"], 10000.0)
        self.assertEqual("TSLA", excluded[0]["symbol"])
        self.assertIn("pre_existing_position", excluded[0]["reason"])

    def test_fetch_latest_reference_contexts_uses_one_batch_sql(self):
        args = Namespace(reference_price=0.0)
        rows = [
            {"symbol": "AAPL", "close": "200.5", "extra": json.dumps({"conid": 265598})},
            {"symbol": "MSFT", "close": 410.25, "extra": {"conidEx": 272093}},
        ]

        with mock.patch.object(probe.fee_probe, "run_remote_sql", return_value=rows) as run_sql:
            contexts = probe.fetch_latest_reference_contexts(args, ["AAPL", "MSFT", ""])

        run_sql.assert_called_once()
        sql = run_sql.call_args.args[1]
        self.assertIn("values ('AAPL'), ('MSFT')", sql)
        self.assertIn("interval = '5m'", sql)
        self.assertEqual((200.5, 265598), contexts["AAPL"])
        self.assertEqual((410.25, 272093), contexts["MSFT"])

    def test_fetch_latest_reference_contexts_respects_reference_price_override(self):
        args = Namespace(reference_price=123.45)

        with mock.patch.object(probe.fee_probe, "run_remote_sql") as run_sql:
            contexts = probe.fetch_latest_reference_contexts(args, ["AAPL", "MSFT"])

        run_sql.assert_not_called()
        self.assertEqual({"AAPL": (123.45, 0), "MSFT": (123.45, 0)}, contexts)

    def test_select_probe_plan_excludes_batch_missing_reference_without_fallback(self):
        args = Namespace(
            symbols="AAPL,BKNG,MSFT",
            orders=3,
            min_orders=2,
            quantity=1,
            direction="long",
            target_notional_per_order=5000.0,
            entry_distance_pct=0.5,
            protection_gap_pct=0.15,
            reference_price=0.0,
        )
        snapshot = {"ok": True, "environment": "paper", "broker_mode": "paper", "positions": [], "orders": [], "live_open_orders": []}

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot), mock.patch.object(
            probe,
            "fetch_latest_reference_contexts",
            return_value={"AAPL": (200.0, 265598), "MSFT": (400.0, 272093)},
        ), mock.patch.object(probe, "fetch_latest_reference_context") as fallback_context:
            plans, excluded, summary = probe.select_probe_plan(args)

        fallback_context.assert_not_called()
        self.assertEqual(["AAPL", "MSFT"], [plan.symbol for plan in plans])
        self.assertEqual("BKNG", excluded[0]["symbol"])
        self.assertIn("latest_reference_context_unavailable", excluded[0]["reason"])
        self.assertEqual(2, summary["selected_orders"])

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
        self.assertTrue(args.exit_cancel_storm)
        self.assertEqual("all", args.exit_cancel_scope)
        self.assertTrue(args.exit_cancel_batch_by_symbol)
        self.assertGreaterEqual(args.exit_burst_workers, 45)
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

    def test_pending_account_access_retries_and_fails_required_summary_unavailable(self):
        args = Namespace(
            pending_hold_seconds=0.5,
            post_place_sleep_seconds=0.0,
            min_pending_hold_samples=1,
            pending_hold_sample_interval_sec=0.25,
            min_pending_visible_orders=1,
            max_pending_snapshot_elapsed_sec=10.0,
            pending_snapshot_orders_fast=True,
            require_orders_fast_summary=True,
            orders_fast_summary_retries=2,
            orders_fast_summary_retry_delay_sec=0.0,
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

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot) as get_snapshot:
            result = probe.observe_pending_account_access(args, ["AAPL"])

        self.assertFalse(result["ok"])
        self.assertIn("account_summary_unavailable", {item["name"] for item in result["failures"]})
        self.assertGreaterEqual(get_snapshot.call_count, 2)

    def test_account_access_flags_stale_route_cache(self):
        snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "service_running": True,
            "session_authenticated": True,
            "websocket_ready": True,
            "summary_available": True,
            "stale": False,
            "_cache": {"state": "bypass_stale_error", "stale": True, "age_s": 30.0},
            "diagnostics": {"account_snapshot": {"upstream_elapsed_ms": 8000.0}},
            "positions": [],
            "orders": [{"symbol": "AAPL", "status": "Submitted", "order_id": "1"}],
            "live_open_orders": [{"symbol": "AAPL", "status": "Submitted", "order_id": "1"}],
            "counts": {"open_orders": 1},
        }

        summary = probe.summarize_account_snapshot(snapshot, ["AAPL"])

        self.assertFalse(probe.account_access_ok(summary))
        self.assertTrue(summary["route_cache_stale"])
        self.assertEqual("bypass_stale_error", summary["route_cache_state"])
        self.assertEqual({"open_orders": 1}, summary["counts"])
        self.assertEqual(8000.0, summary["account_snapshot_diagnostics"]["upstream_elapsed_ms"])

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

    def test_submit_burst_retries_transient_buying_power_unavailable(self):
        args = Namespace(
            api_base_url="https://example.test",
            http_timeout_sec=30.0,
            burst_workers=1,
            burst_spacing_seconds=0.0,
            submit_timeout_sec=10.0,
            buying_power_unavailable_retries=2,
            buying_power_unavailable_retry_delay_sec=0.01,
        )
        plans = [probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5)]
        calls = []

        def fake_submit(_client, plan, *, delay_s=0.0):
            calls.append(plan.symbol)
            if len(calls) == 1:
                return {
                    "ok": False,
                    "symbol": plan.symbol,
                    "error": "buying_power_unavailable",
                    "response": {"ok": False, "error": "buying_power_unavailable"},
                    "order_ids": [],
                    "elapsed_s": 0.1,
                }
            return {"ok": True, "symbol": plan.symbol, "order_ids": ["101"], "elapsed_s": 0.2}

        with mock.patch.object(probe.fee_probe, "ApiClient", return_value=object()), mock.patch.object(
            probe,
            "submit_one",
            side_effect=fake_submit,
        ), mock.patch.object(probe.time, "sleep", return_value=None):
            results = probe.submit_burst(args, plans)

        self.assertEqual(2, len(calls))
        self.assertTrue(results[0]["ok"])
        self.assertEqual(2, results[0]["attempt"])
        self.assertEqual(2, len(results[0]["attempts"]))

    def test_timeout_unknown_reconciles_snapshot_and_pb_then_cancels_recovered_ids(self):
        args = Namespace(
            run_id="TEST_RUN",
            api_base_url="https://example.test",
            http_timeout_sec=30.0,
            cleanup_http_timeout_sec=2.0,
            cancel_spacing_seconds=0.0,
        )
        plan = probe.ensure_plan_tracking(
            args,
            [probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5)],
        )[0]
        place_results = [probe.submit_timeout_result(plan, timeout_s=1.0)]
        snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "positions": [],
            "live_open_orders": [
                {
                    "symbol": "AAPL",
                    "order_id": "101",
                    "status": "Submitted",
                    "client_order_id": plan.client_order_id,
                    "trade_group_id": plan.trade_group_id,
                    "signal_id": plan.signal_id,
                    "role": "entry",
                }
            ],
            "orders": [],
        }
        pb_rows = [
            {
                "symbol": "AAPL",
                "order_id": "102",
                "status": "Submitted",
                "unique_id": f"tp_{plan.trade_group_id}",
                "trade_group_id": plan.trade_group_id,
                "signal_id": plan.signal_id,
                "role": "take_profit",
            },
            {
                "symbol": "AAPL",
                "order_id": "103",
                "status": "Submitted",
                "unique_id": f"sl_{plan.trade_group_id}",
                "trade_group_id": plan.trade_group_id,
                "signal_id": plan.signal_id,
                "role": "stop_loss",
            },
        ]
        posts = []

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            def post(self, path, payload, params):
                posts.append((path, payload, params))
                return {"ok": True, "result": {"ok": True, "submitted_order_ids": [payload["order_id"]]}}

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot), mock.patch.object(
            probe.fee_probe,
            "run_remote_sql",
            return_value=pb_rows,
        ), mock.patch.object(probe.fee_probe, "ApiClient", _Client):
            reconcile = probe.reconcile_place_results(args, [plan], place_results)
            cancel_results = probe.cancel_known_order_ids(args, place_results)

        self.assertTrue(reconcile["ok"])
        self.assertTrue(place_results[0]["submitted_unknown_recovered"])
        self.assertEqual(["101", "102", "103"], place_results[0]["order_ids"])
        self.assertEqual(["101", "102", "103"], [payload["order_id"] for _path, payload, _params in posts])
        self.assertTrue(all(item["ok"] for item in cancel_results))
        acceptance = probe.summarize_place_acceptance(Namespace(expect_buying_power_blocks=False, min_buying_power_blocks=0), place_results, [plan])
        self.assertTrue(acceptance["ok"])
        self.assertEqual(1, acceptance["submitted_unknown_recovered"])
        self.assertEqual(0, acceptance["unknown_submitted_orders"])

    def test_unrecovered_unknown_submit_fails_acceptance_with_unknown_count(self):
        args = Namespace(
            run_id="TEST_RUN",
            http_timeout_sec=30.0,
            cleanup_http_timeout_sec=2.0,
        )
        plan = probe.ensure_plan_tracking(
            args,
            [probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5)],
        )[0]
        place_results = [probe.submit_timeout_result(plan, timeout_s=1.0)]
        snapshot = {"ok": True, "environment": "paper", "broker_mode": "paper", "positions": [], "live_open_orders": [], "orders": []}

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot), mock.patch.object(
            probe.fee_probe,
            "run_remote_sql",
            return_value=[],
        ):
            reconcile = probe.reconcile_place_results(args, [plan], place_results)

        acceptance = probe.summarize_place_acceptance(
            Namespace(expect_buying_power_blocks=False, min_buying_power_blocks=0),
            place_results,
            [plan],
        )

        self.assertFalse(reconcile["ok"])
        self.assertEqual(1, reconcile["unknown_unresolved_orders"])
        self.assertTrue(place_results[0]["submitted_unknown_unresolved"])
        self.assertFalse(acceptance["ok"])
        self.assertEqual(1, acceptance["unknown_submitted_orders"])

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
        self.assertEqual("1", calls[0][1]["open_orders_only"])
        self.assertEqual("0", calls[0][1]["include_pnl"])
        self.assertEqual("0", calls[0][1]["cache"])

    def test_gateway_readiness_precheck_rejects_running_but_unreachable_gateway(self):
        status = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "status_mode": "lite",
            "gateway": {
                "running": True,
                "reachable": False,
                "status_code": 502,
                "api_socket_listening": False,
                "api_socket_port": 4001,
                "api_socket_reason": "port_not_listening",
                "broker": {"connected": False, "ready": False, "status_code": 0},
            },
            "session": {"authenticated": False},
            "service_topology": {"services": {"ibkr-gateway": {"status": "degraded"}}},
        }

        summary = probe.summarize_gateway_readiness(status)

        self.assertFalse(summary["ok"])
        self.assertFalse(summary["api_socket_listening"])
        self.assertFalse(summary["gateway_reachable"])
        self.assertEqual("port_not_listening", summary["api_socket_reason"])
        failure_names = {item["name"] for item in summary["failures"]}
        self.assertIn("api_socket_listening", failure_names)
        self.assertIn("gateway_reachable", failure_names)
        self.assertIn("session_authenticated", failure_names)

    def test_gateway_readiness_precheck_accepts_ready_paper_gateway(self):
        status = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "status_mode": "lite",
            "gateway": {
                "running": True,
                "reachable": True,
                "status_code": 200,
                "api_socket_listening": True,
                "api_socket_port": 4001,
                "broker": {"connected": True, "ready": True, "status_code": 200},
            },
            "session": {"authenticated": True},
            "service_topology": {"services": {"ibkr-gateway": {"status": "running"}}},
        }

        summary = probe.summarize_gateway_readiness(status)

        self.assertTrue(summary["ok"])
        self.assertFalse(summary["failures"])
        self.assertEqual(4001, summary["api_socket_port"])

    def test_gateway_readiness_accepts_public_statusz_runtime_shape(self):
        status = {
            "ok": True,
            "requested_broker_mode": "paper",
            "runtime": {
                "ok": True,
                "environment": "paper",
                "broker_mode": "paper",
                "gateway": {
                    "running": True,
                    "reachable": True,
                    "status_code": 200,
                    "api_socket_listening": True,
                    "api_socket_port": 4001,
                },
                "session": {"authenticated": True},
                "websocket": {"ready": True},
            },
            "service_topology": {"services": {"ibkr-gateway": {"status": "running"}}},
        }

        summary = probe.summarize_gateway_readiness(status)

        self.assertTrue(summary["ok"])
        self.assertEqual("paper", summary["broker_mode"])
        self.assertTrue(summary["gateway_reachable"])
        self.assertTrue(summary["session_authenticated"])
        self.assertTrue(summary["broker_connected"])
        self.assertTrue(summary["broker_ready"])

    def test_get_gateway_status_lite_falls_back_to_public_statusz(self):
        args = Namespace()
        calls = []
        statusz = {
            "ok": True,
            "requested_broker_mode": "paper",
            "runtime": {
                "ok": True,
                "environment": "paper",
                "broker_mode": "paper",
                "gateway": {"running": True, "reachable": True, "api_socket_listening": True},
                "session": {"authenticated": True},
            },
        }

        class _Client:
            def get(self, path, params=None):
                calls.append((path, dict(params or {})))
                if path == "/ibkr/status":
                    return {"ok": False, "error": "non_json_response", "_request_url": "https://example/ibkr/status"}
                return statusz

        with mock.patch.object(probe, "account_snapshot_client", return_value=(_Client(), "")):
            payload = probe.get_gateway_status_lite(args)

        self.assertEqual(["/ibkr/status", "/api/custom/ibkr/statusz"], [item[0] for item in calls])
        self.assertEqual(statusz, {key: value for key, value in payload.items() if not key.startswith("_status_probe")})
        self.assertEqual("https://example/ibkr/status", payload["_status_probe_fallback_from"])

    def test_run_probe_checks_confirmation_before_gateway_readiness(self):
        args = Namespace(execute=True, confirm="", skip_gateway_readiness_precheck=False)

        with mock.patch.object(probe, "collect_gateway_readiness_precheck") as precheck:
            with self.assertRaises(probe.GatewayProbeError) as ctx:
                probe.run_probe(args)

        precheck.assert_not_called()
        self.assertIn("confirmation_required", str(ctx.exception))

    def test_run_probe_fails_fast_when_gateway_readiness_precheck_fails(self):
        args = Namespace(execute=True, confirm=probe.CONFIRM_TEXT, skip_gateway_readiness_precheck=False)
        precheck = {
            "ok": False,
            "api_socket_listening": False,
            "gateway_reachable": False,
            "failures": [{"name": "api_socket_listening"}],
        }

        with mock.patch.object(probe, "collect_gateway_readiness_precheck", return_value=precheck):
            with mock.patch.object(probe, "select_probe_plan") as select_plan:
                with self.assertRaises(probe.GatewayProbeError) as ctx:
                    probe.run_probe(args)

        select_plan.assert_not_called()
        self.assertIn("gateway_readiness_precheck_failed", str(ctx.exception))
        self.assertIn("api_socket_listening", str(ctx.exception))
        self.assertEqual("gateway_readiness_precheck_failed", ctx.exception.payload["error"])
        self.assertEqual(precheck, ctx.exception.payload["gateway_readiness_precheck"])

    def test_account_global_activity_summary_detects_unrelated_open_orders(self):
        snapshot = {
            "counts": {"open_orders": 1, "open_positions": 1},
            "live_open_orders": [{"symbol": "MSFT", "order_id": "201", "status": "Submitted"}],
            "positions": [{"ticker": "TSLA", "position": 2}],
        }

        summary = probe.account_global_activity_summary(snapshot)

        self.assertFalse(summary["flat"])
        self.assertEqual(1, summary["open_order_count"])
        self.assertEqual(1, summary["open_position_count"])
        self.assertEqual("MSFT", summary["sample_open_orders"][0]["symbol"])
        self.assertEqual("TSLA", summary["sample_positions"][0]["symbol"])

    def test_run_probe_rejects_unrelated_open_orders_before_submit(self):
        args = Namespace(
            execute=True,
            confirm=probe.CONFIRM_TEXT,
            skip_gateway_readiness_precheck=True,
            allow_unrelated_open_orders=False,
            run_id="TEST",
            artifact_root="artifacts/test",
            min_orders=1,
        )
        plans = [probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5)]
        plan_summary = {"selected_symbols": ["AAPL"], "selected_orders": 1}
        snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "live_open_orders": [{"symbol": "MSFT", "order_id": "201", "status": "Submitted"}],
            "orders": [],
            "positions": [],
        }

        with mock.patch.object(probe, "select_probe_plan", return_value=(plans, [], plan_summary)), mock.patch.object(
            probe,
            "get_snapshot",
            return_value=snapshot,
        ), mock.patch.object(probe, "submit_burst") as submit_burst:
            with self.assertRaises(probe.GatewayProbeError) as ctx:
                probe.run_probe(args)

        submit_burst.assert_not_called()
        self.assertIn("preflight_account_not_flat", str(ctx.exception))

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

        get_snapshot.assert_called_once_with(args, timeout_sec=30.0, orders_fast=True)
        cleanup_symbol.assert_not_called()
        self.assertEqual(2, len(results))
        self.assertTrue(all(item["ok"] for item in results))
        self.assertTrue(all(item["skipped"] for item in results))

    def test_cleanup_symbols_uses_visible_order_rescue_before_per_symbol_cleanup(self):
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
        first_snapshot = {
            "ok": True,
            "positions": [],
            "live_open_orders": [{"symbol": "AAPL", "order_id": "101", "status": "Submitted"}],
        }
        flat_snapshot = {"ok": True, "positions": [], "orders": [], "live_open_orders": []}
        rescue_results = [{"ok": True, "order_id": "101", "symbol": "AAPL"}]

        with mock.patch.object(probe, "get_snapshot", return_value=first_snapshot), mock.patch.object(
            probe,
            "cancel_visible_orders_for_symbols",
            return_value=rescue_results,
        ) as rescue, mock.patch.object(
            probe,
            "wait_for_flat_symbols",
            return_value={"ok": True, "snapshot": flat_snapshot},
        ), mock.patch.object(
            probe.fee_probe,
            "cleanup_symbol",
        ) as cleanup_symbol:
            results = probe.cleanup_symbols(args, plans, [])

        rescue.assert_called_once_with(args, ["AAPL", "MSFT"])
        cleanup_symbol.assert_not_called()
        self.assertEqual(2, len(results))
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual("already_flat_after_visible_order_rescue", results[0]["reason"])
        self.assertEqual(1, results[0]["result"]["visible_rescue_cancel_count"])

    def test_cleanup_symbols_waits_after_bulk_cancel_before_rescue(self):
        args = Namespace(
            api_base_url="https://example.test",
            account_base_url="",
            account_snapshot_path="",
            http_timeout_sec=1200.0,
            cleanup_http_timeout_sec=30.0,
            cleanup_timeout_sec=1200.0,
            bulk_cancel_settle_before_rescue_sec=5.0,
            symbol_cleanup_timeout_sec=0.0,
            poll_interval_sec=2.0,
            cancel_spacing_seconds=0.0,
            bulk_cancel_all=True,
        )
        plans = [
            probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5),
            probe.OrderProbePlan("MSFT", "long", 1, 200.0, 100.0, 115.0, 85.0),
        ]
        first_snapshot = {
            "ok": True,
            "positions": [],
            "live_open_orders": [{"symbol": "AAPL", "order_id": "101", "status": "Submitted"}],
        }
        flat_snapshot = {"ok": True, "positions": [], "orders": [], "live_open_orders": []}
        cancel_results = [{"source": "cancel_all", "ok": True, "cancelled": 2}]

        with mock.patch.object(probe, "get_snapshot", return_value=first_snapshot), mock.patch.object(
            probe,
            "wait_for_flat_symbols",
            return_value={"ok": True, "snapshot": flat_snapshot},
        ) as wait_for_flat, mock.patch.object(
            probe,
            "cancel_visible_orders_for_symbols",
        ) as rescue, mock.patch.object(
            probe.fee_probe,
            "cleanup_symbol",
        ) as cleanup_symbol:
            results = probe.cleanup_symbols(args, plans, [], cancel_results)

        wait_args = wait_for_flat.call_args.args[0]
        self.assertEqual(5.0, wait_args.cleanup_timeout_sec)
        rescue.assert_not_called()
        cleanup_symbol.assert_not_called()
        self.assertEqual(2, len(results))
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual("already_flat_after_bulk_cancel_wait", results[0]["reason"])

    def test_cleanup_symbols_uses_visible_rescue_after_bulk_wait_before_cancel_all_retry(self):
        args = Namespace(
            api_base_url="https://example.test",
            account_base_url="",
            account_snapshot_path="",
            http_timeout_sec=1200.0,
            cleanup_http_timeout_sec=30.0,
            cleanup_timeout_sec=1200.0,
            bulk_cancel_settle_before_rescue_sec=5.0,
            symbol_cleanup_timeout_sec=0.0,
            poll_interval_sec=2.0,
            cancel_spacing_seconds=0.0,
            bulk_cancel_all=True,
            cancel_all_attempts=3,
            cancel_all_retry_delay_sec=0.0,
        )
        plans = [
            probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5),
            probe.OrderProbePlan("MSFT", "long", 1, 200.0, 100.0, 115.0, 85.0),
        ]
        first_snapshot = {
            "ok": True,
            "positions": [],
            "live_open_orders": [{"symbol": "AAPL", "order_id": "101", "status": "Submitted"}],
        }
        flat_snapshot = {"ok": True, "positions": [], "orders": [], "live_open_orders": []}
        cancel_results = [{"source": "cancel_all", "ok": True, "cancelled": 2}]
        rescue_results = [{"ok": True, "symbol": "AAPL", "order_ids": ["101"], "submitted": 1}]

        with mock.patch.object(probe, "get_snapshot", return_value=first_snapshot), mock.patch.object(
            probe,
            "wait_for_flat_symbols",
            side_effect=[
                {"ok": False, "snapshot": first_snapshot},
                {"ok": True, "snapshot": flat_snapshot},
            ],
        ) as wait_for_flat, mock.patch.object(
            probe,
            "cancel_all_orders",
            return_value=[{"ok": True, "source": "cancel_all", "cancelled": 1}],
        ) as cancel_all, mock.patch.object(
            probe,
            "cancel_visible_orders_for_symbols",
            return_value=rescue_results,
        ) as rescue, mock.patch.object(
            probe.fee_probe,
            "cleanup_symbol",
        ) as cleanup_symbol:
            results = probe.cleanup_symbols(args, plans, [], cancel_results)

        self.assertEqual(2, wait_for_flat.call_count)
        cancel_all.assert_not_called()
        rescue.assert_called_once_with(args, ["AAPL", "MSFT"])
        cleanup_symbol.assert_not_called()
        self.assertEqual("already_flat_after_bulk_visible_order_rescue", results[0]["reason"])
        self.assertEqual(1, results[0]["result"]["visible_rescue_cancel_count"])

    def test_cleanup_symbols_retries_cancel_all_when_visible_rescue_has_no_orders(self):
        args = Namespace(
            api_base_url="https://example.test",
            account_base_url="",
            account_snapshot_path="",
            http_timeout_sec=1200.0,
            cleanup_http_timeout_sec=30.0,
            cleanup_timeout_sec=1200.0,
            bulk_cancel_settle_before_rescue_sec=5.0,
            symbol_cleanup_timeout_sec=0.0,
            poll_interval_sec=2.0,
            cancel_spacing_seconds=0.0,
            bulk_cancel_all=True,
            cancel_all_attempts=3,
            cancel_all_retry_delay_sec=0.0,
        )
        plans = [
            probe.OrderProbePlan("AAPL", "long", 1, 100.0, 50.0, 57.5, 42.5),
            probe.OrderProbePlan("MSFT", "long", 1, 200.0, 100.0, 115.0, 85.0),
        ]
        first_snapshot = {
            "ok": True,
            "positions": [],
            "live_open_orders": [{"symbol": "AAPL", "order_id": "101", "status": "Submitted"}],
        }
        flat_snapshot = {"ok": True, "positions": [], "orders": [], "live_open_orders": []}
        cancel_results = [{"source": "cancel_all", "ok": True, "cancelled": 2}]

        with mock.patch.object(probe, "get_snapshot", return_value=first_snapshot), mock.patch.object(
            probe,
            "wait_for_flat_symbols",
            side_effect=[
                {"ok": False, "snapshot": first_snapshot},
                {"ok": True, "snapshot": flat_snapshot},
            ],
        ) as wait_for_flat, mock.patch.object(
            probe,
            "cancel_all_orders",
            return_value=[{"ok": True, "source": "cancel_all", "cancelled": 1}],
        ) as cancel_all, mock.patch.object(
            probe,
            "cancel_visible_orders_for_symbols",
            return_value=[],
        ) as rescue, mock.patch.object(
            probe.fee_probe,
            "cleanup_symbol",
        ) as cleanup_symbol:
            results = probe.cleanup_symbols(args, plans, [], cancel_results)

        self.assertEqual(2, wait_for_flat.call_count)
        cancel_all.assert_called_once()
        self.assertEqual("gateway_order_probe_post_visibility_cancel_all", cancel_all.call_args.kwargs["source"])
        rescue.assert_called_once_with(args, ["AAPL", "MSFT"])
        cleanup_symbol.assert_not_called()
        self.assertEqual("already_flat_after_post_visibility_cancel_all", results[0]["reason"])

    def test_wait_for_submission_quiescence_waits_for_active_commands_to_drain(self):
        args = Namespace(
            pre_cancel_quiesce_sec=1.0,
            pre_cancel_quiet_sec=0.0,
            min_pre_cancel_visible_orders=6,
            min_pending_visible_orders=0,
            cleanup_http_timeout_sec=1.0,
            http_timeout_sec=1.0,
            poll_interval_sec=0.01,
        )
        busy_snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "positions": [],
            "live_open_orders": [
                {"symbol": "AAPL", "order_id": str(order_id), "status": "Submitted"}
                for order_id in range(100, 106)
            ],
            "orders_fast_diagnostics": {
                "pb_fallback_trust": {
                    "broker_open_count": 6,
                    "active_order_command_count": 2,
                }
            },
        }
        ready_snapshot = {
            **busy_snapshot,
            "orders_fast_diagnostics": {
                "pb_fallback_trust": {
                    "broker_open_count": 6,
                    "active_order_command_count": 0,
                }
            },
        }

        with mock.patch.object(probe, "get_snapshot", side_effect=[busy_snapshot, ready_snapshot]):
            result = probe.wait_for_submission_quiescence(args, ["AAPL"])

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(2, result["sample_count"])
        self.assertEqual(0, result["last_sample"]["active_order_command_count"])

    def test_wait_for_submission_quiescence_keeps_broker_count_diagnostic_only(self):
        args = Namespace(
            pre_cancel_quiesce_sec=0.03,
            pre_cancel_quiet_sec=0.0,
            min_pre_cancel_visible_orders=6,
            min_pending_visible_orders=0,
            cleanup_http_timeout_sec=1.0,
            http_timeout_sec=1.0,
            poll_interval_sec=0.01,
        )
        snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "positions": [],
            "live_open_orders": [
                {"symbol": "AAPL", "order_id": str(order_id), "status": "Submitted"}
                for order_id in range(100, 106)
            ],
            "orders_fast_diagnostics": {
                "pb_fallback_trust": {
                    "broker_open_count": 5,
                    "active_order_command_count": 0,
                }
            },
        }

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot):
            result = probe.wait_for_submission_quiescence(args, ["AAPL"])

        self.assertTrue(result["ok"])
        self.assertEqual(5, result["last_sample"]["broker_open_count"])
        self.assertEqual(6, result["last_sample"]["selected_open_order_count"])

    def test_exit_cancel_storm_batches_all_legs_by_symbol(self):
        args = Namespace(
            api_base_url="https://example.test",
            http_timeout_sec=30.0,
            exit_cancel_scope="all",
            exit_burst_workers=20,
            exit_cancel_batch_by_symbol=True,
        )
        place_results = [
            {"ok": True, "symbol": "AAPL", "order_ids": ["101", "102", "103"]},
            {"ok": True, "symbol": "MSFT", "order_ids": ["201", "202", "203"]},
        ]
        posts = []

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            def post(self, path, payload, params):
                posts.append((path, payload, params))
                return {
                    "ok": True,
                    "result": {
                        "ok": True,
                        "submitted_order_ids": list(payload.get("order_ids") or []),
                    },
                }

        with mock.patch.object(probe.fee_probe, "ApiClient", _Client):
            result = probe.exit_cancel_storm(args, place_results)

        self.assertTrue(result["ok"])
        self.assertTrue(result["batch_by_symbol"])
        self.assertEqual(6, result["requested"])
        self.assertEqual(6, result["ok_count"])
        self.assertEqual(2, result["group_count"])
        self.assertEqual(2, len(posts))
        self.assertEqual(["101", "102", "103"], posts[0][1]["order_ids"])
        self.assertEqual(["201", "202", "203"], posts[1][1]["order_ids"])

    def test_apply_probe_safety_defaults_adds_quiesce_for_large_bulk_cancel(self):
        args = Namespace(
            bulk_cancel_all=True,
            orders=45,
            pre_cancel_quiesce_sec=0.0,
            pre_cancel_quiet_sec=15.0,
            min_pre_cancel_visible_orders=0,
            min_pending_visible_orders=135,
        )

        result = probe.apply_probe_safety_defaults(args)

        self.assertEqual(300.0, result.pre_cancel_quiesce_sec)
        self.assertEqual(30.0, result.pre_cancel_quiet_sec)
        self.assertEqual(135, result.min_pre_cancel_visible_orders)
        self.assertTrue(result.require_orders_fast_summary)
        self.assertEqual(3, result.orders_fast_summary_retries)
        self.assertEqual(30.0, result.orders_fast_summary_degraded_max_wait_sec)
        self.assertEqual(4, result.visible_rescue_rounds)
        self.assertEqual(45.0, result.visible_rescue_wait_sec)

    def test_bulk_cancel_default_settle_before_rescue_is_short(self):
        args = Namespace(cleanup_timeout_sec=1200.0, bulk_cancel_settle_before_rescue_sec=0.0)

        self.assertEqual(30.0, probe._bulk_cancel_settle_timeout(args))

    def test_wait_for_submission_quiescence_fails_fast_when_required_summary_stays_degraded(self):
        args = Namespace(
            pre_cancel_quiesce_sec=5.0,
            pre_cancel_quiet_sec=0.0,
            min_pre_cancel_visible_orders=1,
            min_pending_visible_orders=0,
            cleanup_http_timeout_sec=1.0,
            http_timeout_sec=1.0,
            poll_interval_sec=0.01,
            require_orders_fast_summary=True,
            orders_fast_summary_retries=1,
            orders_fast_summary_retry_delay_sec=0.0,
            orders_fast_summary_degraded_max_wait_sec=0.01,
        )
        degraded_snapshot = {
            "ok": True,
            "environment": "paper",
            "broker_mode": "paper",
            "summary_available": False,
            "positions": [],
            "live_open_orders": [{"symbol": "AAPL", "order_id": "101", "status": "Submitted"}],
        }

        with mock.patch.object(probe, "get_snapshot", return_value=degraded_snapshot):
            result = probe.wait_for_submission_quiescence(args, ["AAPL"])

        self.assertFalse(result["ok"])
        self.assertEqual("orders_fast_summary_unavailable_after_retry", result["error"])
        self.assertTrue(result["last_sample"]["summary_degraded"])

    def test_wait_for_flat_rejects_failed_snapshot_without_false_flat(self):
        args = Namespace(cleanup_timeout_sec=0.1, cleanup_http_timeout_sec=1.0, http_timeout_sec=1.0, poll_interval_sec=0.01)
        bad_snapshot = {"ok": False, "environment": "paper", "error": "runtime timeout"}

        with mock.patch.object(probe, "get_snapshot", return_value=bad_snapshot):
            result = probe.wait_for_flat_symbols(args, ["TSLA"])

        self.assertFalse(result["ok"])
        self.assertEqual("cleanup_timeout_snapshot_unusable", result["error"])

    def test_cancel_visible_orders_requires_usable_orders_snapshot(self):
        args = Namespace(
            api_base_url="https://example.test",
            account_base_url="",
            account_snapshot_path="",
            http_timeout_sec=30.0,
            cleanup_http_timeout_sec=2.0,
        )
        bad_snapshot = {"ok": False, "environment": "paper", "error": "timeout"}

        with mock.patch.object(probe, "get_snapshot", return_value=bad_snapshot):
            results = probe.cancel_visible_orders_for_symbols(args, ["TSLA"])

        self.assertEqual(1, len(results))
        self.assertFalse(results[0]["ok"])
        self.assertEqual("account_snapshot_visibility_unavailable", results[0]["error"])

    def test_cancel_visible_orders_batches_rescue_by_symbol(self):
        args = Namespace(
            api_base_url="https://example.test",
            account_base_url="",
            account_snapshot_path="",
            http_timeout_sec=30.0,
            cleanup_http_timeout_sec=2.0,
            orders_fast_summary_retries=1,
            orders_fast_summary_retry_delay_sec=0.0,
            visible_rescue_batch_by_symbol=True,
            visible_rescue_workers=1,
        )
        snapshot = {
            "ok": True,
            "positions": [],
            "live_open_orders": [
                {"symbol": "AAPL", "order_id": "101", "status": "Submitted"},
                {"symbol": "AAPL", "order_id": "102", "status": "Submitted"},
                {"symbol": "MSFT", "order_id": "201", "status": "Submitted"},
            ],
        }
        posts = []

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            def post(self, path, payload, params):
                posts.append((path, payload, params))
                return {
                    "ok": True,
                    "result": {
                        "ok": True,
                        "submitted_order_ids": list(payload.get("order_ids") or []),
                    },
                }

        with mock.patch.object(probe, "get_snapshot", return_value=snapshot), mock.patch.object(
            probe.fee_probe,
            "ApiClient",
            _Client,
        ):
            results = probe.cancel_visible_orders_for_symbols(args, ["AAPL", "MSFT"])

        self.assertEqual(2, len(results))
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(["101", "102"], posts[0][1]["order_ids"])
        self.assertEqual(["201"], posts[1][1]["order_ids"])
        self.assertEqual("visible_order_rescue", posts[0][1]["source"])

    def test_visible_order_rescue_can_repeat_until_flat(self):
        args = Namespace(
            cleanup_timeout_sec=120.0,
            visible_rescue_rounds=2,
            visible_rescue_wait_sec=1.0,
        )
        first_snapshot = {
            "ok": True,
            "positions": [],
            "live_open_orders": [{"symbol": "AAPL", "order_id": "101", "status": "Submitted"}],
        }
        flat_snapshot = {"ok": True, "positions": [], "orders": [], "live_open_orders": []}

        with mock.patch.object(
            probe,
            "cancel_visible_orders_for_symbols",
            side_effect=[
                [{"ok": True, "symbol": "AAPL", "order_ids": ["101"]}],
                [{"ok": True, "symbol": "AAPL", "order_ids": ["101"]}],
            ],
        ) as rescue, mock.patch.object(
            probe,
            "wait_for_flat_symbols",
            side_effect=[
                {"ok": False, "snapshot": first_snapshot},
                {"ok": True, "snapshot": flat_snapshot},
            ],
        ) as wait_for_flat:
            result = probe.cancel_visible_orders_until_flat(args, ["AAPL"])

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["rounds"])
        self.assertEqual(2, rescue.call_count)
        self.assertEqual(2, wait_for_flat.call_count)
        self.assertEqual([1, 2], [item["visible_rescue_round"] for item in result["results"]])

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
