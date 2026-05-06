import copy
import re
import sys
import unittest
from pathlib import Path

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
                "environment": "live",
                "status": "submitted",
                "extra": {"source": "ibkr_compute", "bracket_group": "group_SIG_1"},
            },
            "sig-row-2": {
                "id": "sig-row-2",
                "signal_id": "SIG_2",
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
        self.updated = []
        self.acked = []

    def get_records(self, collection, filter=None, sort=None, per_page=100, page=1):
        rows = self.orders if collection == "orders" else []
        text = str(filter or "")
        unique_id = self._extract(text, "unique_id")
        entry_unique_id = self._extract(text, "entry_order_unique_id")
        order_id = self._extract(text, "order_id")
        broker_order_id = self._extract(text, "broker_order_id")
        signal_id = self._extract(text, "signal_id")
        trade_group_id = self._extract(text, "trade_group_id")
        environment = self._extract(text, "environment")
        result = []
        for row in rows:
            if environment and row.get("environment") != environment:
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
        current = copy.deepcopy(self.signals[str(record_id)])
        current.update(copy.deepcopy(patch))
        self.signals[str(record_id)] = current
        self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
        return copy.deepcopy(current)

    def ack_ibkr_signal(self, **payload):
        self.acked.append(copy.deepcopy(payload))
        return {"status": payload.get("status"), "fallback": False}

    @staticmethod
    def _extract(filter_text, field):
        match = re.search(rf'{re.escape(field)}\s*=\s*"([^"]*)"', filter_text)
        return match.group(1) if match else ""


class _FakeSignalProcessor:
    def __init__(self):
        self.filled = []

    def register_filled_position(self, symbol, payload):
        self.filled.append((symbol, dict(payload)))


class _FakeOrderLifecycle:
    def __init__(self):
        self.diagnostics = []

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


class _FakeService(TradingServiceRuntimeOpsMixin):
    def __init__(self):
        self.pb = _FakePB()
        self.signal_processor = _FakeSignalProcessor()
        self.order_lifecycle = _FakeOrderLifecycle()


class _FakeSignalsService(TradingServiceSignalsMixin):
    def __init__(self):
        self.pb = _FakePB()
        self.order_lifecycle = _FakeOrderLifecycle()

    def _now_iso(self):
        return "2026-05-06T12:00:00Z"


class RuntimeSignalLifecycleTest(unittest.TestCase):
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
