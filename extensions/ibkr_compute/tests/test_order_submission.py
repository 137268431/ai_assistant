import os
import sys
import time
import unittest
from unittest import mock
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker import ib_gateway
from ibkr_compute.core.broker_mode import (
    broker_mode_payload,
    configured_broker_mode,
    configured_gateway_mode,
    configured_market_data_mode,
    resolve_data_environment,
    resolve_market_data_mode,
)
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


class FakeOrderAndSignalPBClient(FakeOrderPBClient):
    def __init__(self, record):
        super().__init__()
        self.record = dict(record)
        self.updates = []
        self.lookups = []

    def get_first_record(self, collection, filter=None):
        self.lookups.append((collection, filter))
        return dict(self.record) if collection == "ibkr_signals" else {}

    def update_record(self, collection, record_id, data):
        self.updates.append((collection, record_id, dict(data)))
        self.record.update(dict(data))
        return dict(self.record)


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
        if kwargs.get("lifecycle_update") and not kwargs.get("order"):
            mode = str(kwargs.get("environment") or "paper")
            existing_extra = dict(self.record.get("extra") or {})
            execution_by_mode = dict(existing_extra.get("execution_by_mode") or {})
            broker_execution = dict(execution_by_mode.get(mode) or {})
            status = kwargs.get("status")
            note = kwargs.get("note")
            extra = dict(kwargs.get("extra") or {})
            execution_by_mode[mode] = {
                **broker_execution,
                "status": status,
                "note": note,
                "status_reason": extra.get("status_reason") or note or status,
                "data_environment": "live",
            }
            patch = {
                "extra": {
                    **existing_extra,
                    **extra,
                    "execution_by_mode": execution_by_mode,
                }
            }
            self.update_record("ibkr_signals", self.record.get("id"), patch)
        return {"status": kwargs.get("status"), "fallback": False}

    def notify_system_event(self, title, detail=None, **kwargs):
        self.events.append({"title": title, "detail": dict(detail or {}), **kwargs})
        return {"ok": True}


class FakeFailingAckSignalPBClient(FakeSignalPBClient):
    def ack_ibkr_signal(self, **kwargs):
        self.acks.append(dict(kwargs))
        raise RuntimeError("ack_unavailable")


class FakeBracketBroker:
    def __init__(self):
        self.calls = []

    def place_bracket_order(self, **kwargs):
        call_index = len(self.calls)
        self.calls.append(dict(kwargs))
        suffix = str(kwargs.get("order_ref_suffix") or "").strip()
        group = str(kwargs.get("trade_group_id") or kwargs.get("bracket_group") or "").strip()
        if not group:
            group = "NFLX_short_20260506_101500" + (f"_{suffix}" if suffix else "")
        base_order_id = 101 + call_index * 10
        family = kwargs.get("order_family_type") or "bracket_oco"
        return {
            "ok": True,
            "entry_coid": f"entry_{group}",
            "tp_coid": f"tp_{group}",
            "sl_coid": f"sl_{group}",
            "bracket_group": group,
            "trade_group_id": group,
            "oca_group": group if family == "bracket_oco" else "",
            "order_family_type": family,
            "quantity": kwargs.get("quantity"),
            "take_profit_quantity": kwargs.get("take_profit_quantity", kwargs.get("quantity")),
            "stop_loss_quantity": kwargs.get("stop_loss_quantity", kwargs.get("quantity")),
            "order_ids": [str(base_order_id), str(base_order_id + 1), str(base_order_id + 2)],
            "submission": {"ok": True},
            "protection_complete": True,
        }


class FakeMarketCloseBroker:
    def __init__(self):
        self.calls = []

    def place_market_close(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "ok": True,
            "submitted": True,
            "filled": False,
            "order_ids": ["901"],
            "entry_coid": "close_NFLX_20260506_101500",
            "bracket_group": "close_NFLX_20260506_101500",
            "order_type": "LMT",
            "limit_price": 101.25,
        }


class FakeUnconfirmedMarketCloseBroker(FakeMarketCloseBroker):
    def place_market_close(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "ok": False,
            "submitted": False,
            "error": "order_submission_unconfirmed",
            "order_ids": ["157"],
            "entry_coid": "close_BA_20260603_101500",
            "bracket_group": "close_BA_20260603_101500",
        }


class FakeRejectedMarketCloseBroker(FakeMarketCloseBroker):
    def place_market_close(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "ok": False,
            "submitted": False,
            "error": "broker_rejected_order",
            "order_ids": ["158"],
            "entry_coid": "close_BA_20260603_101501",
            "bracket_group": "close_BA_20260603_101501",
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
        return {
            "ok": True,
            "order_ids": ["101", "102", "103"],
            "bracket_group": "AAPL_long",
            "protection_complete": True,
        }

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
        self.symbol_map = {}

    def get_quote(self, symbol):
        normalized_symbol = str(symbol or "").strip().upper()
        self.calls.append(normalized_symbol)
        quote = self.quotes.get(normalized_symbol)
        return dict(quote or {}) if quote else None

    def set_quote(self, symbol, quote):
        self.quotes[str(symbol or "").strip().upper()] = dict(quote or {})

    def set_symbol_map(self, symbol_map):
        self.symbol_map = dict(symbol_map or {})


class FakeWsClient:
    def __init__(self, quote_book=None, quotes_on_subscribe=None, snapshot_result=None, subscribed_count=0, pending_count=0):
        self.quote_book = quote_book
        self.quotes_on_subscribe = dict(quotes_on_subscribe or {})
        self.snapshot_result = snapshot_result
        self.subscribed_count = subscribed_count
        self.pending_count = pending_count
        self.subscribed = []
        self.unsubscribed = []
        self.snapshots = []

    def status(self):
        return {
            "subscribed_count": self.subscribed_count,
            "pending_count": self.pending_count,
        }

    def subscribe(self, conid):
        self.subscribed.append(int(conid))
        for symbol, quote in self.quotes_on_subscribe.items():
            if self.quote_book:
                self.quote_book.set_quote(symbol, quote)

    def unsubscribe(self, conid):
        self.unsubscribed.append(int(conid))

    def request_market_data_snapshot(self, **kwargs):
        self.snapshots.append(dict(kwargs))
        result = self.snapshot_result
        if callable(result):
            result = result(**kwargs)
        return dict(result or {"ok": False, "error": "snapshot_timeout"})


class FakeSignalService(TradingServiceSignalsMixin):
    def __init__(self, signal, *, lifecycle, pb, quote_book=None, config=None, account_snapshot=None, ws_client=None):
        from ibkr_compute.orchestration import trading_service as service_mod

        service_mod.ENVIRONMENT = "paper"
        service_mod.DATA_ENVIRONMENT = "live"
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
        self.ws_client = ws_client
        self.account_snapshot_provider = lambda: dict(account_snapshot or {
            "ok": True,
            "summary": {"buying_power": 100000, "net_liquidation": 120000},
        })

    def _now_iso(self):
        return "2026-05-13T10:00:00-04:00"


def _broker_execution(patch, mode="paper"):
    extra = patch.get("extra") or {}
    return (extra.get("execution_by_mode") or {}).get(mode) or {}


class BrokerModeResolverTest(unittest.TestCase):
    def test_mode_defaults_ignore_removed_environment_variables(self):
        old_env = {
            "IBKR_" + "ENVIRONMENT": "live",
            "IBKR_" + "DATA_ENVIRONMENT": "backtest",
            "IBKR_GATEWAY_" + "TRADING_MODE": "live",
        }

        self.assertEqual(configured_broker_mode(old_env), "paper")
        self.assertEqual(configured_market_data_mode(old_env), "live")
        self.assertEqual(configured_gateway_mode(old_env), "paper")
        self.assertEqual(resolve_market_data_mode(None, old_env), "live")
        self.assertEqual(resolve_data_environment("paper", old_env), "live")

    def test_mode_payload_uses_new_mode_variables(self):
        env = {
            "IBKR_BROKER_MODE": "live",
            "IBKR_MARKET_DATA_MODE": "backtest",
            "IBKR_GATEWAY_MODE": "broker",
        }

        payload = broker_mode_payload(env=env)

        self.assertEqual(payload["broker_mode"], "live")
        self.assertEqual(payload["market_data_mode"], "backtest")
        self.assertEqual(payload["gateway_mode"], "live")
        self.assertEqual(payload["data_environment"], "backtest")
        self.assertEqual(payload["broker_mode_source"], "IBKR_BROKER_MODE")
        self.assertEqual(payload["market_data_mode_source"], "IBKR_MARKET_DATA_MODE")
        self.assertEqual(payload["gateway_mode_source"], "IBKR_GATEWAY_MODE")


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

    def test_place_bracket_order_smart_routes_us_stock_with_primary_exchange(self):
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
            "symbol": "DDOG",
            "sec_type": "STK",
            "exchange": "NASDAQ",
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
                symbol="DDOG",
                direction="long",
                quantity=20,
                entry_price=243.91,
                take_profit_price=250.0,
                stop_loss_price=240.0,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        contracts = [contract for contract, _ in adapter.client.placed_orders]
        self.assertEqual(3, len(contracts))
        for contract in contracts:
            self.assertEqual("SMART", contract.exchange)
            self.assertEqual("NASDAQ", contract.primaryExchange)

    def test_place_bracket_order_sets_adaptive_algo_on_entry_limit(self):
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
            "symbol": "AAPL",
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
                symbol="AAPL",
                direction="long",
                quantity=10,
                entry_price=100.15,
                take_profit_price=104.0,
                stop_loss_price=98.0,
                entry_algo_strategy="Adaptive",
                entry_adaptive_priority="Patient",
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        entry_order = adapter.client.placed_orders[0][1]
        self.assertEqual("Adaptive", entry_order.algoStrategy)
        self.assertEqual("adaptivePriority", entry_order.algoParams[0].tag)
        self.assertEqual("Patient", entry_order.algoParams[0].value)
        self.assertEqual("Adaptive", result["entry_algo_strategy"])
        self.assertEqual("Patient", result["entry_adaptive_priority"])

    def test_place_bracket_order_uses_explicit_sanitized_trade_group_for_order_refs(self):
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
                trade_group_id="tv/sig 1:ABC",
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        self.assertEqual("tv_sig_1_ABC", result["bracket_group"])
        self.assertEqual("tv_sig_1_ABC", result["trade_group_id"])
        self.assertEqual("tv_sig_1_ABC", result["oca_group"])
        entry_order = adapter.client.placed_orders[0][1]
        tp_order = adapter.client.placed_orders[1][1]
        sl_order = adapter.client.placed_orders[2][1]
        self.assertEqual("entry_tv_sig_1_ABC", entry_order.orderRef)
        self.assertEqual("tp_tv_sig_1_ABC", tp_order.orderRef)
        self.assertEqual("sl_tv_sig_1_ABC", sl_order.orderRef)

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

    def test_order_prices_are_normalized_to_cent_tick_before_submission(self):
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
                quantity=114,
                entry_price=88.018772,
                take_profit_price=86.6988,
                stop_loss_price=88.9088,
                order_family_type="partial_harvest_bracket",
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        entry_order = adapter.client.placed_orders[0][1]
        tp_order = adapter.client.placed_orders[1][1]
        sl_order = adapter.client.placed_orders[2][1]
        self.assertEqual(88.02, entry_order.lmtPrice)
        self.assertEqual(86.70, tp_order.lmtPrice)
        self.assertEqual(88.91, sl_order.auxPrice)
        self.assertEqual(88.91, result["stop_loss_price"])
        self.assertTrue(result["price_normalization"]["stop_loss_price"]["changed"])

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

    def test_place_market_close_smart_routes_us_stock_with_primary_exchange(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient({"ok": True})
        adapter.resolve_contract = lambda **kwargs: {
            "conid": 123,
            "symbol": "BWA",
            "sec_type": "STK",
            "exchange": "NYSE",
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
                symbol="BWA",
                direction="long",
                quantity=66,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        contract = adapter.client.placed_orders[0][0]
        self.assertEqual("SMART", contract.exchange)
        self.assertEqual("NYSE", contract.primaryExchange)

    def test_place_market_close_preserves_non_us_stock_exchange(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient({"ok": True})
        adapter.resolve_contract = lambda **kwargs: {
            "conid": 123,
            "symbol": "VOD",
            "sec_type": "STK",
            "exchange": "LSE",
            "currency": "GBP",
        }

        original_order = ib_gateway.Order
        original_contract = ib_gateway.Contract
        try:
            ib_gateway.Order = FakeOrder
            ib_gateway.Contract = FakeContract
            result = ib_gateway.BrokerAdapter.place_market_close(
                adapter,
                conid=123,
                symbol="VOD",
                direction="long",
                quantity=10,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        contract = adapter.client.placed_orders[0][0]
        self.assertEqual("LSE", contract.exchange)
        self.assertEqual("", getattr(contract, "primaryExchange", ""))

    def test_place_market_close_can_use_marketable_limit_protection(self):
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
                direction="short",
                quantity=7,
                order_type="marketable_limit",
                limit_price=101.25,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        close_order = adapter.client.placed_orders[0][1]
        self.assertEqual("BUY", close_order.action)
        self.assertEqual("LMT", close_order.orderType)
        self.assertEqual(101.25, close_order.lmtPrice)
        self.assertEqual("LMT", result["order_type"])
        self.assertEqual(101.25, result["limit_price"])

    def test_place_market_close_normalizes_marketable_limit_price(self):
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
                direction="short",
                quantity=7,
                order_type="marketable_limit",
                limit_price=101.257,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        close_order = adapter.client.placed_orders[0][1]
        self.assertEqual(101.26, close_order.lmtPrice)
        self.assertEqual(101.26, result["limit_price"])
        self.assertTrue(result["price_normalization"]["limit_price"]["changed"])

    def test_place_market_close_rejects_marketable_limit_without_limit_price(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient({"ok": True})
        adapter.resolve_contract = lambda **kwargs: {
            "conid": 123,
            "symbol": "NFLX",
            "sec_type": "STK",
            "exchange": "SMART",
            "currency": "USD",
        }

        result = ib_gateway.BrokerAdapter.place_market_close(
            adapter,
            conid=123,
            symbol="NFLX",
            direction="short",
            quantity=7,
            order_type="marketable_limit",
            limit_price=0,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("limit_price_required_for_marketable_limit", result["error"])
        self.assertEqual([], adapter.client.placed_orders)

    def test_cancel_order_clears_stale_modify_error_and_accepts_ib_canceled_callback(self):
        class FakeCancelClient:
            def __init__(self):
                self.errors = {"86": {"code": 201, "message": "Order rejected - reason:Invalid Price"}}
                self.cleared = []
                self.cancelled = []

            def clear_order_error(self, order_id):
                self.cleared.append(str(order_id))
                self.errors.pop(str(order_id), None)

            def cancel_open_order(self, order_id):
                self.cancelled.append(str(order_id))
                self.errors[str(order_id)] = {"code": 202, "message": "Order Canceled"}

            def get_order_error(self, order_id):
                return dict(self.errors.get(str(order_id)) or {})

            def get_order_snapshot(self, order_id):
                return {}

            def request_open_orders(self, timeout=1, include_all=False):
                return []

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeCancelClient()

        result = ib_gateway.BrokerAdapter.cancel_order(adapter, "86")

        self.assertTrue(result["ok"])
        self.assertEqual("CANCELLED", result["status"])
        self.assertEqual(["86"], adapter.client.cancelled)
        self.assertGreaterEqual(adapter.client.cleared.count("86"), 2)

    def test_cancel_order_ignores_stale_invalid_price_after_cancel_request(self):
        class FakeCancelClient:
            def __init__(self):
                self.errors = {}
                self.cleared = []
                self.cancelled = []
                self.open_status = "Cancelled"

            def clear_order_error(self, order_id):
                self.cleared.append(str(order_id))
                self.errors.pop(str(order_id), None)

            def cancel_open_order(self, order_id):
                self.cancelled.append(str(order_id))
                self.errors[str(order_id)] = {"code": 201, "message": "Order rejected - reason:Invalid Price"}

            def get_order_error(self, order_id):
                return dict(self.errors.get(str(order_id)) or {})

            def get_order_snapshot(self, order_id):
                return {"orderId": str(order_id), "status": self.open_status}

            def request_open_orders(self, timeout=1, include_all=False):
                return [{"orderId": "86", "status": self.open_status}]

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeCancelClient()

        result = ib_gateway.BrokerAdapter.cancel_order(adapter, "86")

        self.assertTrue(result["ok"])
        self.assertEqual("CANCELLED", result["status"])
        self.assertEqual(["86"], adapter.client.cancelled)
        self.assertEqual([{"code": 201, "message": "Order rejected - reason:Invalid Price"}], result["confirm"]["ignored_errors"])

    def test_modify_order_normalizes_price_updates_before_confirmation(self):
        class FakeModifyClient:
            def __init__(self):
                self.contract = FakeContract()
                self.order = FakeOrder()
                self.order.orderId = 555
                self.order.orderType = "STP"
                self.placed_orders = []
                self.cleared = []

            def get_order_objects(self, order_id):
                return self.contract, self.order

            def clear_order_error(self, order_id):
                self.cleared.append(str(order_id))

            def place_order(self, contract, order):
                self.placed_orders.append((contract, order))

            def await_order_submission(self, order_id, timeout=3.0, poll_interval=0.2):
                return {"ok": True, "order": self.get_order_snapshot(order_id)}

            def get_order_error(self, order_id):
                return {}

            def get_order_snapshot(self, order_id):
                return {
                    "orderId": str(order_id),
                    "status": "Submitted",
                    "auxPrice": getattr(self.order, "auxPrice", 0),
                }

            def request_open_orders(self, timeout=1, include_all=False):
                return [self.get_order_snapshot("555")]

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeModifyClient()

        result = ib_gateway.BrokerAdapter.modify_order(adapter, "555", {"auxPrice": 88.9088})

        self.assertTrue(result["ok"])
        self.assertEqual(88.91, adapter.client.order.auxPrice)
        self.assertEqual(88.91, result["price_normalization"]["auxPrice"]["normalized"])


class OrderPlacerBracketMetadataTest(unittest.TestCase):
    def test_marketable_limit_close_order_is_logged_with_actual_type_and_price(self):
        pb_client = FakeOrderPBClient()
        broker = FakeMarketCloseBroker()
        placer = OrderPlacer(pb_client=pb_client, broker=broker, account_id="DU123")

        result = placer.place_market_close(
            conid=123,
            symbol="NFLX",
            direction="short",
            quantity=7,
            order_type="marketable_limit",
            limit_price=101.25,
            trade_group_id="NFLX_short_harvest",
            entry_order_unique_id="entry_NFLX_short_harvest",
            source="order_flow_full_exit",
            close_reason="order_flow_full_exit",
            close_reason_human="订单流风控平仓",
            position_snapshot={"avg_cost": 103.0, "market_price": 101.5, "unrealized_pnl": 10.5},
        )

        self.assertTrue(result["ok"])
        self.assertEqual("marketable_limit", broker.calls[0]["order_type"])
        self.assertEqual(101.25, broker.calls[0]["limit_price"])
        close_row = pb_client.upserts[-1]
        self.assertEqual("LMT", close_row["order_type"])
        self.assertEqual(101.25, close_row["limit_price"])
        self.assertEqual("Submitted", close_row["status"])
        self.assertEqual("active", close_row["relation_status"])
        self.assertEqual("LMT", close_row["extra"]["market_close_result"]["order_type"])
        self.assertEqual("order_flow_full_exit", close_row["extra"]["close_reason"])
        self.assertEqual("order_flow_full_exit", close_row["extra"]["reason"])
        self.assertEqual("订单流风控平仓", close_row["extra"]["close_reason_human"])
        self.assertEqual(103.0, close_row["extra"]["position_avg_cost"])
        self.assertEqual(103.0, close_row["extra"]["entry_price_for_pnl"])
        self.assertEqual(10.5, close_row["extra"]["position_snapshot"]["unrealized_pnl"])

    def test_unconfirmed_market_close_prewrites_pending_close_mapping(self):
        pb_client = FakeOrderAndSignalPBClient(
            {
                "id": "sig-ba-row",
                "signal_id": "sig-ba",
                "environment": "live",
                "extra": {
                    "trade_group_id": "TV_BA_GROUP",
                    "execution_by_mode": {
                        "paper": {
                            "trade_group_id": "TV_BA_GROUP",
                            "bracket_group": "TV_BA_GROUP",
                            "entry_order_unique_id": "entry_TV_BA_GROUP",
                        }
                    },
                },
            }
        )
        broker = FakeUnconfirmedMarketCloseBroker()
        placer = OrderPlacer(pb_client=pb_client, broker=broker, account_id="DU123", environment="paper")

        result = placer.place_market_close(
            conid=123,
            symbol="BA",
            direction="long",
            quantity=9,
            signal_id="sig-ba",
            source="reverse_signal_close",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(1, len(pb_client.upserts))
        close_row = pb_client.upserts[0]
        self.assertEqual("close_BA_20260603_101500", close_row["unique_id"])
        self.assertEqual("157", close_row["broker_order_id"])
        self.assertEqual("close", close_row["role"])
        self.assertEqual("Submitted", close_row["status"])
        self.assertEqual("active", close_row["relation_status"])
        self.assertEqual("TV_BA_GROUP", close_row["trade_group_id"])
        self.assertEqual("entry_TV_BA_GROUP", close_row["entry_order_unique_id"])
        self.assertEqual("sig-ba", close_row["signal_id"])
        self.assertTrue(close_row["extra"]["submission_unconfirmed"])
        self.assertTrue(close_row["extra"]["order_submission_unconfirmed"])
        self.assertEqual("order_submission_unconfirmed", close_row["extra"]["submission_error"])

    def test_rejected_market_close_does_not_prewrite_active_close_mapping(self):
        pb_client = FakeOrderAndSignalPBClient(
            {
                "id": "sig-ba-row",
                "signal_id": "sig-ba",
                "environment": "live",
                "extra": {
                    "trade_group_id": "TV_BA_GROUP",
                    "execution_by_mode": {
                        "paper": {
                            "trade_group_id": "TV_BA_GROUP",
                            "entry_order_unique_id": "entry_TV_BA_GROUP",
                        }
                    },
                },
            }
        )
        broker = FakeRejectedMarketCloseBroker()
        placer = OrderPlacer(pb_client=pb_client, broker=broker, account_id="DU123", environment="paper")

        result = placer.place_market_close(
            conid=123,
            symbol="BA",
            direction="long",
            quantity=9,
            signal_id="sig-ba",
            source="reverse_signal_close",
        )

        self.assertFalse(result["ok"])
        self.assertEqual([], pb_client.upserts)

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

    def test_order_placer_passes_adaptive_entry_algo_to_broker(self):
        pb_client = FakeOrderPBClient()
        broker = FakeBracketBroker()
        placer = OrderPlacer(pb_client=pb_client, broker=broker, account_id="DU123")

        result = placer.place_bracket_order(
            conid=123,
            symbol="AAPL",
            direction="long",
            quantity=10,
            entry_price=100.15,
            take_profit_price=104.0,
            stop_loss_price=98.0,
            signal_id="sig-aapl",
            entry_algo_strategy="Adaptive",
            entry_adaptive_priority="Patient",
        )

        self.assertTrue(result["ok"])
        self.assertEqual("Adaptive", broker.calls[0]["entry_algo_strategy"])
        self.assertEqual("Patient", broker.calls[0]["entry_adaptive_priority"])
        self.assertEqual("Adaptive", result["entry_algo_strategy"])
        self.assertEqual("Patient", result["entry_adaptive_priority"])

    def test_bracket_order_links_origin_signal_execution_metadata_in_live_data_env(self):
        pb_client = FakeOrderAndSignalPBClient(
            {
                "id": "signal-row-1",
                "signal_id": "sig-nflx",
                "environment": "live",
                "extra": {
                    "trade_group_id": "TV_GROUP_1",
                    "execution_by_mode": {"paper": {"status": "pending"}},
                },
            }
        )
        broker = FakeBracketBroker()
        placer = OrderPlacer(pb_client=pb_client, broker=broker, account_id="DU123", environment="paper")

        with mock.patch.dict(os.environ, {"IBKR_MARKET_DATA_MODE": "live"}):
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
        self.assertEqual("TV_GROUP_1", broker.calls[0]["trade_group_id"])
        self.assertEqual("TV_GROUP_1", result["trade_group_id"])
        self.assertEqual("TV_GROUP_1", result["bracket_group"])
        self.assertEqual(3, len(pb_client.upserts))
        entry, tp, sl = pb_client.upserts
        self.assertEqual("TV_GROUP_1", entry["trade_group_id"])
        self.assertEqual("TV_GROUP_1", tp["trade_group_id"])
        self.assertEqual("TV_GROUP_1", sl["trade_group_id"])
        self.assertEqual("entry_TV_GROUP_1", entry["unique_id"])
        self.assertEqual("101", entry["order_id"])
        self.assertEqual("102", tp["order_id"])
        self.assertEqual("103", sl["order_id"])
        self.assertEqual("entry", entry["role"])
        self.assertEqual("take_profit", tp["role"])
        self.assertEqual("stop_loss", sl["role"])
        self.assertTrue(any('environment = "live"' in lookup[1] for lookup in pb_client.lookups))
        patch = pb_client.updates[-1][2]
        broker_execution = patch["extra"]["execution_by_mode"]["paper"]
        self.assertEqual("pending", broker_execution["status"])
        self.assertEqual("TV_GROUP_1", broker_execution["trade_group_id"])
        self.assertEqual("TV_GROUP_1", broker_execution["bracket_group"])
        self.assertEqual("101", broker_execution["entry_order_id"])
        self.assertEqual("102", broker_execution["tp_order_id"])
        self.assertEqual("103", broker_execution["sl_order_id"])
        self.assertEqual("entry_TV_GROUP_1", broker_execution["entry_order_unique_id"])
        self.assertEqual("tp_TV_GROUP_1", broker_execution["tp_order_unique_id"])
        self.assertEqual("sl_TV_GROUP_1", broker_execution["sl_order_unique_id"])
        self.assertTrue(broker_execution["protection_complete"])

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

    def _tv_signal(self, symbol="AAPL", *, age_sec=1.0):
        signal = self._signal(symbol)
        pine_eval_ms = int((time.time() - age_sec) * 1000)
        extra = {
            "source": "tradingview",
            "pine_eval_ms": pine_eval_ms,
            "risk_per_share": 2.0,
            "reward_risk": 2.0,
            "atr": 1.25,
        }
        signal["extra"] = extra
        signal["raw"]["extra"] = dict(extra)
        signal["source"] = "tradingview"
        signal["raw"]["source"] = "tradingview"
        return signal

    def test_capacity_full_keeps_signal_pending_and_retriable(self):
        signal = self._signal("AAPL")
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(capacity_full=True), pb=pb)

        service._process_signals()

        self.assertEqual([], service.signal_router.processed)
        self.assertEqual(["sig-aapl"], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        self.assertEqual("pending", _broker_execution(patch)["status"])
        self.assertEqual("strategy_capacity_full", patch["extra"]["status_reason"])
        self.assertEqual("waiting_for_capacity", patch["extra"]["execution_state"])

    def test_order_flow_marketable_reprice_preserves_stop_and_target_distance(self):
        signal = self._signal("AAPL")
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=FakeSignalPBClient({"id": "row-aapl"}))

        adjusted = service._apply_order_flow_entry_decision(
            signal,
            {
                "enforced": True,
                "marketable_limit": {"ok": True, "price": 100.08, "order_type": "marketable_limit"},
            },
        )

        self.assertEqual(100.08, adjusted["entry"])
        self.assertEqual(98.08, adjusted["stop_loss"])
        self.assertEqual(104.08, adjusted["take_profit"])
        self.assertEqual("marketable", adjusted["extra"]["entry_limit_intent"])
        self.assertEqual(98.0, adjusted["extra"]["order_flow_original_stop_loss"])

    def test_tv_direct_signal_uses_freshness_and_bounded_adaptive_limit_without_quote_subscription(self):
        signal = self._tv_signal("AAPL")
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        quote_book = FakeQuoteBook({})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            quote_book=quote_book,
            config=FakeConfig({"tv_entry_limit_cap_bps": 15, "entry_pre_submit_guard_enabled": "true"}),
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual([], quote_book.calls)
        self.assertEqual(1, len(service.order_placer.calls))
        order_payload = service.order_placer.calls[0]
        self.assertEqual(100.15, order_payload["entry_price"])
        self.assertEqual(98.0, order_payload["stop_loss_price"])
        self.assertEqual(104.0, order_payload["take_profit_price"])
        self.assertEqual("tv_direct", order_payload["order_ref_suffix"])
        self.assertEqual("Adaptive", order_payload["entry_algo_strategy"])
        self.assertEqual("Normal", order_payload["entry_adaptive_priority"])
        self.assertEqual("submitted_waiting_fill", pb.acks[-1]["status"])
        ack_extra = pb.acks[-1]["order"]["extra"]
        self.assertTrue(ack_extra["tv_direct_entry"])
        self.assertEqual("bounded_marketable", ack_extra["entry_limit_intent"])
        self.assertEqual(100.0, ack_extra["reference_entry"])
        self.assertEqual(100.15, ack_extra["submitted_entry_limit_price"])
        self.assertTrue(ack_extra["final_protection_from_fill"])

    def test_tv_direct_signal_skips_runtime_readiness_validation(self):
        signal = self._tv_signal("AAPL")
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=pb)
        service.signal_processor.validate_signal = mock.Mock(side_effect=AssertionError("runtime validation should be skipped"))
        service.signal_processor._is_signal_expired = mock.Mock(side_effect=AssertionError("runtime expiry should be skipped"))

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual(1, len(service.order_placer.calls))
        service.signal_processor.validate_signal.assert_not_called()
        service.signal_processor._is_signal_expired.assert_not_called()
        ack_extra = pb.acks[-1]["order"]["extra"]
        self.assertTrue(ack_extra["tv_direct_signal_processor_validation_skipped"])
        self.assertEqual("freshness_only_then_hard_safety", ack_extra["tv_direct_validation_policy"])

    def test_tv_direct_signal_uses_tv_limit_cap_when_present(self):
        signal = self._tv_signal("AAPL")
        signal["submitted_limit_cap_bps"] = 20
        signal["extra"]["submitted_limit_cap_bps"] = 20
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"tv_entry_limit_cap_bps": 15}),
        )

        service._process_signals()

        self.assertEqual(100.2, service.order_placer.calls[0]["entry_price"])
        ack_extra = pb.acks[-1]["order"]["extra"]
        self.assertEqual(20.0, ack_extra["submitted_limit_cap_bps"])
        self.assertEqual(100.2, ack_extra["submitted_entry_limit_price"])

    def test_tv_direct_stale_signal_is_expired_before_order_submission(self):
        signal = self._tv_signal("AAPL", age_sec=300.0)
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"tv_entry_freshness_sec": 120}),
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        self.assertEqual("expired", pb.acks[-1]["status"])
        self.assertTrue(pb.acks[-1]["lifecycle_update"])
        patch = pb.updates[-1][2]
        self.assertEqual("expired", _broker_execution(patch)["status"])
        self.assertEqual("stale_signal", patch["extra"]["status_reason"])
        self.assertTrue(patch["extra"]["tv_direct_rejected"])

    def test_signal_expired_validation_marks_expired_not_rejected(self):
        signal = self._signal("AAPL")
        pb = FakeFailingAckSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=pb)
        service.signal_processor.validate_signal = lambda _signal: (False, "signal_expired")

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        self.assertEqual("expired", _broker_execution(patch)["status"])
        self.assertEqual("signal_expired", _broker_execution(patch)["note"])
        extra = patch["extra"]
        self.assertEqual("signal_expired", extra["status_reason"])
        self.assertEqual("ibkr_compute_validation_fallback", extra["expired_by"])
        self.assertEqual("expired", extra["execution_by_mode"]["paper"]["status"])

    def test_history_repair_pending_defers_and_remains_retriable(self):
        signal = self._signal("AAPL")
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=pb)
        service.signal_processor.validate_signal = lambda _signal: (False, "history_repair_pending")

        service._process_signals()

        self.assertEqual([], service.signal_router.processed)
        self.assertEqual(["sig-aapl"], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        self.assertEqual([], pb.updates)

    def test_fixed_symbol_is_blocked_and_marked_processed(self):
        signal = self._signal("BOXX")
        pb = FakeSignalPBClient({"id": "row-boxx", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(fixed_symbols={"BOXX", "IBKR"}), pb=pb)

        service._process_signals()

        self.assertEqual(["sig-boxx"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        self.assertEqual("rejected", _broker_execution(patch)["status"])
        self.assertEqual("fixed_position_symbol_blocked", patch["extra"]["status_reason"])
        self.assertTrue(patch["extra"]["fixed_position_symbol_blocked"])

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

    def test_entry_guard_temporarily_subscribes_for_missing_quote(self):
        signal = self._signal("AAPL")
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        quote_book = FakeQuoteBook({})
        ws_client = FakeWsClient(
            quote_book=quote_book,
            quotes_on_subscribe={
                "AAPL": {
                    "last_price": 100.1,
                    "bid": 100.05,
                    "ask": 100.15,
                    "quote_age_s": 0.0,
                }
            },
        )
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            quote_book=quote_book,
            ws_client=ws_client,
            config=FakeConfig({"entry_pre_submit_quote_wait_sec": 0}),
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        self.assertEqual([123], ws_client.subscribed)
        self.assertEqual([], ws_client.snapshots)
        ack_order = pb.acks[-1]["order"]
        self.assertEqual("temp_ws", ack_order["extra"]["quote_acquire_source"])
        self.assertTrue(ack_order["extra"]["temporary_quote_subscription"])
        self.assertEqual(0.0, ack_order["extra"]["quote_age_s"])

    def test_entry_guard_uses_snapshot_after_temporary_subscription_timeout(self):
        signal = self._signal("AAPL")
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        quote_book = FakeQuoteBook({})
        ws_client = FakeWsClient(
            quote_book=quote_book,
            snapshot_result={
                "ok": True,
                "quote": {
                    "last_price": 100.1,
                    "bid": 100.05,
                    "ask": 100.15,
                    "quote_age_s": 0.0,
                },
            },
        )
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            quote_book=quote_book,
            ws_client=ws_client,
            config=FakeConfig({"entry_pre_submit_quote_wait_sec": 0}),
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        self.assertEqual([123], ws_client.subscribed)
        self.assertEqual(1, len(ws_client.snapshots))
        ack_order = pb.acks[-1]["order"]
        self.assertEqual("snapshot", ack_order["extra"]["quote_acquire_source"])
        self.assertEqual(100.1, ack_order["extra"]["pre_submit_reference_price"])

    def test_entry_guard_rejects_when_quote_capacity_is_full(self):
        signal = self._signal("AAPL")
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        ws_client = FakeWsClient(subscribed_count=1)
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            quote_book=FakeQuoteBook({}),
            ws_client=ws_client,
            config=FakeConfig({"ibkr_total_subscription_limit": 1}),
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual([], service.order_placer.calls)
        self.assertEqual([], ws_client.subscribed)
        patch = pb.updates[-1][2]
        self.assertEqual("rejected", _broker_execution(patch)["status"])
        self.assertEqual("entry_guard_quote_capacity_full", patch["extra"]["status_reason"])
        self.assertEqual("total_subscription_limit", patch["extra"]["quote_acquire_error"])

    def test_entry_guard_rejects_when_subscription_and_snapshot_do_not_return_quote(self):
        signal = self._signal("AAPL")
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        ws_client = FakeWsClient(snapshot_result={"ok": False, "error": "snapshot_timeout"})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            quote_book=FakeQuoteBook({}),
            ws_client=ws_client,
            config=FakeConfig({"entry_pre_submit_quote_wait_sec": 0}),
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual([], service.order_placer.calls)
        self.assertEqual([123], ws_client.subscribed)
        self.assertEqual(1, len(ws_client.snapshots))
        patch = pb.updates[-1][2]
        self.assertEqual("rejected", _broker_execution(patch)["status"])
        self.assertEqual("entry_guard_quote_snapshot_timeout", patch["extra"]["status_reason"])
        self.assertEqual("snapshot_timeout", patch["extra"]["quote_acquire_error"])

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
        self.assertEqual("rejected", _broker_execution(patch)["status"])
        self.assertEqual("buying_power_blocked", patch["extra"]["status_reason"])
        self.assertEqual("blocked", patch["extra"]["buying_power_guard"]["state"])
        self.assertEqual("自动开仓已被动态购买力上限拦截", pb.events[-1]["title"])

    def test_buying_power_guard_waits_when_account_snapshot_unavailable(self):
        signal = self._signal("AAPL")
        signal["shares"] = 50
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"entry_pre_submit_guard_enabled": "false"}),
            account_snapshot={
                "ok": False,
                "summary": {
                    "account_code": "DU123",
                    "account_type": "",
                    "net_liquidation": 0,
                    "available_funds": 0,
                    "buying_power": 0,
                    "excess_liquidity": 0,
                    "equity_with_loan": 0,
                    "gross_position_value": 0,
                    "total_cash_value": 0,
                    "initial_margin": 0,
                    "maintenance_margin": 0,
                },
                "buying_power_guard": {
                    "state": "unavailable",
                    "reason": "gateway_unavailable",
                    "available": False,
                },
            },
        )

        service._process_signals()

        self.assertEqual([], service.signal_router.processed)
        self.assertEqual(["sig-aapl"], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        self.assertEqual("pending", _broker_execution(patch)["status"])
        self.assertEqual("gateway_unavailable", patch["extra"]["status_reason"])
        self.assertEqual("waiting_for_account_snapshot", patch["extra"]["execution_state"])
        self.assertEqual("unavailable", patch["extra"]["buying_power_guard"]["state"])
        self.assertEqual("自动开仓暂停：购买力风控不可用", pb.events[-1]["title"])

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
    def test_fetch_pending_signals_skips_broker_scoped_awaiting_confirm(self):
        rows = [
            {
                "signal_id": "awaiting-paper",
                "symbol": "TTD",
                "direction": "long",
                "entry": 22.68,
                "stop_loss": 22.34,
                "take_profit": 24.04,
                "shares": 441,
                "rr": "1:2",
                "us_time": "2026-05-19 09:35:00",
                "extra": {
                    "source": "ibkr_compute",
                    "execution_by_mode": {
                        "paper": {
                            "status": "awaiting_confirm",
                            "note": "manual_confirmation_required",
                        }
                    },
                },
            },
            {
                "signal_id": "confirmed-paper",
                "symbol": "AAPL",
                "direction": "long",
                "entry": 100.0,
                "stop_loss": 98.0,
                "take_profit": 104.0,
                "shares": 10,
                "rr": "1:2",
                "us_time": "2026-05-19 09:40:00",
                "extra": {
                    "source": "ibkr_compute",
                    "execution_by_mode": {
                        "paper": {
                            "status": "pending",
                            "note": "",
                        }
                    },
                },
            },
        ]
        router = SignalRouter(FakePBClient(rows), FakeConfig(), environment="live", broker_mode="paper")

        fetched = router.fetch_pending_signals()

        self.assertEqual(["confirmed-paper"], [item["signal_id"] for item in fetched])

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
