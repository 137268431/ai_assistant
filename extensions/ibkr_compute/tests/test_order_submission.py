import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker import ib_gateway
from ibkr_compute.order.order_placer import OrderPlacer
from ibkr_compute.orchestration.signals import TradingServiceSignalsMixin
from ibkr_compute.signal.signal_router import SignalRouter


class FakeOrder:
    def __init__(self):
        self.eTradeOnly = True
        self.firmQuoteOnly = True


class FakeContract:
    pass


class FakeClient:
    def __init__(self, ack_result=None, submission_result=None, open_orders=None):
        self.ack_result = ack_result or {"ok": True}
        self.submission_result = submission_result
        self.open_orders = list(open_orders or [])
        self.placed_orders = []
        self.cleared_order_errors = []

    def next_order_ids(self, count):
        base = [101, 102, 103]
        return base[:count]

    def next_order_ids_above(self, count, minimum=0):
        start = max(101, int(minimum or 0))
        return list(range(start, start + int(count or 1)))

    def max_seen_order_id(self):
        return max([0, *[int(item.get("orderId", 0) or 0) for item in self.open_orders]])

    def request_open_orders(self, include_all=False):
        return list(self.open_orders)

    def clear_order_error(self, order_id):
        self.cleared_order_errors.append(str(order_id))

    def place_order(self, contract, order):
        self.placed_orders.append((contract, order))

    def await_order_submission(self, order_id, timeout=3.0, poll_interval=0.2):
        return dict(self.ack_result)

    def await_order_submissions(self, order_ids, timeout=5.0, poll_interval=0.2):
        if self.submission_result is not None:
            return dict(self.submission_result)
        return dict(self.ack_result)


class FakePBClient:
    def __init__(self, rows):
        self.rows = list(rows)

    def get_records(self, collection, filter=None, sort=None, per_page=100, page=1):
        return list(self.rows)


class FakeOrderPBClient:
    def __init__(self):
        self.upserts = []

    def upsert_order(self, data):
        self.upserts.append(dict(data))
        return {"success": True}


class FakeSignalPBClient:
    def __init__(self, record):
        self.record = dict(record)
        self.updates = []

    def get_first_record(self, collection, filter=None):
        return dict(self.record)

    def update_record(self, collection, record_id, data):
        self.updates.append((collection, record_id, dict(data)))
        self.record.update(data)
        return dict(self.record)


class FakeBracketBroker:
    def place_bracket_order(self, **kwargs):
        return {
            "ok": True,
            "entry_coid": "entry_NFLX_short_20260506_101500",
            "tp_coid": "tp_NFLX_short_20260506_101500",
            "sl_coid": "sl_NFLX_short_20260506_101500",
            "bracket_group": "NFLX_short_20260506_101500",
            "oca_group": "NFLX_short_20260506_101500",
            "order_family_type": "bracket_oco",
            "order_ids": ["101", "102", "103"],
            "submission": {"ok": True},
            "protection_complete": True,
        }


class FakeConfig:
    def get_for_environment(self, key, environment, default=None):
        return default


class FakeSignalRouter:
    def __init__(self, signals):
        self.signals = list(signals)
        self.processed = []
        self.released = []
        self.claimed = []

    def fetch_pending_signals(self):
        return list(self.signals)

    def claim_signal(self, signal_id):
        self.claimed.append(signal_id)
        return True

    def mark_processed(self, signal_id):
        self.processed.append(signal_id)

    def release_signal(self, signal_id):
        self.released.append(signal_id)


class FakeSignalProcessor:
    def validate_signal(self, signal):
        return True, "ok"


class FakeOrderTracker:
    def find_duplicate_open_entry(self, **kwargs):
        return None


class FakeConidResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, symbol):
        self.calls.append(symbol)
        return 123


class FakeOrderPlacer:
    def __init__(self):
        self.calls = []

    def place_bracket_order(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"ok": True, "order_ids": ["101", "102", "103"], "bracket_group": "AAPL_long"}


class FakeLifecycle:
    def __init__(self, capacity_full=False, fixed_symbols=None):
        self.capacity_full = capacity_full
        self.fixed_symbols = {str(item).upper() for item in (fixed_symbols or [])}

    def is_fixed_position_symbol(self, symbol):
        return str(symbol or "").upper() in self.fixed_symbols

    def _fixed_position_symbols(self):
        return set(self.fixed_symbols)

    def strategy_capacity_snapshot(self, order_tracker=None):
        return {
            "capacity_full": self.capacity_full,
            "strategy_capacity_used": 5 if self.capacity_full else 0,
            "max_strategy_open_positions": 5,
            "strategy_open_positions": 4 if self.capacity_full else 0,
            "open_strategy_entry_orders": 1 if self.capacity_full else 0,
        }

    def increment_position_count(self):
        pass


class FakeSessionKeeper:
    is_authenticated = True


class FakeSignalService(TradingServiceSignalsMixin):
    def __init__(self, signal, *, lifecycle, pb):
        self.session_keeper = FakeSessionKeeper()
        self.signal_router = FakeSignalRouter([signal])
        self.signal_processor = FakeSignalProcessor()
        self.order_tracker = FakeOrderTracker()
        self.conid_resolver = FakeConidResolver()
        self.order_placer = FakeOrderPlacer()
        self.order_lifecycle = lifecycle
        self.pb = pb

    def _now_iso(self):
        return "2026-05-13T10:00:00-04:00"


class BrokerAdapterOrderSubmissionTest(unittest.TestCase):
    def test_place_bracket_order_clears_legacy_flags_and_surfaces_entry_rejection(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            {
                "ok": False,
                "error": "The 'EtradeOnly' order attribute is not supported.",
                "details": {"code": 10268},
            }
        )
        adapter.resolve_contract = lambda **kwargs: {
            "conid": 346218218,
            "symbol": "DELL",
            "sec_type": "STK",
            "exchange": "SMART",
            "currency": "USD",
        }

        original_order = ib_gateway.Order
        original_contract = ib_gateway.Contract
        try:
            ib_gateway.Order = FakeOrder
            ib_gateway.Contract = FakeContract
            result = ib_gateway.BrokerAdapter.place_bracket_order(
                adapter,
                conid=346218218,
                symbol="DELL",
                direction="long",
                quantity=53,
                entry_price=191.23,
                take_profit_price=195.48,
                stop_loss_price=188.4,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertFalse(result["ok"])
        self.assertIn("EtradeOnly", result["error"])
        self.assertEqual(["101", "102", "103"], result["order_ids"])
        self.assertEqual(["101", "102", "103"], adapter.client.cleared_order_errors)
        self.assertEqual(3, len(adapter.client.placed_orders))
        for _, placed_order in adapter.client.placed_orders:
            self.assertFalse(placed_order.eTradeOnly)
            self.assertFalse(placed_order.firmQuoteOnly)

    def test_place_bracket_order_keeps_partial_bracket_protection_incomplete(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            submission_result={
                "ok": False,
                "error": "order_submission_unconfirmed",
                "orders": {"101": {"ok": True}, "102": {"ok": True}},
                "missing_order_ids": ["103"],
            }
        )
        adapter.resolve_contract = lambda **kwargs: {
            "conid": 346218218,
            "symbol": "DELL",
            "sec_type": "STK",
            "exchange": "SMART",
            "currency": "USD",
        }

        original_order = ib_gateway.Order
        original_contract = ib_gateway.Contract
        try:
            ib_gateway.Order = FakeOrder
            ib_gateway.Contract = FakeContract
            result = ib_gateway.BrokerAdapter.place_bracket_order(
                adapter,
                conid=346218218,
                symbol="DELL",
                direction="long",
                quantity=53,
                entry_price=191.23,
                take_profit_price=195.48,
                stop_loss_price=188.4,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        self.assertFalse(result["protection_complete"])
        self.assertTrue(result["protection_incomplete"])
        self.assertEqual(["103"], result["missing_order_ids"])
        self.assertEqual(["stop_loss"], result["missing_protection_roles"])
        self.assertIn("missing=103", result["error"])

    def test_place_bracket_order_uses_order_id_high_water_and_confirms_all_legs(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            submission_result={
                "ok": True,
                "orders": {"206": {"ok": True}, "207": {"ok": True}, "208": {"ok": True}},
                "missing_order_ids": [],
            },
            open_orders=[{"orderId": "205"}],
        )
        adapter.resolve_contract = lambda **kwargs: {
            "conid": 346218218,
            "symbol": "DELL",
            "sec_type": "STK",
            "exchange": "SMART",
            "currency": "USD",
        }

        original_order = ib_gateway.Order
        original_contract = ib_gateway.Contract
        try:
            ib_gateway.Order = FakeOrder
            ib_gateway.Contract = FakeContract
            result = ib_gateway.BrokerAdapter.place_bracket_order(
                adapter,
                conid=346218218,
                symbol="DELL",
                direction="long",
                quantity=53,
                entry_price=191.23,
                take_profit_price=195.48,
                stop_loss_price=188.4,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        self.assertTrue(result["protection_complete"])
        self.assertEqual(["206", "207", "208"], result["order_ids"])

    def test_place_bracket_order_sets_child_oca_metadata(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            submission_result={
                "ok": True,
                "orders": {"101": {"ok": True}, "102": {"ok": True}, "103": {"ok": True}},
                "missing_order_ids": [],
            }
        )
        adapter.resolve_contract = lambda **kwargs: {
            "conid": 123,
            "symbol": "NFLX",
            "sec_type": "STK",
            "exchange": "SMART",
            "currency": "USD",
        }

        original_order = ib_gateway.Order
        original_contract = ib_gateway.Contract
        try:
            ib_gateway.Order = FakeOrder
            ib_gateway.Contract = FakeContract
            result = ib_gateway.BrokerAdapter.place_bracket_order(
                adapter,
                conid=123,
                symbol="NFLX",
                direction="short",
                quantity=7,
                entry_price=600.0,
                take_profit_price=580.0,
                stop_loss_price=610.0,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        self.assertEqual(result["bracket_group"], result["oca_group"])
        self.assertEqual("bracket_oco", result["order_family_type"])
        entry_order = adapter.client.placed_orders[0][1]
        tp_order = adapter.client.placed_orders[1][1]
        sl_order = adapter.client.placed_orders[2][1]
        self.assertEqual(0, getattr(entry_order, "parentId", 0))
        self.assertEqual(101, tp_order.parentId)
        self.assertEqual(101, sl_order.parentId)
        self.assertFalse(entry_order.transmit)
        self.assertFalse(tp_order.transmit)
        self.assertTrue(sl_order.transmit)
        self.assertEqual(result["oca_group"], tp_order.ocaGroup)
        self.assertEqual(result["oca_group"], sl_order.ocaGroup)
        self.assertEqual(1, tp_order.ocaType)
        self.assertEqual(1, sl_order.ocaType)


class OrderPlacerBracketMetadataTest(unittest.TestCase):
    def test_pb_upserts_use_canonical_bracket_trade_group_and_oco_metadata(self):
        pb_client = FakeOrderPBClient()
        placer = OrderPlacer(pb_client=pb_client, broker=FakeBracketBroker(), account_id="DU123")

        result = placer.place_bracket_order(
            conid=123,
            symbol="NFLX",
            direction="short",
            quantity=7,
            entry_price=600.0,
            take_profit_price=580.0,
            stop_loss_price=610.0,
            signal_id="sig-nflx",
        )

        self.assertTrue(result["ok"])
        self.assertEqual("NFLX_short_20260506_101500", result["bracket_group"])
        self.assertEqual("NFLX_short_20260506_101500", result["oca_group"])
        self.assertEqual("bracket_oco", result["order_family_type"])
        self.assertEqual(3, len(pb_client.upserts))
        for upsert in pb_client.upserts:
            self.assertEqual("NFLX_short_20260506_101500", upsert["trade_group_id"])
            self.assertEqual("NFLX_short_20260506_101500", upsert["bracket_group"])
            self.assertEqual("NFLX_short_20260506_101500", upsert["oca_group"])
            self.assertEqual("bracket_oco", upsert["order_family_type"])
            self.assertEqual("NFLX_short_20260506_101500", upsert["extra"]["bracket_group"])
            self.assertEqual("NFLX_short_20260506_101500", upsert["extra"]["oca_group"])
            self.assertEqual("bracket_oco", upsert["extra"]["order_family_type"])
            self.assertEqual("entry_NFLX_short_20260506_101500", upsert["entry_order_unique_id"])
        self.assertEqual("entry_NFLX_short_20260506_101500", pb_client.upserts[0]["unique_id"])
        self.assertEqual("entry_NFLX_short_20260506_101500", pb_client.upserts[1]["parent_order_unique_id"])
        self.assertEqual("entry_NFLX_short_20260506_101500", pb_client.upserts[2]["parent_order_unique_id"])


class LiveSignalCapacityLifecycleTest(unittest.TestCase):
    def _signal(self, symbol="AAPL"):
        return {
            "signal_id": f"sig-{symbol.lower()}",
            "symbol": symbol,
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "shares": 10,
            "signal_time": "2026-05-13 10:00:00",
            "extra": {"source": "ibkr_compute"},
            "raw": {
                "id": f"row-{symbol.lower()}",
                "signal_id": f"sig-{symbol.lower()}",
                "environment": "live",
                "extra": {"source": "ibkr_compute"},
            },
        }

    def test_capacity_full_keeps_signal_pending_and_retriable(self):
        signal = self._signal("AAPL")
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(capacity_full=True), pb=pb)

        service._process_signals()

        self.assertEqual([], service.signal_router.processed)
        self.assertEqual(["sig-aapl"], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        self.assertEqual("pending", pb.updates[-1][2]["status"])
        self.assertEqual("strategy_capacity_full", pb.updates[-1][2]["extra"]["status_reason"])
        self.assertEqual("waiting_for_capacity", pb.updates[-1][2]["extra"]["execution_state"])

    def test_fixed_symbol_is_blocked_and_marked_processed(self):
        signal = self._signal("BOXX")
        pb = FakeSignalPBClient({"id": "row-boxx", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(fixed_symbols={"BOXX", "IBKR"}), pb=pb)

        service._process_signals()

        self.assertEqual(["sig-boxx"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        self.assertEqual("rejected", pb.updates[-1][2]["status"])
        self.assertEqual("fixed_position_symbol_blocked", pb.updates[-1][2]["extra"]["status_reason"])
        self.assertTrue(pb.updates[-1][2]["extra"]["fixed_position_symbol_blocked"])


class SignalRouterDedupeTest(unittest.TestCase):
    def test_fetch_pending_signals_skips_duplicate_inflight_and_processed_ids(self):
        rows = [
            {
                "signal_id": "dup_signal",
                "symbol": "DELL",
                "direction": "long",
                "entry": 191.23,
                "stop_loss": 188.4,
                "take_profit": 195.48,
                "shares": 53,
                "rr": "1:2",
                "us_time": "2026-04-17 09:45:00",
                "extra": {"source": "ibkr_compute"},
            },
            {
                "signal_id": "dup_signal",
                "symbol": "DELL",
                "direction": "long",
                "entry": 191.23,
                "stop_loss": 188.4,
                "take_profit": 195.48,
                "shares": 53,
                "rr": "1:2",
                "us_time": "2026-04-17 09:45:00",
                "extra": {"source": "ibkr_compute"},
            },
            {
                "signal_id": "other_signal",
                "symbol": "AAPL",
                "direction": "short",
                "entry": 200.0,
                "stop_loss": 203.0,
                "take_profit": 194.0,
                "shares": 10,
                "rr": "1:2",
                "us_time": "2026-04-17 09:50:00",
                "extra": {"source": "tradingview"},
            },
        ]
        router = SignalRouter(FakePBClient(rows), FakeConfig(), environment="live")

        first_fetch = router.fetch_pending_signals()
        self.assertEqual(["dup_signal", "other_signal"], [item["signal_id"] for item in first_fetch])

        self.assertTrue(router.claim_signal("dup_signal"))
        self.assertFalse(router.claim_signal("dup_signal"))

        second_fetch = router.fetch_pending_signals()
        self.assertEqual(["other_signal"], [item["signal_id"] for item in second_fetch])

        router.release_signal("dup_signal")
        third_fetch = router.fetch_pending_signals()
        self.assertEqual(["dup_signal", "other_signal"], [item["signal_id"] for item in third_fetch])

        router.mark_processed("dup_signal")
        final_fetch = router.fetch_pending_signals()
        self.assertEqual(["other_signal"], [item["signal_id"] for item in final_fetch])


if __name__ == "__main__":
    unittest.main()
