import sys
import unittest
from pathlib import Path


SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))


from ibkr_api.universe.lifecycle_flow import build_lifecycle_flow_response


class FakePocketBase:
    def __init__(self, records=None):
        self.records = records or {}

    def get_all_records(self, collection, **kwargs):
        return [dict(row) for row in self.records.get(collection, [])]


def normalize_environment(value, default="live"):
    return str(value or default).strip().lower()


def time_strings():
    return {
        "date": "2026-04-28",
        "us": "2026-04-28 10:00:00",
        "cn": "2026-04-28 22:00:00",
    }


class LifecycleFlowApiTest(unittest.TestCase):
    def build(self, records, payload):
        return build_lifecycle_flow_response(
            FakePocketBase(records),
            payload={"environment": "live", "date": "2026-04-28", "symbol": "AAPL", **payload},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
        )

    def test_actual_execution_fill_wins_over_order_fill_price(self):
        payload, status_code = self.build(
            {
                "ibkr_signals": [
                    {"symbol": "AAPL", "signal_id": "sig1", "status": "pending", "entry": 100, "shares": 10, "bar_time_ms": 1000}
                ],
                "orders": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig1",
                        "trade_group_id": "tg1",
                        "order_id": "1001",
                        "role": "entry",
                        "status": "Filled",
                        "quantity": 10,
                        "filled_qty": 10,
                        "fill_price": 99,
                        "bar_time_ms": 2000,
                    }
                ],
                "ibkr_execution_fills": [
                    {"symbol": "AAPL", "order_id": "1001", "exec_id": "e1", "shares": 10, "price": 101, "trade_time_ms": 2100}
                ],
            },
            {"signal_id": "sig1", "trade_group_id": "tg1"},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        fill_events = [event for event in payload["events"] if event["event_type"] in {"fill_execution", "entry_filled"}]
        self.assertTrue(fill_events)
        self.assertTrue(all(event.get("fill_source") == "actual_ibkr" for event in fill_events))
        self.assertIn(101, [event.get("price") for event in fill_events])
        self.assertNotIn(99, [event.get("price") for event in fill_events])
        self.assertEqual(payload["source_summary"]["actual_entry_qty"], 10)

    def test_filled_status_without_actual_fill_does_not_fabricate_plan_price(self):
        payload, status_code = self.build(
            {
                "ibkr_signals": [
                    {"symbol": "AAPL", "signal_id": "sig2", "status": "pending", "entry": 150, "shares": 10, "bar_time_ms": 1000}
                ],
                "orders": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig2",
                        "trade_group_id": "tg2",
                        "order_id": "1002",
                        "role": "entry",
                        "status": "Filled",
                        "quantity": 10,
                        "filled_qty": 0,
                        "fill_price": 0,
                        "bar_time_ms": 2000,
                    }
                ],
            },
            {"signal_id": "sig2", "trade_group_id": "tg2"},
        )

        self.assertEqual(status_code, 200)
        warning_codes = {warning["code"] for warning in payload["warnings"]}
        self.assertIn("filled_status_without_fill_qty", warning_codes)
        missing_events = [event for event in payload["events"] if event["event_type"] == "fill_missing_actual"]
        self.assertEqual(1, len(missing_events))
        self.assertEqual("unknown", missing_events[0].get("fill_source"))
        self.assertNotEqual(150, missing_events[0].get("price"))
        actual_fill_events = [event for event in payload["events"] if event.get("fill_source") == "actual_ibkr"]
        self.assertEqual([], actual_fill_events)

    def test_partial_fill_warns_when_protection_exceeds_remaining_position(self):
        payload, status_code = self.build(
            {
                "orders": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig3",
                        "trade_group_id": "tg3",
                        "order_id": "1003",
                        "role": "entry",
                        "status": "Submitted",
                        "quantity": 100,
                        "filled_qty": 40,
                        "fill_price": 120,
                        "bar_time_ms": 2000,
                    },
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig3",
                        "trade_group_id": "tg3",
                        "order_id": "1004",
                        "role": "stop_loss",
                        "status": "Submitted",
                        "quantity": 100,
                        "sl_price": 115,
                        "bar_time_ms": 2200,
                    },
                ],
                "ibkr_execution_fills": [
                    {"symbol": "AAPL", "order_id": "1003", "exec_id": "e3", "shares": 40, "price": 120, "trade_time_ms": 2100}
                ],
            },
            {"signal_id": "sig3", "trade_group_id": "tg3"},
        )

        self.assertEqual(status_code, 200)
        event_types = {event["event_type"] for event in payload["events"]}
        warning_codes = {warning["code"] for warning in payload["warnings"]}
        self.assertIn("entry_partially_filled", event_types)
        self.assertIn("protection_qty_exceeds_remaining_position", warning_codes)
        mismatch = [event for event in payload["events"] if event["event_type"] == "protection_qty_mismatch"]
        self.assertTrue(mismatch)
        self.assertEqual(40, mismatch[0]["details"]["remaining_position_qty"])

    def test_normal_bracket_tp_and_sl_each_match_remaining_qty(self):
        payload, status_code = self.build(
            {
                "orders": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_bracket",
                        "trade_group_id": "tg_bracket",
                        "order_id": "2001",
                        "role": "entry",
                        "status": "Filled",
                        "quantity": 10,
                        "filled_qty": 10,
                        "fill_price": 100,
                        "bar_time_ms": 2000,
                    },
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_bracket",
                        "trade_group_id": "tg_bracket",
                        "order_id": "2002",
                        "role": "take_profit",
                        "status": "Submitted",
                        "quantity": 10,
                        "tp_price": 110,
                        "bar_time_ms": 2100,
                    },
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_bracket",
                        "trade_group_id": "tg_bracket",
                        "order_id": "2003",
                        "role": "stop_loss",
                        "status": "Submitted",
                        "quantity": 10,
                        "sl_price": 95,
                        "bar_time_ms": 2100,
                    },
                ],
                "ibkr_execution_fills": [
                    {"symbol": "AAPL", "order_id": "2001", "exec_id": "e_bracket", "shares": 10, "price": 100, "trade_time_ms": 2050}
                ],
            },
            {"signal_id": "sig_bracket", "trade_group_id": "tg_bracket"},
        )

        self.assertEqual(status_code, 200)
        warning_codes = {warning["code"] for warning in payload["warnings"]}
        self.assertNotIn("protection_qty_exceeds_remaining_position", warning_codes)
        self.assertEqual({"take_profit": 10, "stop_loss": 10}, payload["source_summary"]["open_protection_qty_by_role"])
        tp_event = next(event for event in payload["events"] if event["event_type"] == "take_profit_created")
        sl_event = next(event for event in payload["events"] if event["event_type"] == "stop_loss_created")
        self.assertEqual("take_profit_price", tp_event["price_kind"])
        self.assertEqual("止盈价", tp_event["price_label"])
        self.assertEqual("stop_loss_price", sl_event["price_kind"])
        self.assertEqual("止损价", sl_event["price_label"])
        tp_node = next(node for node in payload["nodes"] if node["event_id"] == tp_event["id"])
        self.assertEqual("AAPL", tp_node["symbol"])
        self.assertEqual("take_profit_price", tp_node["price_kind"])

    def test_execution_fill_matches_broker_order_id(self):
        payload, status_code = self.build(
            {
                "orders": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_broker",
                        "trade_group_id": "tg_broker",
                        "order_id": "client_ref_1",
                        "broker_order_id": "3001",
                        "role": "entry",
                        "status": "Filled",
                        "quantity": 7,
                        "filled_qty": 7,
                        "fill_price": 88,
                        "bar_time_ms": 2000,
                    }
                ],
                "ibkr_execution_fills": [
                    {"symbol": "AAPL", "order_id": "3001", "exec_id": "e_broker", "shares": 7, "price": 89, "trade_time_ms": 2050}
                ],
            },
            {"signal_id": "sig_broker", "trade_group_id": "tg_broker"},
        )

        self.assertEqual(status_code, 200)
        fill_events = [event for event in payload["events"] if event["event_type"] == "fill_execution"]
        self.assertEqual(1, len(fill_events))
        self.assertEqual(89, fill_events[0]["price"])
        warning_codes = {warning["code"] for warning in payload["warnings"]}
        self.assertNotIn("execution_fill_unmatched", warning_codes)

    def test_order_details_do_not_double_count_position(self):
        payload, status_code = self.build(
            {
                "orders": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_detail",
                        "trade_group_id": "tg_detail",
                        "order_id": "4001",
                        "role": "entry",
                        "status": "Filled",
                        "quantity": 5,
                        "filled_qty": 5,
                        "fill_price": 77,
                        "bar_time_ms": 2000,
                    }
                ],
                "ibkr_order_details": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_detail",
                        "trade_group_id": "tg_detail",
                        "order_id": "4001",
                        "role": "entry",
                        "status": "Filled",
                        "quantity": 5,
                        "filled_qty": 5,
                        "fill_price": 77,
                        "bar_time_ms": 2010,
                    }
                ],
                "ibkr_execution_fills": [
                    {"symbol": "AAPL", "order_id": "4001", "exec_id": "e_detail", "shares": 5, "price": 77, "trade_time_ms": 2050}
                ],
            },
            {"signal_id": "sig_detail", "trade_group_id": "tg_detail"},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(5, payload["source_summary"]["actual_entry_qty"])
        self.assertEqual(1, len([event for event in payload["events"] if event["event_type"] == "order_detail_snapshot"]))
        snapshot = next(event for event in payload["events"] if event["event_type"] == "order_detail_snapshot")
        self.assertEqual("order_detail_unverified_fill", snapshot["price_kind"])
        self.assertNotEqual("actual_ibkr", snapshot.get("fill_source"))
        self.assertEqual([], [node for node in payload["nodes"] if node["type"] == "order_detail_snapshot"])

    def test_order_detail_snapshots_stay_out_of_graph_nodes(self):
        payload, status_code = self.build(
            {
                "orders": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_snapshots",
                        "trade_group_id": "tg_snapshots",
                        "order_id": "4101",
                        "role": "entry",
                        "status": "Submitted",
                        "quantity": 2,
                        "limit_price": 77,
                        "bar_time_ms": 2000,
                    }
                ],
                "ibkr_order_details": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_snapshots",
                        "trade_group_id": "tg_snapshots",
                        "order_id": f"410{i}",
                        "role": "entry",
                        "status": "Submitted",
                        "quantity": 2,
                        "limit_price": 77,
                        "bar_time_ms": 2010 + i,
                    }
                    for i in range(1, 4)
                ],
            },
            {"signal_id": "sig_snapshots", "trade_group_id": "tg_snapshots"},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(3, len([event for event in payload["events"] if event["event_type"] == "order_detail_snapshot"]))
        self.assertFalse(any(node["type"] == "order_detail_snapshot" for node in payload["nodes"]))
        self.assertTrue(any(node["type"] == "lifecycle_endpoint" for node in payload["nodes"]))

    def test_modified_protection_events_include_change_summary_and_times(self):
        payload, status_code = self.build(
            {
                "orders": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_modify",
                        "trade_group_id": "tg_modify",
                        "order_id": "4201",
                        "role": "repair_sl",
                        "status": "Submitted",
                        "quantity": 5,
                        "limit_price": 96.25,
                        "sl_price": 96.25,
                        "bar_time_ms": 2200,
                        "extra": {"old_sl": 95.0, "new_sl": 96.25},
                    }
                ],
            },
            {"signal_id": "sig_modify", "trade_group_id": "tg_modify"},
        )

        self.assertEqual(status_code, 200)
        event = next(event for event in payload["events"] if event["event_type"] == "stop_loss_modified")
        self.assertEqual("SL 95 -> 96.25", event["change_summary"])
        self.assertEqual(2200, event["changed_at_ms"])
        self.assertTrue(event["changed_at_us"])
        self.assertTrue(event["changed_at_cn"])
        self.assertEqual([("stop_loss", 95.0, 96.25)], [(item["field"], item["before"], item["after"]) for item in event["changes"]])
        node = next(node for node in payload["nodes"] if node["type"] == "stop_loss_modified")
        self.assertEqual(event["changes"], node["changes"])
        self.assertEqual("SL 95 -> 96.25", node["change_summary"])

    def test_lifecycle_endpoint_marks_active_open_chain(self):
        payload, status_code = self.build(
            {
                "orders": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_active",
                        "trade_group_id": "tg_active",
                        "order_id": "4301",
                        "role": "entry",
                        "status": "Submitted",
                        "quantity": 3,
                        "limit_price": 123,
                        "bar_time_ms": 2000,
                    }
                ]
            },
            {"signal_id": "sig_active", "trade_group_id": "tg_active"},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(all(node.get("ts_ms", 0) > 0 for node in payload["nodes"]))
        endpoint = next(node for node in payload["nodes"] if node["type"] == "lifecycle_endpoint")
        self.assertEqual("active", endpoint["state"])
        self.assertEqual("当前仍进行中", endpoint["label"])
        self.assertEqual("tg_active", endpoint["trade_group_id"])

    def test_lifecycle_endpoint_marks_terminal_closed_chain(self):
        payload, status_code = self.build(
            {
                "orders": [
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_done",
                        "trade_group_id": "tg_done",
                        "order_id": "4401",
                        "role": "entry",
                        "status": "Filled",
                        "quantity": 3,
                        "filled_qty": 3,
                        "fill_price": 100,
                        "bar_time_ms": 2000,
                    },
                    {
                        "symbol": "AAPL",
                        "signal_id": "sig_done",
                        "trade_group_id": "tg_done",
                        "order_id": "4402",
                        "role": "take_profit",
                        "status": "Filled",
                        "quantity": 3,
                        "filled_qty": 3,
                        "fill_price": 110,
                        "bar_time_ms": 3000,
                    },
                ]
            },
            {"signal_id": "sig_done", "trade_group_id": "tg_done"},
        )

        self.assertEqual(status_code, 200)
        endpoint = next(node for node in payload["nodes"] if node["type"] == "lifecycle_endpoint")
        self.assertEqual("terminal", endpoint["state"])
        self.assertEqual("生命周期结束", endpoint["label"])
        self.assertEqual("ended_by_exit_take_profit", endpoint["reason"])

    def test_edges_stay_within_trade_group_context(self):
        payload, status_code = self.build(
            {
                "ibkr_signals": [
                    {"symbol": "NOW", "signal_id": "sig_now", "status": "pending", "entry": 93.76, "shares": 2, "bar_time_ms": 900}
                ],
                "orders": [
                    {
                        "symbol": "NOW",
                        "signal_id": "sig_now",
                        "trade_group_id": "NOW_long_core",
                        "order_id": "core-entry",
                        "role": "entry",
                        "status": "Submitted",
                        "quantity": 1,
                        "limit_price": 93.76,
                        "bar_time_ms": 1000,
                    },
                    {
                        "symbol": "NOW",
                        "signal_id": "sig_now",
                        "trade_group_id": "NOW_long_tactical",
                        "order_id": "tactical-entry",
                        "role": "entry",
                        "status": "Submitted",
                        "quantity": 1,
                        "limit_price": 93.76,
                        "bar_time_ms": 1100,
                    },
                    {
                        "symbol": "NOW",
                        "signal_id": "sig_now",
                        "trade_group_id": "NOW_long_core",
                        "order_id": "core-tp",
                        "role": "take_profit",
                        "status": "Submitted",
                        "quantity": 1,
                        "tp_price": 99.37,
                        "bar_time_ms": 1200,
                    },
                    {
                        "symbol": "NOW",
                        "signal_id": "sig_now",
                        "trade_group_id": "NOW_long_tactical",
                        "order_id": "tactical-tp",
                        "role": "take_profit",
                        "status": "Submitted",
                        "quantity": 1,
                        "tp_price": 99.37,
                        "bar_time_ms": 1300,
                    },
                ],
            },
            {"symbol": "NOW", "signal_id": "sig_now"},
        )

        self.assertEqual(status_code, 200)
        nodes_by_id = {node["id"]: node for node in payload["nodes"]}
        for edge in payload["edges"]:
            left = nodes_by_id[edge["source"]]
            right = nodes_by_id[edge["target"]]
            left_group = left.get("trade_group_id") or ""
            right_group = right.get("trade_group_id") or ""
            if left_group and right_group:
                self.assertEqual(left_group, right_group)
        tp_nodes = [node for node in payload["nodes"] if node["type"] == "take_profit_created"]
        self.assertEqual({"NOW"}, {node["symbol"] for node in tp_nodes})
        self.assertEqual({"take_profit_price"}, {node["price_kind"] for node in tp_nodes})

    def test_backtest_fill_events_are_labeled_simulated(self):
        payload, status_code = build_lifecycle_flow_response(
            FakePocketBase(
                {
                    "ibkr_backtest_runs": [
                        {
                            "id": "run1",
                            "status": "completed",
                            "metrics": {
                                "backtest_audit": {
                                    "timeline": [
                                        {
                                            "event_type": "entry_filled",
                                            "stage": "execution",
                                            "symbol": "AAPL",
                                            "signal_id": "bt_sig1",
                                            "bar_time_ms": 1000,
                                            "shares": 5,
                                            "price": 123.45,
                                            "reason": "simulated_fill",
                                        }
                                    ]
                                }
                            },
                        }
                    ]
                }
            ),
            payload={"mode": "backtest", "run_id": "run1", "symbol": "AAPL"},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual("backtest", payload["mode"])
        fill_events = [event for event in payload["events"] if event["event_type"] == "entry_filled"]
        self.assertEqual(1, len(fill_events))
        self.assertEqual("backtest_simulated", fill_events[0]["fill_source"])
        self.assertEqual("backtest_simulated", payload["nodes"][0]["fill_source"])

    def test_backtest_non_fill_events_are_not_simulated_fills(self):
        payload, status_code = build_lifecycle_flow_response(
            FakePocketBase(
                {
                    "ibkr_backtest_runs": [{"id": "run2", "status": "completed", "metrics": {}}],
                    "ibkr_backtest_targets": [{"run_id": "run2", "symbol": "AAPL", "date": "2026-04-28", "score": 9, "bar_time_ms": 1000}],
                    "ibkr_backtest_signals": [
                        {"run_id": "run2", "symbol": "AAPL", "date": "2026-04-28", "signal_id": "bt_sig2", "status": "generated", "entry": 100, "shares": 1, "bar_time_ms": 2000}
                    ],
                }
            ),
            payload={"mode": "backtest", "run_id": "run2", "symbol": "AAPL", "backtest_date": "2026-04-28"},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
        )

        self.assertEqual(status_code, 200)
        non_fill_events = [event for event in payload["events"] if event["event_type"] in {"target_selected", "signal_generated"}]
        self.assertTrue(non_fill_events)
        self.assertTrue(all(not event.get("fill_source") for event in non_fill_events))
        self.assertTrue(all(event.get("price_kind") == "not_a_fill" for event in non_fill_events))


if __name__ == "__main__":
    unittest.main()
