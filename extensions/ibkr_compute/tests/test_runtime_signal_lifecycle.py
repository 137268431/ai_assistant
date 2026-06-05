import copy
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration.runtime_ops import TradingServiceRuntimeOpsMixin
from ibkr_compute.orchestration.signals import TradingServiceSignalsMixin


class _FakePB:
    def __init__(self):
        self.orders = [
            {
                "id": "order-1",
                "unique_id": "entry_SIG_1",
                "entry_order_unique_id": "entry_SIG_1",
                "order_id": "1001",
                "broker_order_id": "1001",
                "signal_id": "SIG_1",
                "environment": "live",
            }
        ]
        self.signals = {
            "sig-row-1": {
                "id": "sig-row-1",
                "signal_id": "SIG_1",
                "symbol": "AAPL",
                "date": "2026-05-06",
                "environment": "live",
                "status": "submitted",
                "entry": 100.0,
                "stop_loss": 99.0,
                "take_profit": 105.0,
                "extra": {"source": "ibkr_compute", "bracket_group": "group_SIG_1"},
            },
            "sig-row-2": {
                "id": "sig-row-2",
                "signal_id": "SIG_2",
                "symbol": "MSFT",
                "date": "2026-05-06",
                "environment": "live",
                "status": "protection_incomplete",
                "extra": {
                    "source": "ibkr_compute",
                    "protection_complete": False,
                    "protection_incomplete": True,
                    "missing_order_ids": ["1005"],
                    "submitted_order_ids": ["1003", "1004", "1005"],
                    "bracket_group": "group_SIG_2",
                },
            }
        }
        self.targets = []
        self.updated = []
        self.acked = []
        self.events = []

    def get_records(self, collection, filter=None, sort=None, per_page=100, page=1):
        if collection == "orders":
            rows = self.orders
        elif collection == "ibkr_signals":
            rows = list(self.signals.values())
        elif collection == "ibkr_targets":
            rows = self.targets
        else:
            rows = []
        text = str(filter or "")
        unique_id = self._extract(text, "unique_id")
        entry_unique_id = self._extract(text, "entry_order_unique_id")
        order_id = self._extract(text, "order_id")
        broker_order_id = self._extract(text, "broker_order_id")
        signal_id = self._extract(text, "signal_id")
        trade_group_id = self._extract(text, "trade_group_id")
        environment = self._extract(text, "environment")
        symbol = self._extract(text, "symbol")
        date = self._extract(text, "date")
        status = self._extract(text, "status")
        result = []
        for row in rows:
            if environment and row.get("environment") != environment:
                continue
            if symbol and row.get("symbol") != symbol:
                continue
            if date and row.get("date") != date:
                continue
            if status and row.get("status") != status:
                continue
            if signal_id and row.get("signal_id") != signal_id:
                continue
            if trade_group_id and row.get("trade_group_id") != trade_group_id:
                continue
            if unique_id and row.get("unique_id") != unique_id:
                continue
            if entry_unique_id and row.get("entry_order_unique_id") != entry_unique_id:
                continue
            if order_id and row.get("order_id") != order_id:
                continue
            if broker_order_id and row.get("broker_order_id") != broker_order_id:
                continue
            result.append(copy.deepcopy(row))
        return result[:per_page]

    def get_all_records(self, collection, filter=None, sort=None, max_pages=10):
        return self.get_records(collection, filter=filter, sort=sort, per_page=200, page=1)

    def get_first_record(self, collection, filter=None, sort=None):
        if collection != "ibkr_signals":
            return None
        signal_id = self._extract(str(filter or ""), "signal_id")
        environment = self._extract(str(filter or ""), "environment")
        for row in self.signals.values():
            if signal_id and row.get("signal_id") != signal_id:
                continue
            if environment and row.get("environment") != environment:
                continue
            return copy.deepcopy(row)
        return None

    def update_record(self, collection, record_id, patch):
        if collection == "orders":
            for idx, row in enumerate(self.orders):
                if str(row.get("id")) == str(record_id):
                    current = copy.deepcopy(row)
                    current.update(copy.deepcopy(patch))
                    self.orders[idx] = current
                    self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
                    return copy.deepcopy(current)
            raise KeyError(record_id)
        if collection == "ibkr_targets":
            for idx, row in enumerate(self.targets):
                if str(row.get("id")) == str(record_id):
                    current = copy.deepcopy(row)
                    current.update(copy.deepcopy(patch))
                    self.targets[idx] = current
                    self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
                    return copy.deepcopy(current)
            raise KeyError(record_id)
        current = copy.deepcopy(self.signals[str(record_id)])
        current.update(copy.deepcopy(patch))
        self.signals[str(record_id)] = current
        self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
        return copy.deepcopy(current)

    def upsert_order(self, payload):
        payload = copy.deepcopy(payload)
        unique_id = str(payload.get("unique_id") or "")
        order_id = str(payload.get("order_id") or payload.get("broker_order_id") or "")
        for idx, row in enumerate(self.orders):
            if unique_id and row.get("unique_id") == unique_id:
                current = copy.deepcopy(row)
                current.update(payload)
                self.orders[idx] = current
                return copy.deepcopy(current)
            if order_id and str(row.get("order_id") or row.get("broker_order_id") or "") == order_id:
                current = copy.deepcopy(row)
                current.update(payload)
                self.orders[idx] = current
                return copy.deepcopy(current)
        payload.setdefault("id", f"order-{len(self.orders) + 1}")
        self.orders.append(payload)
        return copy.deepcopy(payload)

    def ack_ibkr_signal(self, **payload):
        self.acked.append(copy.deepcopy(payload))
        return {"status": payload.get("status"), "fallback": False}

    def notify_system_event(self, title, detail=None, **kwargs):
        event = {"title": title, "detail": copy.deepcopy(detail or {}), **copy.deepcopy(kwargs)}
        self.events.append(event)
        return {"ok": True, "notified": True, "persisted": True}

    @staticmethod
    def _extract(filter_text, field):
        match = re.search(rf'{re.escape(field)}\s*=\s*"([^"]*)"', filter_text)
        return match.group(1) if match else ""


class _FakeSignalProcessor:
    def __init__(self):
        self.filled = []
        self.removed = []
        self.cooldowns = []

    def register_filled_position(self, symbol, payload):
        self.filled.append((symbol, dict(payload)))

    def remove_position(self, symbol):
        self.removed.append(symbol)

    def start_cooldown(self, symbol, bars, reason):
        self.cooldowns.append((symbol, bars, reason))

    def cooldown_bars_after_sl(self):
        return 3


class _FakeOrderLifecycle:
    def __init__(self):
        self.diagnostics = []
        self.sl_count = 0
        self.sl_limit = 3
        self.reset_count = 0

    def handle_protection_incomplete(self, **kwargs):
        diagnostic = {
            "status": "protection_incomplete",
            "reason": kwargs.get("reason"),
            "signal_id": kwargs.get("signal_id"),
            "symbol": kwargs.get("symbol"),
            "direction": kwargs.get("direction"),
            "protection_complete": False,
            "missing_order_ids": list((kwargs.get("result") or {}).get("missing_order_ids") or []),
            "submitted_order_ids": list((kwargs.get("result") or {}).get("order_ids") or []),
            "safe_action": "diagnostic_only_no_broker_call",
            "recommended_action": "review_and_cancel_or_repair_unprotected_entry",
            "cancel_recommended": True,
        }
        self.diagnostics.append(diagnostic)
        return diagnostic

    def increment_sl_count(self):
        self.sl_count += 1

    def reset_sl_count(self):
        self.reset_count += 1

    @property
    def is_sl_circuit_breaker(self):
        return self.sl_count >= self.sl_limit


class _FakeOrderTracker:
    def __init__(self, live_orders):
        self.live_orders = [copy.deepcopy(item) for item in live_orders]

    def get_complete_live_open_orders(self, *, pb_seed_ids=None, **kwargs):
        seed_ids = {str(item or "").strip() for item in (pb_seed_ids or []) if str(item or "").strip()}
        orders = []
        for item in self.live_orders:
            order_id = str(item.get("orderId") or item.get("order_id") or item.get("id") or "").strip()
            if seed_ids and order_id not in seed_ids:
                continue
            orders.append(copy.deepcopy(item))
        return {
            "orders": orders,
            "coverage": {
                "coverage_state": "complete",
                "unresolved_order_ids": [],
            },
        }


class _FakeOrderModifier:
    def __init__(self, fail_role=""):
        self.fail_role = fail_role
        self.stop_updates = []
        self.take_profit_updates = []

    def update_stop_loss(self, order_id, new_sl_price, acct_id=None):
        self.stop_updates.append((str(order_id), float(new_sl_price)))
        if self.fail_role == "stop_loss":
            return {"ok": False, "error": "stop update failed"}
        return {"ok": True, "order_id": str(order_id), "price": float(new_sl_price)}

    def update_take_profit(self, order_id, new_tp_price, acct_id=None):
        self.take_profit_updates.append((str(order_id), float(new_tp_price)))
        if self.fail_role == "take_profit":
            return {"ok": False, "error": "take profit update failed"}
        return {"ok": True, "order_id": str(order_id), "price": float(new_tp_price)}


class _FakeService(TradingServiceRuntimeOpsMixin):
    def __init__(self):
        from ibkr_compute.orchestration import trading_service as service_mod

        service_mod.ENVIRONMENT = "live"
        service_mod.DATA_ENVIRONMENT = "live"
        self.pb = _FakePB()
        self.signal_processor = _FakeSignalProcessor()
        self.order_lifecycle = _FakeOrderLifecycle()

    def _now_iso(self):
        return "2026-05-06T12:00:00Z"

    def _market_date(self):
        return "2026-05-06"


class _FakeSignalsService(TradingServiceSignalsMixin):
    def __init__(self):
        from ibkr_compute.orchestration import trading_service as service_mod

        service_mod.ENVIRONMENT = "live"
        service_mod.DATA_ENVIRONMENT = "live"
        self.pb = _FakePB()
        self.order_lifecycle = _FakeOrderLifecycle()

    def _now_iso(self):
        return "2026-05-06T12:00:00Z"


class RuntimeSignalLifecycleTest(unittest.TestCase):
    def _make_tv_direct_signal(self, service, *, direction="long"):
        signal = service.pb.signals["sig-row-1"]
        signal["status"] = "submitted_waiting_fill"
        signal["direction"] = direction
        signal["entry"] = 100.15 if direction == "long" else 99.85
        signal["stop_loss"] = 98.4 if direction == "long" else 101.6
        signal["take_profit"] = 102.4 if direction == "long" else 97.6
        signal["extra"] = {
            "source": "tradingview",
            "bracket_group": "group_SIG_1",
            "tv_direct_entry": True,
            "final_protection_from_fill": True,
            "reference_entry": 100.0,
            "reference_stop_loss": 98.4 if direction == "long" else 101.6,
            "reference_take_profit": 102.4 if direction == "long" else 97.6,
            "risk_per_share": 1.6,
            "reward_risk": 1.5,
            "atr": 1.0,
        }
        service.pb.orders[0]["limit_price"] = signal["entry"]
        service.pb.orders.extend(
            [
                {
                    "id": "order-tp-1",
                    "unique_id": "tp_SIG_1",
                    "order_id": "1002",
                    "broker_order_id": "1002",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "take_profit",
                    "limit_price": signal["take_profit"],
                    "status": "Submitted",
                    "environment": "live",
                },
                {
                    "id": "order-sl-1",
                    "unique_id": "sl_SIG_1",
                    "order_id": "1003",
                    "broker_order_id": "1003",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "stop_loss",
                    "limit_price": signal["stop_loss"],
                    "status": "Submitted",
                    "environment": "live",
                },
            ]
        )

    def test_entry_fill_moves_submitted_signal_to_protected_active(self):
        service = _FakeService()
        service.pb.orders.extend(
            [
                {
                    "id": "order-tp-1",
                    "unique_id": "tp_SIG_1",
                    "order_id": "1002",
                    "broker_order_id": "1002",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "take_profit",
                    "status": "Submitted",
                    "environment": "live",
                },
                {
                    "id": "order-sl-1",
                    "unique_id": "sl_SIG_1",
                    "order_id": "1003",
                    "broker_order_id": "1003",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "stop_loss",
                    "status": "PreSubmitted",
                    "environment": "live",
                },
            ]
        )

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
            }
        )

        self.assertEqual(service.signal_processor.filled[0][0], "AAPL")
        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protected_active")
        self.assertEqual(row["note"], "entry_filled_protection_expected")
        self.assertEqual(row["extra"]["entry_fill_broker_order_id"], "1001")
        self.assertEqual(row["extra"]["entry_fill_direction"], "long")
        self.assertTrue(row["extra"]["protection_complete"])
        self.assertEqual([], row["extra"]["missing_protection_roles"])

    def test_entry_fill_rebases_child_orders_when_actual_fill_moves(self):
        service = _FakeService()
        service.order_modifier = _FakeOrderModifier()
        service.pb.orders[0]["limit_price"] = 100.0
        service.pb.orders.extend(
            [
                {
                    "id": "order-tp-1",
                    "unique_id": "tp_SIG_1",
                    "order_id": "1002",
                    "broker_order_id": "1002",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "take_profit",
                    "limit_price": 105.0,
                    "status": "Submitted",
                    "environment": "live",
                },
                {
                    "id": "order-sl-1",
                    "unique_id": "sl_SIG_1",
                    "order_id": "1003",
                    "broker_order_id": "1003",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "stop_loss",
                    "limit_price": 99.0,
                    "status": "Submitted",
                    "environment": "live",
                },
            ]
        )

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
                "avgPrice": 100.25,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protected_active")
        self.assertEqual(row["executed_price"], 100.25)
        self.assertEqual(row["stop_loss"], 99.25)
        self.assertEqual(row["take_profit"], 105.25)
        self.assertEqual([("1003", 99.25)], service.order_modifier.stop_updates)
        self.assertEqual([("1002", 105.25)], service.order_modifier.take_profit_updates)
        self.assertEqual(row["extra"]["entry_fill_price"], 100.25)
        self.assertAlmostEqual(row["extra"]["entry_fill_rebase_delta"], 0.25)
        self.assertTrue(row["extra"]["protection_rebase_result"]["attempted"])
        self.assertTrue(row["extra"]["protection_rebase_result"]["ok"])
        orders_by_id = {item["id"]: item for item in service.pb.orders}
        self.assertEqual(100.25, orders_by_id["order-1"]["fill_price"])
        self.assertEqual(99.25, orders_by_id["order-sl-1"]["limit_price"])
        self.assertEqual(105.25, orders_by_id["order-tp-1"]["limit_price"])

    def test_short_entry_fill_rebases_child_orders_from_submitted_limit(self):
        service = _FakeService()
        service.order_modifier = _FakeOrderModifier()
        service.pb.signals["sig-row-1"]["entry"] = 100.0
        service.pb.signals["sig-row-1"]["close"] = 100.0
        service.pb.signals["sig-row-1"]["stop_loss"] = 103.0
        service.pb.signals["sig-row-1"]["take_profit"] = 96.0
        service.pb.orders.extend(
            [
                {
                    "id": "order-tp-1",
                    "unique_id": "tp_SIG_1",
                    "order_id": "1002",
                    "broker_order_id": "1002",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "take_profit",
                    "limit_price": 96.0,
                    "status": "Submitted",
                    "environment": "live",
                },
                {
                    "id": "order-sl-1",
                    "unique_id": "sl_SIG_1",
                    "order_id": "1003",
                    "broker_order_id": "1003",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "stop_loss",
                    "limit_price": 103.0,
                    "status": "Submitted",
                    "environment": "live",
                },
            ]
        )

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "SELL",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
                "limitPrice": 101.0,
                "avgPrice": 101.4,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protected_active")
        self.assertEqual(row["extra"]["entry_fill_direction"], "short")
        self.assertEqual(row["executed_price"], 101.4)
        self.assertAlmostEqual(row["stop_loss"], 103.4)
        self.assertAlmostEqual(row["take_profit"], 96.4)
        self.assertEqual([("1003", 103.4)], service.order_modifier.stop_updates)
        self.assertEqual([("1002", 96.4)], service.order_modifier.take_profit_updates)
        self.assertAlmostEqual(row["extra"]["protection_rebase_result"]["submitted_entry"], 101.0)
        self.assertAlmostEqual(row["extra"]["entry_fill_rebase_delta"], 0.4)
        self.assertTrue(row["extra"]["protection_rebase_result"]["attempted"])
        self.assertTrue(row["extra"]["protection_rebase_result"]["ok"])
        orders_by_id = {item["id"]: item for item in service.pb.orders}
        self.assertEqual(101.4, orders_by_id["order-1"]["fill_price"])
        self.assertAlmostEqual(103.4, orders_by_id["order-sl-1"]["limit_price"])
        self.assertAlmostEqual(96.4, orders_by_id["order-tp-1"]["limit_price"])

    def test_tv_direct_long_entry_fill_reprices_protection_from_actual_fill(self):
        service = _FakeService()
        service.order_modifier = _FakeOrderModifier()
        self._make_tv_direct_signal(service, direction="long")

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
                "avgPrice": 100.06,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protected_active")
        self.assertEqual(row["extra"]["signal_lifecycle_status"], "filled_position")
        self.assertEqual(row["extra"]["pb_signal_status_mapped_from"], "filled_position")
        self.assertEqual(row["note"], "entry_filled_final_protection_repriced")
        self.assertEqual(row["executed_price"], 100.06)
        self.assertAlmostEqual(row["stop_loss"], 98.46)
        self.assertAlmostEqual(row["take_profit"], 102.46)
        self.assertEqual([("1003", 98.46)], service.order_modifier.stop_updates)
        self.assertEqual([("1002", 102.46)], service.order_modifier.take_profit_updates)
        reprice = row["extra"]["protection_reprice_result"]
        self.assertEqual("tv_fill_based", reprice["method"])
        self.assertAlmostEqual(1.6, reprice["risk_per_share"])
        self.assertAlmostEqual(1.5, reprice["reward_risk"])
        self.assertAlmostEqual(6.0, reprice["slippage_bps"])
        self.assertAlmostEqual(0.0375, reprice["slippage_r"])
        self.assertEqual("protected_active", service.pb.acked[-1]["status"])
        self.assertEqual("filled_position", service.pb.acked[-1]["order"]["extra"]["signal_lifecycle_status"])

    def test_tv_direct_short_entry_fill_reprices_protection_from_actual_fill(self):
        service = _FakeService()
        service.order_modifier = _FakeOrderModifier()
        self._make_tv_direct_signal(service, direction="short")

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "SELL",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
                "avgPrice": 99.94,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protected_active")
        self.assertEqual(row["extra"]["signal_lifecycle_status"], "filled_position")
        self.assertEqual(row["extra"]["entry_fill_direction"], "short")
        self.assertAlmostEqual(row["stop_loss"], 101.54)
        self.assertAlmostEqual(row["take_profit"], 97.54)
        self.assertEqual([("1003", 101.54)], service.order_modifier.stop_updates)
        self.assertEqual([("1002", 97.54)], service.order_modifier.take_profit_updates)
        reprice = row["extra"]["protection_reprice_result"]
        self.assertEqual("tv_fill_based", reprice["method"])
        self.assertAlmostEqual(6.0, reprice["slippage_bps"])
        self.assertAlmostEqual(0.0375, reprice["slippage_r"])

    def test_tv_direct_reprice_failure_marks_protection_reprice_failed(self):
        service = _FakeService()
        service.order_modifier = _FakeOrderModifier(fail_role="take_profit")
        self._make_tv_direct_signal(service, direction="long")

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
                "avgFillPrice": 100.06,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protection_incomplete")
        self.assertEqual(row["note"], "protection_reprice_failed")
        self.assertEqual(row["extra"]["signal_lifecycle_status"], "protection_reprice_failed")
        self.assertTrue(row["extra"]["protection_reprice_failed"])
        self.assertTrue(row["extra"]["protection_incomplete"])
        self.assertFalse(row["extra"]["protection_complete"])
        self.assertEqual("broker_modify_failed", row["extra"]["protection_reprice_result"]["reason"])
        self.assertEqual("protection_incomplete", service.pb.acked[-1]["status"])
        self.assertEqual(
            "protection_reprice_failed",
            service.pb.acked[-1]["order"]["extra"]["signal_lifecycle_status"],
        )

    def test_tv_direct_entry_cancel_marks_cancelled_with_missed_limit_reason(self):
        service = _FakeService()
        self._make_tv_direct_signal(service, direction="long")

        service._on_order_cancel(
            {
                "orderId": "1001",
                "cOID": "entry_SIG_1",
                "ticker": "AAPL",
                "status": "Cancelled",
                "side": "BUY",
                "orderType": "LMT",
                "filledQuantity": 0,
                "avgPrice": 0,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual("rejected", row["status"])
        self.assertEqual("cancelled", row["note"])
        self.assertEqual("cancelled", row["extra"]["signal_lifecycle_status"])
        self.assertEqual("entry_missed_limit_cap", row["extra"]["status_reason"])
        self.assertTrue(row["extra"]["entry_missed_limit_cap"])
        self.assertEqual("entry_missed_limit_cap", row["extra"]["entry_missed_reason"])
        self.assertEqual("Cancelled", row["extra"]["entry_cancel_status"])
        self.assertFalse(row["extra"]["position_open"])
        ack = service.pb.acked[-1]
        self.assertEqual("rejected", ack["status"])
        self.assertEqual("cancelled", ack["note"])
        self.assertEqual("cancelled", ack["order"]["extra"]["signal_lifecycle_status"])
        self.assertEqual("entry_missed_limit_cap", ack["order"]["extra"]["status_reason"])
        self.assertTrue(ack["order"]["extra"]["entry_missed_limit_cap"])
        self.assertEqual(["AAPL"], service.signal_processor.removed)
        entry_order = next(item for item in service.pb.orders if item["id"] == "order-1")
        self.assertEqual("Cancelled", entry_order["status"])
        self.assertEqual("entry_missed", entry_order["relation_status"])

    def test_entry_fill_rebase_failure_marks_protection_incomplete(self):
        service = _FakeService()
        service.order_modifier = _FakeOrderModifier(fail_role="take_profit")
        service.pb.orders[0]["limit_price"] = 100.0
        service.pb.orders.extend(
            [
                {
                    "id": "order-tp-1",
                    "unique_id": "tp_SIG_1",
                    "order_id": "1002",
                    "broker_order_id": "1002",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "take_profit",
                    "limit_price": 105.0,
                    "status": "Submitted",
                    "environment": "live",
                },
                {
                    "id": "order-sl-1",
                    "unique_id": "sl_SIG_1",
                    "order_id": "1003",
                    "broker_order_id": "1003",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "stop_loss",
                    "limit_price": 99.0,
                    "status": "Submitted",
                    "environment": "live",
                },
            ]
        )

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
                "avgFillPrice": 100.25,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protection_incomplete")
        self.assertEqual(row["note"], "protection_rebase_failed")
        self.assertEqual(row["executed_price"], 100.25)
        self.assertTrue(row["extra"]["protection_incomplete"])
        self.assertFalse(row["extra"]["protection_complete"])
        self.assertTrue(row["extra"]["safety_cancel_recommended"])
        self.assertEqual("broker_modify_failed", row["extra"]["protection_rebase_result"]["reason"])
        self.assertEqual("protection_rebase_failed", row["extra"]["protection_rebase_failed"]["reason"])
        self.assertEqual([("1003", 99.25)], service.order_modifier.stop_updates)
        self.assertEqual([("1002", 105.25)], service.order_modifier.take_profit_updates)

    def test_entry_fill_keeps_protection_incomplete_signal_diagnostic(self):
        service = _FakeService()
        service.pb.orders.append(
            {
                "id": "order-2",
                "unique_id": "entry_SIG_2",
                "entry_order_unique_id": "entry_SIG_2",
                "order_id": "1003",
                "broker_order_id": "1003",
                "signal_id": "SIG_2",
                "environment": "live",
            }
        )

        service._on_order_fill(
            {
                "orderId": "1003",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_2",
            }
        )

        row = service.pb.signals["sig-row-2"]
        self.assertEqual(row["status"], "protection_incomplete")
        self.assertEqual(row["note"], "entry_fill_detected_with_incomplete_protection")
        self.assertTrue(row["extra"]["safety_cancel_recommended"])
        self.assertEqual(["take_profit", "stop_loss"], row["extra"]["missing_protection_roles"])
        self.assertEqual({"take_profit": [], "stop_loss": []}, row["extra"]["protection_order_statuses"])
        self.assertEqual(1, row["extra"]["protection_orders_checked"])
        self.assertEqual(
            "entry_fill_detected_with_incomplete_protection",
            row["extra"]["protection_incomplete_diagnostic"]["reason"],
        )

    def test_entry_fill_requires_both_working_protection_roles(self):
        service = _FakeService()
        service.pb.signals["sig-row-1"]["extra"]["bracket_group"] = "group_SIG_1"
        service.pb.orders.append(
            {
                "id": "order-tp-1",
                "unique_id": "tp_SIG_1",
                "order_id": "1002",
                "broker_order_id": "1002",
                "signal_id": "SIG_1",
                "trade_group_id": "group_SIG_1",
                "role": "take_profit",
                "status": "Submitted",
                "environment": "live",
            }
        )

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protection_incomplete")
        self.assertEqual(["stop_loss"], row["extra"]["missing_protection_roles"])
        self.assertEqual(["Submitted"], row["extra"]["protection_order_statuses"]["take_profit"])
        self.assertTrue(row["extra"]["safety_cancel_recommended"])

    def test_entry_fill_requires_live_open_protection_when_tracker_present(self):
        service = _FakeService()
        service.pb.orders.extend(
            [
                {
                    "id": "order-tp-1",
                    "unique_id": "tp_SIG_1",
                    "order_id": "1002",
                    "broker_order_id": "1002",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "take_profit",
                    "status": "Submitted",
                    "environment": "live",
                },
                {
                    "id": "order-sl-1",
                    "unique_id": "sl_SIG_1",
                    "order_id": "1003",
                    "broker_order_id": "1003",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "stop_loss",
                    "status": "Submitted",
                    "environment": "live",
                },
            ]
        )
        service.order_tracker = _FakeOrderTracker(
            [
                {"orderId": "1002", "cOID": "tp_SIG_1", "status": "SUBMITTED"},
            ]
        )

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protection_incomplete")
        self.assertEqual(["stop_loss"], row["extra"]["missing_protection_roles"])
        self.assertTrue(row["extra"]["protection_live_check_performed"])
        self.assertEqual(["SUBMITTED"], row["extra"]["protection_live_order_statuses"]["take_profit"])
        self.assertEqual([], row["extra"]["protection_live_order_statuses"]["stop_loss"])

    def test_entry_fill_promotes_when_tracker_confirms_both_protection_roles(self):
        service = _FakeService()
        service.pb.orders.extend(
            [
                {
                    "id": "order-tp-1",
                    "unique_id": "tp_SIG_1",
                    "order_id": "1002",
                    "broker_order_id": "1002",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "take_profit",
                    "status": "Submitted",
                    "environment": "live",
                },
                {
                    "id": "order-sl-1",
                    "unique_id": "sl_SIG_1",
                    "order_id": "1003",
                    "broker_order_id": "1003",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "stop_loss",
                    "status": "Submitted",
                    "environment": "live",
                },
            ]
        )
        service.order_tracker = _FakeOrderTracker(
            [
                {"orderId": "1002", "cOID": "tp_SIG_1", "status": "SUBMITTED"},
                {"orderId": "1003", "cOID": "sl_SIG_1", "status": "PRESUBMITTED"},
            ]
        )

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protected_active")
        self.assertTrue(row["extra"]["protection_complete"])
        self.assertTrue(row["extra"]["protection_live_check_performed"])
        self.assertEqual([], row["extra"]["missing_protection_roles"])

    def test_stop_loss_fill_closes_protected_active_signal(self):
        service = _FakeService()
        service.pb.signals["sig-row-1"]["status"] = "protected_active"
        service.pb.signals["sig-row-1"]["note"] = "entry_filled_protection_expected"
        service.pb.signals["sig-row-1"]["extra"]["protection_complete"] = True
        service.pb.orders.append(
            {
                "id": "order-sl-1",
                "unique_id": "sl_SIG_1",
                "entry_order_unique_id": "entry_SIG_1",
                "order_id": "1003",
                "broker_order_id": "1003",
                "signal_id": "SIG_1",
                "trade_group_id": "group_SIG_1",
                "role": "stop_loss",
                "status": "Filled",
                "environment": "live",
            }
        )

        service._on_order_fill(
            {
                "orderId": "1003",
                "ticker": "AAPL",
                "side": "SELL",
                "orderType": "STP",
                "status": "FILLED",
                "parentId": "1001",
                "cOID": "sl_SIG_1",
                "avgPrice": 99.25,
                "filledQuantity": 10,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["note"], "closed_by_stop_loss")
        self.assertEqual(row["extra"]["status_reason"], "closed_by_stop_loss")
        self.assertEqual(row["extra"]["exit_fill_role"], "stop_loss")
        self.assertEqual(row["extra"]["exit_fill_broker_order_id"], "1003")
        self.assertFalse(row["extra"]["protection_active"])
        self.assertFalse(row["extra"]["protection_incomplete"])
        self.assertEqual(["AAPL"], service.signal_processor.removed)
        self.assertEqual(1, service.order_lifecycle.sl_count)
        self.assertEqual([("AAPL", 3, "cooldown_after_stop_loss")], service.signal_processor.cooldowns)
        self.assertEqual(1, len(service.pb.events))
        event = service.pb.events[0]
        self.assertEqual("止损成交报警", event["title"])
        self.assertEqual("alert", event["event_type"])
        self.assertEqual("warning", event["level"])
        self.assertEqual("live", event["environment"])
        self.assertEqual("AAPL", event["detail"]["标的"])
        self.assertEqual("SIG_1", event["detail"]["信号ID"])
        self.assertEqual("1003", event["detail"]["Broker订单ID"])
        self.assertEqual(1, event["detail"]["连续止损次数"])
        self.assertEqual(3, event["detail"]["连续止损上限"])
        self.assertEqual("no", event["detail"]["熔断状态"])
        self.assertEqual(3, event["detail"]["冷却K线"])

    def test_exit_fill_demotes_entry_activated_target_to_candidate(self):
        service = _FakeService()
        service.pb.signals["sig-row-1"]["status"] = "protected_active"
        service.pb.targets.append(
            {
                "id": "target-1",
                "symbol": "AAPL",
                "date": "2026-05-06",
                "environment": "live",
                "status": "active",
                "direction_bias": "long",
                "extra": {
                    "source": "tradingview",
                    "entry_signal_id": "SIG_1",
                    "entry_backfilled_target": True,
                    "execution_eligible": True,
                    "target_layer": "execution",
                },
            }
        )
        service.pb.orders.append(
            {
                "id": "order-sl-1",
                "unique_id": "sl_SIG_1",
                "entry_order_unique_id": "entry_SIG_1",
                "order_id": "1003",
                "broker_order_id": "1003",
                "signal_id": "SIG_1",
                "trade_group_id": "group_SIG_1",
                "role": "stop_loss",
                "status": "Filled",
                "environment": "live",
            }
        )

        service._on_order_fill(
            {
                "orderId": "1003",
                "ticker": "AAPL",
                "side": "SELL",
                "orderType": "STP",
                "status": "FILLED",
                "parentId": "1001",
                "cOID": "sl_SIG_1",
            }
        )

        target = service.pb.targets[0]
        self.assertEqual("candidate", target["status"])
        self.assertTrue(target["extra"]["deactivated_after_close"])
        self.assertEqual("SIG_1", target["extra"]["deactivated_signal_id"])
        self.assertEqual("closed_by_stop_loss", target["extra"]["deactivated_reason"])
        self.assertFalse(target["extra"]["execution_eligible"])
        self.assertEqual("observe", target["extra"]["target_layer"])
        self.assertIn("deactivated_after_close", target["extra"]["execution_blockers"])

    def test_exit_fill_keeps_target_active_when_another_signal_is_open(self):
        service = _FakeService()
        service.pb.signals["sig-row-1"]["status"] = "protected_active"
        service.pb.signals["sig-row-3"] = {
            "id": "sig-row-3",
            "signal_id": "SIG_3",
            "symbol": "AAPL",
            "date": "2026-05-06",
            "environment": "live",
            "status": "submitted",
            "extra": {"source": "ibkr_compute"},
        }
        service.pb.targets.append(
            {
                "id": "target-1",
                "symbol": "AAPL",
                "date": "2026-05-06",
                "environment": "live",
                "status": "active",
                "direction_bias": "long",
                "extra": {"source": "tradingview", "entry_signal_id": "SIG_1"},
            }
        )
        service.pb.orders.append(
            {
                "id": "order-tp-1",
                "unique_id": "tp_SIG_1",
                "entry_order_unique_id": "entry_SIG_1",
                "order_id": "1002",
                "broker_order_id": "1002",
                "signal_id": "SIG_1",
                "trade_group_id": "group_SIG_1",
                "role": "take_profit",
                "status": "Filled",
                "environment": "live",
            }
        )

        service._on_order_fill(
            {
                "orderId": "1002",
                "ticker": "AAPL",
                "side": "SELL",
                "orderType": "LMT",
                "status": "FILLED",
                "parentId": "1001",
                "cOID": "tp_SIG_1",
            }
        )

        target = service.pb.targets[0]
        self.assertEqual("active", target["status"])
        self.assertNotIn("deactivated_after_close", target["extra"])

    def test_exit_fill_does_not_demote_manual_target(self):
        service = _FakeService()
        service.pb.signals["sig-row-1"]["status"] = "protected_active"
        service.pb.targets.append(
            {
                "id": "target-1",
                "symbol": "AAPL",
                "date": "2026-05-06",
                "environment": "live",
                "status": "active",
                "direction_bias": "long",
                "extra": {"source": "manual_page_add", "entry_signal_id": "SIG_1"},
            }
        )
        service.pb.orders.append(
            {
                "id": "order-tp-1",
                "unique_id": "tp_SIG_1",
                "entry_order_unique_id": "entry_SIG_1",
                "order_id": "1002",
                "broker_order_id": "1002",
                "signal_id": "SIG_1",
                "trade_group_id": "group_SIG_1",
                "role": "take_profit",
                "status": "Filled",
                "environment": "live",
            }
        )

        service._on_order_fill(
            {
                "orderId": "1002",
                "ticker": "AAPL",
                "side": "SELL",
                "orderType": "LMT",
                "status": "FILLED",
                "parentId": "1001",
                "cOID": "tp_SIG_1",
            }
        )

        target = service.pb.targets[0]
        self.assertEqual("active", target["status"])
        self.assertNotIn("deactivated_after_close", target["extra"])

    def test_take_profit_fill_closes_protected_active_signal(self):
        service = _FakeService()
        service.pb.signals["sig-row-1"]["status"] = "protected_active"
        service.pb.signals["sig-row-1"]["note"] = "entry_filled_protection_expected"
        service.pb.orders.append(
            {
                "id": "order-tp-1",
                "unique_id": "tp_SIG_1",
                "entry_order_unique_id": "entry_SIG_1",
                "order_id": "1002",
                "broker_order_id": "1002",
                "signal_id": "SIG_1",
                "trade_group_id": "group_SIG_1",
                "role": "take_profit",
                "status": "Filled",
                "environment": "live",
            }
        )

        service._on_order_fill(
            {
                "orderId": "1002",
                "ticker": "AAPL",
                "side": "SELL",
                "orderType": "LMT",
                "status": "FILLED",
                "parentId": "1001",
                "cOID": "tp_SIG_1",
                "avgPrice": 105.5,
                "filledQuantity": 10,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["note"], "closed_by_take_profit")
        self.assertEqual(row["extra"]["exit_fill_role"], "take_profit")
        self.assertEqual(row["extra"]["exit_fill_price"], 105.5)
        self.assertEqual(["AAPL"], service.signal_processor.removed)
        self.assertEqual(1, service.order_lifecycle.reset_count)
        self.assertEqual([], service.pb.events)

    def test_stop_loss_fill_sends_error_alert_when_circuit_breaker_trips(self):
        service = _FakeService()
        service.order_lifecycle.sl_limit = 1
        service.pb.signals["sig-row-1"]["status"] = "protected_active"
        service.pb.orders.append(
            {
                "id": "order-sl-1",
                "unique_id": "sl_SIG_1",
                "entry_order_unique_id": "entry_SIG_1",
                "order_id": "1003",
                "broker_order_id": "1003",
                "signal_id": "SIG_1",
                "trade_group_id": "group_SIG_1",
                "role": "stop_loss",
                "status": "Filled",
                "environment": "live",
            }
        )

        service._on_order_fill(
            {
                "orderId": "1003",
                "ticker": "AAPL",
                "side": "SELL",
                "orderType": "STP",
                "status": "FILLED",
                "parentId": "1001",
                "cOID": "sl_SIG_1",
                "avgPrice": 99.25,
                "filledQuantity": 10,
            }
        )

        self.assertEqual(1, service.order_lifecycle.sl_count)
        self.assertEqual(1, len(service.pb.events))
        event = service.pb.events[0]
        self.assertEqual("连续止损熔断已触发", event["title"])
        self.assertEqual("error", event["level"])
        self.assertEqual(1, event["detail"]["连续止损次数"])
        self.assertEqual(1, event["detail"]["连续止损上限"])
        self.assertEqual("yes", event["detail"]["熔断状态"])
        self.assertIn("sl_circuit_breaker", event["detail"]["状态结论"])

    def test_market_close_fill_closes_signal_with_realized_pnl_and_commission(self):
        service = _FakeService()
        service.pb.signals["sig-row-1"]["status"] = "protected_active"
        service.pb.orders[0].update(
            {
                "trade_group_id": "group_SIG_1",
                "role": "entry",
                "status": "Filled",
                "quantity": 10,
                "filled_qty": 10,
                "fill_price": 100.0,
                "commission": 0.35,
            }
        )
        service.pb.orders.append(
            {
                "id": "order-close-1",
                "unique_id": "close_AAPL_20260506_100000",
                "entry_order_unique_id": "entry_SIG_1",
                "parent_order_unique_id": "entry_SIG_1",
                "order_id": "1004",
                "broker_order_id": "1004",
                "signal_id": "SIG_1",
                "trade_group_id": "group_SIG_1",
                "role": "close",
                "status": "Submitted",
                "environment": "live",
            }
        )

        service._on_order_fill(
            {
                "orderId": "1004",
                "ticker": "AAPL",
                "side": "SELL",
                "orderType": "MKT",
                "status": "FILLED",
                "cOID": "close_AAPL_20260506_100000",
                "avgPrice": 102.0,
                "filledQuantity": 10,
                "commission": 0.45,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["note"], "closed_by_manual_close")
        self.assertEqual(row["extra"]["exit_fill_role"], "close")
        self.assertAlmostEqual(row["extra"]["realized_gross_pnl"], 20.0)
        self.assertAlmostEqual(row["extra"]["realized_net_pnl"], 19.2)
        self.assertAlmostEqual(row["extra"]["realized_pnl_diagnostic"]["commission"], 0.8)
        close_updates = [
            patch
            for collection, record_id, patch in service.pb.updated
            if collection == "orders" and record_id == "order-close-1"
        ]
        self.assertTrue(close_updates)
        self.assertAlmostEqual(close_updates[-1]["pnl"], 20.0)
        self.assertAlmostEqual(close_updates[-1]["commission"], 0.45)
        self.assertAlmostEqual(close_updates[-1]["extra"]["total_commission"], 0.8)
        self.assertEqual(["AAPL"], service.signal_processor.removed)
        self.assertEqual(1, service.order_lifecycle.reset_count)
        self.assertEqual([], service.pb.events)

    def test_duplicate_stop_loss_fill_does_not_retrigger_cooldown(self):
        service = _FakeService()
        service.pb.signals["sig-row-1"]["status"] = "protected_active"
        service.pb.orders.append(
            {
                "id": "order-sl-1",
                "unique_id": "sl_SIG_1",
                "entry_order_unique_id": "entry_SIG_1",
                "order_id": "1003",
                "broker_order_id": "1003",
                "signal_id": "SIG_1",
                "trade_group_id": "group_SIG_1",
                "role": "stop_loss",
                "status": "Filled",
                "environment": "live",
            }
        )
        fill = {
            "orderId": "1003",
            "ticker": "AAPL",
            "side": "SELL",
            "orderType": "STP",
            "status": "FILLED",
            "parentId": "1001",
            "cOID": "sl_SIG_1",
            "avgPrice": 99.25,
        }

        service._on_order_fill(fill)
        service._on_order_fill(fill)

        self.assertEqual(service.pb.signals["sig-row-1"]["status"], "closed")
        self.assertEqual(1, service.order_lifecycle.sl_count)
        self.assertEqual([("AAPL", 3, "cooldown_after_stop_loss")], service.signal_processor.cooldowns)
        self.assertEqual(["AAPL"], service.signal_processor.removed)
        self.assertEqual(1, len(service.pb.events))

    def test_entry_fill_reconciles_existing_filled_take_profit(self):
        service = _FakeService()
        service.pb.orders.extend(
            [
                {
                    "id": "order-tp-1",
                    "unique_id": "tp_SIG_1",
                    "order_id": "1002",
                    "broker_order_id": "1002",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "take_profit",
                    "status": "Filled",
                    "fill_price": 105.5,
                    "environment": "live",
                },
                {
                    "id": "order-sl-1",
                    "unique_id": "sl_SIG_1",
                    "order_id": "1003",
                    "broker_order_id": "1003",
                    "signal_id": "SIG_1",
                    "trade_group_id": "group_SIG_1",
                    "role": "stop_loss",
                    "status": "Submitted",
                    "environment": "live",
                },
            ]
        )

        service._on_order_fill(
            {
                "orderId": "1001",
                "ticker": "AAPL",
                "side": "BUY",
                "orderType": "LMT",
                "status": "FILLED",
                "cOID": "entry_SIG_1",
                "avgPrice": 100.0,
            }
        )

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["note"], "closed_by_take_profit")
        self.assertEqual(row["extra"]["exit_fill_role"], "take_profit")
        self.assertEqual(row["extra"]["exit_fill_broker_order_id"], "1002")
        self.assertEqual(row["extra"]["exit_fill_price"], 105.5)
        self.assertEqual(1, service.order_lifecycle.reset_count)

    def test_submit_failure_with_missing_bracket_leg_marks_protection_incomplete(self):
        service = _FakeSignalsService()
        sig = {"signal_id": "SIG_1", "symbol": "AAPL", "direction": "long"}
        result = {
            "ok": False,
            "error": "order_submission_unconfirmed:missing=1005",
            "order_ids": ["1003", "1004", "1005"],
            "missing_order_ids": ["1005"],
            "bracket_group": "AAPL_long_group",
            "protection_complete": False,
        }

        service._mark_signal_submit_failed(sig, result)

        row = service.pb.signals["sig-row-1"]
        self.assertEqual(row["status"], "protection_incomplete")
        self.assertEqual(row["note"], "protection_incomplete")
        self.assertTrue(row["extra"]["protection_incomplete"])
        self.assertFalse(row["extra"]["protection_complete"])
        self.assertEqual(["1005"], row["extra"]["missing_order_ids"])
        self.assertEqual(
            "bracket_submission_protection_incomplete",
            row["extra"]["protection_incomplete_diagnostic"]["reason"],
        )

    def test_submit_failure_with_traceable_order_ids_requests_cancel_sync(self):
        service = _FakeSignalsService()

        class _CancelModifier:
            def __init__(self):
                self.cancelled = []

            def cancel_order(self, order_id):
                self.cancelled.append(str(order_id))
                return {"ok": True, "order_id": str(order_id)}

        modifier = _CancelModifier()
        service.order_modifier = modifier
        sig = {"signal_id": "SIG_1", "symbol": "AAPL", "direction": "long"}
        result = {
            "ok": False,
            "error": "order_submission_unconfirmed:missing=1005",
            "order_ids": ["1003", "1004", "1005"],
            "missing_order_ids": ["1005"],
            "bracket_group": "AAPL_long_group",
            "protection_complete": False,
        }

        service._mark_signal_submit_failed(sig, result)

        row = service.pb.signals["sig-row-1"]
        cancel_sync = row["extra"]["submit_failed_cancel_sync"]
        self.assertEqual(["1003", "1004", "1005"], modifier.cancelled)
        self.assertTrue(cancel_sync["attempted"])
        self.assertFalse(cancel_sync["gateway_request_blocked"])
        self.assertEqual("cancel_sync_requested", cancel_sync["reason"])

    def test_validation_rejection_persists_human_reason_and_direction_context(self):
        service = _FakeSignalsService()
        service.signal_processor = SimpleNamespace(target_direction_provider=lambda: {"AAPL": "long"})
        sig = {"signal_id": "SIG_1", "symbol": "AAPL", "direction": "short"}

        service._mark_signal_validation_rejected(sig, "target_direction_mismatch")

        row = service.pb.signals["sig-row-1"]
        extra = row["extra"]
        self.assertEqual(extra["status_reason"], "target_direction_mismatch")
        self.assertEqual(extra["rejection_reason_code"], "target_direction_mismatch")
        self.assertEqual(extra["rejected_by"], "signal_validation")
        self.assertEqual(extra["signal_direction_at_validation"], "short")
        self.assertEqual(extra["target_direction_at_validation"], "long")
        self.assertEqual(extra["target_direction_source"], "active_target_direction_provider")
        self.assertIn("方向不匹配", extra["rejection_reason_human"])

    def test_process_signals_expires_before_readiness_waiting(self):
        service = _FakeSignalsService()
        processed = []
        expired = []

        class _Router:
            def fetch_pending_signals(self):
                return [{"signal_id": "SIG_OLD", "symbol": "AAPL", "direction": "long"}]

            def claim_signal(self, signal_id):
                return True

            def mark_processed(self, signal_id):
                processed.append(signal_id)

            def release_signal(self, signal_id):
                raise AssertionError("expired signal should be finalized, not released")

        service.session_keeper = SimpleNamespace(is_authenticated=True)
        service.signal_router = _Router()
        service._is_fixed_position_signal = lambda sig: False
        service._mark_signal_validation_expired = lambda sig: expired.append(sig["signal_id"])
        service.signal_processor = SimpleNamespace(
            _is_signal_expired=lambda sig, now: True,
            validate_signal=lambda sig: self.fail("expired signal should not wait on readiness"),
        )

        service._process_signals()

        self.assertEqual(["SIG_OLD"], expired)
        self.assertEqual(["SIG_OLD"], processed)

    def test_ack_submission_surfaces_protection_incomplete_status(self):
        service = _FakeSignalsService()
        sig = {
            "signal_id": "SIG_1",
            "symbol": "AAPL",
            "direction": "long",
            "shares": 10,
            "entry": 101.0,
            "take_profit": 105.0,
            "stop_loss": 99.0,
            "raw": {"bar_time_ms": 1, "us_time": "2026-05-06 09:30:00"},
        }
        result = {
            "entry_coid": "entry_SIG_1",
            "tp_coid": "tp_SIG_1",
            "sl_coid": "sl_SIG_1",
            "bracket_group": "AAPL_long_group",
            "order_ids": ["1003", "1004", "1005"],
            "missing_order_ids": ["1005"],
            "protection_complete": False,
        }

        service._ack_signal_after_order_submission(sig, result)

        ack = service.pb.acked[-1]
        self.assertEqual(ack["status"], "protection_incomplete")
        self.assertEqual(ack["note"], "protection_incomplete")
        self.assertEqual(ack["order"]["relation_status"], "protection_incomplete")
        self.assertTrue(ack["order"]["extra"]["safety_cancel_recommended"])
        self.assertEqual(["1005"], ack["order"]["extra"]["missing_order_ids"])


if __name__ == "__main__":
    unittest.main()
