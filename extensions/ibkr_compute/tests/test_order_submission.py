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
        self.acks = []
        self.events = []

    def get_first_record(self, collection, filter=None):
        return dict(self.record)

    def update_record(self, collection, record_id, data):
        self.updates.append((collection, record_id, dict(data)))
        self.record.update(data)
        return dict(self.record)

    def ack_ibkr_signal(self, **kwargs):
        self.acks.append(dict(kwargs))
        return {"status": kwargs.get("status"), "fallback": False}

    def notify_system_event(self, title, detail=None, **kwargs):
        self.events.append({"title": title, "detail": dict(detail or {}), **kwargs})
        return {"ok": True}


class FakeBracketBroker:
    def __init__(self):
        self.calls = []

    def place_bracket_order(self, **kwargs):
        call_index = len(self.calls)
        self.calls.append(dict(kwargs))
        suffix = str(kwargs.get("order_ref_suffix") or "").strip()
        group = "NFLX_short_20260506_101500" + (f"_{suffix}" if suffix else "")
        base_order_id = 101 + call_index * 10
        family = kwargs.get("order_family_type") or "bracket_oco"
        return {
            "ok": True,
            "entry_coid": f"entry_{group}",
            "tp_coid": f"tp_{group}",
            "sl_coid": f"sl_{group}",
            "bracket_group": group,
            "oca_group": group if family == "bracket_oco" else "",
            "order_family_type": family,
            "quantity": kwargs.get("quantity"),
            "take_profit_quantity": kwargs.get("take_profit_quantity", kwargs.get("quantity")),
            "stop_loss_quantity": kwargs.get("stop_loss_quantity", kwargs.get("quantity")),
            "order_ids": [str(base_order_id), str(base_order_id + 1), str(base_order_id + 2)],
            "submission": {"ok": True},
            "protection_complete": True,
        }


class FakeConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_for_environment(self, key, environment, default=None):
        return self.values.get(key, default)

    def get_bool_for_environment(self, key, environment, default=False):
        value = self.values.get(key, default)
        return str(value).lower() in ("true", "1", "yes") if isinstance(value, str) else bool(value)

    def get_float_for_environment(self, key, environment, default=0.0):
        try:
            return float(self.values.get(key, default))
        except (TypeError, ValueError):
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
    def __init__(self):
        self.pending_entries = []

    def validate_signal(self, signal):
        return True, "ok"

    def register_pending_entry(self, symbol, position_data):
        self.pending_entries.append((symbol, dict(position_data or {})))


class FakeOrderTracker:
    def __init__(self):
        self.duplicate_calls = []
        self.registered = []

    def find_duplicate_open_entry(self, **kwargs):
        self.duplicate_calls.append(dict(kwargs))
        return None

    def register_submitted_orders(self, order_ids, seed=None):
        self.registered.append((list(order_ids or []), dict(seed or {})))


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

    def place_harvest_bracket_order(self, **kwargs):
        self.calls.append(dict(kwargs))
        quantity = int(kwargs.get("quantity") or 0)
        tp_quantity = max(1, int(round(quantity * 0.30))) if quantity >= 2 else quantity
        return {
            "ok": True,
            "harvest_split": False,
            "partial_harvest": True,
            "harvest_profile": "intraday_volatility_harvest_v1",
            "order_ids": ["101", "102", "103"],
            "bracket_group": "AAPL_long_harvest",
            "entry_coid": "entry_AAPL_long_harvest",
            "tp_coid": "tp_AAPL_long_harvest",
            "sl_coid": "sl_AAPL_long_harvest",
            "quantity": quantity,
            "take_profit_quantity": tp_quantity,
            "stop_loss_quantity": quantity,
            "order_family_type": "partial_harvest_bracket",
            "order_extra": {
                "harvest_managed": True,
                "harvest_lot": "primary",
                "harvest_profile": "intraday_volatility_harvest_v1",
                "partial_harvest_managed": True,
                "partial_tp_quantity": tp_quantity,
                "reentry_allowed": True,
            },
            "protection_complete": True,
        }


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


class FakeQuoteBook:
    def __init__(self, quotes):
        self.quotes = dict(quotes or {})
        self.calls = []

    def get_quote(self, symbol):
        normalized_symbol = str(symbol or "").strip().upper()
        self.calls.append(normalized_symbol)
        quote = self.quotes.get(normalized_symbol)
        return dict(quote or {}) if quote else None


class FakeSignalService(TradingServiceSignalsMixin):
    def __init__(self, signal, *, lifecycle, pb, quote_book=None, config=None, account_snapshot=None):
        self.session_keeper = FakeSessionKeeper()
        self.signal_router = FakeSignalRouter([signal])
        self.signal_processor = FakeSignalProcessor()
        self.order_tracker = FakeOrderTracker()
        self.conid_resolver = FakeConidResolver()
        self.order_placer = FakeOrderPlacer()
        self.order_lifecycle = lifecycle
        self.pb = pb
        self.config = config or FakeConfig()
        self.realtime_quote_book = quote_book or FakeQuoteBook({})
        self.account_snapshot_provider = lambda: dict(account_snapshot or {
            "ok": True,
            "summary": {"buying_power": 100000, "net_liquidation": 120000},
        })

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
                account_id="U123456",
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
        self.assertEqual("U123456", entry_order.account)
        self.assertEqual("U123456", tp_order.account)
        self.assertEqual("U123456", sl_order.account)
        self.assertEqual(result["oca_group"], tp_order.ocaGroup)
        self.assertEqual(result["oca_group"], sl_order.ocaGroup)
        self.assertEqual(1, tp_order.ocaType)
        self.assertEqual(1, sl_order.ocaType)

    def test_place_partial_harvest_bracket_uses_single_entry_with_partial_tp_and_no_oca(self):
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
                direction="long",
                quantity=107,
                take_profit_quantity=32,
                stop_loss_quantity=107,
                entry_price=93.76,
                take_profit_price=99.37,
                stop_loss_price=92.36,
                order_family_type="partial_harvest_bracket",
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        self.assertEqual("partial_harvest_bracket", result["order_family_type"])
        self.assertEqual("", result["oca_group"])
        entry_order = adapter.client.placed_orders[0][1]
        tp_order = adapter.client.placed_orders[1][1]
        sl_order = adapter.client.placed_orders[2][1]
        self.assertEqual(107.0, entry_order.totalQuantity)
        self.assertEqual(32.0, tp_order.totalQuantity)
        self.assertEqual(107.0, sl_order.totalQuantity)
        self.assertFalse(hasattr(tp_order, "ocaGroup"))
        self.assertFalse(hasattr(sl_order, "ocaGroup"))

    def test_place_market_close_sets_account_id_on_order(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient({"ok": True})
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
            result = ib_gateway.BrokerAdapter.place_market_close(
                adapter,
                conid=123,
                symbol="NFLX",
                direction="long",
                quantity=7,
                account_id="U123456",
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(adapter.client.placed_orders))
        close_order = adapter.client.placed_orders[0][1]
        self.assertEqual("U123456", close_order.account)
        self.assertEqual("SELL", close_order.action)
        self.assertEqual("MKT", close_order.orderType)


class OrderPlacerBracketMetadataTest(unittest.TestCase):
    def test_pb_upserts_use_canonical_bracket_trade_group_and_oco_metadata(self):
        pb_client = FakeOrderPBClient()
        broker = FakeBracketBroker()
        placer = OrderPlacer(pb_client=pb_client, broker=broker, account_id="DU123")

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
        self.assertEqual("DU123", broker.calls[0]["account_id"])
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

    def test_harvest_bracket_uses_single_entry_with_partial_tp_metadata(self):
        pb_client = FakeOrderPBClient()
        broker = FakeBracketBroker()
        placer = OrderPlacer(pb_client=pb_client, broker=broker, account_id="DU123")

        result = placer.place_harvest_bracket_order(
            conid=123,
            symbol="NFLX",
            direction="short",
            quantity=100,
            entry_price=600.0,
            take_profit_price=580.0,
            stop_loss_price=610.0,
            signal_id="sig-nflx",
            settings={"tactical_fraction": 0.30},
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["harvest_split"])
        self.assertTrue(result["partial_harvest"])
        self.assertEqual(1, len(broker.calls))
        self.assertEqual(100, broker.calls[0]["quantity"])
        self.assertEqual(30, broker.calls[0]["take_profit_quantity"])
        self.assertEqual(100, broker.calls[0]["stop_loss_quantity"])
        self.assertEqual("partial_harvest_bracket", broker.calls[0]["order_family_type"])
        self.assertEqual("", result["oca_group"])
        self.assertEqual(3, len(pb_client.upserts))
        entry, tp, sl = pb_client.upserts
        self.assertEqual(100, entry["quantity"])
        self.assertEqual(30, tp["quantity"])
        self.assertEqual(100, sl["quantity"])
        self.assertEqual("", entry["oca_group"])
        self.assertEqual("", tp["oca_group"])
        self.assertEqual("", sl["oca_group"])
        self.assertEqual("primary", entry["extra"]["harvest_lot"])
        self.assertEqual(30, entry["extra"]["partial_tp_quantity"])
        self.assertTrue(entry["extra"]["partial_harvest_managed"])
        self.assertTrue(entry["extra"]["reentry_allowed"])
        self.assertTrue(all(row["extra"]["harvest_managed"] for row in pb_client.upserts))


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

    def test_entry_guard_corrects_adverse_drift_on_stale_30m_confirmation(self):
        signal = self._signal("AAPL")
        signal.update(
            {
                "entry": 100.0,
                "stop_loss": 98.0,
                "take_profit": 104.0,
                "signal_time": "2026-05-13 09:30:00",
            }
        )
        signal["extra"] = {**signal["extra"], "entry_limit_offset": 0.05}
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        quote_book = FakeQuoteBook(
            {
                "AAPL": {
                    "last_price": 98.9,
                    "bid": 98.85,
                    "ask": 98.95,
                    "quote_age_s": 1.0,
                }
            }
        )
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=pb, quote_book=quote_book)

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual(1, len(service.order_placer.calls))
        order_payload = service.order_placer.calls[0]
        self.assertEqual(98.8, order_payload["entry_price"])
        self.assertEqual(96.8, order_payload["stop_loss_price"])
        self.assertEqual(102.8, order_payload["take_profit_price"])
        self.assertEqual(98.8, service.order_tracker.duplicate_calls[0]["entry_price"])
        ack_order = pb.acks[-1]["order"]
        self.assertEqual(98.8, ack_order["limit_price"])
        self.assertEqual(96.8, ack_order["stop_loss"])
        self.assertEqual(102.8, ack_order["take_profit"])
        self.assertEqual("passive", ack_order["extra"]["entry_limit_intent"])
        self.assertEqual("passive_limit", ack_order["extra"]["entry_price_plan"])
        pre_submit_patch = next(
            patch
            for collection, _record_id, patch in pb.updates
            if collection == "ibkr_signals" and patch.get("extra", {}).get("pre_submit_prices_patched")
        )
        extra = pre_submit_patch["extra"]
        self.assertEqual(100.0, extra["original_entry"])
        self.assertEqual(98.0, extra["original_stop_loss"])
        self.assertEqual(104.0, extra["original_take_profit"])
        self.assertEqual(1.0, extra["quote_age_s"])
        self.assertEqual(98.9, extra["pre_submit_reference_price"])
        self.assertAlmostEqual(0.55, extra["price_drift_r"])
        self.assertTrue(extra["price_drift_exceeds_threshold"])
        self.assertTrue(extra["entry_repriced"])
        self.assertEqual("bid-signal_extra.entry_limit_offset", extra["reprice_source"])
        self.assertEqual("passive", extra["entry_limit_intent"])

    def test_entry_guard_reprices_order_payload_and_tracker_seed_for_acceptable_drift(self):
        signal = self._signal("AAPL")
        signal.update({"entry": 100.0, "stop_loss": 98.0, "take_profit": 104.0})
        signal["extra"] = {**signal["extra"], "entry_limit_offset": 0.05}
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        quote_book = FakeQuoteBook(
            {
                "AAPL": {
                    "last_price": 100.2,
                    "bid": 100.15,
                    "ask": 100.3,
                    "quote_age_s": 2.0,
                }
            }
        )
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=pb, quote_book=quote_book)

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual(1, len(service.order_placer.calls))
        order_payload = service.order_placer.calls[0]
        self.assertEqual(100.1, order_payload["entry_price"])
        self.assertEqual(98.1, order_payload["stop_loss_price"])
        self.assertEqual(104.1, order_payload["take_profit_price"])
        self.assertEqual(100.1, service.order_tracker.duplicate_calls[0]["entry_price"])
        order_ids, seed = service.order_tracker.registered[0]
        self.assertEqual(["101", "102", "103"], order_ids)
        self.assertEqual(100.1, seed["entry_price"])
        self.assertEqual(98.1, seed["sl_price"])
        self.assertEqual(104.1, seed["tp_price"])
        self.assertEqual("LMT", seed["entry_order_type"])
        self.assertEqual("passive", seed["entry_limit_intent"])
        self.assertEqual("passive_limit", seed["entry_price_plan"])
        ack_order = pb.acks[-1]["order"]
        self.assertEqual(100.1, ack_order["limit_price"])
        self.assertEqual(98.1, ack_order["stop_loss"])
        self.assertEqual(104.1, ack_order["take_profit"])
        self.assertEqual(10, ack_order["quantity"])
        self.assertEqual("partial_harvest_bracket", ack_order["extra"]["order_family_type"])
        self.assertEqual("", ack_order["extra"]["oca_group"])
        self.assertFalse(ack_order["extra"]["intraday_harvest_split"])
        self.assertEqual(2, len(pb.acks[-1]["child_orders"]))
        self.assertEqual(3, pb.acks[-1]["child_orders"][0]["quantity"])
        self.assertEqual(10, pb.acks[-1]["child_orders"][1]["quantity"])
        self.assertEqual("", pb.acks[-1]["child_orders"][0]["extra"]["oca_group"])
        self.assertEqual("", pb.acks[-1]["child_orders"][1]["extra"]["oca_group"])
        self.assertEqual("LMT", ack_order["extra"]["entry_order_type"])
        self.assertEqual("passive", ack_order["extra"]["entry_limit_intent"])
        self.assertEqual("passive_limit", ack_order["extra"]["entry_price_plan"])
        self.assertNotIn("marketable", str(seed).lower())
        self.assertNotIn("marketable", str(ack_order["extra"]).lower())
        self.assertTrue(service.signal_processor.pending_entries)

    def test_entry_guard_reprices_short_with_passive_ask_offset_and_shifted_protection(self):
        signal = self._signal("MSFT")
        signal.update(
            {
                "direction": "short",
                "entry": 100.0,
                "stop_loss": 102.0,
                "take_profit": 96.0,
            }
        )
        signal["extra"] = {**signal["extra"], "entry_limit_offset": 0.05}
        pb = FakeSignalPBClient({"id": "row-msft", "extra": {"source": "ibkr_compute"}})
        quote_book = FakeQuoteBook(
            {
                "MSFT": {
                    "last_price": 101.2,
                    "bid": 101.0,
                    "ask": 101.4,
                    "quote_age_s": 1.0,
                }
            }
        )
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=pb, quote_book=quote_book)

        service._process_signals()

        self.assertEqual(["sig-msft"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual(1, len(service.order_placer.calls))
        order_payload = service.order_placer.calls[0]
        self.assertEqual("short", order_payload["direction"])
        self.assertEqual(101.45, order_payload["entry_price"])
        self.assertEqual(103.45, order_payload["stop_loss_price"])
        self.assertEqual(97.45, order_payload["take_profit_price"])
        self.assertEqual(101.45, service.order_tracker.duplicate_calls[0]["entry_price"])
        order_ids, seed = service.order_tracker.registered[0]
        self.assertEqual(["101", "102", "103"], order_ids)
        self.assertEqual(101.45, seed["entry_price"])
        self.assertEqual(103.45, seed["sl_price"])
        self.assertEqual(97.45, seed["tp_price"])
        self.assertEqual("LMT", seed["entry_order_type"])
        self.assertEqual("passive", seed["entry_limit_intent"])
        ack_order = pb.acks[-1]["order"]
        self.assertEqual(101.45, ack_order["limit_price"])
        self.assertEqual(103.45, ack_order["stop_loss"])
        self.assertEqual(97.45, ack_order["take_profit"])
        self.assertEqual("passive_limit", ack_order["extra"]["entry_price_plan"])
        pre_submit_patch = next(
            patch
            for collection, _record_id, patch in pb.updates
            if collection == "ibkr_signals" and patch.get("extra", {}).get("pre_submit_prices_patched")
        )
        extra = pre_submit_patch["extra"]
        self.assertEqual(100.0, extra["original_entry"])
        self.assertEqual(102.0, extra["original_stop_loss"])
        self.assertEqual(96.0, extra["original_take_profit"])
        self.assertEqual(101.2, extra["pre_submit_reference_price"])
        self.assertAlmostEqual(0.6, extra["price_drift_r"])
        self.assertTrue(extra["price_drift_exceeds_threshold"])
        self.assertTrue(extra["entry_repriced"])
        self.assertEqual("ask+signal_extra.entry_limit_offset", extra["reprice_source"])
        self.assertEqual("passive", extra["entry_limit_intent"])

    def test_entry_guard_reprices_short_with_passive_limit_buffer(self):
        signal = self._signal("AAPL")
        signal.update({"direction": "short", "entry": 100.0, "stop_loss": 102.0, "take_profit": 96.0})
        signal["extra"] = {**signal["extra"], "entry_limit_offset": 0.05}
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        quote_book = FakeQuoteBook(
            {
                "AAPL": {
                    "last_price": 101.1,
                    "bid": 101.0,
                    "ask": 101.2,
                    "quote_age_s": 1.0,
                }
            }
        )
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=pb, quote_book=quote_book)

        service._process_signals()

        self.assertEqual(1, len(service.order_placer.calls))
        order_payload = service.order_placer.calls[0]
        self.assertEqual(101.25, order_payload["entry_price"])
        self.assertEqual(103.25, order_payload["stop_loss_price"])
        self.assertEqual(97.25, order_payload["take_profit_price"])
        ack_order = pb.acks[-1]["order"]
        self.assertEqual(101.25, ack_order["limit_price"])
        self.assertEqual(103.25, ack_order["stop_loss"])
        self.assertEqual(97.25, ack_order["take_profit"])
        self.assertEqual("passive", ack_order["extra"]["entry_limit_intent"])

    def test_buying_power_guard_blocks_signal_before_order_submission(self):
        signal = self._signal("AAPL")
        signal["shares"] = 50
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"entry_pre_submit_guard_enabled": "false"}),
            account_snapshot={"ok": True, "summary": {"buying_power": 12000, "net_liquidation": 100000}},
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        self.assertEqual("rejected", patch["status"])
        self.assertEqual("buying_power_blocked", patch["extra"]["status_reason"])
        self.assertEqual("blocked", patch["extra"]["buying_power_guard"]["state"])
        self.assertEqual("自动开仓已被购买力阈值拦截", pb.events[-1]["title"])

    def test_buying_power_warning_continues_and_ack_includes_guard(self):
        signal = self._signal("AAPL")
        signal["shares"] = 100
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"entry_pre_submit_guard_enabled": "false"}),
            account_snapshot={"ok": True, "summary": {"buying_power": 30000, "net_liquidation": 100000}},
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        ack_order = pb.acks[-1]["order"]
        self.assertEqual("warning", ack_order["extra"]["buying_power_guard"]["state"])
        self.assertEqual(10000.0, ack_order["extra"]["buying_power_requested_exposure"])
        self.assertEqual(20000.0, ack_order["extra"]["buying_power_remaining_after"])
        self.assertTrue(any(event["title"] == "自动开仓购买力预警" for event in pb.events))
        self.assertTrue(any(event["title"] == "自动开仓已提交" for event in pb.events))


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
