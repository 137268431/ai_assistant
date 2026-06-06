import json
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

OPS_VALIDATE_ROOT = Path(__file__).resolve().parents[3] / "ops" / "validate"
if str(OPS_VALIDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(OPS_VALIDATE_ROOT))

from run_today_tv_replay_stress import (  # noqa: E402
    adopt_routed_signal_id,
    apply_max_chains,
    build_chains,
    cleanup_synthetic_orders,
    evaluate_flow_requirements,
    evaluate_stability,
    is_nyse_trading_day,
    next_nyse_market_window,
    parse_args,
    persist_retry_summary,
    ReplayAttemptError,
    rewrite_chain_payloads,
    run_single_attempt,
    send_followups,
    split_active_symbol_conflicts,
    verify_account_flat_for_chains,
    verify_no_open_synthetic_orders,
)

ET = ZoneInfo("America/New_York")


def _event(event_type, signal_id="sig-a", position_id="pos-a", symbol="AAPL", **extra):
    payload = {
        "source": "tv",
        "event_type": event_type,
        "event_id": f"{signal_id}-{event_type}",
        "signal_id": signal_id,
        "position_id": position_id,
        "symbol": symbol,
        "direction": "long",
        "position_side": "long",
        "date": "2026-06-05",
        "market_date": "2026-06-05",
        "us_time": "2026-06-05 09:46:00",
        "cn_time": "2026-06-05 21:46:00",
        "bar_time_ms": 1780667160000,
        "bar_close_ms": 1780667280000,
        "pine_eval_ms": 1780667281000,
        "interval": "2",
        "chart_tf": "2",
        "entry": 100.0,
        "stop_loss": 98.0,
        "take_profit": 103.0,
        "shares": 1,
        "trade_group_id": "grp-origin",
        "entry_order_unique_id": "entry-origin",
        "tp_order_unique_id": "tp-origin",
        "sl_order_unique_id": "sl-origin",
        "extra": {"source": "tradingview", "trade_group_id": "grp-origin"},
    }
    payload.update(extra.pop("payload_updates", {}))
    return {
        "event_id": payload["event_id"],
        "event_type": event_type,
        "signal_id": signal_id if event_type != "pre_alert" else "",
        "position_id": position_id,
        "symbol": symbol,
        "direction": "long",
        "position_side": "long",
        "date": "2026-06-05",
        "environment": "live",
        "broker_mode": "paper",
        "status": "routed",
        "bar_time_ms": payload["bar_time_ms"],
        "created": "2026-06-05 13:46:01.000Z",
        "payload": payload,
        **extra,
    }


def _metric(value):
    return {"ok": True, "data": {"data": {"result": [{"value": [0, str(value)]}]}}}


def _stability_args(**extra):
    data = {
        "skip_health": False,
        "skip_stability_gate": False,
        "max_firing_alerts": 0,
        "max_broker_pending_requests": 0,
        "max_order_failures": 0,
        "max_signal_attention": 0,
        "max_order_operation_p95": 10,
        "max_gateway_serial_wait_p95": 2,
        "max_gateway_serial_timeouts": 0,
    }
    data.update(extra)
    return Namespace(**data)


def _flow_args(**extra):
    data = {
        "min_full_chains": 0,
        "min_bracket_chains": 0,
        "min_filled_entry_chains": 0,
        "min_routed_exit_chains": 0,
        "min_cleanup_exit_chains": 0,
        "min_closed_reverse_chains": 0,
    }
    data.update(extra)
    return Namespace(**data)


def _account_args(**extra):
    data = {"skip_account_flat_check": False}
    data.update(extra)
    return Namespace(**data)


class TodayTvReplayStressTest(unittest.TestCase):
    def test_next_market_window_skips_after_close_to_next_trading_day(self):
        start, end = next_nyse_market_window(
            market_start_et="09:45",
            market_end_et="14:30",
            now_et=datetime(2026, 6, 5, 15, 18, tzinfo=ET),
        )

        self.assertEqual("2026-06-08T09:45:00-04:00", start.isoformat())
        self.assertEqual("2026-06-08T14:30:00-04:00", end.isoformat())

    def test_next_market_window_skips_observed_holiday(self):
        self.assertFalse(is_nyse_trading_day(date(2026, 7, 3)))

        start, _end = next_nyse_market_window(
            market_start_et="09:45",
            market_end_et="14:30",
            now_et=datetime(2026, 7, 3, 10, 0, tzinfo=ET),
        )

        self.assertEqual("2026-07-06T09:45:00-04:00", start.isoformat())

    def test_build_chains_excludes_existing_paper_orders(self):
        events = [_event("entry", "sig-a"), _event("entry", "sig-b", "pos-b", "MSFT")]
        signals = [
            {"signal_id": "sig-a", "environment": "live", "extra": {"execution_by_mode": {"paper": {"status": "expired"}}}},
            {"signal_id": "sig-b", "environment": "live", "extra": {"execution_by_mode": {"paper": {"status": "expired"}}}},
        ]
        orders = [{"signal_id": "sig-b", "environment": "paper", "status": "Submitted"}]

        candidates, excluded = build_chains(events, signals, orders, broker_mode="paper")

        self.assertEqual(["sig-a"], [chain.origin_signal_id for chain in candidates])
        self.assertEqual(["paper_order_exists"], [chain.exclude_reason for chain in excluded])

    def test_apply_max_chains_prefers_full_chains_for_limited_canary(self):
        events = [
            _event("entry", "sig-entry-only", "pos-entry", "AAPL", payload_updates={"bar_time_ms": 1000}),
            _event("entry", "sig-full", "pos-full", "MSFT", payload_updates={"bar_time_ms": 2000}),
            _event("risk_update", "sig-full", "pos-full", "MSFT", payload_updates={"bar_time_ms": 3000}),
            _event("exit", "sig-full", "pos-full", "MSFT", payload_updates={"bar_time_ms": 4000}),
            _event("entry", "sig-risk", "pos-risk", "NVDA", payload_updates={"bar_time_ms": 5000}),
            _event("risk_update", "sig-risk", "pos-risk", "NVDA", payload_updates={"bar_time_ms": 6000}),
        ]
        candidates, excluded = build_chains(events, [], [], broker_mode="paper")
        self.assertFalse(excluded)

        selected = apply_max_chains(candidates, "2", prefer_full_chains=True)

        self.assertEqual(["sig-full", "sig-risk"], [chain.origin_signal_id for chain in selected])

    def test_apply_max_chains_can_keep_chronological_order(self):
        events = [
            _event("entry", "sig-entry-only", "pos-entry", "AAPL", payload_updates={"bar_time_ms": 1000}),
            _event("entry", "sig-full", "pos-full", "MSFT", payload_updates={"bar_time_ms": 2000}),
            _event("risk_update", "sig-full", "pos-full", "MSFT", payload_updates={"bar_time_ms": 3000}),
            _event("exit", "sig-full", "pos-full", "MSFT", payload_updates={"bar_time_ms": 4000}),
        ]
        candidates, excluded = build_chains(events, [], [], broker_mode="paper")
        self.assertFalse(excluded)

        selected = apply_max_chains(candidates, "1", prefer_full_chains=False)

        self.assertEqual(["sig-entry-only"], [chain.origin_signal_id for chain in selected])

    def test_rewrite_chain_payloads_replaces_ids_and_preserves_origin(self):
        events = [_event("pre_alert"), _event("entry"), _event("risk_update"), _event("exit")]
        candidates, excluded = build_chains(events, [], [], broker_mode="paper")
        self.assertFalse(excluded)

        rewrite_chain_payloads(
            candidates,
            run_id="SIMTV_TEST",
            broker_mode="paper",
            data_environment="live",
            include_pre_alert=True,
            base_time=datetime(2026, 6, 5, 10, 0, 0, tzinfo=ET),
        )

        chain = candidates[0]
        entry = chain.payloads["entry"][0]
        risk = chain.payloads["risk_update"][0]
        self.assertEqual("SIMTV_TEST_001_AAPL_long", chain.synthetic_signal_id)
        self.assertEqual(chain.synthetic_signal_id, entry["signal_id"])
        self.assertNotIn("grp-origin", entry["entry_order_unique_id"])
        self.assertEqual("paper", entry["broker_mode"])
        self.assertEqual("live", entry["data_environment"])
        self.assertEqual("sig-a", entry["extra"]["replay_origin"]["signal_id"])
        self.assertEqual(chain.synthetic_signal_id, risk["signal_id"])
        self.assertGreater(entry["pine_eval_ms"], entry["bar_close_ms"])

    def test_cleanup_skips_when_disabled(self):
        chain = build_chains([_event("entry")], [], [], broker_mode="paper")[0][0]
        args = Namespace(no_cleanup=True)
        self.assertEqual([], cleanup_synthetic_orders(args, [chain]))

    def test_active_symbol_conflicts_are_excluded_before_replay(self):
        chain = build_chains([_event("entry")], [], [], broker_mode="paper")[0][0]
        active_signals = [
            {
                "signal_id": "active-aapl",
                "symbol": "AAPL",
                "direction": "short",
                "status": "pending",
                "bar_time_ms": 1780668160000,
                "extra": {"execution_by_mode": {"paper": {"status": "protected_active"}}},
            }
        ]

        candidates, excluded = split_active_symbol_conflicts(
            [chain],
            active_signals,
            broker_mode="paper",
            data_environment="live",
        )

        self.assertEqual([], candidates)
        self.assertEqual(["active_symbol_protected_active"], [item.exclude_reason for item in excluded])
        self.assertEqual("active-aapl", excluded[0].active_conflict_signal_id)

    def test_adopt_routed_signal_id_updates_followup_payloads(self):
        events = [_event("entry"), _event("risk_update"), _event("exit")]
        chain = build_chains(events, [], [], broker_mode="paper")[0][0]
        rewrite_chain_payloads(
            [chain],
            run_id="SIMTV_TEST",
            broker_mode="paper",
            data_environment="live",
            include_pre_alert=False,
            base_time=datetime(2026, 6, 5, 10, 0, 0, tzinfo=ET),
        )

        adopt_routed_signal_id(chain, {"signal_id": "active-signal", "action": "refreshed_active_signal"})

        self.assertEqual("active-signal", chain.synthetic_signal_id)
        self.assertEqual("active-signal", chain.payloads["risk_update"][0]["signal_id"])
        self.assertEqual("active-signal", chain.payloads["exit"][0]["signal_id"])
        self.assertEqual("entry_routed_to_active_signal", chain.checks[-1]["name"])

    def test_terminal_active_policy_response_is_not_adopted(self):
        chain = build_chains([_event("entry")], [], [], broker_mode="paper")[0][0]
        chain.synthetic_signal_id = "sim-signal"

        adopt_routed_signal_id(
            chain,
            {
                "signal_id": "active-signal",
                "action": "blocked_opposite_entry_requires_tv_exit",
                "reason": "blocked_opposite_entry_requires_tv_exit",
            },
        )

        self.assertEqual("sim-signal", chain.synthetic_signal_id)
        self.assertEqual("entry_routed_to_existing_signal_not_adopted", chain.checks[-1]["name"])
        self.assertFalse(chain.checks[-1]["ok"])

    def test_strict_canary_enables_concurrent_followup_stress(self):
        args = parse_args(["--strict-canary", "--dry-run"])

        self.assertTrue(args.followup_stress_concurrent)
        self.assertEqual(3, args.risk_burst_workers)
        self.assertEqual(3, args.exit_burst_workers)
        self.assertEqual(0.0, args.followup_burst_spacing_seconds)

    def test_send_followups_bursts_risk_updates_before_exit_burst(self):
        events = [
            _event("entry", "sig-a", "pos-a", "AAPL"),
            _event("risk_update", "sig-a", "pos-a", "AAPL"),
            _event("exit", "sig-a", "pos-a", "AAPL"),
            _event("entry", "sig-b", "pos-b", "MSFT"),
            _event("risk_update", "sig-b", "pos-b", "MSFT"),
            _event("exit", "sig-b", "pos-b", "MSFT"),
        ]
        chains, excluded = build_chains(events, [], [], broker_mode="paper")
        self.assertFalse(excluded)
        rewrite_chain_payloads(
            chains,
            run_id="SIMTV_BURST",
            broker_mode="paper",
            data_environment="live",
            include_pre_alert=False,
            base_time=datetime(2026, 6, 5, 10, 0, 0, tzinfo=ET),
        )
        orders = [
            {"role": "entry", "status": "Filled", "quantity": 1, "filled_qty": 1, "limit_price": 100.0, "fill_price": 100.0},
            {"role": "take_profit", "status": "Submitted", "quantity": 1, "limit_price": 103.0, "tp_price": 103.0},
            {"role": "stop_loss", "status": "Submitted", "quantity": 1, "limit_price": 98.0, "sl_price": 98.0},
        ]
        args = Namespace(
            followup_stress_concurrent=True,
            risk_burst_workers=4,
            exit_burst_workers=4,
            followup_burst_spacing_seconds=0.0,
            data_environment="live",
        )
        sent_types = []

        def fake_send(_args, payload):
            sent_types.append(payload["event_type"])
            return {"ok": True, "event_id": payload["event_id"], "event_type": payload["event_type"], "response": {"ok": True}}

        with mock.patch("run_today_tv_replay_stress.query_orders", return_value=orders), mock.patch(
            "run_today_tv_replay_stress.send_payload",
            side_effect=fake_send,
        ), mock.patch(
            "run_today_tv_replay_stress.wait_for_tv_event_terminal",
            side_effect=lambda _args, event_id: {"status": "routed", "route_record_id": f"route-{event_id}"},
        ):
            send_followups(args, chains)

        self.assertCountEqual(["risk_update", "risk_update", "exit", "exit"], sent_types)
        self.assertTrue(all(any(check.get("name") == "send_risk_update" for check in chain.checks) for chain in chains))
        self.assertTrue(all(any(check.get("name") == "send_exit" for check in chain.checks) for chain in chains))
        self.assertEqual(2, sum(1 for chain in chains for check in chain.checks if check.get("name") == "route_risk_update" and check.get("ok")))
        self.assertEqual(2, sum(1 for chain in chains for check in chain.checks if check.get("name") == "route_exit" and check.get("ok")))

    def test_cleanup_filled_entry_uses_tv_exit_with_actual_trade_group(self):
        chain = build_chains([_event("entry")], [], [], broker_mode="paper")[0][0]
        chain.synthetic_signal_id = "sim-signal"
        chain.synthetic_position_id = "sim-pos"
        chain.synthetic_trade_group_id = "stale-grp"
        args = Namespace(
            no_cleanup=False,
            base_url="https://example.invalid",
            http_timeout=1.0,
            broker_mode="paper",
            data_environment="live",
            run_id="SIMTV_TEST",
            poll_seconds=1.0,
            poll_interval=0.1,
            cleanup_spacing_seconds=0.0,
        )
        orders = [
            {
                "role": "entry",
                "status": "Filled",
                "symbol": "AAPL",
                "direction": "long",
                "quantity": 1,
                "filled_qty": 1,
                "trade_group_id": "actual-grp",
                "unique_id": "entry-actual",
                "fill_price": 100.0,
            },
            {"role": "take_profit", "status": "Submitted", "trade_group_id": "actual-grp", "limit_price": 103.0},
            {"role": "stop_loss", "status": "Submitted", "trade_group_id": "actual-grp", "limit_price": 98.0},
        ]

        with mock.patch("run_today_tv_replay_stress.query_orders", return_value=orders), mock.patch(
            "run_today_tv_replay_stress.post_json", return_value={"ok": True, "_http_status": 200}
        ) as post_json, mock.patch("run_today_tv_replay_stress.wait_for_tv_event_terminal", return_value={"status": "routed"}), mock.patch(
            "run_today_tv_replay_stress.wait_for", return_value=[{"status": "confirmed"}]
        ):
            results = cleanup_synthetic_orders(args, [chain])

        self.assertEqual("exit_cleanup", results[0]["action"])
        self.assertTrue(results[0]["ok"])
        self.assertEqual("actual-grp", results[0]["trade_group_id"])
        self.assertEqual("/webhook/tv", post_json.call_args.args[1])
        self.assertEqual("actual-grp", post_json.call_args.args[2]["trade_group_id"])

    def test_cleanup_missing_trade_group_adds_failing_chain_check(self):
        chain = build_chains([_event("entry")], [], [], broker_mode="paper")[0][0]
        chain.synthetic_signal_id = "sim-signal"
        args = Namespace(no_cleanup=False)
        orders = [{"role": "entry", "status": "Submitted", "unique_id": "entry-actual"}]

        with mock.patch("run_today_tv_replay_stress.query_orders", return_value=orders):
            results = cleanup_synthetic_orders(args, [chain])

        self.assertFalse(results[0]["ok"])
        self.assertEqual("cleanup_synthetic_orders", chain.checks[-1]["name"])
        self.assertFalse(chain.checks[-1]["ok"])

    def test_post_cleanup_open_orders_are_a_failure(self):
        chain = build_chains([_event("entry")], [], [], broker_mode="paper")[0][0]
        chain.synthetic_signal_id = "sim-signal"
        args = Namespace(no_cleanup=False)
        orders = [{"role": "take_profit", "status": "Submitted", "order_id": "123"}]

        with mock.patch("run_today_tv_replay_stress.query_orders", return_value=orders):
            results = verify_no_open_synthetic_orders(args, [chain])

        self.assertFalse(results[0]["ok"])
        self.assertEqual(1, results[0]["open_order_count"])
        self.assertEqual("post_cleanup_no_open_synthetic_orders", chain.checks[-1]["name"])
        self.assertFalse(chain.checks[-1]["ok"])

    def test_account_flat_check_fails_on_symbol_position_or_open_order(self):
        chain = build_chains([_event("entry")], [], [], broker_mode="paper")[0][0]
        snapshot = {
            "ok": True,
            "environment": "paper",
            "positions": [{"symbol": "AAPL", "quantity": 2}],
            "orders": [{"symbol": "AAPL", "status": "Submitted", "order_id": "123"}],
        }

        result = verify_account_flat_for_chains(_account_args(), [chain], phase="before", snapshot=snapshot)

        self.assertFalse(result["ok"])
        self.assertEqual("AAPL", result["failures"][0]["symbol"])
        self.assertEqual("account_flat_before", chain.checks[-1]["name"])
        self.assertFalse(chain.checks[-1]["ok"])

    def test_account_flat_check_passes_when_selected_symbols_flat(self):
        chain = build_chains([_event("entry")], [], [], broker_mode="paper")[0][0]
        snapshot = {
            "ok": True,
            "environment": "paper",
            "positions": [{"symbol": "AAPL", "quantity": 0}, {"symbol": "MSFT", "quantity": 5}],
            "orders": [{"symbol": "AAPL", "status": "Filled", "order_id": "123"}],
        }

        result = verify_account_flat_for_chains(_account_args(), [chain], phase="after", snapshot=snapshot)

        self.assertTrue(result["ok"])
        self.assertEqual("account_flat_after", chain.checks[-1]["name"])
        self.assertTrue(chain.checks[-1]["ok"])

    def test_flow_requirements_fail_when_minimums_are_not_met(self):
        chain = build_chains([_event("entry"), _event("risk_update"), _event("exit")], [], [], broker_mode="paper")[0][0]
        chain.final_orders = [
            {"role": "entry", "status": "Submitted"},
            {"role": "take_profit", "status": "Submitted"},
            {"role": "stop_loss", "status": "Submitted"},
        ]

        result = evaluate_flow_requirements(
            _flow_args(min_full_chains=1, min_bracket_chains=1, min_filled_entry_chains=1),
            [chain],
            [],
        )

        self.assertFalse(result["ok"])
        self.assertIn("filled_entry_chains", {item["name"] for item in result["failures"]})

    def test_flow_requirements_accept_complete_filled_and_closed_chain(self):
        chain = build_chains([_event("entry"), _event("risk_update"), _event("exit")], [], [], broker_mode="paper")[0][0]
        chain.final_orders = [
            {"role": "entry", "status": "Filled", "filled_qty": 1},
            {"role": "take_profit", "status": "Canceled"},
            {"role": "stop_loss", "status": "Canceled"},
        ]
        chain.checks.append({"name": "route_exit", "ok": True})
        chain.final_reverses = [{"action_type": "close", "status": "confirmed"}]
        cleanup_results = [{"ok": True, "action": "exit_cleanup"}]

        result = evaluate_flow_requirements(
            _flow_args(
                min_full_chains=1,
                min_bracket_chains=1,
                min_filled_entry_chains=1,
                min_routed_exit_chains=1,
                min_cleanup_exit_chains=1,
                min_closed_reverse_chains=1,
            ),
            [chain],
            cleanup_results,
        )

        self.assertTrue(result["ok"])

    def test_run_single_attempt_raises_plan_diagnostic_for_insufficient_full_chains(self):
        chain = build_chains([_event("entry")], [], [], broker_mode="paper")[0][0]
        args = Namespace(dry_run=False, min_selected_chains=1, min_full_chains=1, preview_payloads=0)
        summary = {
            "run_id": "SIMTV_TEST",
            "market_date": "2026-06-05",
            "selected_chains": 1,
            "selected_full_chains": 0,
        }

        with mock.patch("run_today_tv_replay_stress.build_replay_plan", return_value=([chain], [], summary)):
            with self.assertRaises(ReplayAttemptError) as raised:
                run_single_attempt(args)

        payload = raised.exception.payload
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["plan_diagnostic"])
        self.assertEqual("plan_preflight", payload["stage"])
        self.assertIn("insufficient_full_replay_chains", payload["error"])

    def test_persist_retry_summary_writes_json_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = Namespace(artifact_root=tmp, write_artifacts=False, dry_run=False)
            payload = {
                "ok": False,
                "reason": "retrying_until_market_end",
                "attempts": [{"attempt": 1, "ok": False, "run_id": "SIMTV_TEST_R01", "error": "boom"}],
            }

            artifact_dir = persist_retry_summary(args, "SIMTV_TEST", payload)

            summary_path = Path(artifact_dir) / "retry_summary.json"
            report_path = Path(artifact_dir) / "retry_report.md"
            self.assertTrue(summary_path.exists())
            self.assertTrue(report_path.exists())
            stored = json.loads(summary_path.read_text())
            self.assertEqual("retrying_until_market_end", stored["reason"])
            self.assertEqual(artifact_dir, payload["artifact_dir"])
            self.assertIn("SIMTV_TEST_R01", report_path.read_text())

    def test_strict_canary_preset_applies_retry_and_flow_gates(self):
        args = parse_args(["--strict-canary", "--dry-run", "--no-cleanup", "--skip-health", "--max-chains", "1"])

        self.assertEqual("today-et", args.market_date)
        self.assertEqual("3", args.max_chains)
        self.assertTrue(args.retry_until_market_end)
        self.assertTrue(args.wait_for_next_market_window)
        self.assertEqual(3, args.min_selected_chains)
        self.assertEqual(3, args.min_full_chains)
        self.assertEqual(3, args.min_bracket_chains)
        self.assertEqual(1, args.min_filled_entry_chains)
        self.assertEqual(1, args.min_routed_exit_chains)
        self.assertEqual(1, args.min_closed_reverse_chains)
        self.assertFalse(args.no_cleanup)
        self.assertFalse(args.skip_health)
        self.assertFalse(args.skip_account_flat_check)

    def test_stability_gate_rejects_alerts_and_gateway_timeouts(self):
        health = {
            "stack": {"ok": True},
            "monitoring": {"ok": True},
            "alerts": {"ok": True, "firing_count": 1},
            "metrics": {
                "broker_pending_requests": _metric(0),
                "order_failures_window": _metric(0),
                "signal_attention_window": _metric(0),
                "order_operation_p95": _metric(0.2),
                "gateway_serial_wait_p95": _metric(0.1),
                "gateway_serial_timeouts_window": _metric(1),
                "_lookback": {"range": "5m"},
            },
        }

        result = evaluate_stability(_stability_args(), health, "after")

        self.assertFalse(result["ok"])
        self.assertIn("firing_alerts", {item["name"] for item in result["failures"]})
        self.assertIn("gateway_serial_timeouts_window", {item["name"] for item in result["failures"]})

    def test_stability_gate_accepts_clean_health_and_metrics(self):
        health = {
            "stack": {"ok": True},
            "monitoring": {"ok": True},
            "alerts": {"ok": True, "firing_count": 0},
            "metrics": {
                "broker_pending_requests": _metric(0),
                "order_failures_window": _metric(0),
                "signal_attention_window": _metric(0),
                "order_operation_p95": _metric(0.2),
                "gateway_serial_wait_p95": _metric(0.1),
                "gateway_serial_timeouts_window": _metric(0),
                "_lookback": {"range": "5m"},
            },
        }

        result = evaluate_stability(_stability_args(), health, "after")

        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
