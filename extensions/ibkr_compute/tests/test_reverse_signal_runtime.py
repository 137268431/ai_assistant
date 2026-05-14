import copy
import re
import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.signal.reverse_signal import REVERSE_SIGNAL_COLLECTION, ReverseSignalHandler


class _FakePB:
    def __init__(self, *, reverse_rows=None, order_rows=None, signal_rows=None):
        self.records = {
            REVERSE_SIGNAL_COLLECTION: [copy.deepcopy(row) for row in (reverse_rows or [])],
            "orders": [copy.deepcopy(row) for row in (order_rows or [])],
            "ibkr_signals": [copy.deepcopy(row) for row in (signal_rows or [])],
        }
        self.updates = []
        self.acks = []
        self.created = []

    def get_records(self, collection, filter=None, sort=None, per_page=100, page=1):
        rows = [copy.deepcopy(row) for row in self.records.get(collection, [])]
        rows = [row for row in rows if self._matches(row, str(filter or ""))]
        if sort:
            for key in reversed([part.strip() for part in str(sort).split(",") if part.strip()]):
                reverse = key.startswith("-")
                field = key[1:] if reverse else key
                rows.sort(key=lambda row: row.get(field) or 0, reverse=reverse)
        start = max(0, (int(page) - 1) * int(per_page))
        return rows[start:start + int(per_page)]

    def get_first_record(self, collection, filter=None, sort=None):
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
        return rows[0] if rows else None

    def create_record(self, collection, data):
        row = copy.deepcopy(data)
        row["id"] = row.get("id") or f"{collection}-{len(self.records.get(collection, [])) + 1}"
        self.records.setdefault(collection, []).append(copy.deepcopy(row))
        self.created.append((collection, copy.deepcopy(row)))
        return copy.deepcopy(row)

    def update_record(self, collection, record_id, patch):
        self.updates.append((collection, record_id, copy.deepcopy(patch)))
        for index, row in enumerate(self.records.get(collection, [])):
            if str(row.get("id")) != str(record_id):
                continue
            updated = copy.deepcopy(row)
            updated.update(copy.deepcopy(patch))
            self.records[collection][index] = updated
            return copy.deepcopy(updated)
        raise KeyError(record_id)

    def ack_ibkr_reverse_signal(self, reverse_id, status="confirmed", reason="", detail=None):
        payload = {
            "reverse_id": reverse_id,
            "status": status,
            "reason": reason,
            "detail": copy.deepcopy(detail or {}),
        }
        self.acks.append(payload)
        return {"ok": True, **payload}

    def set_order_status(self, order_id, status):
        for row in self.records["orders"]:
            ids = {str(row.get("broker_order_id") or ""), str(row.get("order_id") or ""), str(row.get("orderId") or "")}
            if str(order_id) in ids:
                row["status"] = status

    @staticmethod
    def _matches(row, filter_text):
        if not filter_text:
            return True
        for field, value in re.findall(r'(\w+)\s*=\s*"([^"]*)"', filter_text):
            if str(row.get(field) or "") != value:
                return False
        return True


class _FakeBroker:
    def __init__(self, open_orders=None):
        self.open_orders = [copy.deepcopy(row) for row in (open_orders or [])]
        self.calls = []

    def list_open_orders(self, include_all=False):
        self.calls.append({"include_all": include_all})
        return [copy.deepcopy(row) for row in self.open_orders]


class _FakeOrderModifier:
    def __init__(self, pb, *, broker=None, ok=True):
        self.pb = pb
        self.broker = broker or _FakeBroker([])
        self.ok = ok
        self.cancelled = []

    def cancel_order(self, order_id):
        self.cancelled.append(str(order_id))
        if not self.ok:
            return {"ok": False, "error": "cancel rejected", "order_id": str(order_id)}
        self.pb.set_order_status(order_id, "Canceled")
        return {"ok": True, "order_id": str(order_id)}


class _FakeOrderLifecycle:
    def __init__(self, position_snapshots):
        self.position_snapshots = [copy.deepcopy(item) for item in position_snapshots]
        self.calls = 0

    def get_positions(self):
        self.calls += 1
        if self.position_snapshots:
            return copy.deepcopy(self.position_snapshots.pop(0))
        return []


class _FakeOrderPlacer:
    def __init__(self, result=None):
        self.result = result or {"ok": True, "order_id": "close-1"}
        self.calls = []

    def place_market_close(self, conid, symbol, direction, quantity, **kwargs):
        self.calls.append(
            {
                "conid": conid,
                "symbol": symbol,
                "direction": direction,
                "quantity": quantity,
                **kwargs,
            }
        )
        return dict(self.result)


class _FakeSignalProcessor:
    def __init__(self):
        self.removed = []
        self.cooldowns = []

    def remove_position(self, symbol):
        self.removed.append(symbol)

    def cooldown_bars_after_reverse(self):
        return 2

    def start_cooldown(self, symbol, bars, reason):
        self.cooldowns.append((symbol, bars, reason))


class ReverseSignalRuntimeTests(unittest.TestCase):
    def test_cancel_pending_bracket_requires_inactive_confirmation_before_ready_reentry(self):
        reverse = {
            "id": "rev-cancel",
            "symbol": "AAPL",
            "action_type": "cancel",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "trade_group_id": "grp-1",
                "entry_order_unique_id": "grp-1",
                "new_direction": "short",
            },
        }
        orders = [
            {"id": "entry", "broker_order_id": "1001", "order_id": "1001", "trade_group_id": "grp-1", "status": "Submitted", "environment": "live"},
            {"id": "tp", "broker_order_id": "1002", "order_id": "1002", "trade_group_id": "grp-1", "status": "PreSubmitted", "environment": "live"},
            {"id": "sl", "broker_order_id": "1003", "order_id": "1003", "trade_group_id": "grp-1", "status": "Submitted", "environment": "live"},
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders)
        broker = _FakeBroker(open_orders=[])
        modifier = _FakeOrderModifier(pb, broker=broker)
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        self.assertEqual(["1001", "1002", "1003"], modifier.cancelled)
        self.assertEqual(1, len(broker.calls))
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("confirmed", extra["cancel_old_order"])
        self.assertTrue(extra["cancel_confirmation"]["confirmed"])
        self.assertEqual("broker_open_orders", extra["cancel_confirmation"]["source"])
        self.assertTrue(extra["ready_reentry"])
        self.assertFalse(extra["reentry_submitted"])
        self.assertFalse(extra["blocked"])
        self.assertEqual("ready_reentry", pb.acks[0]["detail"]["result_status"])

    def test_close_position_requires_flat_confirmation_before_ready_reentry(self):
        reverse = {
            "id": "rev-close",
            "symbol": "AAPL",
            "conid": 123,
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {"new_direction": "short"},
        }
        pb = _FakePB(reverse_rows=[reverse])
        lifecycle = _FakeOrderLifecycle(
            [
                [{"ticker": "AAPL", "position": 10}],
                [{"ticker": "AAPL", "position": 0}],
            ]
        )
        placer = _FakeOrderPlacer()
        processor = _FakeSignalProcessor()
        handler = ReverseSignalHandler(
            pb,
            order_placer=placer,
            order_lifecycle=lifecycle,
            signal_processor=processor,
            environment="live",
        )

        handler.check_and_process()

        self.assertEqual(1, len(placer.calls))
        self.assertEqual(
            {"conid": 123, "symbol": "AAPL", "direction": "long", "quantity": 10},
            {key: placer.calls[0][key] for key in ("conid", "symbol", "direction", "quantity")},
        )
        self.assertEqual("reverse_signal_close", placer.calls[0]["source"])
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("confirmed", extra["close_old_position"])
        self.assertEqual("confirmed", extra["wait_flat"])
        self.assertTrue(extra["flat_confirmation"]["confirmed"])
        self.assertEqual("started", extra["cooldown"])
        self.assertEqual(["AAPL"], processor.removed)
        self.assertEqual([("AAPL", 2, "cooldown_after_reverse_close")], processor.cooldowns)
        self.assertTrue(extra["ready_reentry"])
        self.assertFalse(extra["blocked"])
        self.assertIn("ready_reentry", extra["reverse_state_path"])

    def test_protection_incomplete_blocks_without_broker_actions(self):
        reverse = {
            "id": "rev-blocked",
            "symbol": "AAPL",
            "conid": 123,
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "new_direction": "short",
                "protection_incomplete": True,
                "protection_complete": False,
            },
        }
        pb = _FakePB(reverse_rows=[reverse])
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 10}]])
        placer = _FakeOrderPlacer()
        modifier = _FakeOrderModifier(pb)
        handler = ReverseSignalHandler(
            pb,
            order_placer=placer,
            order_modifier=modifier,
            order_lifecycle=lifecycle,
            environment="live",
        )

        handler.check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("blocked", updated["status"])
        self.assertTrue(extra["blocked"])
        self.assertFalse(extra["ready_reentry"])
        self.assertEqual("protection_incomplete", extra["reentry_blocked"]["reason"])
        self.assertEqual("blocked", extra["reverse_state"])
        self.assertEqual([], placer.calls)
        self.assertEqual([], modifier.cancelled)
        self.assertEqual(0, lifecycle.calls)
        self.assertEqual("blocked", pb.acks[0]["status"])

    def test_cancel_confirmed_creates_reentry_signal_and_closes_origin_signal(self):
        reverse = {
            "id": "rev-reentry",
            "symbol": "AAPL",
            "action_type": "cancel",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "trade_group_id": "grp-1",
                "origin_signal_id": "sig-old",
                "new_direction": "short",
                "reentry_signal_payload": {
                    "environment": "live",
                    "symbol": "AAPL",
                    "signal_id": "sig-new-short",
                    "direction": "short",
                    "entry": 179,
                    "stop_loss": 181,
                    "take_profit": 174,
                    "shares": 10,
                    "date": "2026-05-13",
                    "extra": {"source": "ibkr_compute"},
                },
            },
        }
        orders = [
            {"id": "entry", "broker_order_id": "1001", "order_id": "1001", "trade_group_id": "grp-1", "status": "Submitted", "environment": "live"},
        ]
        signals = [
            {
                "id": "sig-row-old",
                "signal_id": "sig-old",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "submitted",
                "extra": {},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders, signal_rows=signals)
        handler = ReverseSignalHandler(pb, order_modifier=_FakeOrderModifier(pb, broker=_FakeBroker([])), environment="live")

        handler.check_and_process()

        reentry_rows = [row for collection, row in pb.created if collection == "ibkr_signals"]
        self.assertEqual(1, len(reentry_rows))
        self.assertEqual("sig-new-short", reentry_rows[0]["signal_id"])
        self.assertEqual("pending", reentry_rows[0]["status"])
        self.assertTrue(reentry_rows[0]["extra"]["reverse_reentry"])
        self.assertEqual("auto_reverse", reentry_rows[0]["extra"]["signal_confirmation_mode"])
        origin = pb.records["ibkr_signals"][0]
        self.assertEqual("closed", origin["status"])
        self.assertEqual("old_risk_resolved", origin["extra"]["reverse_stage"])
        reverse_extra = pb.records[REVERSE_SIGNAL_COLLECTION][0]["extra"]
        self.assertTrue(reverse_extra["ready_reentry"])
        self.assertTrue(reverse_extra["reentry_submitted"])
        self.assertEqual("sig-new-short", reverse_extra["reentry_signal_id"])


if __name__ == "__main__":
    unittest.main()
