import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

if "flask" not in sys.modules:
    import types

    flask_stub = types.ModuleType("flask")
    class _FakeFlask:
        def __init__(self, name):
            self.name = name

        def route(self, _path, methods=None):
            def decorator(func):
                return func
            return decorator

    flask_stub.Flask = _FakeFlask
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = types.SimpleNamespace(args={}, headers={}, method="GET", get_json=lambda silent=True: {})
    sys.modules["flask"] = flask_stub

from ibkr_api.account.snapshot import build_account_snapshot_response, enrich_account_snapshot


class _FakePB:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = {
            "orders": [
                {
                    "id": "ord-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "Filled",
                    "signal_id": "sig-1",
                    "trade_group_id": "grp-1",
                    "entry_order_unique_id": "entry-1",
                    "unique_id": "entry-1",
                    "role": "entry",
                    "relation_status": "active",
                    "direction": "long",
                    "position_side": "long",
                    "quantity": 100,
                    "filled_qty": 100,
                    "limit_price": 101,
                    "fill_price": 101.2,
                    "commission": 1.23,
                    "commission_currency": "USD",
                    "updated": "2026-04-23 09:40:00",
                }
            ],
            "ibkr_signals": [
                {
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "executed",
                    "note": "filled",
                    "updated": "2026-04-23 09:41:00",
                }
            ],
        }
        if rows is not None:
            self.rows = rows

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        self.calls.append(
            {
                "collection": collection,
                "filter": filter,
                "sort": sort,
                "per_page": per_page,
                "page": page,
            }
        )
        return list(self.rows.get(collection, []))


class AccountSnapshotRoutesTest(unittest.TestCase):
    def test_build_account_snapshot_response_forwards_include_pnl_flag(self):
        calls = []

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            calls.append(
                {
                    "method": method,
                    "base_url": base_url,
                    "path": path,
                    "params": list(params or []),
                    "timeout": timeout,
                }
            )
            return {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "environment": "paper",
                    "summary": {},
                    "positions": [],
                    "orders": [],
                    "live_open_orders": [],
                    "counts": {},
                },
                "target_url": "http://runtime/ibkr/account",
                "elapsed_ms": 12.3,
                "timeout_s": timeout,
            }

        payload, status_code = build_account_snapshot_response(
            _FakePB(rows={"orders": [], "ibkr_signals": []}),
            payload={"broker_mode": "paper", "environment": "paper", "include_pnl": "0"},
            normalize_environment=lambda value, default="live": value or default,
            request_json_request=request_json_request,
            runtime_base_url="http://runtime",
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            [("broker_mode", "paper"), ("environment", "paper"), ("include_pnl", "0")],
            calls[0]["params"],
        )

    def test_enrich_account_snapshot_builds_reconciliation_fields(self):
        pb = _FakePB()
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [{"symbol": "AAPL", "quantity": 100}],
            "orders": [{"order_id": "1001", "client_order_id": "entry-1", "symbol": "AAPL", "status": "Submitted", "total_quantity": 100, "filled_quantity": 0}],
            "live_open_orders": [
                {
                    "order_id": "1001",
                    "client_order_id": "entry-1",
                    "symbol": "AAPL",
                    "status": "Submitted",
                    "total_quantity": 100,
                    "filled_quantity": 0,
                    "remaining_quantity": 100,
                    "can_cancel": True,
                    "can_modify": True,
                    "outside_rth": False,
                    "submitted_time": "2026-04-23 09:42:00",
                }
            ],
            "live_order_coverage": {"coverage_state": "complete", "bulk_open_count": 1},
            "counts": {"open_positions": 1},
        }

        enriched = enrich_account_snapshot(pb, payload, "live")

        self.assertEqual("system_managed", enriched["positions"][0]["relation"]["status"])
        self.assertEqual(1, enriched["counts"]["system_managed_positions"])
        self.assertEqual(1, enriched["order_reconciliation"]["broker_matched_orders"])
        self.assertEqual(1, len(enriched["matched_order_groups"]))
        self.assertEqual("matched", enriched["live_open_orders"][0]["pb_context"]["match_state"])
        self.assertEqual(1.23, enriched["live_open_orders"][0]["commission"])
        self.assertEqual(1.23, enriched["live_open_orders"][0]["pb_context"]["pb_commission"])
        self.assertEqual(1.23, enriched["matched_order_groups"][0]["commission"])

    def test_close_role_offsets_open_exposure_for_history_groups(self):
        pb = _FakePB(
            {
                "orders": [
                    {
                        "id": "ord-entry",
                        "environment": "live",
                        "symbol": "NVDA",
                        "status": "Closed",
                        "signal_id": "sig-nvda",
                        "trade_group_id": "grp-nvda",
                        "entry_order_unique_id": "entry-nvda",
                        "unique_id": "entry-nvda",
                        "role": "entry",
                        "relation_status": "closed",
                        "direction": "long",
                        "position_side": "long",
                        "quantity": 43,
                        "filled_qty": 43,
                        "limit_price": 232.77,
                        "fill_price": 232.42,
                        "updated": "2026-05-14 14:55:16",
                    },
                    {
                        "id": "ord-close",
                        "environment": "live",
                        "symbol": "NVDA",
                        "status": "Filled",
                        "signal_id": "sig-nvda",
                        "trade_group_id": "grp-nvda",
                        "entry_order_unique_id": "entry-nvda",
                        "unique_id": "close-nvda",
                        "role": "close",
                        "relation_status": "closed",
                        "direction": "long",
                        "position_side": "long",
                        "quantity": 43,
                        "filled_qty": 43,
                        "limit_price": 234.86,
                        "fill_price": 234.87,
                        "updated": "2026-05-14 14:57:22",
                    },
                ],
                "ibkr_signals": [],
            }
        )
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [],
            "orders": [],
            "live_open_orders": [],
            "live_order_coverage": {"coverage_state": "complete", "bulk_open_count": 0},
            "counts": {},
        }

        enriched = enrich_account_snapshot(pb, payload, "live")

        self.assertEqual([], enriched["managed_order_groups"])
        self.assertEqual([], enriched["stale_pb_order_groups"])
        self.assertEqual(0, enriched["counts"]["pb_active_order_groups"])

    def test_flat_position_relation_uses_closed_order_group_and_commissions(self):
        pb = _FakePB(
            {
                "orders": [
                    {
                        "id": "ord-tsla-entry",
                        "environment": "live",
                        "symbol": "TSLA",
                        "status": "Filled",
                        "signal_id": "sig-tsla",
                        "trade_group_id": "grp-tsla",
                        "entry_order_unique_id": "entry-tsla",
                        "unique_id": "entry-tsla",
                        "order_id": "1001",
                        "broker_order_id": "1001",
                        "role": "entry",
                        "relation_status": "closed",
                        "direction": "long",
                        "position_side": "long",
                        "quantity": 10,
                        "filled_qty": 10,
                        "fill_price": 250.0,
                        "updated": "2026-05-14 14:55:16",
                    },
                    {
                        "id": "ord-tsla-close",
                        "environment": "live",
                        "symbol": "TSLA",
                        "status": "Filled",
                        "signal_id": "sig-tsla",
                        "trade_group_id": "grp-tsla",
                        "entry_order_unique_id": "entry-tsla",
                        "unique_id": "close-tsla",
                        "order_id": "1002",
                        "broker_order_id": "1002",
                        "role": "close",
                        "relation_status": "closed",
                        "direction": "long",
                        "position_side": "long",
                        "quantity": 10,
                        "filled_qty": 10,
                        "fill_price": 260.0,
                        "updated": "2026-05-14 15:02:11",
                    },
                ],
                "ibkr_execution_fills": [
                    {"environment": "live", "order_id": "1001", "commission": 0.35, "commission_known": True, "currency": "USD"},
                    {"environment": "live", "order_id": "1002", "commission": 0.45, "commission_known": True, "currency": "USD"},
                ],
                "ibkr_signals": [
                    {
                        "signal_id": "sig-tsla",
                        "environment": "live",
                        "symbol": "TSLA",
                        "status": "executed",
                        "updated": "2026-05-14 15:02:12",
                    }
                ],
            }
        )
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [{"symbol": "TSLA", "quantity": 0, "realized_pnl": 94.86, "currency": "USD"}],
            "orders": [],
            "live_open_orders": [],
            "counts": {},
        }

        enriched = enrich_account_snapshot(pb, payload, "live")

        relation = enriched["positions"][0]["relation"]
        self.assertEqual("flat_broker_position", relation["status"])
        self.assertEqual("sig-tsla", relation["signal_id"])
        self.assertEqual("grp-tsla", relation["trade_group_id"])
        self.assertEqual("entry-tsla", relation["entry_order_unique_id"])
        self.assertEqual("Filled", relation["last_order_status"])
        self.assertEqual("2026-05-14 15:02:11", relation["order_updated"])
        self.assertEqual(2, relation["order_count"])
        self.assertEqual(10.0, relation["entry_filled_qty"])
        self.assertEqual(10.0, relation["exit_filled_qty"])
        self.assertAlmostEqual(0.8, relation["commission"])
        self.assertEqual("USD", relation["commission_currency"])
        self.assertTrue(relation["commission_known"])
        self.assertEqual(2, relation["commission_fill_count"])

    def test_live_order_group_orders_are_sorted_and_include_trade_direction(self):
        pb = _FakePB(
            {
                "orders": [
                    {
                        "id": "ord-tp",
                        "environment": "live",
                        "symbol": "AAPL",
                        "status": "Submitted",
                        "signal_id": "sig-short",
                        "trade_group_id": "grp-short",
                        "entry_order_unique_id": "entry-short",
                        "unique_id": "tp-short",
                        "role": "take_profit",
                        "direction": "short",
                        "position_side": "short",
                        "quantity": 10,
                        "limit_price": 170,
                    },
                    {
                        "id": "ord-entry",
                        "environment": "live",
                        "symbol": "AAPL",
                        "status": "Submitted",
                        "signal_id": "sig-short",
                        "trade_group_id": "grp-short",
                        "entry_order_unique_id": "entry-short",
                        "unique_id": "entry-short",
                        "role": "entry",
                        "direction": "short",
                        "position_side": "short",
                        "quantity": 10,
                        "limit_price": 180,
                    },
                    {
                        "id": "ord-sl",
                        "environment": "live",
                        "symbol": "AAPL",
                        "status": "Submitted",
                        "signal_id": "sig-short",
                        "trade_group_id": "grp-short",
                        "entry_order_unique_id": "entry-short",
                        "unique_id": "sl-short",
                        "role": "stop_loss",
                        "direction": "short",
                        "position_side": "short",
                        "quantity": 10,
                        "limit_price": 182,
                    },
                ],
                "ibkr_signals": [],
            }
        )
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [],
            "orders": [],
            "live_open_orders": [
                {"order_id": "1002", "client_order_id": "tp-short", "symbol": "AAPL", "side": "BUY", "status": "Submitted", "total_quantity": 10, "remaining_quantity": 10},
                {"order_id": "1003", "client_order_id": "sl-short", "symbol": "AAPL", "side": "BUY", "status": "Submitted", "total_quantity": 10, "remaining_quantity": 10},
                {"order_id": "1001", "client_order_id": "entry-short", "symbol": "AAPL", "side": "SELL", "status": "Submitted", "total_quantity": 10, "remaining_quantity": 10},
            ],
            "counts": {},
        }

        enriched = enrich_account_snapshot(pb, payload, "live")

        group = enriched["live_order_groups"][0]
        self.assertEqual("short", group["trade_direction"])
        self.assertEqual(["entry", "take_profit", "stop_loss"], [order["leg_role"] for order in group["orders"]])
        self.assertEqual(["SELL", "BUY", "BUY"], [order["side"] for order in group["orders"]])

    def test_live_exit_only_long_chain_does_not_render_as_short(self):
        pb = _FakePB(
            {
                "orders": [
                    {
                        "id": "ord-tp",
                        "environment": "live",
                        "symbol": "NVDA",
                        "status": "Submitted",
                        "signal_id": "NVDA_20260514_0940_vwappb_L",
                        "trade_group_id": "NVDA_long_20260514_094637",
                        "entry_order_unique_id": "entry_NVDA_long_20260514_094637",
                        "unique_id": "tp_NVDA_long_20260514_094637",
                        "role": "take_profit",
                        "direction": "short",
                        "position_side": "short",
                        "quantity": 43,
                        "limit_price": 244.96,
                    },
                    {
                        "id": "ord-sl",
                        "environment": "live",
                        "symbol": "NVDA",
                        "status": "Submitted",
                        "signal_id": "NVDA_20260514_0940_vwappb_L",
                        "trade_group_id": "NVDA_long_20260514_094637",
                        "entry_order_unique_id": "entry_NVDA_long_20260514_094637",
                        "unique_id": "sl_NVDA_long_20260514_094637",
                        "role": "stop_loss",
                        "direction": "short",
                        "position_side": "short",
                        "quantity": 43,
                        "limit_price": 229.72,
                    },
                ],
                "ibkr_signals": [],
            }
        )
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [],
            "orders": [],
            "live_open_orders": [
                {
                    "order_id": "35",
                    "parent_id": "34",
                    "client_order_id": "tp_NVDA_long_20260514_094637",
                    "symbol": "NVDA",
                    "side": "SELL",
                    "order_type": "LMT",
                    "status": "Submitted",
                    "total_quantity": 43,
                    "remaining_quantity": 43,
                },
                {
                    "order_id": "36",
                    "parent_id": "34",
                    "client_order_id": "sl_NVDA_long_20260514_094637",
                    "symbol": "NVDA",
                    "side": "SELL",
                    "order_type": "STP",
                    "status": "Submitted",
                    "total_quantity": 43,
                    "remaining_quantity": 43,
                },
            ],
            "counts": {},
        }

        enriched = enrich_account_snapshot(pb, payload, "live")

        group = enriched["live_order_groups"][0]
        self.assertEqual("long", group["trade_direction"])
        self.assertEqual("long", group["direction"])
        self.assertEqual(["long", "long"], [order["position_side"] for order in group["orders"]])
        self.assertEqual(["SELL", "SELL"], [order["side"] for order in group["orders"]])

    def test_pb_only_active_orders_are_stale_not_live_open(self):
        pb = _FakePB(
            {
                "orders": [
                    {
                        "id": "ord-stale-1",
                        "environment": "live",
                        "symbol": "MSFT",
                        "status": "Submitted",
                        "signal_id": "sig-stale",
                        "trade_group_id": "grp-stale",
                        "entry_order_unique_id": "entry-stale",
                        "unique_id": "entry-stale",
                        "role": "entry",
                        "relation_status": "active",
                        "direction": "long",
                        "position_side": "long",
                        "quantity": 50,
                        "filled_qty": 0,
                        "limit_price": 420,
                        "updated": "2026-04-23 09:50:00",
                    }
                ],
                "ibkr_signals": [],
            }
        )
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [],
            "orders": [],
            "live_open_orders": [],
            "live_order_coverage": {"coverage_state": "complete", "bulk_open_count": 0},
            "counts": {"open_orders": 99, "cancelable_orders": 99},
        }

        enriched = enrich_account_snapshot(pb, payload, "live")

        self.assertEqual([], enriched["live_open_orders"])
        self.assertEqual([], enriched["live_order_groups"])
        self.assertEqual(0, enriched["counts"]["open_orders"])
        self.assertEqual(0, enriched["counts"]["cancelable_orders"])
        self.assertEqual(1, len(enriched["stale_pb_order_groups"]))
        self.assertEqual("pb_stale", enriched["stale_pb_order_groups"][0]["authority"])
        self.assertEqual("pb_stale", enriched["stale_pb_order_groups"][0]["orders"][0]["authority"])
        self.assertEqual(enriched["stale_pb_order_groups"], enriched["pb_only_order_groups"])
        self.assertEqual(enriched["stale_pb_order_groups"], enriched["order_reconciliation"]["stale_pb_groups"])
        self.assertEqual(1, enriched["order_reconciliation"]["stale_pb_order_groups"])
        self.assertEqual(1, enriched["counts"]["pb_shadow_groups"])

    def test_pb_authority_rows_in_payload_do_not_count_as_broker_live(self):
        pb = _FakePB({"orders": [], "ibkr_signals": []})
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [],
            "orders": [],
            "live_open_orders": [
                {
                    "order_id": "pb-local-1",
                    "client_order_id": "entry-stale",
                    "symbol": "MSFT",
                    "status": "Submitted",
                    "total_quantity": 50,
                    "remaining_quantity": 50,
                    "can_cancel": True,
                    "authority": "pb_stale",
                    "source": "pb",
                }
            ],
            "counts": {},
        }

        enriched = enrich_account_snapshot(pb, payload, "live")

        self.assertEqual([], enriched["live_open_orders"])
        self.assertEqual(0, enriched["counts"]["open_orders"])
        self.assertEqual(0, enriched["counts"]["cancelable_orders"])
        self.assertEqual(0, enriched["order_reconciliation"]["broker_open_orders"])

    def test_status_only_broker_rows_without_identity_do_not_count_as_live_open(self):
        pb = _FakePB({"orders": [], "ibkr_signals": []})
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [],
            "orders": [],
            "live_open_orders": [
                {
                    "order_id": "12",
                    "status": "ApiPending",
                    "remaining_quantity": 25,
                    "can_cancel": True,
                    "can_modify": True,
                    "recovery_source": "bulk",
                }
            ],
            "counts": {"open_orders": 1},
        }

        enriched = enrich_account_snapshot(pb, payload, "live")

        self.assertEqual([], enriched["live_open_orders"])
        self.assertEqual(0, enriched["counts"]["open_orders"])
        self.assertEqual(0, enriched["order_reconciliation"]["broker_open_orders"])

    def test_identified_external_broker_rows_still_count_as_live_open(self):
        pb = _FakePB({"orders": [], "ibkr_signals": []})
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [],
            "orders": [],
            "live_open_orders": [
                {
                    "order_id": "31",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "order_type": "LMT",
                    "status": "Submitted",
                    "total_quantity": 10,
                    "remaining_quantity": 10,
                }
            ],
            "counts": {},
        }

        enriched = enrich_account_snapshot(pb, payload, "live")

        self.assertEqual(1, len(enriched["live_open_orders"]))
        self.assertEqual(1, enriched["counts"]["open_orders"])
        self.assertEqual(1, enriched["order_reconciliation"]["broker_open_orders"])

    def test_account_snapshot_response_wraps_runtime_upstream(self):
        pb = _FakePB()
        payload, status_code = build_account_snapshot_response(
            pb,
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            request_json_request=lambda method, base_url, path, params=None, json_body=None, timeout=5.0: {
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "environment": "live",
                    "summary": {
                        "account_today_pnl": {
                            "ok": True,
                            "net": 12.5,
                            "currency": "USD",
                            "source": "broker_daily_pnl",
                            "raw_field": "DailyPnL",
                        }
                    },
                    "positions": [{"symbol": "AAPL", "quantity": 100}],
                    "orders": [],
                    "live_open_orders": [],
                    "counts": {},
                },
                "target_url": f"{base_url.rstrip('/')}{path}",
            },
            runtime_base_url="http://127.0.0.1:5101",
        )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("ibkr-api", payload["proxy_source"])
        self.assertEqual("http://127.0.0.1:5101/ibkr/account", payload["proxy_upstream"])
        self.assertEqual(12.5, payload["summary"]["account_today_pnl"]["net"])
        self.assertEqual("broker_daily_pnl", payload["summary"]["account_today_pnl"]["source"])
        diagnostics = payload["diagnostics"]["account_snapshot"]
        self.assertIn("upstream_elapsed_ms", diagnostics)
        self.assertIn("enrichment_elapsed_ms", diagnostics)
        self.assertIn("total_elapsed_ms", diagnostics)

    def test_account_snapshot_monitor_probe_uses_lightweight_runtime_status(self):
        pb = _FakePB()
        calls = []

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            calls.append({"method": method, "base_url": base_url, "path": path, "params": list(params or []), "timeout": timeout})
            return {
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "status": "running",
                    "environment": "paper",
                    "gateway": {"running": True},
                    "session": {"authenticated": True},
                    "websocket": {"ready": True},
                },
                "target_url": f"{base_url.rstrip('/')}{path}",
                "elapsed_ms": 25.0,
                "timeout_s": timeout,
            }

        payload, status_code = build_account_snapshot_response(
            pb,
            payload={"environment": "paper", "monitor_probe": "1", "include_pnl": "0"},
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            request_json_request=request_json_request,
            runtime_base_url="http://127.0.0.1:5101",
            upstream_timeout=4.0,
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["monitor_probe"])
        self.assertTrue(payload["gateway_running"])
        self.assertTrue(payload["session_authenticated"])
        self.assertEqual("/ibkr/status", calls[0]["path"])
        self.assertEqual([("broker_mode", "paper"), ("environment", "paper")], calls[0]["params"])
        self.assertEqual(4.0, calls[0]["timeout"])
        self.assertEqual("http://127.0.0.1:5101/ibkr/status", payload["proxy_upstream"])
        self.assertTrue(payload["diagnostics"]["account_snapshot"]["monitor_probe"])

    def test_enrich_account_snapshot_skips_execution_fill_scan_without_order_ids(self):
        pb = _FakePB({"orders": [], "ibkr_execution_fills": [{"order_id": "old", "commission": 9.99}], "ibkr_signals": []})
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [],
            "orders": [],
            "live_open_orders": [],
            "counts": {},
        }

        enrich_account_snapshot(pb, payload, "live")

        collections = [call["collection"] for call in pb.calls]
        self.assertIn("orders", collections)
        self.assertNotIn("ibkr_execution_fills", collections)

    def test_enrich_account_snapshot_filters_pb_orders_to_open_like_rows(self):
        pb = _FakePB({"orders": [], "ibkr_signals": []})
        payload = {
            "ok": True,
            "environment": "live",
            "positions": [],
            "orders": [],
            "live_open_orders": [],
            "counts": {},
        }

        enrich_account_snapshot(pb, payload, "live")

        order_call = next(call for call in pb.calls if call["collection"] == "orders")
        self.assertIn('environment = "live"', order_call["filter"])
        self.assertIn('relation_status = "active"', order_call["filter"])
        self.assertIn('status = "Submitted"', order_call["filter"])


if __name__ == "__main__":
    unittest.main()
