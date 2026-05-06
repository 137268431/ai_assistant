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
        return list(self.rows.get(collection, []))


class AccountSnapshotRoutesTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
