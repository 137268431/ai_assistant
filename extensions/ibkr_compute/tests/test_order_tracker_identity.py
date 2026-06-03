import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.order.order_tracker import OrderTracker


class FakePBClient:
    def __init__(self, rows=None, signal_rows=None):
        self.rows = list(rows or [])
        self.signal_rows = list(signal_rows or [])
        self.upserts = []
        self.execution_fill_upserts = []
        self.updates = []
        self.events = []
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
        if collection == "ibkr_signals":
            text = filter or ""
            result = []
            for row in self.signal_rows:
                if 'signal_id = "' in text:
                    signal_id = text.split('signal_id = "', 1)[1].split('"', 1)[0]
                    if row.get("signal_id") != signal_id:
                        continue
                if 'environment = "' in text:
                    environment = text.split('environment = "', 1)[1].split('"', 1)[0]
                    if row.get("environment") != environment:
                        continue
                result.append(dict(row))
            return result[:per_page]
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

    def upsert_execution_fills(self, items):
        self.execution_fill_upserts.extend(dict(item) for item in (items or []))
        return {"ok": True, "total": len(items or [])}

    def get_first_record(self, collection, filter=None, sort=None):
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1)
        return rows[0] if rows else None

    def update_record(self, collection, record_id, data):
        self.updates.append((collection, record_id, dict(data)))
        if collection == "ibkr_signals":
            for idx, row in enumerate(self.signal_rows):
                if row.get("id") == record_id:
                    updated = {**row, **data}
                    if "extra" in data:
                        updated["extra"] = dict(data["extra"] or {})
                    self.signal_rows[idx] = updated
                    return dict(updated)
        return {**dict(data), "id": record_id}

    def notify_system_event(self, title, detail=None, **kwargs):
        self.events.append({"title": title, "detail": dict(detail or {}), **kwargs})
        return {"ok": True}


class FakeBroker:
    def __init__(self, *, all_open_orders=None):
        self.all_open_orders = list(all_open_orders or [])
        self.list_open_orders_calls = []
        self.execution_fill_listeners = []

    def list_open_orders(self, include_all=False):
        self.list_open_orders_calls.append(bool(include_all))
        if include_all:
            return list(self.all_open_orders)
        return []

    def get_order_snapshot(self, order_id):
        return {}

    def list_recent_fills(self):
        return []

    def add_execution_fill_listener(self, callback):
        self.execution_fill_listeners.append(callback)

    def remove_execution_fill_listener(self, callback):
        self.execution_fill_listeners = [item for item in self.execution_fill_listeners if item != callback]


class OrderTrackerIdentityTest(unittest.TestCase):
    def test_execution_fill_callback_persists_verified_ibkr_fill(self):
        pb_client = FakePBClient()
        broker = FakeBroker()
        tracker = OrderTracker(pb_client=pb_client, broker=broker, environment="paper")

        self.assertEqual([tracker.on_execution_fill_update], broker.execution_fill_listeners)

        tracker.on_execution_fill_update(
            {
                "exec_id": "0000e1.123",
                "order_id": "117",
                "symbol": "OKTA",
                "side": "BOT",
                "shares": 38,
                "price": 104.21,
                "commission": 0.5,
                "commission_known": True,
                "commission_currency": "USD",
                "realized_pnl": 0,
                "realized_pnl_known": True,
                "time": "20260602  10:01:00",
                "account": "DU123",
            }
        )

        self.assertEqual(1, len(pb_client.execution_fill_upserts))
        fill = pb_client.execution_fill_upserts[0]
        self.assertEqual("0000e1.123", fill["exec_id"])
        self.assertEqual("117", fill["order_id"])
        self.assertEqual("OKTA", fill["symbol"])
        self.assertEqual("buy", fill["side"])
        self.assertEqual("paper", fill["environment"])
        self.assertTrue(fill["commission_known"])

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

    def test_sync_filled_order_uses_broker_execution_time(self):
        pb_client = FakePBClient()
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="paper")

        tracker._sync_to_pb(
            {
                "orderId": "95",
                "ticker": "ZTS",
                "side": "SELL",
                "orderType": "LMT",
                "totalSize": 64,
                "filledQuantity": 64,
                "avgPrice": 77.21,
                "price": 77.21,
                "status": "Filled",
                "cOID": "entry_ZTS_short_20260601_111046_harvest",
                "lastExecutionTime": "20260601  11:11:02",
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        upsert = pb_client.upserts[0]
        self.assertEqual("Filled", upsert["status"])
        self.assertEqual("2026-06-01 11:11:02", upsert["us_time"])
        self.assertEqual("2026-06-01 23:11:02", upsert["cn_time"])
        self.assertEqual("2026-06-01 11:11:02", upsert["fill_time"])
        self.assertEqual("2026-06-01 11:11:02", upsert["fill_us_time"])
        self.assertEqual("2026-06-01 23:11:02", upsert["fill_cn_time"])
        self.assertEqual("20260601  11:11:02", upsert["extra"]["last_execution_time"])

    def test_existing_self_linked_close_relinks_to_matching_entry(self):
        pb_client = FakePBClient(
            rows=[
                {
                    "id": "order-close",
                    "unique_id": "close_DELL_20260602_155536",
                    "symbol": "DELL",
                    "environment": "paper",
                    "role": "close",
                    "status": "Submitted",
                    "broker_order_id": "166",
                    "trade_group_id": "close_DELL_20260602_155536",
                    "entry_order_unique_id": "close_DELL_20260602_155536",
                    "parent_order_unique_id": "",
                    "signal_id": "",
                },
                {
                    "id": "order-entry",
                    "unique_id": "entry_DELL_short_20260602_101500",
                    "symbol": "DELL",
                    "environment": "paper",
                    "role": "entry",
                    "status": "Filled",
                    "broker_order_id": "165",
                    "trade_group_id": "DELL_short_20260602_101500",
                    "entry_order_unique_id": "entry_DELL_short_20260602_101500",
                    "signal_id": "DELL_20260602_1000_mr_U",
                    "direction": "short",
                    "quantity": 11,
                    "filled_qty": 11,
                },
            ]
        )
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="paper")

        tracker._sync_to_pb(
            {
                "orderId": "166",
                "ticker": "DELL",
                "side": "BUY",
                "orderType": "MKT",
                "totalSize": 11,
                "filledQuantity": 11,
                "avgPrice": 435.66,
                "price": 435.66,
                "status": "Filled",
                "cOID": "close_DELL_20260602_155536",
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        upsert = pb_client.upserts[0]
        self.assertEqual("close", upsert["role"])
        self.assertEqual("DELL_short_20260602_101500", upsert["trade_group_id"])
        self.assertEqual("entry_DELL_short_20260602_101500", upsert["entry_order_unique_id"])
        self.assertEqual("entry_DELL_short_20260602_101500", upsert["parent_order_unique_id"])
        self.assertEqual("DELL_20260602_1000_mr_U", upsert["signal_id"])
        self.assertEqual("DELL_short_20260602_101500", upsert["extra"]["linked_trade_group_id"])

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

    def test_sync_order_id_only_close_callback_preserves_existing_close_identity(self):
        pb_client = FakePBClient(
            rows=[
                {
                    "id": "ba-close",
                    "unique_id": "close_BA_20260529_103000",
                    "order_id": "157",
                    "broker_order_id": "157",
                    "order_type": "MKT",
                    "symbol": "BA",
                    "direction": "long",
                    "position_side": "long",
                    "trade_group_id": "BA_long_20260529_101112",
                    "signal_id": "BA_20260529_1010_mr_L",
                    "entry_order_unique_id": "entry_BA_long_20260529_101112",
                    "parent_order_unique_id": "entry_BA_long_20260529_101112",
                    "role": "close",
                    "status": "Submitted",
                    "quantity": 8,
                    "environment": "live",
                }
            ]
        )
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker._sync_to_pb(
            {
                "orderId": "157",
                "status": "Filled",
                "totalSize": 0,
                "filledQuantity": 8,
                "avgPrice": 183.42,
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        upsert = pb_client.upserts[0]
        self.assertEqual("close_BA_20260529_103000", upsert["unique_id"])
        self.assertEqual("close", upsert["role"])
        self.assertEqual("BA_long_20260529_101112", upsert["trade_group_id"])
        self.assertEqual("BA_20260529_1010_mr_L", upsert["signal_id"])
        self.assertEqual("entry_BA_long_20260529_101112", upsert["entry_order_unique_id"])
        self.assertEqual("entry_BA_long_20260529_101112", upsert["parent_order_unique_id"])
        self.assertEqual("MKT", upsert["order_type"])
        self.assertEqual("BA", upsert["symbol"])
        self.assertEqual(8, upsert["quantity"])
        self.assertEqual(8, upsert["filled_qty"])

    def test_sync_order_id_only_callback_does_not_bias_to_entry_when_identity_missing(self):
        pb_client = FakePBClient(
            rows=[
                {
                    "id": "old-entry",
                    "unique_id": "entry_OTHER_long_20260529_093000",
                    "order_id": "157",
                    "broker_order_id": "157",
                    "order_type": "LMT",
                    "symbol": "OTHER",
                    "trade_group_id": "OTHER_long_20260529_093000",
                    "entry_order_unique_id": "entry_OTHER_long_20260529_093000",
                    "role": "entry",
                    "status": "Filled",
                    "relation_status": "closed",
                    "quantity": 1,
                    "environment": "live",
                },
                {
                    "id": "ba-close",
                    "unique_id": "close_BA_20260529_103000",
                    "order_id": "157",
                    "broker_order_id": "157",
                    "order_type": "MKT",
                    "symbol": "BA",
                    "direction": "long",
                    "position_side": "long",
                    "trade_group_id": "BA_long_20260529_101112",
                    "signal_id": "BA_20260529_1010_mr_L",
                    "entry_order_unique_id": "entry_BA_long_20260529_101112",
                    "parent_order_unique_id": "entry_BA_long_20260529_101112",
                    "role": "close",
                    "status": "Submitted",
                    "relation_status": "active",
                    "quantity": 8,
                    "environment": "live",
                },
            ]
        )
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker._sync_to_pb(
            {
                "orderId": "157",
                "status": "Filled",
                "totalSize": 0,
                "filledQuantity": 8,
                "avgPrice": 183.42,
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        upsert = pb_client.upserts[0]
        self.assertEqual("close_BA_20260529_103000", upsert["unique_id"])
        self.assertEqual("close", upsert["role"])
        self.assertEqual("BA_long_20260529_101112", upsert["trade_group_id"])
        self.assertEqual("MKT", upsert["order_type"])

    def test_sync_mkt_order_id_only_callback_prefers_active_close_candidate(self):
        pb_client = FakePBClient(
            rows=[
                {
                    "id": "ambiguous-entry",
                    "unique_id": "entry_BA_long_20260529_101112",
                    "order_id": "157",
                    "broker_order_id": "157",
                    "order_type": "LMT",
                    "symbol": "BA",
                    "trade_group_id": "BA_long_20260529_101112",
                    "entry_order_unique_id": "entry_BA_long_20260529_101112",
                    "role": "entry",
                    "status": "Submitted",
                    "relation_status": "active",
                    "quantity": 8,
                    "environment": "live",
                },
                {
                    "id": "ba-close",
                    "unique_id": "close_BA_20260529_103000",
                    "order_id": "157",
                    "broker_order_id": "157",
                    "order_type": "MKT",
                    "symbol": "BA",
                    "direction": "long",
                    "position_side": "long",
                    "trade_group_id": "BA_long_20260529_101112",
                    "signal_id": "BA_20260529_1010_mr_L",
                    "entry_order_unique_id": "entry_BA_long_20260529_101112",
                    "parent_order_unique_id": "entry_BA_long_20260529_101112",
                    "role": "close",
                    "status": "Submitted",
                    "relation_status": "active",
                    "quantity": 8,
                    "environment": "live",
                },
            ]
        )
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker._sync_to_pb(
            {
                "orderId": "157",
                "orderType": "MKT",
                "status": "Filled",
                "totalSize": 0,
                "filledQuantity": 8,
                "avgPrice": 183.42,
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        upsert = pb_client.upserts[0]
        self.assertEqual("close_BA_20260529_103000", upsert["unique_id"])
        self.assertEqual("close", upsert["role"])
        self.assertEqual("MKT", upsert["order_type"])

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

    def test_live_order_merge_keeps_filled_quantity_and_terminal_status_monotonic(self):
        pb_client = FakePBClient()
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker._handle_live_order_payload(
            {
                "orderId": "103",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "totalSize": 10,
                "filledQuantity": 10,
                "remainingQuantity": 0,
                "avgPrice": 180.2,
                "price": 180.1,
                "status": "Filled",
                "cOID": "entry_AAPL_long_20260422_093500",
                "broker_realtime_callback": True,
                "ib_callback_type": "execDetails",
            },
            source="ws",
        )
        tracker._handle_live_order_payload(
            {
                "orderId": "103",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "totalSize": 10,
                "filledQuantity": 0,
                "remainingQuantity": 10,
                "avgPrice": 0,
                "price": 180.1,
                "status": "Submitted",
                "cOID": "entry_AAPL_long_20260422_093500",
                "broker_realtime_callback": True,
                "ib_callback_type": "openOrder",
            },
            source="ws",
        )

        self.assertGreaterEqual(len(pb_client.upserts), 1)
        latest = pb_client.upserts[-1]
        self.assertEqual(10, latest["filled_qty"])
        self.assertEqual(180.2, latest["fill_price"])
        self.assertEqual("Filled", latest["status"])

    def test_partial_harvest_take_profit_quantity_mismatch_marks_signal(self):
        pb_client = FakePBClient(
            rows=[
                {
                    "id": "tp-row",
                    "unique_id": "tp_AAPL_long_20260422_093500_harvest",
                    "order_id": "202",
                    "broker_order_id": "202",
                    "symbol": "AAPL",
                    "environment": "live",
                    "signal_id": "sig-1",
                    "trade_group_id": "AAPL_long_20260422_093500_harvest",
                    "entry_order_unique_id": "entry_AAPL_long_20260422_093500_harvest",
                    "parent_order_unique_id": "entry_AAPL_long_20260422_093500_harvest",
                    "role": "take_profit",
                    "status": "Submitted",
                    "quantity": 3,
                    "extra": {
                        "order_family_type": "partial_harvest_bracket",
                        "partial_harvest_managed": True,
                        "partial_tp_quantity": 3,
                    },
                }
            ],
            signal_rows=[
                {
                    "id": "sig-row",
                    "signal_id": "sig-1",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "submitted",
                    "extra": {},
                }
            ],
        )
        tracker = OrderTracker(pb_client=pb_client, broker=FakeBroker(), environment="live")

        tracker._sync_to_pb(
            {
                "orderId": "202",
                "parentId": "201",
                "ticker": "AAPL",
                "side": "SELL",
                "orderType": "LMT",
                "totalSize": 10,
                "filledQuantity": 0,
                "avgPrice": 0,
                "price": 184.0,
                "status": "Submitted",
                "cOID": "tp_AAPL_long_20260422_093500_harvest",
            }
        )

        self.assertEqual(1, len(pb_client.upserts))
        extra = pb_client.upserts[0]["extra"]
        self.assertTrue(extra["protection_quantity_mismatch"])
        self.assertEqual(3, extra["expected_quantity"])
        self.assertEqual(10, extra["broker_quantity"])
        self.assertEqual(1, len(pb_client.updates))
        self.assertEqual("ibkr_signals", pb_client.updates[0][0])
        signal_patch = pb_client.updates[0][2]
        self.assertEqual("protection_incomplete", signal_patch["status"])
        self.assertEqual("protection_quantity_mismatch", signal_patch["note"])
        self.assertTrue(signal_patch["extra"]["safety_cancel_recommended"])
        self.assertEqual(1, len(pb_client.events))
        self.assertEqual("保护单数量不一致", pb_client.events[0]["title"])

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
