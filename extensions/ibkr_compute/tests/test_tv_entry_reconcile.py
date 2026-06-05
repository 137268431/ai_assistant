import copy
import re
import sys
import time
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration.signals import TradingServiceSignalsMixin


class _FakePB:
    def __init__(self, signal_record, orders):
        self.signal = copy.deepcopy(signal_record)
        self.orders = [copy.deepcopy(row) for row in orders]
        self.acks = []
        self.updates = []

    def get_first_record(self, collection, filter=None, sort=None):
        if collection != "ibkr_signals":
            return None
        criteria = self._criteria(filter)
        if self._matches(self.signal, criteria):
            return copy.deepcopy(self.signal)
        return None

    def get_records(self, collection, filter=None, sort=None, per_page=100, page=1):
        rows = self.orders if collection == "orders" else [self.signal] if collection == "ibkr_signals" else []
        criteria = self._criteria(filter)
        return [copy.deepcopy(row) for row in rows if self._matches(row, criteria)][:per_page]

    def update_record(self, collection, record_id, patch):
        if collection != "ibkr_signals":
            raise AssertionError(f"unexpected collection: {collection}")
        current = copy.deepcopy(self.signal)
        current.update(copy.deepcopy(patch))
        self.signal = current
        self.updates.append((collection, record_id, copy.deepcopy(patch)))
        return copy.deepcopy(current)

    def ack_ibkr_signal(self, **payload):
        self.acks.append(copy.deepcopy(payload))
        extra = copy.deepcopy(self.signal.get("extra") or {})
        order_extra = copy.deepcopy(((payload.get("order") or {}).get("extra") or {}))
        extra.update(order_extra)
        self.signal.update(
            {
                "status": payload.get("status"),
                "note": payload.get("note"),
                "extra": extra,
            }
        )
        order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
        if order:
            self._upsert_order({**order, "environment": payload.get("environment")})
        for child in payload.get("child_orders") or []:
            if isinstance(child, dict):
                self._upsert_order({**child, "environment": payload.get("environment")})
        return {"status": payload.get("status"), "fallback": False}

    def _upsert_order(self, order):
        order = copy.deepcopy(order)
        key = str(order.get("unique_id") or order.get("order_id") or order.get("broker_order_id") or "")
        for index, row in enumerate(self.orders):
            row_key = str(row.get("unique_id") or row.get("order_id") or row.get("broker_order_id") or "")
            if key and row_key == key:
                merged = copy.deepcopy(row)
                merged.update(order)
                self.orders[index] = merged
                return
        order.setdefault("id", f"order-{len(self.orders) + 1}")
        self.orders.append(order)

    @staticmethod
    def _criteria(filter_text):
        return re.findall(r'([A-Za-z0-9_]+)\s*=\s*"([^"]*)"', str(filter_text or ""))

    @staticmethod
    def _matches(row, criteria):
        for field, expected in criteria:
            if str(row.get(field) or "") != expected:
                return False
        return True


class _FakeSignalRouter:
    def __init__(self, signals):
        self.signals = [copy.deepcopy(sig) for sig in signals]
        self.processed = []
        self.released = []

    def fetch_pending_signals(self):
        return [copy.deepcopy(sig) for sig in self.signals]

    def claim_signal(self, signal_id):
        return True

    def mark_processed(self, signal_id):
        self.processed.append(signal_id)

    def release_signal(self, signal_id):
        self.released.append(signal_id)


class _FakeSignalProcessor:
    def __init__(self):
        self.pending_entries = []

    def validate_signal(self, signal):
        raise AssertionError("TV direct reconcile should not use runtime validation")

    def register_pending_entry(self, symbol, position_data):
        self.pending_entries.append((symbol, dict(position_data or {})))


class _FakeOrderTracker:
    def __init__(self, live_orders):
        self.live_orders = [copy.deepcopy(row) for row in live_orders]
        self.registered = []
        self.synced = []

    def get_complete_live_open_orders(self, *, pb_seed_ids=None, **kwargs):
        seeds = {str(item or "").strip() for item in (pb_seed_ids or []) if str(item or "").strip()}
        orders = []
        for row in self.live_orders:
            order_id = str(row.get("orderId") or row.get("order_id") or row.get("broker_order_id") or "")
            if seeds and order_id not in seeds:
                continue
            orders.append(copy.deepcopy(row))
        return {
            "orders": orders,
            "coverage": {"coverage_state": "complete", "unresolved_order_ids": []},
            "diagnostics": {"seed_sources": {seed: ["pb"] for seed in seeds}},
        }

    def sync_live_orders_snapshot(self, orders=None):
        self.synced.extend(copy.deepcopy(orders or []))
        return len(orders or [])

    def find_duplicate_open_entry(self, **kwargs):
        return None

    def register_submitted_orders(self, order_ids, seed=None):
        self.registered.append((list(order_ids or []), dict(seed or {})))


class _FakeOrderPlacer:
    def __init__(self):
        self.calls = []

    def place_bracket_order(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "ok": True,
            "order_ids": ["901", "902", "903"],
            "bracket_group": kwargs.get("trade_group_id") or kwargs.get("bracket_group") or "new_group",
            "protection_complete": True,
        }


class _FakeConfig:
    def get_bool_for_environment(self, key, environment, default=False):
        return bool(default)

    def get_float_for_environment(self, key, environment, default=0.0):
        return float(default)

    def get_for_environment(self, key, environment, default=None):
        return default


class _FakeConidResolver:
    def resolve(self, symbol):
        return 123


class _FakeLifecycle:
    def is_fixed_position_symbol(self, symbol):
        return False

    def strategy_capacity_snapshot(self, order_tracker=None):
        return {"capacity_full": False}

    @property
    def is_sl_circuit_breaker(self):
        return False

    @property
    def is_position_limit_reached(self):
        return False

    def increment_position_count(self):
        pass


class _FakeSessionKeeper:
    is_authenticated = True


class _Service(TradingServiceSignalsMixin):
    def __init__(self, signal, *, pb, live_orders):
        from ibkr_compute.orchestration import trading_service as service_mod

        service_mod.ENVIRONMENT = "paper"
        service_mod.DATA_ENVIRONMENT = "live"
        self.session_keeper = _FakeSessionKeeper()
        self.signal_router = _FakeSignalRouter([signal])
        self.signal_processor = _FakeSignalProcessor()
        self.order_tracker = _FakeOrderTracker(live_orders)
        self.conid_resolver = _FakeConidResolver()
        self.order_placer = _FakeOrderPlacer()
        self.order_lifecycle = _FakeLifecycle()
        self.pb = pb
        self.config = _FakeConfig()
        self.account_snapshot_provider = lambda: {
            "ok": True,
            "summary": {"buying_power": 100000, "net_liquidation": 120000},
        }

    def _now_iso(self):
        return "2026-06-06T10:00:00-04:00"


def _tv_signal(signal_id="SIG_TV_RECON", group="group_SIG_TV_RECON"):
    pine_eval_ms = int((time.time() - 1.0) * 1000)
    extra = {
        "source": "tradingview",
        "pine_eval_ms": pine_eval_ms,
        "trade_group_id": group,
        "bracket_group": group,
    }
    return {
        "signal_id": signal_id,
        "symbol": "AAPL",
        "direction": "long",
        "entry": 100.0,
        "stop_loss": 98.0,
        "take_profit": 104.0,
        "shares": 10,
        "source": "tradingview",
        "signal_time": "2026-06-06 10:00:00",
        "extra": dict(extra),
        "raw": {
            "signal_id": signal_id,
            "symbol": "AAPL",
            "environment": "live",
            "source": "tradingview",
            "extra": dict(extra),
        },
    }


def _signal_record(signal):
    return {
        "id": "signal-row",
        "signal_id": signal["signal_id"],
        "symbol": signal["symbol"],
        "environment": "live",
        "status": "pending",
        "extra": copy.deepcopy(signal["extra"]),
    }


def _pb_order(signal, *, role, order_id, status, unique_id, group=None):
    group = group or signal["extra"]["trade_group_id"]
    return {
        "id": f"order-{order_id}",
        "signal_id": signal["signal_id"],
        "symbol": signal["symbol"],
        "environment": "paper",
        "trade_group_id": group,
        "bracket_group": group,
        "unique_id": unique_id,
        "entry_order_unique_id": f"entry_{group}",
        "order_id": str(order_id),
        "broker_order_id": str(order_id),
        "role": role,
        "status": status,
        "quantity": 10,
        "extra": {"trade_group_id": group, "bracket_group": group},
    }


def _live_order(signal, *, role, order_id, status, unique_id, parent_id=""):
    side = "BUY" if role == "entry" else "SELL"
    order_type = "STP" if role == "stop_loss" else "LMT"
    return {
        "orderId": str(order_id),
        "ticker": signal["symbol"],
        "side": side,
        "orderType": order_type,
        "totalSize": 10,
        "filledQuantity": 0,
        "avgPrice": 0,
        "parentId": str(parent_id or ""),
        "status": status,
        "cOID": unique_id,
    }


class TVEntryReconcileTest(unittest.TestCase):
    def test_existing_open_bracket_marks_submitted_waiting_fill_without_duplicate_submit(self):
        signal = _tv_signal()
        group = signal["extra"]["trade_group_id"]
        pb_orders = [
            _pb_order(signal, role="entry", order_id="101", status="Submitted", unique_id=f"entry_{group}"),
            _pb_order(signal, role="take_profit", order_id="102", status="Submitted", unique_id=f"tp_{group}"),
            _pb_order(signal, role="stop_loss", order_id="103", status="Submitted", unique_id=f"sl_{group}"),
        ]
        live_orders = [
            _live_order(signal, role="entry", order_id="101", status="Submitted", unique_id=f"entry_{group}"),
            _live_order(signal, role="take_profit", order_id="102", status="Submitted", unique_id=f"tp_{group}", parent_id="101"),
            _live_order(signal, role="stop_loss", order_id="103", status="Submitted", unique_id=f"sl_{group}", parent_id="101"),
        ]
        pb = _FakePB(_signal_record(signal), pb_orders)
        service = _Service(signal, pb=pb, live_orders=live_orders)

        service._process_signals()

        self.assertEqual([], service.order_placer.calls)
        self.assertEqual([signal["signal_id"]], service.signal_router.processed)
        self.assertEqual([], service.signal_router.released)
        self.assertEqual("submitted_waiting_fill", pb.acks[-1]["status"])
        extra = pb.signal["extra"]
        self.assertTrue(extra["tv_direct_reconciled"])
        self.assertEqual("submitted_waiting_fill", extra["execution_by_mode"]["paper"]["status"])
        self.assertEqual(["101", "102", "103"], extra["execution_by_mode"]["paper"]["order_ids"])
        self.assertTrue(service.order_tracker.synced)

    def test_filled_entry_with_live_protection_marks_execution_mode_filled_position(self):
        signal = _tv_signal(signal_id="SIG_TV_FILLED", group="group_SIG_TV_FILLED")
        group = signal["extra"]["trade_group_id"]
        pb_orders = [
            {
                **_pb_order(signal, role="entry", order_id="201", status="Filled", unique_id=f"entry_{group}"),
                "filled_qty": 10,
                "fill_price": 100.25,
            },
            _pb_order(signal, role="take_profit", order_id="202", status="Submitted", unique_id=f"tp_{group}"),
            _pb_order(signal, role="stop_loss", order_id="203", status="Submitted", unique_id=f"sl_{group}"),
        ]
        live_orders = [
            _live_order(signal, role="take_profit", order_id="202", status="Submitted", unique_id=f"tp_{group}", parent_id="201"),
            _live_order(signal, role="stop_loss", order_id="203", status="Submitted", unique_id=f"sl_{group}", parent_id="201"),
        ]
        pb = _FakePB(_signal_record(signal), pb_orders)
        service = _Service(signal, pb=pb, live_orders=live_orders)

        service._process_signals()

        self.assertEqual([], service.order_placer.calls)
        self.assertEqual([signal["signal_id"]], service.signal_router.processed)
        self.assertEqual([], pb.acks)
        extra = pb.signal["extra"]
        self.assertTrue(extra["tv_direct_reconciled"])
        self.assertEqual("filled_position", extra["execution_by_mode"]["paper"]["status"])
        self.assertEqual("tv_direct_reconciled_filled_protected", extra["execution_by_mode"]["paper"]["note"])
        self.assertEqual("filled_position", extra["signal_lifecycle_status"])
        self.assertTrue(extra["protection_complete"])
        self.assertEqual(100.25, extra["entry_fill_price"])
        self.assertEqual("201", extra["entry_fill_broker_order_id"])


if __name__ == "__main__":
    unittest.main()
