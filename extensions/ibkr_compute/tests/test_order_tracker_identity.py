import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.order.order_tracker import OrderTracker


class FakePBClient:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.upserts = []
        self.get_records_calls = []

    def get_records(self, collection, filter=None, sort=None, per_page=100, page=1):
        self.get_records_calls.append(
            {
                "collection": collection,
                "filter": filter or "",
                "sort": sort or "",
                "per_page": per_page,
                "page": page,
            }
        )
        if 'unique_id = "entry_XOM_long_20260417_111312"' in (filter or ""):
            return [
                {
                    "id": "xom-entry",
                    "unique_id": "entry_XOM_long_20260417_111312",
                    "signal_id": "XOM_20260417_1100_mr_L",
                    "trade_group_id": "entry_XOM_long_20260417_111312",
                    "entry_order_unique_id": "entry_XOM_long_20260417_111312",
                    "parent_order_unique_id": "",
                    "role": "entry",
                }
            ]
        if self.rows:
            text = filter or ""
            result = []
            for row in self.rows:
                if 'symbol = "' in text:
                    symbol = text.split('symbol = "', 1)[1].split('"', 1)[0]
                    if row.get("symbol") != symbol:
                        continue
                if 'environment = "' in text:
                    environment = text.split('environment = "', 1)[1].split('"', 1)[0]
                    if row.get("environment") != environment:
                        continue
                if 'role = "' in text:
                    role = text.split('role = "', 1)[1].split('"', 1)[0]
                    if row.get("role") != role:
                        continue
                if 'unique_id = "' in text:
                    unique_id = text.split('unique_id = "', 1)[1].split('"', 1)[0]
                    if row.get("unique_id") != unique_id:
                        continue
                if 'broker_order_id = "' in text:
                    broker_order_id = text.split('broker_order_id = "', 1)[1].split('"', 1)[0]
                    if str(row.get("broker_order_id") or row.get("order_id") or "") != broker_order_id:
                        continue
                result.append(dict(row))
            return result[:per_page]
        if '(broker_order_id = "1" || order_id = "1")' in (filter or ""):
            return [
                {
                    "id": "dell-entry",
                    "unique_id": "entry_DELL_long_20260417_095445",
                    "signal_id": "DELL_20260417_0945_mr_L",
                    "trade_group_id": "entry_DELL_long_20260417_095445",
                    "entry_order_unique_id": "entry_DELL_long_20260417_095445",
                    "parent_order_unique_id": "",
                    "role": "entry",
                    "symbol": "DELL",
                },
                {
                    "id": "xom-entry",
                    "unique_id": "entry_XOM_long_20260417_111312",
                    "signal_id": "XOM_20260417_1100_mr_L",
                    "trade_group_id": "entry_XOM_long_20260417_111312",
                    "entry_order_unique_id": "entry_XOM_long_20260417_111312",
                    "parent_order_unique_id": "",
                    "role": "entry",
                    "symbol": "XOM",
                },
            ]
        return []

    def upsert_order(self, data):
        self.upserts.append(dict(data))
        return {"success": True}


class FakeBroker:
    def __init__(self, *, all_open_orders=None):
        self.all_open_orders = list(all_open_orders or [])
        self.list_open_orders_calls = []

    def list_open_orders(self, include_all=False):
        self.list_open_orders_calls.append(bool(include_all))
        if include_all:
            return list(self.all_open_orders)
        return []

    def get_order_snapshot(self, order_id):
        return {}

    def list_recent_fills(self):
        return []


class OrderTrackerIdentityTest(unittest.TestCase):
    def test_sync_prefers_coid_match_over_duplicate_broker_order_id(self):
        pb_client = FakePBClient()
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker._sync_to_pb(
            {
                "orderId": "1",
                "ticker": "XOM",
                "side": "BUY",
                "orderType": "LMT",
                "totalSize": 71,
                "filledQuantity": 0,
                "avgPrice": 0,
                "price": 141.94,
                "status": "ApiPending",
                "cOID": "entry_XOM_long_20260417_111312",
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        upsert = pb_client.upserts[0]
        self.assertEqual("entry_XOM_long_20260417_111312", upsert["unique_id"])
        self.assertEqual("XOM_20260417_1100_mr_L", upsert["signal_id"])
        self.assertEqual("entry_XOM_long_20260417_111312", upsert["trade_group_id"])
        self.assertEqual("Submitted", upsert["status"])

    def test_sync_stop_order_uses_aux_price_for_pb_stop_price(self):
        pb_client = FakePBClient()
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker._sync_to_pb(
            {
                "orderId": "27",
                "parentId": "25",
                "ticker": "SCCO",
                "side": "BUY",
                "orderType": "STP",
                "totalSize": 53,
                "filledQuantity": 53,
                "avgPrice": 192.805,
                "price": 0,
                "auxPrice": 192.93,
                "status": "Filled",
                "cOID": "sl_SCCO_short_20260513_112139",
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        upsert = pb_client.upserts[0]
        self.assertEqual("stop_loss", upsert["role"])
        self.assertEqual(192.93, upsert["limit_price"])
        self.assertEqual(192.93, upsert["sl_price"])

    def test_sync_exit_order_keeps_position_side_from_chain_identity(self):
        pb_client = FakePBClient()
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker._sync_to_pb(
            {
                "orderId": "35",
                "parentId": "34",
                "ticker": "NVDA",
                "side": "SELL",
                "orderType": "LMT",
                "totalSize": 43,
                "filledQuantity": 0,
                "avgPrice": 0,
                "price": 244.96,
                "status": "Submitted",
                "cOID": "tp_NVDA_long_20260514_094637",
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        upsert = pb_client.upserts[0]
        self.assertEqual("take_profit", upsert["role"])
        self.assertEqual("long", upsert["direction"])
        self.assertEqual("long", upsert["position_side"])

    def test_sync_external_market_close_links_to_matching_pb_entry(self):
        pb_client = FakePBClient(
            rows=[
                {
                    "id": "nvda-entry",
                    "unique_id": "entry_NVDA_long_20260514_094637",
                    "signal_id": "NVDA_20260514_0940_vwappb_L",
                    "trade_group_id": "NVDA_long_20260514_094637",
                    "entry_order_unique_id": "entry_NVDA_long_20260514_094637",
                    "role": "entry",
                    "status": "Filled",
                    "symbol": "NVDA",
                    "position_side": "long",
                    "quantity": 43,
                    "filled_qty": 43,
                    "environment": "live",
                }
            ]
        )
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker._sync_to_pb(
            {
                "orderId": "40",
                "ticker": "NVDA",
                "side": "SELL",
                "orderType": "MKT",
                "totalSize": 43,
                "filledQuantity": 43,
                "avgPrice": 234.8712,
                "price": 234.86,
                "status": "Filled",
                "cOID": "close_NVDA_20260514_105722",
                "commission": 0.43,
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        upsert = pb_client.upserts[0]
        self.assertEqual("close", upsert["role"])
        self.assertEqual("NVDA_long_20260514_094637", upsert["trade_group_id"])
        self.assertEqual("entry_NVDA_long_20260514_094637", upsert["entry_order_unique_id"])
        self.assertEqual("NVDA_20260514_0940_vwappb_L", upsert["signal_id"])
        self.assertEqual("long", upsert["position_side"])
        self.assertEqual(0.43, upsert["commission"])

    def test_realtime_callback_metadata_syncs_to_pb(self):
        pb_client = FakePBClient()
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker.on_order_update(
            {
                "orderId": "101",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "totalSize": 10,
                "filledQuantity": 0,
                "avgPrice": 0,
                "price": 180.1,
                "status": "Submitted",
                "cOID": "entry_AAPL_long_20260422_093500",
                "broker_realtime_callback": True,
                "ib_callback_type": "openOrder",
                "broker_callback_received_at_ms": 1713797700000,
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        extra = pb_client.upserts[0]["extra"]
        self.assertTrue(extra["broker_realtime_callback"])
        self.assertEqual("ws", extra["broker_update_source"])
        self.assertEqual("openOrder", extra["ib_callback_type"])
        self.assertEqual(1713797700000, extra["broker_callback_received_at_ms"])

    def test_poll_source_never_marks_realtime_callback(self):
        pb_client = FakePBClient()
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker._handle_live_order_payload(
            {
                "orderId": "102",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "totalSize": 10,
                "filledQuantity": 0,
                "avgPrice": 0,
                "price": 180.1,
                "status": "Submitted",
                "cOID": "entry_AAPL_long_20260422_093500",
                "broker_realtime_callback": True,
                "ib_callback_type": "openOrder",
            },
            source="poll",
        )

        self.assertEqual(1, len(pb_client.upserts))
        extra = pb_client.upserts[0]["extra"]
        self.assertFalse(extra["broker_realtime_callback"])
        self.assertEqual("poll", extra["broker_update_source"])

    def test_complete_live_open_orders_restores_tracker_identity_fields(self):
        tracker = OrderTracker(pb_client=FakePBClient(), broker=FakeBroker(), environment="live")
        tracker.register_submitted_orders(
            ["1", "2", "3"],
            seed={
                "symbol": "XOM",
                "direction": "long",
                "entry_unique_id": "entry_XOM_long_20260417_111312",
                "tp_unique_id": "tp_XOM_long_20260417_111312",
                "sl_unique_id": "sl_XOM_long_20260417_111312",
                "quantity": 71,
                "entry_price": 141.94,
                "tp_price": 144.89,
                "sl_price": 139.97,
                "entry_order_type": "LMT",
            },
        )

        payload = tracker.get_complete_live_open_orders(
            bulk_orders=[
                {
                    "orderId": "1",
                    "status": "ApiPending",
                    "remainingQuantity": 71,
                }
            ]
        )

        self.assertEqual(1, len(payload["orders"]))
        order = payload["orders"][0]
        self.assertEqual("1", order["orderId"])
        self.assertEqual("XOM", order["ticker"])
        self.assertEqual("entry_XOM_long_20260417_111312", order["cOID"])
        self.assertEqual(71, order["totalSize"])
        self.assertEqual("bulk", order["_recovery_source"])

    def test_complete_live_open_orders_falls_back_to_all_open_orders(self):
        broker = FakeBroker(
            all_open_orders=[
                {
                    "orderId": "1",
                    "status": "Submitted",
                    "remainingQuantity": 71,
                    "account": "U13281777",
                }
            ]
        )
        tracker = OrderTracker(pb_client=FakePBClient(), broker=broker, environment="live")
        tracker.register_submitted_orders(
            ["1"],
            seed={
                "symbol": "XOM",
                "direction": "long",
                "entry_unique_id": "entry_XOM_long_20260417_111312",
                "quantity": 71,
                "entry_price": 141.94,
                "entry_order_type": "LMT",
            },
        )

        payload = tracker.get_complete_live_open_orders(pb_seed_ids=["1"], retries=1)

        self.assertEqual([False, True], broker.list_open_orders_calls)
        self.assertEqual(1, len(payload["orders"]))
        order = payload["orders"][0]
        self.assertEqual("1", order["orderId"])
        self.assertEqual("U13281777", order["account"])
        self.assertEqual("entry_XOM_long_20260417_111312", order["cOID"])

    def test_complete_live_open_orders_skips_tracker_only_status_patch_without_identity(self):
        tracker = OrderTracker(pb_client=FakePBClient(), broker=FakeBroker(), environment="live")
        tracker._known_orders["12"] = {
            "orderId": "12",
            "status": "ApiPending",
        }

        payload = tracker.get_complete_live_open_orders(
            bulk_orders=[
                {
                    "orderId": "12",
                    "status": "ApiPending",
                    "remainingQuantity": 25,
                }
            ]
        )

        self.assertEqual([], payload["orders"])
        self.assertEqual(["12"], payload["diagnostics"]["skipped_unidentified_order_ids"])

    def test_complete_live_open_orders_keeps_identified_external_order(self):
        tracker = OrderTracker(pb_client=FakePBClient(), broker=FakeBroker(), environment="live")

        payload = tracker.get_complete_live_open_orders(
            bulk_orders=[
                {
                    "orderId": "31",
                    "status": "Submitted",
                    "ticker": "AAPL",
                    "side": "BUY",
                    "orderType": "LMT",
                    "totalSize": 10,
                    "remainingQuantity": 10,
                }
            ]
        )

        self.assertEqual(1, len(payload["orders"]))
        self.assertEqual("31", payload["orders"][0]["orderId"])
        self.assertEqual([], payload["diagnostics"]["skipped_unidentified_order_ids"])

    def test_complete_live_open_orders_keeps_pb_seed_even_when_status_only(self):
        tracker = OrderTracker(pb_client=FakePBClient(), broker=FakeBroker(), environment="live")

        payload = tracker.get_complete_live_open_orders(
            pb_seed_ids=["41"],
            bulk_orders=[
                {
                    "orderId": "41",
                    "status": "ApiPending",
                    "remainingQuantity": 5,
                }
            ]
        )

        self.assertEqual(1, len(payload["orders"]))
        self.assertEqual("41", payload["orders"][0]["orderId"])
        self.assertEqual(["pb"], payload["orders"][0]["_seed_sources"])


if __name__ == "__main__":
    unittest.main()
