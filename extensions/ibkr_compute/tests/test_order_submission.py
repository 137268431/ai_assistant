import os
import sys
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timezone
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
from ibkr_compute.core.time_utils import ET
from ibkr_compute.order.order_placer import OrderPlacer
from ibkr_compute.order.close_execution import build_close_execution_plan, infer_close_session
from ibkr_compute.order.buying_power_reservations import BuyingPowerReservationStore
from ibkr_compute.orchestration.signals import TradingServiceSignalsMixin
from ibkr_compute.signal.signal_router import SignalRouter


class FakeOrder:
    def __init__(self):
        self.eTradeOnly = True
        self.firmQuoteOnly = True


class FakeContract:
    pass


class CloseExecutionPlannerTest(unittest.TestCase):
    def test_close_session_classification_supports_all_tradable_sessions(self):
        from datetime import datetime
        from ibkr_compute.core.time_utils import ET

        cases = [
            (datetime(2026, 6, 8, 8, 0, tzinfo=ET), "premarket"),
            (datetime(2026, 6, 8, 10, 0, tzinfo=ET), "regular"),
            (datetime(2026, 6, 8, 17, 0, tzinfo=ET), "afterhours"),
            (datetime(2026, 6, 8, 21, 0, tzinfo=ET), "overnight"),
        ]
        for now, expected in cases:
            with self.subTest(expected=expected):
                session = infer_close_session(now)
                self.assertTrue(session.tradable)
                self.assertEqual(expected, session.name)

    def test_close_limit_plan_uses_bid_for_long_and_ask_for_short(self):
        from datetime import datetime
        from ibkr_compute.core.time_utils import ET

        long_plan = build_close_execution_plan(
            symbol="NVDA",
            direction="long",
            quote={"bid": 100.0, "ask": 100.2},
            now=datetime(2026, 6, 8, 10, 0, tzinfo=ET),
            limit_bps=10,
        )
        short_plan = build_close_execution_plan(
            symbol="NVDA",
            direction="short",
            quote={"bid": 100.0, "ask": 100.2},
            now=datetime(2026, 6, 8, 10, 0, tzinfo=ET),
            limit_bps=10,
        )
        self.assertTrue(long_plan["ok"])
        self.assertEqual("SELL", long_plan["close_action"])
        self.assertEqual(99.9, long_plan["limit_price"])
        self.assertTrue(short_plan["ok"])
        self.assertEqual("BUY", short_plan["close_action"])
        self.assertEqual(100.3, short_plan["limit_price"])

    def test_close_plan_rejects_overnight_premarket_break(self):
        from datetime import datetime
        from ibkr_compute.core.time_utils import ET

        plan = build_close_execution_plan(
            symbol="NVDA",
            direction="long",
            quote={"bid": 100.0},
            now=datetime(2026, 6, 8, 3, 55, tzinfo=ET),
        )
        self.assertFalse(plan["ok"])
        self.assertEqual("overnight_premarket_break", plan["error"])

    def test_close_plan_defaults_overnight_to_smart_include_overnight(self):
        from datetime import datetime
        from ibkr_compute.core.time_utils import ET

        plan = build_close_execution_plan(
            symbol="MSTR",
            direction="short",
            quote={"bid": 126.1, "ask": 126.2},
            now=datetime(2026, 6, 8, 21, 0, tzinfo=ET),
        )

        self.assertTrue(plan["ok"])
        self.assertEqual("overnight", plan["session"])
        self.assertEqual("SMART", plan["exchange"])
        self.assertTrue(plan["include_overnight"])
        self.assertTrue(plan["outside_rth"])


class FakeClient:
    def __init__(self, ack_result=None, submission_result=None, open_orders=None):
        self.ack_result = ack_result or {"ok": True}
        self.submission_result = submission_result
        self.open_orders = list(open_orders or [])
        self.placed_orders = []
        self.cleared_order_errors = []
        self.await_order_submissions_calls = []

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

    def await_order_submissions(self, order_ids, timeout=5.0, poll_interval=0.2, **kwargs):
        self.await_order_submissions_calls.append(
            {"order_ids": list(order_ids or []), "timeout": timeout, "poll_interval": poll_interval, **kwargs}
        )
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
        self.system_events = []

    def upsert_order(self, data):
        self.upserts.append(dict(data))
        return {"success": True}

    def notify_system_event(self, **kwargs):
        self.system_events.append(dict(kwargs))
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


class StrictNotifyOrderAndSignalPBClient(FakeOrderAndSignalPBClient):
    def notify_system_event(
        self,
        title,
        detail=None,
        *,
        event_type="status_change",
        level="info",
        source="ibkr_compute",
        environment=None,
        message_id="",
    ):
        self.system_events.append(
            {
                "title": title,
                "detail": dict(detail or {}),
                "event_type": event_type,
                "level": level,
                "source": source,
                "environment": environment,
                "message_id": message_id,
            }
        )
        return {"ok": True}


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


class FakeSignalStatePBClient(FakeSignalPBClient):
    def __init__(self, record):
        super().__init__(record)
        self.states = {}
        self.upserts = []

    def get_state(self, state_key, environment, date="global"):
        return self.states.get((state_key, environment, date))

    def upsert_state(self, state_key, environment, data, date="global"):
        record = {"id": f"{state_key}:{environment}:{date}", "data": dict(data or {})}
        self.states[(state_key, environment, date)] = record
        return record

    def upsert_order(self, data):
        self.upserts.append(dict(data or {}))
        return {"success": True, **dict(data or {})}

    def get_records(self, collection, filter=None, sort=None, per_page=100, page=1):
        if collection == "orders":
            return list(self.upserts[: int(per_page or 100)])
        return []


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
            "oca_group": "",
            "order_family_type": family,
            "quantity": kwargs.get("quantity"),
            "take_profit_quantity": kwargs.get("take_profit_quantity", kwargs.get("quantity")),
            "stop_loss_quantity": kwargs.get("stop_loss_quantity", kwargs.get("quantity")),
            "order_ids": [str(base_order_id), str(base_order_id + 1), str(base_order_id + 2)],
            "submission": {"ok": True},
            "protection_complete": True,
        }


class FakePendingConfirmBracketBroker(FakeBracketBroker):
    def place_bracket_order(self, **kwargs):
        payload = super().place_bracket_order(**kwargs)
        payload.pop("protection_complete", None)
        payload["confirmation_mode"] = "background"
        payload["pending_confirmation"] = True
        payload["protection_confirmation_pending"] = True
        payload["submission"] = {
            "ok": True,
            "pending_confirmation": True,
            "confirmation_mode": "background",
            "order_ids": list(payload.get("order_ids") or []),
            "orders": {},
            "missing_order_ids": [],
        }
        return payload


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


class FakeProtectionRepairBroker:
    def __init__(self):
        self.calls = []

    def place_protection_repair_orders(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "ok": True,
            "order_ids": ["501", "502"],
            "tp_coid": "repair_tp_group_SIG_1",
            "sl_coid": "repair_sl_group_SIG_1",
            "oca_group": "repair_group_SIG_1",
            "take_profit_price": kwargs.get("take_profit_price"),
            "stop_loss_price": kwargs.get("stop_loss_price"),
            "protection_complete": True,
        }


class FakeQuoteMarketCloseBroker(FakeMarketCloseBroker):
    def __init__(self, quote_result):
        super().__init__()
        self.quote_result = dict(quote_result or {})
        self.snapshots = []

    def request_market_data_snapshot(self, **kwargs):
        self.snapshots.append(dict(kwargs))
        return dict(self.quote_result)


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


class FakePendingAsyncOrderPlacer(FakeOrderPlacer):
    def place_bracket_order(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "ok": True,
            "order_ids": ["101", "102", "103"],
            "bracket_group": "AAPL_long",
            "trade_group_id": "AAPL_long",
            "entry_coid": "entry_AAPL_long",
            "tp_coid": "tp_AAPL_long",
            "sl_coid": "sl_AAPL_long",
            "order_family_type": "bracket_oco",
            "quantity": kwargs.get("quantity"),
            "take_profit_quantity": kwargs.get("quantity"),
            "stop_loss_quantity": kwargs.get("quantity"),
            "confirmation_mode": "background",
            "pending_confirmation": True,
            "protection_confirmation_pending": True,
            "protection_complete": False,
            "protection_incomplete": False,
            "submission": {"ok": True, "pending_confirmation": True, "confirmation_mode": "background"},
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
        self.subscribed_meta = []
        self.unsubscribed = []
        self.snapshots = []

    def status(self):
        return {
            "subscribed_count": self.subscribed_count,
            "pending_count": self.pending_count,
        }

    def subscribe(self, conid, **kwargs):
        self.subscribed.append(int(conid))
        self.subscribed_meta.append({"conid": int(conid), **dict(kwargs)})
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
        snapshot_payload = dict(account_snapshot or {
            "ok": True,
            "summary": {"buying_power": 100000, "net_liquidation": 120000},
        })
        snapshot_payload.setdefault("fetched_at", datetime.now(timezone.utc).isoformat())
        self.account_snapshot_provider = lambda: dict(snapshot_payload)

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
        self.assertEqual(ib_gateway.BRACKET_SUBMISSION_CONFIRM_TIMEOUT_SECONDS, adapter.client.await_order_submissions_calls[-1]["timeout"])

    def test_place_bracket_order_sets_outside_rth_on_all_bracket_legs(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            submission_result={
                "ok": True,
                "orders": {"101": {"ok": True}, "102": {"ok": True}, "103": {"ok": True}},
                "missing_order_ids": [],
            }
        )
        adapter.resolve_contract = lambda **kwargs: {
            "conid": 265598,
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
                conid=265598,
                symbol="AAPL",
                direction="long",
                quantity=10,
                entry_price=188.25,
                take_profit_price=193.10,
                stop_loss_price=185.80,
                outside_rth=True,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        self.assertTrue(result["outside_rth"])
        self.assertEqual(3, len(adapter.client.placed_orders))
        self.assertTrue(all(getattr(order, "outsideRth", False) for _, order in adapter.client.placed_orders))

    def test_place_bracket_order_can_fast_ack_and_confirm_in_background(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(open_orders=[{"orderId": "205"}])
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
            with mock.patch.object(adapter, "_start_bracket_submission_background_confirmation") as background_confirm:
                result = ib_gateway.BrokerAdapter.place_bracket_order(
                    adapter,
                    conid=346218218,
                    symbol="DELL",
                    direction="long",
                    quantity=53,
                    entry_price=191.23,
                    take_profit_price=195.48,
                    stop_loss_price=188.4,
                    confirmation_mode="background",
                )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending_confirmation"])
        self.assertTrue(result["protection_confirmation_pending"])
        self.assertEqual("background", result["confirmation_mode"])
        self.assertEqual(["206", "207", "208"], result["order_ids"])
        self.assertEqual([], adapter.client.await_order_submissions_calls)
        background_confirm.assert_called_once()

    def test_background_bracket_confirmation_downgrades_cleanup_cancel_notice(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            submission_result={
                "ok": False,
                "error": "Order Canceled - reason:",
                "orders": {},
                "failures": {
                    "206": {
                        "ok": False,
                        "order_id": "206",
                        "error": "Order Canceled - reason:",
                        "details": {"order_id": "206", "code": 202, "message": "Order Canceled - reason:"},
                    }
                },
                "missing_order_ids": ["207"],
            }
        )

        with mock.patch.object(ib_gateway.logger, "warning") as warning_log, mock.patch.object(
            ib_gateway.logger, "info"
        ) as info_log, mock.patch.object(ib_gateway, "record_order_event") as record_order_event:
            ib_gateway.BrokerAdapter._confirm_bracket_submission_background(
                adapter,
                order_ids=[206, 207, 208],
                group="AAPL_long_test",
                order_family_type="bracket_oco",
                metric_environment="paper",
            )

        warning_log.assert_not_called()
        self.assertIn("ended after cancellation", info_log.call_args.args[0])
        self.assertEqual("canceled", record_order_event.call_args.kwargs["result"])
        self.assertEqual("order_canceled_before_background_confirm", record_order_event.call_args.kwargs["reason_code"])

    def test_background_bracket_confirmation_downgrades_recent_cancel_missing_orders(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            submission_result={
                "ok": False,
                "error": "order_submission_unconfirmed",
                "orders": {},
                "failures": {},
                "missing_order_ids": ["206", "207", "208"],
            }
        )
        adapter._remember_recent_cancel_order_ids(["206", "207", "208"], ttl=60.0)

        with mock.patch.object(ib_gateway.logger, "warning") as warning_log, mock.patch.object(
            ib_gateway.logger, "info"
        ) as info_log, mock.patch.object(ib_gateway, "record_order_event") as record_order_event:
            ib_gateway.BrokerAdapter._confirm_bracket_submission_background(
                adapter,
                order_ids=[206, 207, 208],
                group="AAPL_long_test",
                order_family_type="bracket_oco",
                metric_environment="paper",
            )

        warning_log.assert_not_called()
        self.assertIn("ended after cancellation", info_log.call_args.args[0])
        self.assertEqual("canceled", record_order_event.call_args.kwargs["result"])
        self.assertEqual("order_canceled_before_background_confirm", record_order_event.call_args.kwargs["reason_code"])

    def test_background_bracket_confirmation_downgrades_recent_cancel_all_missing_orders(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            submission_result={
                "ok": False,
                "error": "order_submission_unconfirmed",
                "orders": {"206": {"ok": True, "order_id": "206"}},
                "failures": {},
                "missing_order_ids": ["207", "208"],
            }
        )
        adapter._remember_recent_cancel_all(ttl=60.0)

        with mock.patch.object(ib_gateway.logger, "warning") as warning_log, mock.patch.object(
            ib_gateway.logger, "info"
        ) as info_log, mock.patch.object(ib_gateway, "record_order_event") as record_order_event:
            ib_gateway.BrokerAdapter._confirm_bracket_submission_background(
                adapter,
                order_ids=[206, 207, 208],
                group="AAPL_long_test",
                order_family_type="bracket_oco",
                metric_environment="paper",
            )

        warning_log.assert_not_called()
        self.assertIn("ended after cancellation", info_log.call_args.args[0])
        self.assertEqual("canceled", record_order_event.call_args.kwargs["result"])
        self.assertEqual("order_canceled_before_background_confirm", record_order_event.call_args.kwargs["reason_code"])

    def test_background_bracket_confirmation_still_warns_true_rejection(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            submission_result={
                "ok": False,
                "error": "Order rejected - reason:Invalid Price",
                "orders": {},
                "failures": {
                    "206": {
                        "ok": False,
                        "order_id": "206",
                        "error": "Order rejected - reason:Invalid Price",
                        "details": {"order_id": "206", "code": 201, "message": "Order rejected - reason:Invalid Price"},
                    }
                },
                "missing_order_ids": [],
            }
        )

        with mock.patch.object(ib_gateway.logger, "warning") as warning_log, mock.patch.object(
            ib_gateway, "record_order_event"
        ) as record_order_event:
            ib_gateway.BrokerAdapter._confirm_bracket_submission_background(
                adapter,
                order_ids=[206, 207, 208],
                group="AAPL_long_test",
                order_family_type="bracket_oco",
                metric_environment="paper",
            )

        warning_log.assert_called_once()
        self.assertEqual("error", record_order_event.call_args.kwargs["result"])
        self.assertIn("Invalid Price", record_order_event.call_args.kwargs["reason_code"])

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
        self.assertEqual("", result["oca_group"])
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
        self.assertFalse(hasattr(tp_order, "ocaGroup"))
        self.assertFalse(hasattr(sl_order, "ocaGroup"))

    def test_place_protection_repair_orders_submits_only_two_close_side_oca_legs(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeClient(
            submission_result={
                "ok": True,
                "orders": {"206": {"ok": True}, "207": {"ok": True}},
                "missing_order_ids": [],
            },
            open_orders=[{"orderId": "205"}],
        )
        adapter.resolve_contract = lambda **kwargs: {
            "conid": 265598,
            "symbol": "AAPL",
            "sec_type": "STK",
            "exchange": "NASDAQ",
            "currency": "USD",
        }

        original_order = ib_gateway.Order
        original_contract = ib_gateway.Contract
        try:
            ib_gateway.Order = FakeOrder
            ib_gateway.Contract = FakeContract
            result = ib_gateway.BrokerAdapter.place_protection_repair_orders(
                adapter,
                conid=265598,
                symbol="AAPL",
                direction="long",
                quantity=16,
                take_profit_price=310.129,
                stop_loss_price=290.124,
                account_id="DU123",
                trade_group_id="group/INTU 1",
                outside_rth=True,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        self.assertEqual(["206", "207"], result["order_ids"])
        self.assertEqual("repair_group_INTU_1", result["oca_group"])
        self.assertEqual(2, len(adapter.client.placed_orders))
        tp_order = adapter.client.placed_orders[0][1]
        sl_order = adapter.client.placed_orders[1][1]
        self.assertEqual("SELL", tp_order.action)
        self.assertEqual("SELL", sl_order.action)
        self.assertEqual("LMT", tp_order.orderType)
        self.assertEqual("STP", sl_order.orderType)
        self.assertEqual(310.13, tp_order.lmtPrice)
        self.assertEqual(290.12, sl_order.auxPrice)
        self.assertEqual(16.0, tp_order.totalQuantity)
        self.assertEqual(16.0, sl_order.totalQuantity)
        self.assertEqual("DU123", tp_order.account)
        self.assertEqual("DU123", sl_order.account)
        self.assertEqual("repair_group_INTU_1", tp_order.ocaGroup)
        self.assertEqual("repair_group_INTU_1", sl_order.ocaGroup)
        self.assertFalse(hasattr(tp_order, "parentId"))
        self.assertFalse(hasattr(sl_order, "parentId"))
        self.assertFalse(tp_order.transmit)
        self.assertTrue(sl_order.transmit)
        self.assertTrue(tp_order.outsideRth)
        self.assertTrue(sl_order.outsideRth)
        self.assertEqual("repair_tp_group_INTU_1", tp_order.orderRef)
        self.assertEqual("repair_sl_group_INTU_1", sl_order.orderRef)
        self.assertEqual(
            {"order_ids": ["206", "207"], "timeout": ib_gateway.BRACKET_SUBMISSION_CONFIRM_TIMEOUT_SECONDS, "poll_interval": 0.2},
            adapter.client.await_order_submissions_calls[-1],
        )

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
        self.assertEqual("", result["oca_group"])
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
                limit_price=500.12,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(adapter.client.placed_orders))
        close_order = adapter.client.placed_orders[0][1]
        self.assertEqual("U123456", close_order.account)
        self.assertEqual("SELL", close_order.action)
        self.assertEqual("LMT", close_order.orderType)
        self.assertEqual(500.12, close_order.lmtPrice)

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
                limit_price=42.25,
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
                limit_price=88.40,
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
                outside_rth=True,
                tif="DAY",
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        close_order = adapter.client.placed_orders[0][1]
        self.assertEqual("BUY", close_order.action)
        self.assertEqual("LMT", close_order.orderType)
        self.assertEqual(101.25, close_order.lmtPrice)
        self.assertTrue(close_order.outsideRth)
        self.assertEqual("DAY", close_order.tif)
        self.assertEqual("LMT", result["order_type"])
        self.assertEqual(101.25, result["limit_price"])
        self.assertTrue(result["outside_rth"])
        self.assertEqual("DAY", result["tif"])

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

    def test_place_market_close_can_route_overnight_limit_order(self):
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
                order_type="LMT",
                limit_price=101.25,
                exchange="OVERNIGHT",
                include_overnight=True,
                outside_rth=True,
            )
        finally:
            ib_gateway.Order = original_order
            ib_gateway.Contract = original_contract

        self.assertTrue(result["ok"])
        contract = adapter.client.placed_orders[0][0]
        self.assertEqual("OVERNIGHT", contract.exchange)
        self.assertTrue(result["include_overnight"])
        self.assertFalse(result["include_overnight_supported"])

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

    def test_cancel_order_treats_already_cancelled_notice_as_terminal(self):
        class FakeCancelClient:
            def __init__(self):
                self.errors = {}
                self.cleared = []
                self.cancelled = []

            def clear_order_error(self, order_id):
                self.cleared.append(str(order_id))
                self.errors.pop(str(order_id), None)

            def cancel_open_order(self, order_id):
                self.cancelled.append(str(order_id))
                self.errors[str(order_id)] = {
                    "code": 10148,
                    "message": "OrderId 86 that needs to be cancelled cannot be cancelled, state: Cancelled.",
                }

            def get_order_error(self, order_id):
                return dict(self.errors.get(str(order_id)) or {})

            def get_order_snapshot(self, order_id):
                return {"orderId": str(order_id), "status": "Cancelled"}

            def request_open_orders(self, timeout=1, include_all=False):
                return []

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeCancelClient()

        result = ib_gateway.BrokerAdapter.cancel_order(adapter, "86")

        self.assertTrue(result["ok"])
        self.assertEqual("CANCELLED", result["status"])
        self.assertEqual(
            [{"code": 10148, "message": "OrderId 86 that needs to be cancelled cannot be cancelled, state: Cancelled."}],
            result["confirm"]["ignored_errors"],
        )

    def test_cancel_order_treats_not_found_notice_as_not_open(self):
        class FakeCancelClient:
            def __init__(self):
                self.errors = {}
                self.cleared = []
                self.cancelled = []

            def clear_order_error(self, order_id):
                self.cleared.append(str(order_id))
                self.errors.pop(str(order_id), None)

            def cancel_open_order(self, order_id):
                self.cancelled.append(str(order_id))
                self.errors[str(order_id)] = {
                    "code": 10147,
                    "message": "OrderId 86 that needs to be cancelled is not found.",
                }

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
        self.assertEqual("NOT_OPEN", result["status"])
        self.assertEqual(
            [{"code": 10147, "message": "OrderId 86 that needs to be cancelled is not found."}],
            result["confirm"]["ignored_errors"],
        )

    def test_cancel_order_uses_extended_confirmation_timeout(self):
        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = mock.Mock()
        adapter.client.clear_order_error = mock.Mock()
        adapter.client.cancel_open_order = mock.Mock()
        observed = {}

        def fake_await(order_id, *, timeout, poll_interval):
            observed["order_id"] = order_id
            observed["timeout"] = timeout
            observed["poll_interval"] = poll_interval
            return {"ok": True, "status": "CANCELLED"}

        adapter.await_order_cancelled = fake_await

        result = ib_gateway.BrokerAdapter.cancel_order(adapter, "86")

        self.assertTrue(result["ok"])
        self.assertEqual("86", observed["order_id"])
        self.assertEqual(ib_gateway.CANCEL_CONFIRM_TIMEOUT_SECONDS, observed["timeout"])
        self.assertEqual(0.2, observed["poll_interval"])

    def test_cancel_all_orders_batches_cancels_and_reconciles_missing_open_orders(self):
        class FakeBatchCancelClient:
            def __init__(self):
                self.open_orders = [
                    {"orderId": "101", "status": "Submitted"},
                    {"orderId": "102", "status": "Submitted"},
                    {"orderId": "103", "status": "PreSubmitted"},
                ]
                self.cancelled = []
                self.cleared = []
                self.request_calls = []
                self.marked_terminal = []

            def request_open_orders(self, timeout=1, include_all=False, force=False):
                self.request_calls.append({"timeout": timeout, "include_all": include_all, "force": force})
                if len(self.cancelled) >= 3:
                    return []
                return list(self.open_orders)

            def clear_order_error(self, order_id):
                self.cleared.append(str(order_id))

            def cancel_open_order(self, order_id):
                self.cancelled.append(str(order_id))

            def get_order_error(self, order_id):
                return {}

            def get_order_snapshot(self, order_id):
                # IB can keep a stale Submitted snapshot briefly after the
                # order disappears from reqAllOpenOrders; cancel-all should
                # trust the fresh open-order reconciliation.
                return {"orderId": str(order_id), "status": "Submitted"}

            def mark_order_terminal(self, order_id, *, status="CANCELLED", reason=""):
                self.marked_terminal.append({"order_id": str(order_id), "status": status, "reason": reason})
                return {"orderId": str(order_id), "status": status}

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeBatchCancelClient()

        with mock.patch("ibkr_compute.broker.ib_gateway.record_order_event"):
            result = ib_gateway.BrokerAdapter.cancel_all_orders(adapter)

        self.assertTrue(result["ok"])
        self.assertEqual(3, result["requested"])
        self.assertEqual(3, result["submitted"])
        self.assertEqual(3, result["cancelled"])
        self.assertEqual([], result["errors"])
        self.assertEqual(["101", "102", "103"], adapter.client.cancelled)
        self.assertCountEqual(["101", "102", "103"], [item["order_id"] for item in adapter.client.marked_terminal])
        self.assertTrue(all(item["reason"] == "cancel_all_open_orders_reconciled_missing" for item in adapter.client.marked_terminal))
        self.assertTrue(all(call["include_all"] and call["force"] for call in adapter.client.request_calls))

    def test_cancel_all_orders_sends_paper_global_cancel_before_id_batch(self):
        class FakeGlobalCancelClient:
            def __init__(self):
                self.open_orders = [
                    {"orderId": "101", "status": "Submitted"},
                    {"orderId": "102", "status": "PreSubmitted"},
                ]
                self.events = []
                self.marked_terminal = []

            def request_open_orders(self, timeout=1, include_all=False, force=False):
                if self.events.count("cancel:101") and self.events.count("cancel:102"):
                    return []
                return list(self.open_orders)

            def request_global_cancel(self):
                self.events.append("global")

            def clear_order_error(self, order_id):
                self.events.append(f"clear:{order_id}")

            def cancel_open_order(self, order_id):
                self.events.append(f"cancel:{order_id}")

            def get_order_error(self, order_id):
                return {}

            def get_order_snapshot(self, order_id):
                return {"orderId": str(order_id), "status": "Submitted"}

            def mark_order_terminal(self, order_id, *, status="CANCELLED", reason=""):
                self.marked_terminal.append({"order_id": str(order_id), "status": status, "reason": reason})
                return {"orderId": str(order_id), "status": status}

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeGlobalCancelClient()

        with mock.patch("ibkr_compute.broker.ib_gateway.record_order_event"), mock.patch(
            "ibkr_compute.broker.ib_gateway.CANCEL_ALL_GLOBAL_CANCEL_GRACE_SECONDS",
            0.0,
        ):
            result = ib_gateway.BrokerAdapter.cancel_all_orders(adapter, metric_environment="paper")

        self.assertTrue(result["ok"])
        self.assertTrue(result["global_cancel_submitted"])
        self.assertEqual("global", adapter.client.events[0])
        self.assertIn("cancel:101", adapter.client.events)
        self.assertIn("cancel:102", adapter.client.events)

    def test_cancel_all_orders_does_not_global_cancel_live(self):
        class FakeLiveCancelClient:
            def __init__(self):
                self.global_cancelled = False
                self.cancelled = []

            def request_open_orders(self, timeout=1, include_all=False, force=False):
                return [] if self.cancelled else [{"orderId": "301", "status": "Submitted"}]

            def request_global_cancel(self):
                self.global_cancelled = True

            def clear_order_error(self, order_id):
                pass

            def cancel_open_order(self, order_id):
                self.cancelled.append(str(order_id))

            def get_order_error(self, order_id):
                return {}

            def get_order_snapshot(self, order_id):
                return {"orderId": str(order_id), "status": "Submitted"}

            def mark_order_terminal(self, order_id, *, status="CANCELLED", reason=""):
                return {"orderId": str(order_id), "status": status}

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeLiveCancelClient()

        with mock.patch("ibkr_compute.broker.ib_gateway.record_order_event"):
            result = ib_gateway.BrokerAdapter.cancel_all_orders(adapter, metric_environment="live")

        self.assertTrue(result["ok"])
        self.assertFalse(adapter.client.global_cancelled)
        self.assertEqual(["301"], adapter.client.cancelled)

    def test_cancel_all_orders_uses_callback_cache_when_fresh_open_orders_empty(self):
        class FakeEmptyFreshOpenOrdersClient:
            def __init__(self):
                self.cancelled = []
                self.cleared = []
                self.marked_terminal = []

            def request_open_orders(self, timeout=1, include_all=False, force=False):
                return []

            def get_order_snapshots(self, include_all=False):
                return [
                    {"orderId": "201", "status": "Submitted"},
                    {"orderId": "202", "status": "PreSubmitted"},
                ]

            def clear_order_error(self, order_id):
                self.cleared.append(str(order_id))

            def cancel_open_order(self, order_id):
                self.cancelled.append(str(order_id))

            def get_order_error(self, order_id):
                return {}

            def get_order_snapshot(self, order_id):
                return {"orderId": str(order_id), "status": "Submitted"}

            def mark_order_terminal(self, order_id, *, status="CANCELLED", reason=""):
                self.marked_terminal.append({"order_id": str(order_id), "status": status, "reason": reason})
                return {"orderId": str(order_id), "status": status}

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeEmptyFreshOpenOrdersClient()

        with mock.patch("ibkr_compute.broker.ib_gateway.record_order_event"):
            result = ib_gateway.BrokerAdapter.cancel_all_orders(adapter)

        self.assertTrue(result["ok"])
        self.assertEqual("callback_cache_empty_open_orders", result["order_list_source"])
        self.assertEqual(2, result["requested"])
        self.assertEqual(2, result["submitted"])
        self.assertEqual(["201", "202"], adapter.client.cancelled)
        self.assertCountEqual(["201", "202"], [item["order_id"] for item in adapter.client.marked_terminal])

    def test_cancel_all_orders_treats_cancel_notice_as_confirmed_even_with_stale_open_row(self):
        class FakeStaleCancelClient:
            def __init__(self):
                self.cancelled = []
                self.cleared = []

            def request_open_orders(self, timeout=1, include_all=False, force=False):
                return [{"orderId": "101", "status": "Submitted"}]

            def clear_order_error(self, order_id):
                self.cleared.append(str(order_id))

            def cancel_open_order(self, order_id):
                self.cancelled.append(str(order_id))

            def get_order_error(self, order_id):
                return {"code": 202, "message": "Order Canceled - reason:", "order_id": str(order_id)}

            def get_order_snapshot(self, order_id):
                return {"orderId": str(order_id), "status": "Submitted"}

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeStaleCancelClient()

        with mock.patch("ibkr_compute.broker.ib_gateway.record_order_event"):
            result = ib_gateway.BrokerAdapter.cancel_all_orders(adapter)

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["requested"])
        self.assertEqual(1, result["submitted"])
        self.assertEqual(1, result["cancelled"])
        self.assertEqual([], result["errors"])
        self.assertIn("101", result["reconcile"]["ignored_errors"])

    def test_open_order_snapshot_request_does_not_return_stale_cached_orders(self):
        app = ib_gateway._IBGatewayApp.__new__(ib_gateway._IBGatewayApp)
        app._pending_requests = {
            1: ib_gateway._PendingRequest(kind="open_orders_all"),
        }
        app._open_orders = {"101": {"orderId": "101", "status": "Submitted"}}
        app._open_order_objects = {}
        app._state_lock = threading.RLock()
        app._listener_lock = threading.RLock()
        app._order_update_listeners = []

        app.openOrderEnd()

        self.assertEqual([], app._pending_requests[1].items)

    def test_open_order_snapshot_request_returns_only_orders_seen_in_snapshot(self):
        class Contract:
            conId = 265598
            symbol = "AAPL"
            localSymbol = "AAPL"
            currency = "USD"
            secType = "STK"

        class Order:
            account = "DU123"
            action = "BUY"
            orderType = "LMT"
            totalQuantity = 1
            lmtPrice = 100.0
            auxPrice = 0.0
            parentId = 0
            tif = "DAY"
            orderRef = "entry_test"
            permId = 0
            clientId = 0

        class OrderState:
            status = "Submitted"

        app = ib_gateway._IBGatewayApp.__new__(ib_gateway._IBGatewayApp)
        app._pending_requests = {
            1: ib_gateway._PendingRequest(kind="open_orders_all"),
        }
        app._open_orders = {"101": {"orderId": "101", "status": "Submitted"}}
        app._open_order_objects = {}
        app._state_lock = threading.RLock()
        app._listener_lock = threading.RLock()
        app._order_update_listeners = []

        app.openOrder(102, Contract(), Order(), OrderState())
        app.openOrderEnd()

        self.assertEqual(["102"], [item["orderId"] for item in app._pending_requests[1].items])

    def test_place_order_seeds_local_order_object_for_immediate_modify(self):
        app = ib_gateway._IBGatewayApp.__new__(ib_gateway._IBGatewayApp)
        app._state_lock = threading.RLock()
        app._open_order_objects = {}
        app.connect_and_start = lambda timeout=1: True
        app.status = lambda: {"ready": True, "connected": True}
        placed = []

        def place_order(order_id, contract, order):
            placed.append((order_id, contract, order))

        app.placeOrder = place_order

        contract = FakeContract()
        order = FakeOrder()
        order.orderId = 456
        order.orderType = "STP"

        ib_gateway._IBGatewayApp.place_order(app, contract, order, timeout=1)

        self.assertEqual([456], [item[0] for item in placed])
        seeded_contract, seeded_order = app.get_order_objects("456")
        self.assertIsNot(seeded_contract, contract)
        self.assertIsNot(seeded_order, order)
        self.assertEqual("STP", seeded_order.orderType)

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

    def test_modify_order_normalizes_overnight_tif_before_resubmit(self):
        class FakeOvernightModifyClient:
            def __init__(self):
                self.contract = FakeContract()
                self.order = FakeOrder()
                self.order.orderId = 11292
                self.order.orderType = "LMT"
                self.order.tif = "OVERNIGHT + DAY"
                self.order.includeOvernight = False
                self.order.outsideRth = False
                self.placed_orders = []

            def get_order_objects(self, order_id):
                return self.contract, self.order

            def clear_order_error(self, order_id):
                return None

            def place_order(self, contract, order):
                self.placed_orders.append((contract, order))

            def await_order_submission(self, order_id, timeout=3.0, poll_interval=0.2):
                return {"ok": True, "order": {"orderId": str(order_id), "status": "Submitted"}}

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeOvernightModifyClient()

        result = ib_gateway.BrokerAdapter.modify_order(adapter, "11292", {"tif": "OVERNIGHT + DAY"})

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(adapter.client.placed_orders))
        modified_order = adapter.client.placed_orders[0][1]
        self.assertEqual("DAY", modified_order.tif)
        self.assertTrue(modified_order.includeOvernight)
        self.assertTrue(modified_order.outsideRth)
        self.assertEqual("DAY", result["session_flags"]["tif"])
        self.assertTrue(result["session_flags"]["include_overnight"])

    def test_modify_order_refreshes_open_orders_when_object_missing(self):
        class FakeRefreshClient:
            def __init__(self):
                self.contract = None
                self.order = None
                self.refresh_calls = 0
                self.placed_orders = []

            def get_order_objects(self, order_id):
                return self.contract, self.order

            def request_open_orders_for_order_confirmation(self, **kwargs):
                self.refresh_calls += 1
                self.contract = FakeContract()
                self.order = FakeOrder()
                self.order.orderId = 777
                self.order.orderType = "STP"
                return [self.get_order_snapshot("777")]

            def clear_order_error(self, order_id):
                return None

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
                    "auxPrice": getattr(self.order, "auxPrice", 0.0) if self.order else 0.0,
                }

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeRefreshClient()

        result = ib_gateway.BrokerAdapter.modify_order(adapter, "777", {"auxPrice": 91.234})

        self.assertTrue(result["ok"])
        self.assertEqual(1, adapter.client.refresh_calls)
        self.assertEqual(91.23, adapter.client.order.auxPrice)
        self.assertEqual(1, len(adapter.client.placed_orders))

    def test_modify_order_returns_pending_when_price_confirmation_lags(self):
        class FakePendingClient:
            def __init__(self):
                self.contract = FakeContract()
                self.order = FakeOrder()
                self.order.orderId = 888
                self.order.orderType = "STP"

            def get_order_objects(self, order_id):
                return self.contract, self.order

            def clear_order_error(self, order_id):
                return None

            def place_order(self, contract, order):
                return None

            def await_order_submission(self, order_id, timeout=3.0, poll_interval=0.2):
                return {"ok": True, "order": {"orderId": str(order_id), "status": "Submitted"}}

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakePendingClient()

        with mock.patch.object(
            adapter,
            "await_order_price_update",
            return_value={"ok": False, "error": "order_modify_price_unconfirmed", "order_id": "888"},
        ):
            result = ib_gateway.BrokerAdapter.modify_order(adapter, "888", {"auxPrice": 90.12})

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending_confirmation"])
        self.assertEqual("order_modify_price_unconfirmed", result["warning"])

    def test_modify_order_returns_pending_when_submit_confirmation_lags(self):
        class FakeSubmitLagClient:
            def __init__(self):
                self.contract = FakeContract()
                self.order = FakeOrder()
                self.order.orderId = 889
                self.order.orderType = "STP"
                self.placed_orders = []

            def get_order_objects(self, order_id):
                return self.contract, self.order

            def clear_order_error(self, order_id):
                return None

            def place_order(self, contract, order):
                self.placed_orders.append((contract, order))

            def await_order_submission(self, order_id, timeout=3.0, poll_interval=0.2):
                return {"ok": False, "error": "order_submission_unconfirmed", "order_id": str(order_id)}

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeSubmitLagClient()

        with mock.patch.object(
            adapter,
            "await_order_price_update",
            return_value={"ok": False, "error": "order_modify_price_unconfirmed", "order_id": "889"},
        ):
            result = ib_gateway.BrokerAdapter.modify_order(adapter, "889", {"auxPrice": 90.12})

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending_confirmation"])
        self.assertTrue(result["entry_submission_unconfirmed"])
        self.assertEqual("order_modify_price_unconfirmed", result["warning"])
        self.assertEqual(1, len(adapter.client.placed_orders))

    def test_modify_order_confirms_price_after_submit_confirmation_lags(self):
        class FakeSubmitLagClient:
            def __init__(self):
                self.contract = FakeContract()
                self.order = FakeOrder()
                self.order.orderId = 890
                self.order.orderType = "STP"

            def get_order_objects(self, order_id):
                return self.contract, self.order

            def clear_order_error(self, order_id):
                return None

            def place_order(self, contract, order):
                return None

            def await_order_submission(self, order_id, timeout=3.0, poll_interval=0.2):
                return {"ok": False, "error": "order_submission_unconfirmed", "order_id": str(order_id)}

        adapter = ib_gateway.BrokerAdapter.__new__(ib_gateway.BrokerAdapter)
        adapter.client = FakeSubmitLagClient()

        with mock.patch.object(
            adapter,
            "await_order_price_update",
            return_value={"ok": True, "order": {"orderId": "890", "auxPrice": 90.12}},
        ):
            result = ib_gateway.BrokerAdapter.modify_order(adapter, "890", {"auxPrice": 90.12})

        self.assertTrue(result["ok"])
        self.assertTrue(result["entry_submission_unconfirmed"])
        self.assertEqual(90.12, result["order"]["auxPrice"])

    def test_account_summary_request_limit_message_is_detected(self):
        self.assertTrue(
            ib_gateway._account_summary_request_limit_message(
                "Error processing request.-'Q' : cause - Maximum number of account summary requests exceeded; "
                "desubscribe to previous request first"
            )
        )

    def test_ib_unset_double_price_is_treated_as_missing(self):
        self.assertEqual(0.0, ib_gateway._safe_float("1.7976931348623157e+308", 0.0))
        self.assertEqual(0.0, ib_gateway._safe_float(float("inf"), 0.0))

    def test_exec_details_does_not_aggregate_reused_order_id_across_contracts(self):
        def obj(**kwargs):
            return type("Obj", (), kwargs)()

        app = ib_gateway._IBGatewayApp.__new__(ib_gateway._IBGatewayApp)
        app._pending_requests = {}
        app._open_orders = {}
        app._executions = {}
        app._commission_reports = {}
        app._state_lock = threading.RLock()
        app._listener_lock = threading.RLock()
        app._order_update_listeners = []
        app._execution_fill_listeners = []

        cvna_contract = obj(conId=274144952, symbol="CVNA", exchange="NYSE", secType="STK", multiplier=0)
        cvna_exec = obj(
            execId="old-cvna-fill",
            orderId="11385",
            permId="1",
            clientId=31,
            orderRef="sl_BATS_CVNA_short_20260615_1042_2_mr_sdUpper",
            side="BOT",
            shares=71,
            price=70.45577464788732,
            time="20260615 11:09:33 US/Eastern",
            acctNumber="DUQ051640",
            exchange="NYSE",
        )
        app.execDetails(-1, cvna_contract, cvna_exec)
        app.commissionReport(obj(execId="old-cvna-fill", commission=1.000213, currency="USD", realizedPNL=-76.36579))

        hood_contract = obj(conId=504546674, symbol="HOOD", exchange="NASDAQ", secType="STK", multiplier=0)
        hood_exec = obj(
            execId="new-hood-fill",
            orderId="11385",
            permId="2",
            clientId=31,
            orderRef="sl_BATS_HOOD_short_20260616_0946_2_mr_sdUpper",
            side="BOT",
            shares=50,
            price=98.97,
            time="20260616 09:48:24 US/Eastern",
            acctNumber="DUQ051640",
            exchange="NASDAQ",
        )
        app.execDetails(-1, hood_contract, hood_exec)
        hood_order = app._open_orders["11385"]

        self.assertEqual("HOOD", hood_order["ticker"])
        self.assertEqual(50, hood_order["filledQuantity"])
        self.assertAlmostEqual(98.97, hood_order["avgPrice"])

        app.commissionReport(obj(execId="new-hood-fill", commission=2.000363, currency="USD", realizedPNL=11.0))
        hood_order = app._open_orders["11385"]
        self.assertEqual(50, hood_order["filledQuantity"])
        self.assertAlmostEqual(98.97, hood_order["avgPrice"])
        self.assertAlmostEqual(2.000363, hood_order["commission"])
        self.assertIn("old-cvna-fill", app._executions)
        self.assertIn("new-hood-fill", app._executions)


class IBGatewayOrderSubmissionWarningTest(unittest.TestCase):
    @staticmethod
    def _client(*, order_errors=None, open_orders=None):
        client = ib_gateway._IBGatewayApp.__new__(ib_gateway._IBGatewayApp)
        client._order_errors = dict(order_errors or {})
        client._open_orders = dict(open_orders or {})
        return client

    def test_await_order_submissions_confirms_code_399_warning_when_visible(self):
        warning = {
            "order_id": "101",
            "code": 399,
            "message": (
                "Order Message:\nBUY 33 AAPL NASDAQ.NMS\n"
                "Warning: Your order will not be placed at the exchange until "
                "2026-06-08 09:30:00 US/Eastern."
            ),
        }
        client = self._client(order_errors={"101": warning})
        client.request_open_orders = lambda timeout=1, force=False: [{"orderId": "101", "status": "PreSubmitted"}]

        result = ib_gateway._IBGatewayApp.await_order_submissions(
            client,
            ["101"],
            timeout=0.5,
            poll_interval=0.01,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("open_orders", result["orders"]["101"]["source"])
        self.assertEqual([warning], result["orders"]["101"]["ignored_warnings"])
        self.assertNotIn("101", client._order_errors)

    def test_await_order_submissions_uses_burst_tolerant_open_order_timeout(self):
        client = self._client()
        calls = []

        def request_open_orders(timeout=1, force=False):
            calls.append({"timeout": timeout, "force": force})
            return [{"orderId": "101", "status": "PreSubmitted"}]

        client.request_open_orders = request_open_orders

        result = ib_gateway._IBGatewayApp.await_order_submissions(
            client,
            ["101"],
            timeout=30,
            poll_interval=0.01,
        )

        self.assertTrue(result["ok"])
        self.assertEqual([{"timeout": 30, "force": True}], calls)

    def test_await_order_submissions_still_fails_true_rejection(self):
        rejection = {"order_id": "101", "code": 201, "message": "Order rejected - reason:Invalid Price"}
        client = self._client(
            order_errors={"101": rejection},
            open_orders={"101": {"orderId": "101", "status": "Submitted"}},
        )
        client.request_open_orders = lambda timeout=1, force=False: []

        result = ib_gateway._IBGatewayApp.await_order_submissions(
            client,
            ["101"],
            timeout=0.5,
            poll_interval=0.01,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(rejection, result["failures"]["101"]["details"])
        self.assertIn("Invalid Price", result["error"])

    def test_error_callback_treats_deferred_exchange_warning_as_benign(self):
        client = ib_gateway._IBGatewayApp.__new__(ib_gateway._IBGatewayApp)
        client._state_lock = threading.RLock()
        client._pending_requests = {}
        client._account_updates_expected_unsubscribe_until = 0.0
        client._recent_errors = []
        client._order_errors = {}
        client._ready = True
        client._status_code = 200
        client._next_order_id = 1
        message = (
            "Order Message:\nBUY 33 AAPL NASDAQ.NMS\n"
            "Warning: Your order will not be placed at the exchange until "
            "2026-06-08 09:30:00 US/Eastern."
        )

        with mock.patch.object(ib_gateway, "record_broker_error") as record_broker_error:
            ib_gateway._IBGatewayApp.error(client, 101, 399, message, "")

        self.assertEqual("benign", record_broker_error.call_args.kwargs["severity"])
        self.assertNotIn("101", client._order_errors)

    def test_error_callback_treats_cancel_notice_as_benign_but_keeps_confirmation_detail(self):
        client = ib_gateway._IBGatewayApp.__new__(ib_gateway._IBGatewayApp)
        client._state_lock = threading.RLock()
        client._pending_requests = {}
        client._account_updates_expected_unsubscribe_until = 0.0
        client._recent_errors = []
        client._order_errors = {}
        client._ready = True
        client._status_code = 200
        client._next_order_id = 1
        message = "OrderId 86 that needs to be cancelled cannot be cancelled, state: Cancelled."

        with mock.patch.object(ib_gateway, "record_broker_error") as record_broker_error:
            ib_gateway._IBGatewayApp.error(client, 86, 10148, message, "")

        self.assertEqual("benign", record_broker_error.call_args.kwargs["severity"])
        self.assertEqual(10148, client._order_errors["86"]["code"])

    def test_error_callback_treats_market_data_cancel_missing_as_benign(self):
        client = ib_gateway._IBGatewayApp.__new__(ib_gateway._IBGatewayApp)
        client._state_lock = threading.RLock()
        client._pending_requests = {}
        client._account_updates_expected_unsubscribe_until = 0.0
        client._recent_errors = []
        client._order_errors = {}
        client._ready = True
        client._status_code = 200
        client._next_order_id = 1
        message = "Can't find EId with tickerId:50164"

        with mock.patch.object(ib_gateway, "record_broker_error") as record_broker_error:
            ib_gateway._IBGatewayApp.error(client, 50164, 300, message, "")

        self.assertEqual("benign", record_broker_error.call_args.kwargs["severity"])
        self.assertNotIn("50164", client._order_errors)


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
        self.assertEqual("LMT", broker.calls[0]["order_type"])
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

    def test_unlinked_market_close_does_not_self_link_close_reference(self):
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
            source="manual_close",
            position_snapshot={"market_price": 101.5},
        )

        self.assertTrue(result["ok"])
        close_row = pb_client.upserts[-1]
        self.assertEqual("close_NFLX_20260506_101500", close_row["unique_id"])
        self.assertEqual("", close_row["trade_group_id"])
        self.assertEqual("", close_row["entry_order_unique_id"])
        self.assertEqual("", close_row["parent_order_unique_id"])
        self.assertEqual("", close_row["extra"]["linked_trade_group_id"])
        self.assertEqual("", close_row["extra"]["linked_entry_order_unique_id"])

    def test_overnight_close_defaults_to_smart_include_overnight(self):
        pb_client = FakeOrderPBClient()
        broker = FakeMarketCloseBroker()
        placer = OrderPlacer(pb_client=pb_client, broker=broker, account_id="DU123")

        result = placer.place_market_close(
            conid=272110,
            symbol="MSTR",
            direction="short",
            quantity=39,
            session_override="overnight",
            position_snapshot={"bid": 126.1, "ask": 126.2, "market_price": 126.2},
        )

        self.assertTrue(result["ok"])
        self.assertEqual("SMART", broker.calls[0]["exchange"])
        self.assertTrue(broker.calls[0]["include_overnight"])
        self.assertTrue(broker.calls[0]["outside_rth"])
        self.assertEqual("SMART", result["close_execution_plan"]["exchange"])

    def test_auto_close_fetches_fresh_side_quote_before_position_price_fallback(self):
        pb_client = FakeOrderPBClient()
        broker = FakeQuoteMarketCloseBroker(
            {"ok": True, "quote": {"bid": 127.70, "ask": 127.80, "last_price": 127.79}}
        )
        placer = OrderPlacer(pb_client=pb_client, broker=broker, account_id="DU123")

        result = placer.place_market_close(
            conid=272110,
            symbol="MSTR",
            direction="short",
            quantity=39,
            session_override="overnight",
            position_snapshot={"market_price": 126.20},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(broker.snapshots))
        self.assertEqual("SMART", broker.snapshots[0]["exchange"])
        self.assertEqual(129.08, broker.calls[0]["limit_price"])
        self.assertEqual(127.8, result["close_execution_plan"]["reference_price"])
        self.assertEqual("ask", result["close_execution_plan"]["reference_source"])

    def test_unconfirmed_market_close_prewrites_pending_close_mapping(self):
        pb_client = StrictNotifyOrderAndSignalPBClient(
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
            position_snapshot={"market_price": 212.0},
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
        pb_client = StrictNotifyOrderAndSignalPBClient(
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
            position_snapshot={"market_price": 212.0},
        )

        self.assertFalse(result["ok"])
        self.assertEqual([], pb_client.upserts)
        self.assertEqual(1, len(pb_client.system_events))
        self.assertEqual("平仓未执行：券商拒绝或提交失败", pb_client.system_events[0]["title"])
        self.assertEqual("ibkr_close_execution", pb_client.system_events[0]["event_type"])
        self.assertNotIn("category", pb_client.system_events[0])
        self.assertEqual("broker_rejected_order", pb_client.system_events[0]["detail"]["错误"])

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
        self.assertEqual("", result["oca_group"])
        self.assertEqual("bracket_oco", result["order_family_type"])
        self.assertEqual(3, len(pb_client.upserts))
        for upsert in pb_client.upserts:
            self.assertEqual("NFLX_short_20260506_101500", upsert["trade_group_id"])
            self.assertEqual("NFLX_short_20260506_101500", upsert["bracket_group"])
            self.assertEqual("", upsert["oca_group"])
            self.assertEqual("bracket_oco", upsert["order_family_type"])
            self.assertEqual("NFLX_short_20260506_101500", upsert["extra"]["bracket_group"])
            self.assertEqual("", upsert["extra"]["oca_group"])
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

    def test_order_placer_passes_outside_rth_to_broker_and_order_rows(self):
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
            signal_id="sig-aapl-outside-rth",
            outside_rth=True,
            tif="DAY",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["outside_rth"])
        self.assertEqual("DAY", result["tif"])
        self.assertTrue(result["order_extra"]["outside_rth"])
        self.assertTrue(broker.calls[0]["outside_rth"])
        self.assertEqual("DAY", broker.calls[0]["tif"])
        self.assertEqual(3, len(pb_client.upserts))
        self.assertTrue(all(row["extra"]["outside_rth"] for row in pb_client.upserts))

    def test_order_placer_treats_background_confirmation_as_pending_not_incomplete(self):
        pb_client = FakeOrderPBClient()
        broker = FakePendingConfirmBracketBroker()
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
            confirmation_mode="background",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending_confirmation"])
        self.assertTrue(result["protection_confirmation_pending"])
        self.assertFalse(result["protection_incomplete"])
        self.assertEqual("", result["recommended_action"])
        self.assertEqual("background", broker.calls[0]["confirmation_mode"])

    def test_order_placer_logs_protection_repair_rows_without_entry_order(self):
        pb_client = FakeOrderPBClient()
        broker = FakeProtectionRepairBroker()
        placer = OrderPlacer(pb_client=pb_client, broker=broker, account_id="DU123", environment="paper")

        result = placer.place_protection_repair_orders(
            conid=265598,
            symbol="aapl",
            direction="long",
            quantity=5,
            take_profit_price=104.0,
            stop_loss_price=98.0,
            signal_id="SIG_1",
            trade_group_id="group_SIG_1",
            entry_order_unique_id="entry_group_SIG_1",
            order_extra={"missing_protection_auto_repair": True},
            outside_rth=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(broker.calls))
        self.assertEqual("DU123", broker.calls[0]["account_id"])
        self.assertTrue(broker.calls[0]["outside_rth"])
        self.assertEqual(2, len(pb_client.upserts))
        tp_row, sl_row = pb_client.upserts
        self.assertEqual("repair_tp", tp_row["role"])
        self.assertEqual("repair_sl", sl_row["role"])
        self.assertEqual("TakeProfit", tp_row["order_type"])
        self.assertEqual("StopLoss", sl_row["order_type"])
        self.assertEqual("Submitted", tp_row["status"])
        self.assertEqual("Submitted", sl_row["status"])
        self.assertEqual("active", tp_row["relation_status"])
        self.assertEqual("active", sl_row["relation_status"])
        self.assertEqual("group_SIG_1", tp_row["trade_group_id"])
        self.assertEqual("entry_group_SIG_1", tp_row["entry_order_unique_id"])
        self.assertEqual("entry_group_SIG_1", tp_row["parent_order_unique_id"])
        self.assertEqual("repair_sl_group_SIG_1", tp_row["sibling_order_unique_id"])
        self.assertEqual("repair_tp_group_SIG_1", sl_row["sibling_order_unique_id"])
        self.assertEqual("501", tp_row["broker_order_id"])
        self.assertEqual("502", sl_row["broker_order_id"])
        self.assertTrue(tp_row["extra"]["missing_protection_auto_repair"])
        self.assertTrue(sl_row["extra"]["missing_protection_auto_repair"])
        self.assertTrue(tp_row["extra"]["protection_repair"])
        self.assertTrue(sl_row["extra"]["protection_repair"])

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
            "tp_sl_model": "structure_first_atr_buffer_v1",
            "rr_basis": "structure_stop_to_structure_target",
            "atr_role": "buffer_and_filter_only",
            "structure_stop_available": True,
            "structure_target_available": True,
            "target_is_structure": True,
            "structure_stop_source": "support_with_atr_buffer",
            "structure_target_source": "next_resistance_structure",
            "structure_stop_loss": 98.0,
            "structure_take_profit": 104.0,
            "structure_risk_per_share": 2.0,
            "structure_reward_per_share": 4.0,
            "structure_reward_risk": 2.0,
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

    def test_order_flow_does_not_worsen_structural_anchor_entry(self):
        signal = self._signal("AAPL")
        signal.update({"entry": 99.5, "stop_loss": 97.5, "take_profit": 103.5})
        signal["extra"] = {
            **signal["extra"],
            "entry_price_plan": "structural_anchor_limit",
            "entry_limit_intent": "structural_anchor",
            "planned_entry_price": 99.5,
            "reference_entry": 100.0,
        }
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=FakeSignalPBClient({"id": "row-aapl"}))

        adjusted = service._apply_order_flow_entry_decision(
            signal,
            {
                "enforced": True,
                "marketable_limit": {"ok": True, "price": 100.08, "order_type": "marketable_limit"},
            },
        )

        self.assertEqual(99.5, adjusted["entry"])
        self.assertEqual(97.5, adjusted["stop_loss"])
        self.assertEqual(103.5, adjusted["take_profit"])
        self.assertEqual("structural_anchor", adjusted["extra"]["entry_limit_intent"])
        self.assertEqual("structural_anchor_limit", adjusted["extra"]["entry_price_plan"])
        self.assertTrue(adjusted["extra"]["order_flow_reprice_blocked"])
        self.assertEqual("structural_anchor_worse_than_planned", adjusted["extra"]["order_flow_reprice_block_reason"])

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
        self.assertEqual("background", order_payload["confirmation_mode"])
        self.assertEqual("submitted_waiting_fill", pb.acks[-1]["status"])
        ack_extra = pb.acks[-1]["order"]["extra"]
        self.assertTrue(ack_extra["tv_direct_entry"])
        self.assertEqual("bounded_marketable", ack_extra["entry_limit_intent"])
        self.assertEqual(100.0, ack_extra["reference_entry"])
        self.assertEqual(100.15, ack_extra["submitted_entry_limit_price"])
        self.assertTrue(ack_extra["final_protection_from_fill"])
        self.assertEqual("structure_first_atr_buffer_v1", ack_extra["tp_sl_model"])
        self.assertEqual("buffer_and_filter_only", ack_extra["atr_role"])
        self.assertEqual("structure_stop_to_structure_target", ack_extra["rr_basis"])
        self.assertTrue(ack_extra["target_is_structure"])
        self.assertEqual(2.0, ack_extra["structure_reward_risk"])
        self.assertEqual(98.0, ack_extra["structure_stop_loss"])
        self.assertEqual(104.0, ack_extra["structure_take_profit"])

    def test_tv_direct_independent_plan_metadata_stays_on_single_leg_bracket(self):
        signal = self._tv_signal("AAPL")
        plan_id = "AAPL_long_20260611_0945_2_mr_sdLower"
        leg_group = f"{plan_id}_leg2"
        signal["extra"] = {
            **signal["extra"],
            "trade_group_id": leg_group,
            "bracket_group": leg_group,
            "scale_plan_enabled": True,
            "plan_type": "independent_two_leg",
            "plan_id": plan_id,
            "leg_index": 2,
            "leg_trigger": "dtp_retest",
            "max_leg_notional": 5000,
            "max_leg_risk": 75,
            "max_plan_risk": 150,
            "independent_legs": True,
            "cross_leg_protection_sync": False,
        }
        signal["raw"]["extra"] = dict(signal["extra"])
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"entry_pre_submit_guard_enabled": "true"}),
        )
        service.order_placer = FakeBracketBroker()

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        order_payload = service.order_placer.calls[0]
        self.assertEqual(leg_group, order_payload["trade_group_id"])
        self.assertEqual(leg_group, order_payload["bracket_group"])
        order_extra = order_payload["order_extra"]
        self.assertEqual(plan_id, order_extra["plan_id"])
        self.assertEqual("independent_two_leg", order_extra["plan_type"])
        self.assertEqual(2, order_extra["leg_index"])
        self.assertEqual(2, order_extra["leg_count"])
        self.assertEqual("secondary", order_extra["leg_role"])
        self.assertEqual("dtp_retest", order_extra["leg_trigger"])
        self.assertEqual(leg_group, order_extra["leg_trade_group_id"])
        self.assertEqual(5000.0, order_extra["max_leg_notional"])
        self.assertEqual(75.0, order_extra["max_leg_risk"])
        self.assertEqual(150.0, order_extra["max_plan_risk"])
        self.assertEqual(150.0, order_extra["plan_risk_budget_used"])
        self.assertTrue(order_extra["plan_risk_budget_ok"])
        self.assertTrue(order_extra["independent_legs"])
        self.assertFalse(order_extra["cross_leg_protection_sync"])
        self.assertFalse(order_extra["aggregate_position_management"])
        self.assertEqual("independent_bracket", order_extra["leg_order_mode"])

        ack = pb.acks[-1]
        ack_extra = ack["order"]["extra"]
        self.assertEqual(plan_id, ack_extra["plan_id"])
        self.assertEqual(2, ack_extra["leg_index"])
        self.assertEqual(leg_group, ack_extra["leg_trade_group_id"])
        self.assertTrue(ack_extra["independent_legs"])
        self.assertFalse(ack_extra["cross_leg_protection_sync"])
        self.assertEqual(2, len(ack["child_orders"]))
        for child_order in ack["child_orders"]:
            self.assertEqual(leg_group, child_order["trade_group_id"])
            self.assertEqual(plan_id, child_order["extra"]["plan_id"])
            self.assertEqual(2, child_order["extra"]["leg_index"])
            self.assertFalse(child_order["extra"]["cross_leg_protection_sync"])

    def test_tv_direct_structural_anchor_preserves_planned_limit_without_market_cap(self):
        signal = self._tv_signal("AAPL")
        signal.update({"entry": 99.5, "stop_loss": 97.5, "take_profit": 103.5})
        signal["extra"] = {
            **signal["extra"],
            "entry_price_plan": "structural_anchor_limit",
            "entry_limit_intent": "structural_anchor",
            "entry_anchor_mode": "setup_structural",
            "entry_anchor_reason": "前低支撑 + EMA20支撑",
            "planned_entry_price": 99.5,
            "submitted_entry_limit_price": 99.5,
            "reference_entry": 100.0,
        }
        signal["raw"]["extra"] = dict(signal["extra"])
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"tv_entry_limit_cap_bps": 15, "entry_pre_submit_guard_enabled": "true"}),
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        order_payload = service.order_placer.calls[0]
        self.assertEqual(99.5, order_payload["entry_price"])
        self.assertEqual(97.5, order_payload["stop_loss_price"])
        self.assertEqual(103.5, order_payload["take_profit_price"])
        ack_extra = pb.acks[-1]["order"]["extra"]
        self.assertEqual("structural_anchor", ack_extra["entry_limit_intent"])
        self.assertEqual("structural_anchor_limit", ack_extra["entry_price_plan"])
        self.assertEqual("freshness_then_structural_anchor_guard", ack_extra["tv_direct_validation_policy"])
        self.assertEqual(100.0, ack_extra["reference_entry"])
        self.assertEqual(99.5, ack_extra["planned_entry_price"])
        self.assertEqual(99.5, ack_extra["submitted_entry_limit_price"])
        self.assertFalse(ack_extra["submitted_limit_cap_applied"])

    def test_tv_direct_background_confirmation_is_submitted_not_protection_incomplete(self):
        signal = self._tv_signal("AAPL")
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"entry_pre_submit_guard_enabled": "true"}),
        )
        service.order_placer = FakePendingAsyncOrderPlacer()

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        self.assertEqual("background", service.order_placer.calls[0]["confirmation_mode"])
        self.assertEqual("submitted_waiting_fill", pb.acks[-1]["status"])
        ack_extra = pb.acks[-1]["order"]["extra"]
        self.assertTrue(ack_extra["protection_pending_async"])
        self.assertTrue(ack_extra["protection_confirmation_pending"])
        self.assertFalse(ack_extra["protection_incomplete"])
        self.assertEqual("submitted_waiting_fill", ack_extra["status_reason"])

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

    def test_tv_direct_bounded_limit_uses_extended_freshness_window(self):
        signal = self._tv_signal("AAPL", age_sec=238.0)
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"tv_entry_freshness_sec": 120, "tv_entry_limit_freshness_sec": 240}),
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        ack_extra = pb.acks[-1]["order"]["extra"]
        self.assertEqual("bounded_limit_cap", ack_extra["signal_freshness_policy"])
        self.assertEqual(240.0, ack_extra["signal_freshness_max_age_s"])
        self.assertLess(ack_extra["signal_freshness_age_s"], 240.0)

    def test_tv_direct_bounded_limit_expires_after_extended_freshness_window(self):
        signal = self._tv_signal("AAPL", age_sec=241.0)
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"tv_entry_freshness_sec": 120, "tv_entry_limit_freshness_sec": 240}),
        )

        service._process_signals()

        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        self.assertEqual("expired", _broker_execution(patch)["status"])
        self.assertEqual("stale_signal", patch["extra"]["status_reason"])
        self.assertEqual(240.0, patch["extra"]["signal_freshness_max_age_s"])
        self.assertEqual("bounded_limit_cap", patch["extra"]["signal_freshness_policy"])

    def test_tv_direct_structural_anchor_keeps_default_freshness_window(self):
        signal = self._tv_signal("AAPL", age_sec=130.0)
        signal["entry_price_plan"] = "structural_anchor_limit"
        signal["extra"]["entry_price_plan"] = "structural_anchor_limit"
        signal["raw"]["entry_price_plan"] = "structural_anchor_limit"
        signal["raw"]["extra"] = dict(signal["extra"])
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"tv_entry_freshness_sec": 120, "tv_entry_limit_freshness_sec": 240}),
        )

        service._process_signals()

        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        self.assertEqual("expired", _broker_execution(patch)["status"])
        self.assertEqual("default", patch["extra"]["signal_freshness_policy"])
        self.assertEqual(120.0, patch["extra"]["signal_freshness_max_age_s"])

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

    def test_entry_guard_rejects_structural_anchor_long_when_ask_above_plan(self):
        signal = self._signal("AAPL")
        signal.update({"entry": 99.5, "stop_loss": 97.5, "take_profit": 103.5})
        signal["extra"] = {
            **signal["extra"],
            "entry_price_plan": "structural_anchor_limit",
            "entry_limit_intent": "structural_anchor",
            "planned_entry_price": 99.5,
            "reference_entry": 100.0,
        }
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
        quote_book = FakeQuoteBook(
            {
                "AAPL": {
                    "last_price": 100.0,
                    "bid": 99.95,
                    "ask": 100.05,
                    "quote_age_s": 1.0,
                }
            }
        )
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=pb, quote_book=quote_book)

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        extra = patch["extra"]
        self.assertEqual("entry_structural_price_not_reached", extra["status_reason"])
        self.assertTrue(extra["structural_anchor_guard"])
        self.assertFalse(extra["structural_anchor_price_reached"])
        self.assertEqual("structural_anchor_limit", extra["entry_price_plan"])

    def test_entry_guard_allows_structural_anchor_short_when_bid_reaches_plan_without_repricing_protection(self):
        signal = self._signal("MSFT")
        signal.update(
            {
                "direction": "short",
                "entry": 101.0,
                "stop_loss": 103.0,
                "take_profit": 97.0,
            }
        )
        signal["extra"] = {
            **signal["extra"],
            "entry_price_plan": "structural_anchor_limit",
            "entry_limit_intent": "structural_anchor",
            "planned_entry_price": 101.0,
            "reference_entry": 100.0,
        }
        pb = FakeSignalPBClient({"id": "row-msft", "extra": dict(signal["extra"])})
        quote_book = FakeQuoteBook(
            {
                "MSFT": {
                    "last_price": 101.1,
                    "bid": 101.0,
                    "ask": 101.2,
                    "quote_age_s": 1.0,
                }
            }
        )
        service = FakeSignalService(signal, lifecycle=FakeLifecycle(), pb=pb, quote_book=quote_book)

        service._process_signals()

        self.assertEqual(["sig-msft"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        order_payload = service.order_placer.calls[0]
        self.assertEqual(101.0, order_payload["entry_price"])
        self.assertEqual(103.0, order_payload["stop_loss_price"])
        self.assertEqual(97.0, order_payload["take_profit_price"])
        ack_order = pb.acks[-1]["order"]
        self.assertEqual("structural_anchor", ack_order["extra"]["entry_limit_intent"])
        self.assertEqual("structural_anchor_limit", ack_order["extra"]["entry_price_plan"])
        self.assertTrue(ack_order["extra"]["structural_anchor_guard"])
        self.assertTrue(ack_order["extra"]["structural_anchor_price_reached"])
        self.assertEqual("structural_anchor_guard", ack_order["extra"]["reprice_source"])

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

    def test_entry_guard_allows_missing_quote_for_bounded_limit_cap_signal(self):
        signal = self._signal("AAPL")
        signal["extra"] = {
            **signal["extra"],
            "entry_limit_intent": "bounded_marketable",
            "submitted_limit_cap_applied": True,
        }
        signal["raw"]["extra"] = dict(signal["extra"])
        pb = FakeSignalPBClient({"id": "row-aapl", "extra": dict(signal["extra"])})
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
        self.assertEqual(1, len(service.order_placer.calls))
        self.assertEqual([123], ws_client.subscribed)
        self.assertEqual("AAPL", ws_client.subscribed_meta[-1]["symbol"])
        self.assertEqual("entry_pre_submit", ws_client.subscribed_meta[-1]["kind"])
        self.assertEqual(1, len(ws_client.snapshots))
        ack_extra = pb.acks[-1]["order"]["extra"]
        self.assertEqual("unavailable_allowed_by_limit_cap", ack_extra["quote_guard_status"])
        self.assertTrue(ack_extra["quote_guard_missing_quote_allowed"])
        self.assertEqual("bounded_limit_cap", ack_extra["quote_guard_missing_quote_allow_reason"])

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

    def test_buying_power_guard_uses_local_baseline_when_snapshot_unavailable(self):
        signal = self._signal("AAPL")
        signal["shares"] = 50
        pb = FakeSignalStatePBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        store = BuyingPowerReservationStore(pb, environment="paper")
        fetched_at = datetime.now(timezone.utc).isoformat()
        store.update_baseline_from_snapshot(
            {
                "ok": True,
                "environment": "paper",
                "summary": {"buying_power": 20000.0, "net_liquidation": 100000.0},
                "buying_power_guard": {"available": True, "state": "ok", "source": "account_summary"},
                "fetched_at": fetched_at,
                "source": "account_summary",
            }
        )
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"entry_pre_submit_guard_enabled": "false"}),
        )
        service.buying_power_reservations = store
        service.account_snapshot_provider = mock.Mock(side_effect=AssertionError("baseline should avoid snapshot fetch"))

        service._process_signals()

        service.account_snapshot_provider.assert_not_called()
        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        ack_extra = pb.acks[-1]["order"]["extra"]
        self.assertEqual("local_baseline", ack_extra["buying_power_guard"]["source"])
        self.assertEqual(20000.0, ack_extra["buying_power_remaining"])
        self.assertEqual(15000.0, ack_extra["buying_power_remaining_after"])
        self.assertEqual(fetched_at, ack_extra["buying_power_baseline_fetched_at"])

    def test_buying_power_guard_rejects_stale_local_baseline(self):
        signal = self._signal("AAPL")
        signal["shares"] = 50
        pb = FakeSignalStatePBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        store = BuyingPowerReservationStore(pb, environment="paper")
        store.update_baseline_from_snapshot(
            {
                "ok": True,
                "environment": "paper",
                "summary": {"buying_power": 20000.0, "net_liquidation": 100000.0},
                "buying_power_guard": {"available": True, "state": "ok", "source": "account_summary"},
                "fetched_at": "2026-01-01T00:00:00+00:00",
                "source": "account_summary",
            }
        )
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"entry_pre_submit_guard_enabled": "false"}),
            account_snapshot={
                "ok": False,
                "summary": {},
                "buying_power_guard": {
                    "state": "unavailable",
                    "reason": "account_data_circuit_open",
                    "available": False,
                    "source": "account_data_circuit",
                },
            },
        )
        service.buying_power_reservations = store

        service._process_signals()

        self.assertEqual([], service.signal_router.processed)
        self.assertEqual(["sig-aapl"], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        guard = patch["extra"]["buying_power_guard"]
        self.assertEqual("unavailable", guard["state"])
        self.assertEqual("buying_power_snapshot_stale", guard["reason"])
        self.assertFalse(guard["snapshot_fresh"])
        self.assertEqual(180.0, guard["snapshot_max_age_s"])

    def test_buying_power_guard_allows_safe_stale_snapshot_as_warning(self):
        signal = self._signal("AAPL")
        signal["shares"] = 50
        stale_fetched_at = datetime.fromtimestamp(time.time() - 300, timezone.utc).isoformat()
        pb = FakeSignalStatePBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig(
                {
                    "entry_pre_submit_guard_enabled": "false",
                    "ibkr_buying_power_max_snapshot_age_sec": 180,
                    "ibkr_buying_power_stale_safe_enabled": "true",
                    "ibkr_buying_power_stale_safe_max_age_sec": 1800,
                    "ibkr_buying_power_stale_safe_min_usd": 50000,
                }
            ),
            account_snapshot={
                "ok": True,
                "summary": {"buying_power": 300000.0, "net_liquidation": 300000.0},
                "buying_power_guard": {"available": True, "state": "ok", "source": "account_summary"},
                "fetched_at": stale_fetched_at,
                "source": "account_summary",
            },
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        ack_extra = pb.acks[-1]["order"]["extra"]
        guard = ack_extra["buying_power_guard"]
        self.assertEqual("warning", guard["state"])
        self.assertEqual("buying_power_snapshot_stale_allowed_safe", guard["reason"])
        self.assertTrue(guard["snapshot_stale_allowed"])
        self.assertEqual("buying_power_snapshot_stale", guard["original_freshness_block_reason"])
        self.assertEqual(295000.0, ack_extra["buying_power_remaining_after"])

    def test_buying_power_guard_force_refreshes_too_old_snapshot_before_blocking(self):
        signal = self._signal("AAPL")
        signal["shares"] = 50
        stale_fetched_at = datetime.fromtimestamp(time.time() - 4000, timezone.utc).isoformat()
        fresh_fetched_at = datetime.now(timezone.utc).isoformat()
        pb = FakeSignalStatePBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig(
                {
                    "entry_pre_submit_guard_enabled": "false",
                    "ibkr_buying_power_max_snapshot_age_sec": 180,
                    "ibkr_buying_power_stale_safe_enabled": "true",
                    "ibkr_buying_power_stale_safe_max_age_sec": 1800,
                }
            ),
            account_snapshot={
                "ok": True,
                "summary": {"buying_power": 300000.0, "net_liquidation": 300000.0},
                "buying_power_guard": {"available": True, "state": "ok", "source": "account_summary"},
                "fetched_at": stale_fetched_at,
                "source": "account_summary",
            },
        )
        service.account_snapshot_provider = mock.Mock(
            side_effect=[
                {
                    "ok": True,
                    "summary": {"buying_power": 300000.0, "net_liquidation": 300000.0},
                    "buying_power_guard": {"available": True, "state": "ok", "source": "account_summary"},
                    "fetched_at": stale_fetched_at,
                    "source": "account_summary",
                },
                {
                    "ok": True,
                    "summary": {"buying_power": 300000.0, "net_liquidation": 300000.0},
                    "buying_power_guard": {"available": True, "state": "ok", "source": "account_summary"},
                    "fetched_at": fresh_fetched_at,
                    "source": "account_summary",
                },
            ]
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        self.assertEqual(2, service.account_snapshot_provider.call_count)
        self.assertEqual({"force_refresh": True}, service.account_snapshot_provider.call_args_list[-1].kwargs)
        ack_extra = pb.acks[-1]["order"]["extra"]
        guard = ack_extra["buying_power_guard"]
        self.assertEqual("ok", guard["state"])
        self.assertTrue(guard["snapshot_force_refresh_attempted"])
        self.assertEqual("fresh", guard["snapshot_force_refresh_result"])
        self.assertTrue(guard["snapshot_fresh"])
        self.assertEqual(295000.0, ack_extra["buying_power_remaining_after"])

    def test_buying_power_guard_allows_today_ledger_when_refresh_blocked(self):
        signal = self._signal("AAPL")
        signal["shares"] = 50
        stale_fetched_at = datetime.fromtimestamp(time.time() - 300, timezone.utc).isoformat()
        today_text = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
        pb = FakeSignalStatePBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        pb.upserts.append(
            {
                "environment": "paper",
                "role": "entry",
                "status": "Filled",
                "broker_order_id": "old-entry-1",
                "symbol": "MSFT",
                "direction": "long",
                "quantity": 250,
                "filled_qty": 250,
                "fill_price": 100.0,
                "us_time": today_text,
            }
        )
        store = BuyingPowerReservationStore(pb, environment="paper")
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig(
                {
                    "entry_pre_submit_guard_enabled": "false",
                    "ibkr_buying_power_max_snapshot_age_sec": 60,
                    "ibkr_buying_power_stale_safe_enabled": "true",
                    "ibkr_buying_power_stale_safe_max_age_sec": 120,
                    "ibkr_buying_power_stale_safe_min_usd": 50000,
                    "ibkr_buying_power_warn_usd": 15000,
                    "ibkr_buying_power_warn_pct_net_liq": 10,
                }
            ),
        )
        service.buying_power_reservations = store
        service.account_snapshot_provider = mock.Mock(
            side_effect=[
                {
                    "ok": True,
                    "summary": {"buying_power": 45000.0, "net_liquidation": 100000.0},
                    "buying_power_guard": {"available": True, "state": "ok", "source": "account_summary"},
                    "fetched_at": stale_fetched_at,
                    "source": "account_summary",
                },
                {
                    "ok": True,
                    "summary": {"buying_power": 45000.0, "net_liquidation": 100000.0},
                    "buying_power_guard": {
                        "available": True,
                        "state": "ok",
                        "reason": "account_summary_pacing_blocked",
                        "source": "account_summary",
                    },
                    "fetched_at": stale_fetched_at,
                    "source": "account_summary",
                    "stale": True,
                    "hard_blocked": True,
                    "retry_after_s": 45,
                },
            ]
        )

        service._process_signals()

        self.assertEqual(["sig-aapl"], service.signal_router.processed)
        self.assertEqual(1, len(service.order_placer.calls))
        self.assertEqual(2, service.account_snapshot_provider.call_count)
        ack_extra = pb.acks[-1]["order"]["extra"]
        guard = ack_extra["buying_power_guard"]
        self.assertEqual("ledger_safe", guard["state"])
        self.assertEqual("today_ledger_safe", guard["source"])
        self.assertTrue(guard["ledger_safe_used"])
        self.assertEqual(25000.0, guard["ledger_today_open_exposure"])
        self.assertEqual(15000.0, guard["ledger_remaining_after"])
        self.assertTrue(guard["ledger_next_refresh_at"])
        self.assertEqual("自动开仓按今日账本安全放行：账户刷新待重试", pb.events[-1]["title"])
        self.assertIn("下次购买力刷新时间", pb.events[-1]["detail"])

    def test_buying_power_guard_blocks_when_today_ledger_below_floor(self):
        signal = self._signal("AAPL")
        signal["shares"] = 50
        stale_fetched_at = datetime.fromtimestamp(time.time() - 300, timezone.utc).isoformat()
        today_text = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
        pb = FakeSignalStatePBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        pb.upserts.append(
            {
                "environment": "paper",
                "role": "entry",
                "status": "Filled",
                "broker_order_id": "old-entry-1",
                "symbol": "MSFT",
                "direction": "long",
                "quantity": 250,
                "filled_qty": 250,
                "fill_price": 100.0,
                "us_time": today_text,
            }
        )
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig(
                {
                    "entry_pre_submit_guard_enabled": "false",
                    "ibkr_buying_power_max_snapshot_age_sec": 60,
                    "ibkr_buying_power_stale_safe_enabled": "true",
                    "ibkr_buying_power_stale_safe_max_age_sec": 120,
                    "ibkr_buying_power_stale_safe_min_usd": 50000,
                }
            ),
        )
        service.buying_power_reservations = BuyingPowerReservationStore(pb, environment="paper")
        stale_snapshot = {
            "ok": True,
            "summary": {"buying_power": 35000.0, "net_liquidation": 100000.0},
            "buying_power_guard": {"available": True, "state": "ok", "source": "account_summary"},
            "fetched_at": stale_fetched_at,
            "source": "account_summary",
        }
        service.account_snapshot_provider = mock.Mock(side_effect=[stale_snapshot, {**stale_snapshot, "stale": True, "hard_blocked": True}])

        service._process_signals()

        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        guard = patch["extra"]["buying_power_guard"]
        self.assertEqual("blocked", guard["state"])
        self.assertEqual("buying_power_ledger_below_block_threshold", guard["reason"])
        self.assertEqual(5000.0, guard["remaining_after"])
        self.assertEqual("rejected", _broker_execution(patch)["status"])

    def test_buying_power_guard_rejects_ledger_safe_when_snapshot_not_today(self):
        signal = self._signal("AAPL")
        signal["shares"] = 50
        today_text = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
        pb = FakeSignalStatePBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        pb.upserts.append(
            {
                "environment": "paper",
                "role": "entry",
                "status": "Filled",
                "broker_order_id": "old-entry-1",
                "symbol": "MSFT",
                "direction": "long",
                "quantity": 250,
                "filled_qty": 250,
                "fill_price": 100.0,
                "us_time": today_text,
            }
        )
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig(
                {
                    "entry_pre_submit_guard_enabled": "false",
                    "ibkr_buying_power_max_snapshot_age_sec": 60,
                    "ibkr_buying_power_stale_safe_enabled": "true",
                    "ibkr_buying_power_stale_safe_max_age_sec": 120,
                    "ibkr_buying_power_stale_safe_min_usd": 50000,
                }
            ),
        )
        service.buying_power_reservations = BuyingPowerReservationStore(pb, environment="paper")
        old_snapshot = {
            "ok": True,
            "summary": {"buying_power": 45000.0, "net_liquidation": 100000.0},
            "buying_power_guard": {"available": True, "state": "ok", "source": "account_summary"},
            "fetched_at": "2026-01-01T14:30:00+00:00",
            "source": "account_summary",
        }
        service.account_snapshot_provider = mock.Mock(side_effect=[old_snapshot, {**old_snapshot, "stale": True, "hard_blocked": True}])

        service._process_signals()

        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        guard = patch["extra"]["buying_power_guard"]
        self.assertEqual("unavailable", guard["state"])
        self.assertEqual("snapshot_not_today", guard["ledger_safe_block_reason"])
        self.assertEqual("waiting_for_account_snapshot", patch["extra"]["execution_state"])

    def test_buying_power_guard_blocks_without_initial_baseline_when_snapshot_unavailable(self):
        signal = self._signal("AAPL")
        signal["shares"] = 50
        pb = FakeSignalStatePBClient({"id": "row-aapl", "extra": {"source": "ibkr_compute"}})
        service = FakeSignalService(
            signal,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=FakeConfig({"entry_pre_submit_guard_enabled": "false"}),
            account_snapshot={
                "ok": False,
                "summary": {},
                "buying_power_guard": {
                    "state": "unavailable",
                    "reason": "account_data_circuit_open",
                    "available": False,
                    "source": "account_data_circuit",
                },
            },
        )
        service.buying_power_reservations = BuyingPowerReservationStore(pb, environment="paper")

        service._process_signals()

        self.assertEqual([], service.signal_router.processed)
        self.assertEqual(["sig-aapl"], service.signal_router.released)
        self.assertEqual([], service.order_placer.calls)
        patch = pb.updates[-1][2]
        self.assertEqual("waiting_for_account_snapshot", patch["extra"]["execution_state"])
        self.assertEqual("account_data_circuit_open", patch["extra"]["buying_power_guard"]["reason"])

    def test_buying_power_guard_rolls_local_reservations_across_consecutive_signals(self):
        first = self._tv_signal("AAPL")
        second = self._tv_signal("MSFT")
        first["shares"] = second["shares"] = 50
        pb = FakeSignalStatePBClient({"id": "row-aapl", "extra": dict(first["extra"])})
        store = BuyingPowerReservationStore(pb, environment="paper")
        config = FakeConfig({"entry_pre_submit_guard_enabled": "true"})
        service = FakeSignalService(
            first,
            lifecycle=FakeLifecycle(),
            pb=pb,
            config=config,
        )
        service.signal_router = FakeSignalRouter([first, second])
        service.buying_power_reservations = store
        broker = FakeBracketBroker()
        service.order_placer = OrderPlacer(
            pb_client=pb,
            broker=broker,
            account_id="DU123",
            environment="paper",
            config=config,
            reservation_store=store,
        )
        service.account_snapshot_provider = mock.Mock(
            return_value={
                "ok": True,
                "environment": "paper",
                "summary": {"buying_power": 30000.0, "net_liquidation": 100000.0},
                "buying_power_guard": {"available": True, "state": "ok", "source": "account_summary"},
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": "account_summary",
            }
        )

        service._process_signals()

        self.assertEqual(["sig-aapl", "sig-msft"], service.signal_router.processed)
        self.assertEqual(2, len(broker.calls))
        self.assertEqual(1, service.account_snapshot_provider.call_count)
        first_ack = pb.acks[0]["order"]["extra"]
        second_ack = pb.acks[1]["order"]["extra"]
        self.assertEqual(30000.0, first_ack["buying_power_remaining"])
        self.assertEqual(24992.5, first_ack["buying_power_remaining_after"])
        self.assertEqual(24992.5, second_ack["buying_power_remaining"])
        self.assertEqual(19985.0, second_ack["buying_power_remaining_after"])
        self.assertEqual(5007.5, second_ack["buying_power_local_reserved_exposure"])
        self.assertEqual(2, store.snapshot()["count"])

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
