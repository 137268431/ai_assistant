import copy
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.signal.reverse_signal import REVERSE_SIGNAL_COLLECTION, ReverseSignalHandler


class _FakePB:
    def __init__(self, *, reverse_rows=None, order_rows=None, signal_rows=None):
        normalized_reverse_rows = []
        for row in reverse_rows or []:
            item = copy.deepcopy(row)
            item.setdefault("source", "tradingview")
            normalized_reverse_rows.append(item)
        self.records = {
            REVERSE_SIGNAL_COLLECTION: normalized_reverse_rows,
            "orders": [copy.deepcopy(row) for row in (order_rows or [])],
            "ibkr_signals": [copy.deepcopy(row) for row in (signal_rows or [])],
        }
        self.updates = []
        self.acks = []
        self.created = []
        self.events = []

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

    def notify_system_event(self, title, detail=None, **kwargs):
        self.events.append({"title": title, "detail": copy.deepcopy(detail or {}), **copy.deepcopy(kwargs)})
        return {"ok": True}

    def set_order_status(self, order_id, status):
        for row in self.records["orders"]:
            ids = {str(row.get("broker_order_id") or ""), str(row.get("order_id") or ""), str(row.get("orderId") or "")}
            if str(order_id) in ids:
                row["status"] = status

    def set_order_price(self, order_id, side, price):
        updated = None
        for row in self.records["orders"]:
            ids = {str(row.get("broker_order_id") or ""), str(row.get("order_id") or ""), str(row.get("orderId") or "")}
            if str(order_id) not in ids:
                continue
            row["status"] = row.get("status") or "Submitted"
            if side == "stop_loss":
                row["auxPrice"] = float(price)
            else:
                row["price"] = float(price)
                row["lmtPrice"] = float(price)
            updated = copy.deepcopy(row)
        return updated

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

    def set_order_price(self, order_id, side, price):
        updated = None
        for row in self.open_orders:
            ids = {
                str(row.get("broker_order_id") or ""),
                str(row.get("order_id") or ""),
                str(row.get("orderId") or ""),
                str(row.get("id") or ""),
            }
            if str(order_id) not in ids:
                continue
            row["status"] = row.get("status") or "Submitted"
            if side == "stop_loss":
                row["auxPrice"] = float(price)
            else:
                row["price"] = float(price)
                row["lmtPrice"] = float(price)
            updated = copy.deepcopy(row)
        return updated


class _FakeOrderModifier:
    def __init__(self, pb, *, broker=None, ok=True, update_failures=None):
        self.pb = pb
        self.broker = broker or _FakeBroker([])
        self.ok = ok
        self.cancelled = []
        self.update_failures = set(update_failures or [])
        self.stop_updates = []
        self.take_profit_updates = []

    def cancel_order(self, order_id, *args, **kwargs):
        self.cancelled.append(str(order_id))
        if not self.ok:
            return {"ok": False, "error": "cancel rejected", "order_id": str(order_id)}
        self.pb.set_order_status(order_id, "Canceled")
        return {"ok": True, "order_id": str(order_id)}

    def update_stop_loss(self, order_id, new_sl_price):
        self.stop_updates.append((str(order_id), float(new_sl_price)))
        if "stop_loss" in self.update_failures:
            return {"ok": False, "error": "stop update rejected", "order_id": str(order_id)}
        order = self.pb.set_order_price(order_id, "stop_loss", new_sl_price)
        broker_order = getattr(self.broker, "set_order_price", lambda *_args: None)(order_id, "stop_loss", new_sl_price)
        return {
            "ok": True,
            "order_id": str(order_id),
            "price": float(new_sl_price),
            "order": broker_order or order or {
                "orderId": str(order_id),
                "status": "Submitted",
                "role": "stop_loss",
                "auxPrice": float(new_sl_price),
            },
        }

    def update_take_profit(self, order_id, new_tp_price):
        self.take_profit_updates.append((str(order_id), float(new_tp_price)))
        if "take_profit" in self.update_failures:
            return {"ok": False, "error": "take profit update rejected", "order_id": str(order_id)}
        order = self.pb.set_order_price(order_id, "take_profit", new_tp_price)
        broker_order = getattr(self.broker, "set_order_price", lambda *_args: None)(order_id, "take_profit", new_tp_price)
        return {
            "ok": True,
            "order_id": str(order_id),
            "price": float(new_tp_price),
            "order": broker_order or order or {
                "orderId": str(order_id),
                "status": "Submitted",
                "role": "take_profit",
                "price": float(new_tp_price),
                "lmtPrice": float(new_tp_price),
            },
        }


class _TerminalCancelOrderModifier(_FakeOrderModifier):
    def __init__(self, pb, *, broker=None, results=None):
        super().__init__(pb, broker=broker)
        self.results = {str(key): copy.deepcopy(value) for key, value in (results or {}).items()}

    def cancel_order(self, order_id, *args, **kwargs):
        oid = str(order_id)
        self.cancelled.append(oid)
        result = copy.deepcopy(self.results.get(oid, {"ok": True, "order_id": oid, "status": "CANCELLED"}))
        result.setdefault("order_id", oid)
        if result.get("ok"):
            self.pb.set_order_status(oid, str(result.get("status") or "Canceled"))
        return result


class _FakeOrderLifecycle:
    def __init__(self, position_snapshots=None, position_results=None):
        self.position_snapshots = [copy.deepcopy(item) for item in (position_snapshots or [])]
        self.position_results = [copy.deepcopy(item) for item in (position_results or [])]
        self.calls = 0

    def get_positions_result(self):
        self.calls += 1
        if self.position_results:
            return copy.deepcopy(self.position_results.pop(0))
        if self.position_snapshots:
            return {"ok": True, "positions": copy.deepcopy(self.position_snapshots.pop(0))}
        return {"ok": True, "positions": []}

    def get_positions(self):
        result = self.get_positions_result()
        return copy.deepcopy(result.get("positions") or []) if result.get("ok") else []


class _FakeOrderPlacer:
    def __init__(self, result=None, exception=None):
        self.results = [copy.deepcopy(item) for item in result] if isinstance(result, list) else []
        self.result = {"ok": True, "order_id": "close-1"} if isinstance(result, list) else (result or {"ok": True, "order_id": "close-1"})
        self.exception = exception
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
        if self.exception:
            raise self.exception
        if self.results:
            return copy.deepcopy(self.results.pop(0))
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


def _submitted_child_orders(*order_ids, environment="live"):
    rows = []
    for order_id in order_ids:
        text = str(order_id)
        role = "stop_loss" if text.startswith("sl") else "take_profit" if text.startswith("tp") else ""
        rows.append(
            {
                "id": f"order-{text}",
                "broker_order_id": text,
                "order_id": text,
                "role": role,
                "status": "Submitted",
                "environment": environment,
            }
        )
    return rows


class ReverseSignalRuntimeTests(unittest.TestCase):
    def test_preflight_treats_tv_direct_filled_statuses_as_closeable_exposure(self):
        for status in ("filled_position", "filled_repricing_protection", "protection_reprice_failed"):
            with self.subTest(status=status):
                reverse = {
                    "id": f"rev-{status}",
                    "symbol": "AAPL",
                    "action_type": "close",
                    "environment": "live",
                    "extra": {"origin_signal_id": f"sig-{status}"},
                }
                signals = [
                    {
                        "id": f"row-{status}",
                        "signal_id": f"sig-{status}",
                        "environment": "live",
                        "symbol": "AAPL",
                        "status": status,
                        "extra": {"execution_by_mode": {"live": {"status": status}}},
                    }
                ]
                pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
                preflight = ReverseSignalHandler(pb, environment="live")._execution_preflight(reverse, "close")

                self.assertTrue(preflight["ok"])
                self.assertTrue(preflight["filled_order_or_position_confirmed"])

    def test_preflight_treats_tv_direct_missed_entry_as_terminal_not_closeable(self):
        reverse = {
            "id": "rev-missed",
            "symbol": "AAPL",
            "action_type": "close",
            "environment": "live",
            "extra": {"origin_signal_id": "sig-missed"},
        }
        signals = [
            {
                "id": "row-missed",
                "signal_id": "sig-missed",
                "environment": "live",
                "symbol": "AAPL",
                "status": "entry_missed_limit_cap",
                "extra": {"execution_by_mode": {"live": {"status": "entry_missed_limit_cap"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        preflight = ReverseSignalHandler(pb, environment="live")._execution_preflight(reverse, "close")

        self.assertFalse(preflight["ok"])
        self.assertTrue(preflight["origin_execution_terminal"])
        self.assertEqual("cancelled", preflight["ack_status"])

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
        self.assertFalse(extra["ready_reentry"])
        self.assertFalse(extra["reentry_submitted"])
        self.assertFalse(extra["blocked"])
        self.assertTrue(extra["auto_reentry_disabled"])
        self.assertEqual("tv_exit_confirmed", pb.acks[0]["detail"]["result_status"])
        self.assertEqual("tv_exit_confirmed_no_reentry", pb.acks[0]["reason"])

    def test_tv_close_treats_filled_during_cancel_as_safe_when_flat_and_orders_inactive(self):
        reverse = {
            "id": "rev-terminal-flat",
            "symbol": "AAPL",
            "conid": 123,
            "source": "tradingview",
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "trade_group_id": "grp-1",
                "origin_signal_id": "sig-old",
                "new_direction": "short",
            },
        }
        orders = [
            {"id": "entry", "broker_order_id": "1001", "order_id": "1001", "trade_group_id": "grp-1", "role": "entry", "status": "Submitted", "environment": "live"},
            {"id": "tp", "broker_order_id": "1002", "order_id": "1002", "trade_group_id": "grp-1", "role": "take_profit", "status": "Submitted", "environment": "live"},
            {"id": "sl", "broker_order_id": "1003", "order_id": "1003", "trade_group_id": "grp-1", "role": "stop_loss", "status": "Submitted", "environment": "live"},
        ]
        signals = [
            {
                "id": "sig-row-old",
                "signal_id": "sig-old",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "protected_active",
                "extra": {"execution_by_mode": {"live": {"status": "protected_active"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders, signal_rows=signals)
        modifier = _TerminalCancelOrderModifier(
            pb,
            broker=_FakeBroker(open_orders=[]),
            results={
                "1001": {"ok": False, "error": "order_filled_during_cancel", "order": {"orderId": "1001", "status": "Filled"}},
                "1002": {"ok": True, "status": "CANCELLED", "order": {"orderId": "1002", "status": "Cancelled"}},
                "1003": {"ok": False, "error": "order_filled_during_cancel", "order": {"orderId": "1003", "status": "Filled"}},
            },
        )
        lifecycle = _FakeOrderLifecycle(
            [
                [{"ticker": "AAPL", "position": 10}],
                [{"ticker": "AAPL", "position": 0}],
            ]
        )
        placer = _FakeOrderPlacer()

        ReverseSignalHandler(
            pb,
            order_placer=placer,
            order_modifier=modifier,
            order_lifecycle=lifecycle,
            environment="live",
        ).check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual(["1001", "1002", "1003"], modifier.cancelled)
        self.assertEqual([], placer.calls)
        self.assertTrue(extra["cancel_terminal_during_cancel"])
        self.assertTrue(extra["terminal_conflict_safe"])
        self.assertEqual("confirmed", extra["cancel_old_order"])
        self.assertEqual("confirmed", extra["close_old_position"])
        self.assertEqual("confirmed", extra["wait_flat"])
        self.assertFalse(extra["blocked"])
        self.assertEqual("tv_exit_confirmed_no_reentry", pb.acks[0]["reason"])

    def test_tv_exit_no_quote_uses_market_fallback_before_canceling_protection(self):
        reverse = {
            "id": "rev-tv-exit-no-quote",
            "symbol": "AAPL",
            "conid": 123,
            "source": "tradingview",
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "exit",
                "trade_group_id": "grp-no-quote",
                "origin_signal_id": "sig-no-quote",
                "exit_reason": "stop_loss",
            },
        }
        orders = [
            {"id": "entry", "broker_order_id": "1001", "order_id": "1001", "trade_group_id": "grp-no-quote", "role": "entry", "status": "Filled", "environment": "live"},
            {"id": "tp", "broker_order_id": "1002", "order_id": "1002", "trade_group_id": "grp-no-quote", "role": "take_profit", "status": "Submitted", "environment": "live"},
            {"id": "sl", "broker_order_id": "1003", "order_id": "1003", "trade_group_id": "grp-no-quote", "role": "stop_loss", "status": "Submitted", "environment": "live"},
        ]
        signals = [
            {
                "id": "sig-row-no-quote",
                "signal_id": "sig-no-quote",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "protected_active",
                "extra": {"execution_by_mode": {"live": {"status": "protected_active"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders, signal_rows=signals)
        modifier = _FakeOrderModifier(pb, broker=_FakeBroker(open_orders=[]))
        lifecycle = _FakeOrderLifecycle(
            [
                [{"ticker": "AAPL", "position": 10}],
                [{"ticker": "AAPL", "position": 0}],
            ]
        )
        placer = _FakeOrderPlacer(
            result=[
                {"ok": False, "error": "close_limit_price_unavailable"},
                {"ok": True, "order_id": "close-1", "submitted": True},
            ]
        )

        ReverseSignalHandler(
            pb,
            order_placer=placer,
            order_modifier=modifier,
            order_lifecycle=lifecycle,
            environment="live",
        ).check_and_process()

        self.assertEqual(2, len(placer.calls))
        self.assertEqual("LMT", placer.calls[0].get("order_type", "LMT"))
        self.assertTrue(placer.calls[0]["safety_action"])
        self.assertEqual("MKT", placer.calls[1]["order_type"])
        self.assertTrue(placer.calls[1]["allow_market"])
        self.assertTrue(placer.calls[1]["bypass_normal_symbol_queue"])
        self.assertEqual(["1002", "1003"], modifier.cancelled)
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertTrue(extra["protection_cancel_deferred"])
        self.assertTrue(extra["safety_market_close_fallback_attempted"])
        self.assertEqual("confirmed", extra["close_old_position"])
        self.assertEqual("confirmed", extra["wait_flat"])

    def test_tv_close_keeps_active_protection_when_single_flat_snapshot_has_no_exit_fill(self):
        reverse = {
            "id": "rev-flat-uncertain",
            "symbol": "AAPL",
            "conid": 123,
            "source": "tradingview",
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "trade_group_id": "grp-flat-uncertain",
                "origin_signal_id": "sig-flat-uncertain",
            },
        }
        orders = [
            {
                "id": "entry",
                "broker_order_id": "1001",
                "order_id": "1001",
                "trade_group_id": "grp-flat-uncertain",
                "role": "entry",
                "status": "Filled",
                "environment": "live",
            },
            {
                "id": "tp",
                "broker_order_id": "1002",
                "order_id": "1002",
                "trade_group_id": "grp-flat-uncertain",
                "role": "take_profit",
                "status": "Submitted",
                "environment": "live",
            },
            {
                "id": "sl",
                "broker_order_id": "1003",
                "order_id": "1003",
                "trade_group_id": "grp-flat-uncertain",
                "role": "stop_loss",
                "status": "Submitted",
                "environment": "live",
            },
        ]
        signals = [
            {
                "id": "sig-flat-uncertain-row",
                "signal_id": "sig-flat-uncertain",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "protected_active",
                "extra": {"execution_by_mode": {"live": {"status": "protected_active"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders, signal_rows=signals)
        modifier = _FakeOrderModifier(pb)
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 0}]])
        placer = _FakeOrderPlacer()

        ReverseSignalHandler(
            pb,
            order_placer=placer,
            order_modifier=modifier,
            order_lifecycle=lifecycle,
            environment="live",
        ).check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("pending", updated["status"])
        self.assertIn("flat_uncertain_active_protection_kept", updated["reason"])
        self.assertEqual("pending_retry", extra["result_status"])
        self.assertEqual("skipped_flat_uncertain", extra["close_old_position"])
        self.assertEqual("unconfirmed", extra["wait_flat"])
        self.assertTrue(extra["flat_uncertain"])
        self.assertTrue(extra["protection_cancel_blocked"])
        self.assertEqual(["1002", "1003"], extra["flat_zero_protection_guard"]["active_protection_order_ids"])
        self.assertEqual([], modifier.cancelled)
        self.assertEqual([], placer.calls)
        self.assertEqual(1, len(pb.events))
        self.assertEqual("TV 平仓延迟：flat 快照不可信，已保留保护单", pb.events[0]["title"])
        self.assertEqual("pending", pb.acks[0]["status"])

    def test_tv_close_allows_residual_protection_cancel_when_exit_fill_is_seen(self):
        reverse = {
            "id": "rev-flat-exit-fill",
            "symbol": "AAPL",
            "conid": 123,
            "source": "tradingview",
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "trade_group_id": "grp-flat-exit-fill",
                "origin_signal_id": "sig-flat-exit-fill",
            },
        }
        orders = [
            {
                "id": "entry",
                "broker_order_id": "1001",
                "order_id": "1001",
                "trade_group_id": "grp-flat-exit-fill",
                "role": "entry",
                "status": "Filled",
                "environment": "live",
            },
            {
                "id": "tp",
                "broker_order_id": "1002",
                "order_id": "1002",
                "trade_group_id": "grp-flat-exit-fill",
                "role": "take_profit",
                "status": "Submitted",
                "environment": "live",
            },
            {
                "id": "sl",
                "broker_order_id": "1003",
                "order_id": "1003",
                "trade_group_id": "grp-flat-exit-fill",
                "role": "stop_loss",
                "status": "Filled",
                "environment": "live",
            },
        ]
        signals = [
            {
                "id": "sig-flat-exit-fill-row",
                "signal_id": "sig-flat-exit-fill",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "protected_active",
                "extra": {"execution_by_mode": {"live": {"status": "protected_active"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders, signal_rows=signals)
        modifier = _FakeOrderModifier(pb)
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 0}]])
        placer = _FakeOrderPlacer()

        ReverseSignalHandler(
            pb,
            order_placer=placer,
            order_modifier=modifier,
            order_lifecycle=lifecycle,
            environment="live",
        ).check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual(["1002"], modifier.cancelled)
        self.assertEqual([], placer.calls)
        self.assertEqual("confirmed", extra["cancel_old_order"])
        self.assertEqual("skipped_flat", extra["close_old_position"])
        self.assertEqual(["1002"], extra["flat_zero_protection_guard"]["active_protection_order_ids"])
        self.assertEqual(["1003"], extra["flat_zero_protection_guard"]["exit_fill_order_ids"])
        self.assertEqual([], pb.events)
        self.assertEqual("tv_exit_confirmed_no_reentry", pb.acks[0]["reason"])

    def test_tv_close_terminal_cancel_conflict_stays_pending_when_order_still_open(self):
        reverse = {
            "id": "rev-terminal-pending",
            "symbol": "AAPL",
            "conid": 123,
            "source": "tradingview",
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {"trade_group_id": "grp-1", "origin_signal_id": "sig-old"},
        }
        orders = [
            {"id": "entry", "broker_order_id": "1001", "order_id": "1001", "trade_group_id": "grp-1", "role": "entry", "status": "Submitted", "environment": "live"},
        ]
        signals = [
            {
                "id": "sig-row-old",
                "signal_id": "sig-old",
                "environment": "live",
                "symbol": "AAPL",
                "status": "protected_active",
                "extra": {"execution_by_mode": {"live": {"status": "protected_active"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders, signal_rows=signals)
        modifier = _TerminalCancelOrderModifier(
            pb,
            broker=_FakeBroker(open_orders=[{"orderId": "1001", "status": "Submitted", "orderRef": "grp-1"}]),
            results={"1001": {"ok": False, "error": "order_filled_during_cancel", "order": {"orderId": "1001", "status": "Filled"}}},
        )
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 0}]])
        placer = _FakeOrderPlacer()

        ReverseSignalHandler(
            pb,
            order_placer=placer,
            order_modifier=modifier,
            order_lifecycle=lifecycle,
            environment="live",
        ).check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("pending", updated["status"])
        self.assertIn("cancel_terminal_inactive_not_confirmed", updated["reason"])
        self.assertEqual("pending_retry", extra["result_status"])
        self.assertTrue(extra["cancel_terminal_during_cancel"])
        self.assertFalse(extra.get("terminal_conflict_safe", False))
        self.assertEqual([], placer.calls)
        self.assertEqual("pending", pb.acks[0]["status"])

    def test_true_cancel_failure_still_blocks_tv_close(self):
        reverse = {
            "id": "rev-cancel-rejected",
            "symbol": "AAPL",
            "conid": 123,
            "source": "tradingview",
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {"trade_group_id": "grp-1", "origin_signal_id": "sig-old"},
        }
        orders = [
            {"id": "entry", "broker_order_id": "1001", "order_id": "1001", "trade_group_id": "grp-1", "role": "entry", "status": "Submitted", "environment": "live"},
        ]
        signals = [
            {
                "id": "sig-row-old",
                "signal_id": "sig-old",
                "environment": "live",
                "symbol": "AAPL",
                "status": "protected_active",
                "extra": {"execution_by_mode": {"live": {"status": "protected_active"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders, signal_rows=signals)
        modifier = _TerminalCancelOrderModifier(
            pb,
            results={"1001": {"ok": False, "error": "cancel_rejected", "order": {"orderId": "1001", "status": "Submitted"}}},
        )
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 0}]])
        placer = _FakeOrderPlacer()

        ReverseSignalHandler(
            pb,
            order_placer=placer,
            order_modifier=modifier,
            order_lifecycle=lifecycle,
            environment="live",
        ).check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("cancel_order_failed", updated["reason"])
        self.assertEqual("reentry_blocked", extra["result_status"])
        self.assertEqual("cancel_order_failed", extra["reentry_blocked"]["reason"])
        self.assertEqual([], placer.calls)
        self.assertEqual("cancelled", pb.acks[0]["status"])

    def test_cancel_without_traceable_order_expires_without_gateway(self):
        reverse = {
            "id": "rev-cancel-no-order",
            "symbol": "AAPL",
            "action_type": "cancel",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {"new_direction": "short"},
        }
        pb = _FakePB(reverse_rows=[reverse])
        broker = _FakeBroker(open_orders=[{"orderId": "1001", "status": "Submitted"}])
        modifier = _FakeOrderModifier(pb, broker=broker)

        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("expired", updated["status"])
        self.assertIn("traceable_order_required_for_cancel", updated["reason"])
        self.assertEqual("real_order_preflight", extra["invalidated_by"])
        self.assertTrue(extra["gateway_request_blocked"])
        self.assertEqual([], modifier.cancelled)
        self.assertEqual([], broker.calls)

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
            "extra": {"new_direction": "short", "origin_signal_id": "sig-close"},
        }
        signals = [
            {
                "id": "sig-close-row",
                "signal_id": "sig-close",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "protected_active",
                "extra": {"execution_by_mode": {"live": {"status": "filled"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
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
        self.assertFalse(extra["ready_reentry"])
        self.assertFalse(extra["blocked"])
        self.assertTrue(extra["auto_reentry_disabled"])
        self.assertIn("tv_exit_confirmed", extra["reverse_state_path"])

    def test_close_keeps_pending_and_alerts_when_position_snapshot_unavailable(self):
        reverse = {
            "id": "rev-close-no-positions",
            "symbol": "AAPL",
            "conid": 123,
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "origin_signal_id": "sig-close-no-positions",
                "trade_group_id": "grp-no-positions",
            },
        }
        signals = [
            {
                "id": "sig-close-no-positions-row",
                "signal_id": "sig-close-no-positions",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "protected_active",
                "extra": {"execution_by_mode": {"live": {"status": "filled"}}},
            }
        ]
        orders = [
            {
                "id": "tp",
                "broker_order_id": "1002",
                "order_id": "1002",
                "role": "take_profit",
                "trade_group_id": "grp-no-positions",
                "status": "Submitted",
                "environment": "live",
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals, order_rows=orders)
        lifecycle = _FakeOrderLifecycle(
            position_results=[
                {
                    "ok": False,
                    "positions": [],
                    "error": "account_data_circuit_open:positions_timeout:retry_after_s=60.0",
                    "retry_after_s": 60.0,
                    "account_data_backoff_reason": "positions_timeout",
                }
            ]
        )
        modifier = _FakeOrderModifier(pb)
        placer = _FakeOrderPlacer()

        ReverseSignalHandler(
            pb,
            order_lifecycle=lifecycle,
            order_modifier=modifier,
            order_placer=placer,
            environment="live",
        ).check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("pending", updated["status"])
        self.assertIn("position_snapshot_unavailable", updated["reason"])
        self.assertEqual("pending_retry", extra["result_status"])
        self.assertEqual("skipped", extra["cancel_old_order"])
        self.assertEqual("skipped", extra["close_old_position"])
        self.assertEqual([], modifier.cancelled)
        self.assertEqual([], placer.calls)
        self.assertEqual(1, len(pb.events))
        self.assertEqual("TV 平仓未执行：持仓快照不可用", pb.events[0]["title"])
        self.assertEqual("error", pb.events[0]["level"])

    def test_close_without_real_order_evidence_expires_without_gateway_lookup(self):
        reverse = {
            "id": "rev-close-no-order",
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
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 10}]])
        placer = _FakeOrderPlacer()

        ReverseSignalHandler(pb, order_lifecycle=lifecycle, order_placer=placer, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("expired", updated["status"])
        self.assertIn("real_filled_order_required_for_close", updated["reason"])
        self.assertEqual("real_order_preflight", extra["invalidated_by"])
        self.assertEqual("real_filled_order_required_for_close", extra["invalidated_reason"])
        self.assertTrue(extra["gateway_request_blocked"])
        self.assertFalse(extra["execution_preflight"]["real_order_confirmed"])
        self.assertEqual(0, lifecycle.calls)
        self.assertEqual([], placer.calls)
        self.assertEqual("expired", pb.acks[0]["status"])

    def test_close_exception_stays_pending_retry_and_next_record_continues(self):
        reverse = {
            "id": "rev-close-exception",
            "symbol": "AAPL",
            "conid": 123,
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 20,
            "bar_time_ms": 2,
            "extra": {"origin_signal_id": "sig-close-exception"},
        }
        legacy = {
            "id": "rev-legacy",
            "symbol": "MSFT",
            "conid": 456,
            "source": "legacy",
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
        }
        signals = [
            {
                "id": "sig-close-exception-row",
                "signal_id": "sig-close-exception",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "protected_active",
                "extra": {"execution_by_mode": {"live": {"status": "filled"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse, legacy], signal_rows=signals)
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 10}]])
        placer = _FakeOrderPlacer(exception=NameError("name 'self' is not defined"))
        handler = ReverseSignalHandler(pb, order_lifecycle=lifecycle, order_placer=placer, environment="live")

        handler.check_and_process()

        failed = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        failed_extra = failed["extra"]
        self.assertEqual("pending", failed["status"])
        self.assertIn("reverse pending retry: reverse_action_exception", failed["reason"])
        self.assertEqual("pending_retry", failed_extra["result_status"])
        self.assertTrue(failed_extra["reverse_runtime_detail"]["retryable"])
        self.assertEqual("NameError", failed_extra["reverse_runtime_detail"]["blocked_context"]["exception_type"])
        self.assertNotIn("rev-close-exception", handler._processed_ids)
        self.assertEqual(1, len(placer.calls))

        continued = pb.records[REVERSE_SIGNAL_COLLECTION][1]
        self.assertEqual("expired", continued["status"])
        self.assertEqual("legacy_non_tv_action_disabled", continued["reason"])
        self.assertEqual(["pending", "expired"], [ack["status"] for ack in pb.acks])

    def test_tv_exit_origin_rejected_cancels_without_position_lookup(self):
        reverse = {
            "id": "rev-tv-exit-rejected",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "exit",
                "reverse_kind": "tv_exit",
                "direction": "long",
                "origin_signal_id": "tv-entry-rejected",
                "trade_group_id": "grp-rejected",
            },
        }
        signals = [
            {
                "id": "sig-rejected",
                "signal_id": "tv-entry-rejected",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "pending",
                "extra": {"execution_by_mode": {"live": {"status": "rejected"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        lifecycle = _FakeOrderLifecycle([[{"symbol": "AAPL", "position": 10}]])
        placer = _FakeOrderPlacer()

        ReverseSignalHandler(pb, order_lifecycle=lifecycle, order_placer=placer, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("tv_exit_origin_order_not_active", updated["reason"])
        self.assertEqual("not_executable", extra["execution_readiness"])
        self.assertEqual("tv_exit_origin_order_not_active", extra["execution_blocked_reason"])
        self.assertEqual("origin_order_not_active", extra["order_linkage_status"])
        self.assertTrue(extra["gateway_request_blocked"])
        self.assertEqual(0, lifecycle.calls)
        self.assertEqual([], placer.calls)
        self.assertEqual("cancelled", pb.acks[0]["status"])

    def test_close_submission_unconfirmed_stays_pending_and_self_heals_without_resubmit(self):
        reverse = {
            "id": "rev-close-unconfirmed",
            "symbol": "AAPL",
            "conid": 123,
            "action_type": "close",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {"new_direction": "short", "origin_signal_id": "sig-close-unconfirmed"},
        }
        close_result = {
            "ok": False,
            "submitted": False,
            "error": "order_submission_unconfirmed",
            "order_ids": ["157"],
            "entry_coid": "close_AAPL_20260603_101500",
        }
        signals = [
            {
                "id": "sig-close-unconfirmed-row",
                "signal_id": "sig-close-unconfirmed",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "protected_active",
                "extra": {"execution_by_mode": {"live": {"status": "filled"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        lifecycle = _FakeOrderLifecycle(
            [
                [{"ticker": "AAPL", "position": 10}],
                [{"ticker": "AAPL", "position": 10}],
                [{"ticker": "AAPL", "position": 10}],
                [{"ticker": "AAPL", "position": 10}],
                [{"ticker": "AAPL", "position": 0}],
            ]
        )
        placer = _FakeOrderPlacer(result=close_result)
        handler = ReverseSignalHandler(
            pb,
            order_placer=placer,
            order_lifecycle=lifecycle,
            environment="live",
        )

        handler.check_and_process()
        first_update = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        first_extra = first_update["extra"]
        self.assertEqual("pending", first_update["status"])
        self.assertIn("close_order_submission_unconfirmed", first_update["reason"])
        self.assertEqual("submission_unconfirmed", first_extra["close_old_position"])
        self.assertEqual("unconfirmed", first_extra["wait_flat"])
        self.assertTrue(first_extra["close_submission_unconfirmed"])
        self.assertEqual("pending_retry", first_extra["result_status"])
        self.assertEqual(["157"], first_extra["pending_close_order_ids"])
        self.assertEqual("close_AAPL_20260603_101500", first_extra["pending_close_order_ref"])
        self.assertEqual("pending", pb.acks[0]["status"])

        handler.check_and_process()

        self.assertEqual(1, len(placer.calls))
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("confirmed", extra["close_old_position"])
        self.assertEqual("confirmed", extra["wait_flat"])
        self.assertTrue(extra["flat_confirmation"]["confirmed"])
        self.assertTrue(extra["async_self_heal"]["duplicate_submit_blocked"])
        self.assertTrue(extra["close_submission_unconfirmed"])
        self.assertEqual(["157"], extra["pending_close_order_ids"])
        self.assertEqual("close_AAPL_20260603_101500", extra["pending_close_order_ref"])
        self.assertEqual(close_result, extra["close_result"])
        self.assertFalse(extra["blocked"])
        self.assertEqual(2, len(pb.acks))
        self.assertEqual("confirmed", pb.acks[1]["status"])
        self.assertEqual("tv_exit_confirmed_no_reentry", pb.acks[1]["reason"])

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
        self.assertEqual("cancelled", updated["status"])
        self.assertTrue(extra["blocked"])
        self.assertFalse(extra["ready_reentry"])
        self.assertEqual("protection_incomplete", extra["reentry_blocked"]["reason"])
        self.assertEqual("blocked", extra["reverse_state"])
        self.assertEqual("blocked", extra["ack_status_original"])
        self.assertEqual("cancelled", extra["ack_status_normalized"])
        self.assertEqual([], placer.calls)
        self.assertEqual([], modifier.cancelled)
        self.assertEqual(0, lifecycle.calls)
        self.assertEqual("cancelled", pb.acks[0]["status"])

    def test_tv_cancel_confirmed_does_not_create_reentry_signal_and_closes_origin_signal(self):
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
        self.assertEqual([], reentry_rows)
        origin = pb.records["ibkr_signals"][0]
        self.assertEqual("closed", origin["status"])
        self.assertEqual("old_risk_resolved", origin["extra"]["execution_stage"])
        self.assertEqual("tv_exit_required", origin["extra"]["execution_policy"])
        reverse_extra = pb.records[REVERSE_SIGNAL_COLLECTION][0]["extra"]
        self.assertFalse(reverse_extra["ready_reentry"])
        self.assertFalse(reverse_extra["reentry_submitted"])
        self.assertTrue(reverse_extra["auto_reentry_disabled"])
        self.assertEqual("tv_exit_confirmed", reverse_extra["result_status"])

    def test_adjust_bracket_updates_sl_and_tp_from_top_level_and_extra(self):
        reverse = {
            "id": "rev-adjust-bracket",
            "symbol": "AAPL",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "sl_order_id": "sl-1003",
            "new_sl": "181.25",
            "extra": {
                "tp_order_id": "tp-1002",
                "new_tp": 190.75,
            },
        }
        pb = _FakePB(reverse_rows=[reverse], order_rows=_submitted_child_orders("sl-1003", "tp-1002"))
        modifier = _FakeOrderModifier(pb)
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        self.assertEqual([("sl-1003", 181.25)], modifier.stop_updates)
        self.assertEqual([("tp-1002", 190.75)], modifier.take_profit_updates)
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("adjust_bracket_confirmed", updated["reason"])
        self.assertEqual("confirmed", extra["adjust_bracket"])
        self.assertTrue(extra["adjust_results"]["stop_loss"]["ok"])
        self.assertTrue(extra["adjust_results"]["take_profit"]["ok"])
        self.assertEqual(["stop_loss", "take_profit"], extra["adjust_bracket_result"]["succeeded_sides"])
        self.assertEqual("confirmed", pb.acks[0]["status"])

    def test_adjust_bracket_uses_requested_sides_before_available_order_ids(self):
        reverse = {
            "id": "rev-adjust-bracket-requested-sides",
            "symbol": "AAPL",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "sl_order_id": "sl-1003",
                "tp_order_id": "tp-1002",
                "new_sl": 181.25,
                "new_tp": 190.75,
                "requested_sides": ["stop_loss"],
            },
        }
        pb = _FakePB(reverse_rows=[reverse], order_rows=_submitted_child_orders("sl-1003", "tp-1002"))
        modifier = _FakeOrderModifier(pb)
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        self.assertEqual([("sl-1003", 181.25)], modifier.stop_updates)
        self.assertEqual([], modifier.take_profit_updates)
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("adjust_bracket_confirmed", updated["reason"])
        self.assertEqual(["stop_loss"], extra["requested_sides"])
        self.assertTrue(extra["requested_sides_explicit"])
        self.assertTrue(extra["adjust_results"]["stop_loss"]["ok"])
        self.assertEqual("not_requested", extra["adjust_results"]["take_profit"]["reason"])
        self.assertEqual(["stop_loss"], extra["adjust_bracket_result"]["succeeded_sides"])

    def test_adjust_bracket_infers_requested_sides_from_price_fields_not_order_ids(self):
        reverse = {
            "id": "rev-adjust-bracket-inferred-side",
            "symbol": "AAPL",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "sl_order_id": "sl-1003",
                "tp_order_id": "tp-1002",
                "new_sl": 181.25,
            },
        }
        pb = _FakePB(reverse_rows=[reverse], order_rows=_submitted_child_orders("sl-1003", "tp-1002"))
        modifier = _FakeOrderModifier(pb)
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        self.assertEqual([("sl-1003", 181.25)], modifier.stop_updates)
        self.assertEqual([], modifier.take_profit_updates)
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("adjust_bracket_confirmed", updated["reason"])
        self.assertFalse(extra["requested_sides_explicit"])
        self.assertTrue(extra["adjust_results"]["stop_loss"]["ok"])
        self.assertEqual("not_requested", extra["adjust_results"]["take_profit"]["reason"])
        self.assertEqual(["stop_loss"], extra["adjust_bracket_result"]["succeeded_sides"])

    def test_tv_risk_update_adjust_bracket_processes_latest_only(self):
        older = {
            "id": "rev-adjust-old",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "origin_signal_id": "tv-entry-1",
                "risk_update_reason": "trail_stop",
                "sl_order_id": "sl-1003",
                "new_sl": 180.25,
            },
        }
        latest = copy.deepcopy(older)
        latest["id"] = "rev-adjust-latest"
        latest["bar_time_ms"] = 2
        latest["extra"]["new_sl"] = 181.25
        pb = _FakePB(reverse_rows=[older, latest], order_rows=_submitted_child_orders("sl-1003"))
        modifier = _FakeOrderModifier(pb)
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        self.assertEqual([("sl-1003", 181.25)], modifier.stop_updates)
        by_id = {row["id"]: row for row in pb.records[REVERSE_SIGNAL_COLLECTION]}
        self.assertEqual("cancelled", by_id["rev-adjust-old"]["status"])
        self.assertEqual("superseded_by_latest_adjust_bracket", by_id["rev-adjust-old"]["reason"])
        self.assertTrue(by_id["rev-adjust-old"]["extra"]["superseded_by_latest_adjust_bracket"])
        self.assertEqual("confirmed", by_id["rev-adjust-latest"]["status"])
        self.assertEqual("adjust_bracket_confirmed", by_id["rev-adjust-latest"]["reason"])
        self.assertEqual(1, len(pb.acks))

    def test_adjust_bracket_per_cycle_cap_holds_excess_without_cancelling(self):
        rows = []
        for index, symbol in enumerate(["AAPL", "MSFT", "NVDA", "TSLA"], start=1):
            rows.append(
                {
                    "id": f"rev-adjust-{index}",
                    "symbol": symbol,
                    "source": "tradingview",
                    "action_type": "adjust_bracket",
                    "status": "pending",
                    "environment": "live",
                    "priority": 10,
                    "bar_time_ms": index,
                    "extra": {
                        "event_type": "risk_update",
                        "reverse_kind": "tv_risk_update",
                        "origin_signal_id": f"tv-entry-{index}",
                        "sl_order_id": f"sl-{index}",
                        "new_sl": 100.0 + index,
                    },
                }
            )
        rows.append(
            {
                "id": "rev-close",
                "symbol": "AMZN",
                "source": "tradingview",
                "action_type": "close",
                "status": "pending",
                "environment": "live",
                "priority": 1,
                "bar_time_ms": 0,
                "extra": {},
            }
        )
        pb = _FakePB(reverse_rows=rows)
        handler = ReverseSignalHandler(pb, environment="live")

        with mock.patch.dict("os.environ", {"IBKR_ADJUST_BRACKET_MAX_PROCESS_PER_CYCLE": "2"}):
            filtered = handler._filter_pending_reverse_records(rows)

        adjust_ids = [row["id"] for row in filtered if row["action_type"] == "adjust_bracket"]
        self.assertEqual(["rev-adjust-1", "rev-adjust-2"], adjust_ids)
        self.assertIn("rev-close", [row["id"] for row in filtered])
        self.assertEqual([], pb.updates)

    def test_adjust_bracket_confirms_explicit_noop_runner_activation(self):
        reverse = {
            "id": "rev-adjust-bracket-noop",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "origin_signal_id": "tv-entry-1",
                "risk_update_seq": 2,
                "risk_update_reason": "runner_activation",
                "requested_sides": [],
                "runner_enabled": True,
                "runner_active": True,
                "target_role": "safety_tp",
                "safety_take_profit": 196.10,
                "runner_activation_price": 190.70,
                "runner_activation_r": 1.0,
            },
        }
        signals = [
            {
                "id": "sig-row-1",
                "signal_id": "tv-entry-1",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "submitted",
                "extra": {"runner_active": False},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        modifier = _FakeOrderModifier(pb)
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        self.assertEqual([], modifier.stop_updates)
        self.assertEqual([], modifier.take_profit_updates)
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("adjust_bracket_noop", updated["reason"])
        self.assertEqual("confirmed_noop", extra["adjust_bracket"])
        self.assertTrue(extra["requested_sides_explicit"])
        self.assertEqual([], extra["adjust_bracket_result"]["attempted_sides"])
        origin_extra = pb.records["ibkr_signals"][0]["extra"]
        self.assertTrue(origin_extra["runner_active"])
        self.assertEqual("safety_tp", origin_extra["target_role"])
        self.assertEqual(190.70, origin_extra["runner_activation_price"])

    def test_adjust_bracket_blocks_when_prices_or_orders_missing(self):
        missing_prices = {
            "id": "rev-adjust-no-prices",
            "symbol": "AAPL",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "sl_order_id": "sl-1003",
                "tp_order_id": "tp-1002",
            },
        }
        pb = _FakePB(reverse_rows=[missing_prices], order_rows=_submitted_child_orders("sl-1003", "tp-1002"))
        modifier = _FakeOrderModifier(pb)
        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("adjust_bracket_prices_missing_or_invalid", updated["reason"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual([], modifier.take_profit_updates)
        self.assertEqual("not_requested", extra["adjust_results"]["stop_loss"]["reason"])
        self.assertEqual("not_requested", extra["adjust_results"]["take_profit"]["reason"])
        self.assertEqual("blocked", extra["ack_status_original"])
        self.assertEqual("cancelled", extra["ack_status_normalized"])

        invalid_price = {
            "id": "rev-adjust-invalid-price",
            "symbol": "AAPL",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "sl_order_id": "sl-1003",
                "new_sl": 0,
            },
        }
        pb = _FakePB(reverse_rows=[invalid_price], order_rows=_submitted_child_orders("sl-1003"))
        modifier = _FakeOrderModifier(pb)
        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("adjust_bracket_prices_missing_or_invalid", updated["reason"])
        self.assertEqual("new_sl_missing_or_invalid", extra["adjust_results"]["stop_loss"]["reason"])
        self.assertEqual("not_requested", extra["adjust_results"]["take_profit"]["reason"])

        missing_order = {
            "id": "rev-adjust-no-order",
            "symbol": "AAPL",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {"new_sl": 181.25},
        }
        pb = _FakePB(reverse_rows=[missing_order])
        modifier = _FakeOrderModifier(pb)
        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("expired", updated["status"])
        self.assertIn("real_child_order_required_for_adjust", updated["reason"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual([], modifier.take_profit_updates)
        self.assertEqual("real_order_preflight", extra["invalidated_by"])
        self.assertTrue(extra["gateway_request_blocked"])

    def test_adjust_bracket_does_not_modify_init_child_order(self):
        reverse = {
            "id": "rev-adjust-init-order",
            "symbol": "AAPL",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "origin_signal_id": "tv-entry-init",
                "trade_group_id": "grp-init",
                "sl_order_id": "sl-init",
                "new_sl": 181.25,
            },
        }
        orders = [
            {
                "id": "sl-init-row",
                "broker_order_id": "sl-init",
                "order_id": "sl-init",
                "trade_group_id": "grp-init",
                "role": "stop_loss",
                "status": "Init",
                "environment": "live",
            }
        ]
        signals = [
            {
                "id": "sig-init-row",
                "signal_id": "tv-entry-init",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "submitted",
                "extra": {"execution_by_mode": {"live": {"status": "submitted"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders, signal_rows=signals)
        modifier = _FakeOrderModifier(pb)

        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("expired", updated["status"])
        self.assertIn("real_child_order_required_for_adjust", updated["reason"])
        self.assertEqual("real_order_preflight", extra["invalidated_by"])
        self.assertEqual(["INIT"], extra["execution_preflight"]["pb_order_statuses"])
        self.assertEqual([], modifier.stop_updates)
        self.assertTrue(extra["gateway_request_blocked"])

    def test_adjust_bracket_blocks_with_partial_failure_detail(self):
        reverse = {
            "id": "rev-adjust-partial",
            "symbol": "AAPL",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "sl_order_id": "sl-1003",
                "new_sl": 181.25,
                "tp_order_id": "tp-1002",
                "new_tp": 190.75,
            },
        }
        pb = _FakePB(reverse_rows=[reverse], order_rows=_submitted_child_orders("sl-1003", "tp-1002"))
        modifier = _FakeOrderModifier(pb, update_failures={"take_profit"})
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        self.assertEqual([("sl-1003", 181.25)], modifier.stop_updates)
        self.assertEqual([("tp-1002", 190.75)], modifier.take_profit_updates)
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("adjust_bracket_partial_failed", updated["reason"])
        self.assertEqual("partial_failed", extra["adjust_bracket"])
        self.assertTrue(extra["adjust_results"]["stop_loss"]["ok"])
        self.assertFalse(extra["adjust_results"]["take_profit"]["ok"])
        self.assertEqual("broker_update_failed", extra["adjust_results"]["take_profit"]["reason"])
        self.assertEqual(["stop_loss"], extra["adjust_bracket_result"]["succeeded_sides"])
        self.assertEqual(["take_profit"], extra["adjust_bracket_result"]["failed_sides"])
        self.assertEqual("blocked", extra["ack_status_original"])
        self.assertEqual("cancelled", pb.acks[0]["status"])

    def test_adjust_bracket_invalidates_when_position_is_flat(self):
        reverse = {
            "id": "rev-adjust-flat",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "sl_order_id": "sl-1003",
                "new_sl": 181.25,
            },
        }
        pb = _FakePB(reverse_rows=[reverse], order_rows=_submitted_child_orders("sl-1003"))
        modifier = _FakeOrderModifier(pb)
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 0}]])

        ReverseSignalHandler(
            pb,
            order_modifier=modifier,
            order_lifecycle=lifecycle,
            environment="live",
        ).check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("expired", updated["status"])
        self.assertIn("stale_no_position_for_adjust", updated["reason"])
        self.assertEqual("stale_no_position_for_adjust", extra["invalidated_reason"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual(1, lifecycle.calls)

    def test_tv_risk_update_missing_child_with_position_requires_repair_not_retry(self):
        reverse = {
            "id": "rev-risk-missing-child-with-position",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "origin_signal_id": "tv-entry-1",
                "risk_update_seq": 2,
                "previous_stop_loss": 180.0,
                "new_sl": 181.25,
            },
        }
        signals = [
            {
                "id": "sig-row-1",
                "signal_id": "tv-entry-1",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "submitted",
                "extra": {"execution_by_mode": {"live": {"status": "submitted"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        broker = _FakeBroker(open_orders=[])
        modifier = _FakeOrderModifier(pb, broker=broker)
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 5}]])

        ReverseSignalHandler(
            pb,
            order_modifier=modifier,
            order_lifecycle=lifecycle,
            environment="live",
        ).check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("repair_protection_required", updated["reason"])
        self.assertTrue(extra["repair_protection_required"])
        self.assertEqual(["stop_loss"], extra["missing_protection_roles"])
        self.assertEqual("missing_protection_repair_not_adjust_retry", extra["reentry_blocked"]["safe_action"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual(1, len(broker.calls))
        self.assertEqual(1, lifecycle.calls)

    def test_tv_risk_update_resolves_child_order_ids_from_origin_execution_by_mode(self):
        reverse = {
            "id": "rev-risk-origin-execution-child",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "origin_signal_id": "tv-entry-1",
                "risk_update_seq": 2,
                "previous_stop_loss": 180.0,
                "new_sl": 181.25,
                "new_tp": 190.75,
            },
        }
        signals = [
            {
                "id": "sig-row-1",
                "signal_id": "tv-entry-1",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "submitted",
                "extra": {
                    "execution_by_mode": {
                        "live": {
                            "status": "submitted",
                            "order_results": [
                                {
                                    "role": "take_profit",
                                    "payload": {"order": {"order_id": "tp-1002"}},
                                },
                                {
                                    "role": "stop_loss",
                                    "payload": {"order": {"order_id": "sl-1003"}},
                                },
                            ],
                        }
                    }
                },
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        modifier = _FakeOrderModifier(pb)
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        self.assertEqual([("sl-1003", 181.25)], modifier.stop_updates)
        self.assertEqual([("tp-1002", 190.75)], modifier.take_profit_updates)
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("adjust_bracket_confirmed", updated["reason"])
        self.assertEqual("origin_signal.execution_by_mode", extra["child_order_resolution"]["sources"]["stop_loss"])
        self.assertEqual("origin_signal.execution_by_mode", extra["child_order_resolution"]["sources"]["take_profit"])
        self.assertEqual("confirmed", pb.acks[0]["status"])

    def test_tv_risk_update_resolves_child_order_ids_from_orders_table(self):
        reverse = {
            "id": "rev-risk-orders-table-child",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "origin_signal_id": "tv-entry-1",
                "trade_group_id": "grp-1",
                "risk_update_seq": 2,
                "previous_stop_loss": 180.0,
                "new_sl": 181.25,
                "new_tp": 190.75,
            },
        }
        orders = [
            {
                "id": "tp-row",
                "broker_order_id": "tp-1002",
                "order_id": "tp-1002",
                "signal_id": "tv-entry-1",
                "trade_group_id": "grp-1",
                "role": "take_profit",
                "status": "Submitted",
                "environment": "live",
            },
            {
                "id": "sl-row",
                "broker_order_id": "sl-1003",
                "order_id": "sl-1003",
                "signal_id": "tv-entry-1",
                "trade_group_id": "grp-1",
                "role": "stop_loss",
                "status": "PreSubmitted",
                "environment": "live",
            },
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders)
        modifier = _FakeOrderModifier(pb)

        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        self.assertEqual([("sl-1003", 181.25)], modifier.stop_updates)
        self.assertEqual([("tp-1002", 190.75)], modifier.take_profit_updates)
        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("adjust_bracket_confirmed", updated["reason"])
        self.assertEqual("orders_table", extra["child_order_resolution"]["sources"]["stop_loss"])
        self.assertEqual("orders_table", extra["child_order_resolution"]["sources"]["take_profit"])

    def test_tv_risk_update_symbol_only_cancels_linkage_missing(self):
        reverse = {
            "id": "rev-risk-symbol-only",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "risk_update_seq": 2,
                "previous_stop_loss": 180.0,
                "new_sl": 181.25,
            },
        }
        symbol_only_orders = [
            {
                "id": "symbol-only-sl",
                "broker_order_id": "sl-symbol-only",
                "order_id": "sl-symbol-only",
                "symbol": "AAPL",
                "role": "stop_loss",
                "status": "Submitted",
                "environment": "live",
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=symbol_only_orders)
        broker = _FakeBroker(open_orders=[
            {
                "orderId": "sl-symbol-only-live",
                "orderRef": "sl_unlinked",
                "role": "stop_loss",
                "status": "Submitted",
            }
        ])
        modifier = _FakeOrderModifier(pb, broker=broker)

        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("expired", updated["status"])
        self.assertIn("real_child_order_required_for_adjust", updated["reason"])
        self.assertEqual("real_order_preflight", extra["invalidated_by"])
        self.assertTrue(extra["gateway_request_blocked"])
        self.assertEqual("not_executable", extra["execution_readiness"])
        self.assertEqual("real_child_order_required_for_adjust", extra["execution_blocked_reason"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual([], broker.calls)
        self.assertEqual("expired", pb.acks[0]["status"])

    def test_tv_risk_update_trade_group_hint_only_defers_without_gateway(self):
        reverse = {
            "id": "rev-risk-trade-group-hint-only",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "trade_group_id": "grp-1",
                "risk_update_seq": 2,
                "previous_stop_loss": 180.0,
                "new_sl": 181.25,
            },
        }
        broker = _FakeBroker(open_orders=[
            {
                "orderId": "sl-1003",
                "orderRef": "sl_grp-1",
                "role": "stop_loss",
                "status": "Submitted",
            }
        ])
        pb = _FakePB(reverse_rows=[reverse])
        modifier = _FakeOrderModifier(pb, broker=broker)

        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("expired", updated["status"])
        self.assertIn("real_child_order_required_for_adjust", updated["reason"])
        self.assertEqual("real_order_preflight", extra["invalidated_by"])
        self.assertTrue(extra["gateway_request_blocked"])
        self.assertEqual("not_executable", extra["execution_readiness"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual([], broker.calls)
        self.assertEqual("expired", pb.acks[0]["status"])

    def test_tv_risk_update_origin_rejected_cancels_without_gateway(self):
        reverse = {
            "id": "rev-risk-origin-rejected",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "origin_signal_id": "tv-entry-rejected",
                "trade_group_id": "grp-rejected",
                "risk_update_seq": 2,
                "previous_stop_loss": 180.0,
                "new_sl": 181.25,
            },
        }
        signals = [
            {
                "id": "sig-rejected",
                "signal_id": "tv-entry-rejected",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "pending",
                "extra": {"execution_by_mode": {"live": {"status": "rejected"}}},
            }
        ]
        broker = _FakeBroker(open_orders=[
            {
                "orderId": "sl-1003",
                "orderRef": "sl_grp-rejected",
                "role": "stop_loss",
                "status": "Submitted",
            }
        ])
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        modifier = _FakeOrderModifier(pb, broker=broker)

        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("risk_update_origin_order_not_active", updated["reason"])
        self.assertEqual("real_order_preflight", extra["invalidated_by"])
        self.assertTrue(extra["gateway_request_blocked"])
        self.assertEqual("not_executable", extra["execution_readiness"])
        self.assertEqual("origin_order_not_active", extra["order_linkage_status"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual([], broker.calls)
        self.assertEqual("cancelled", pb.acks[0]["status"])

    def test_tv_risk_update_linked_child_missing_defers_unresolved(self):
        reverse = {
            "id": "rev-risk-linked-child-missing",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "origin_signal_id": "tv-entry-1",
                "risk_update_seq": 2,
                "previous_stop_loss": 180.0,
                "new_sl": 181.25,
            },
        }
        signals = [
            {
                "id": "sig-row-1",
                "signal_id": "tv-entry-1",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "submitted",
                "extra": {"execution_by_mode": {"live": {"status": "submitted"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        broker = _FakeBroker(open_orders=[])
        modifier = _FakeOrderModifier(pb, broker=broker)

        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("pending", updated["status"])
        self.assertIn("risk_update_child_order_id_unresolved", updated["reason"])
        self.assertEqual("child_order_id_unresolved", extra["adjust_results"]["stop_loss"]["reason"])
        self.assertTrue(extra["child_order_resolution"]["linkage_exists"])
        self.assertEqual(["stop_loss"], extra["child_order_resolution"]["unresolved_sides"])
        self.assertFalse(extra["gateway_request_blocked"])
        self.assertTrue(extra["child_order_resolution"]["gateway_lookup_allowed"])
        self.assertEqual("deferred", extra["execution_readiness"])
        self.assertEqual("risk_update_child_order_id_unresolved", extra["execution_blocked_reason"])
        self.assertEqual("child_order_id_unresolved", extra["order_linkage_status"])
        self.assertTrue(extra["missing_child_order_defer"]["enabled"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual(1, len(broker.calls))
        self.assertEqual("pending", pb.acks[0]["status"])

    def test_tv_risk_update_allows_gateway_lookup_with_local_linkage(self):
        reverse = {
            "id": "rev-risk-live-linked-child",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "origin_signal_id": "tv-entry-live-1",
                "trade_group_id": "grp-1",
                "risk_update_seq": 2,
                "previous_stop_loss": 180.0,
                "new_sl": 181.25,
            },
        }
        signals = [
            {
                "id": "sig-row-live-1",
                "signal_id": "tv-entry-live-1",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "submitted",
                "extra": {"execution_by_mode": {"live": {"status": "submitted"}}},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        broker = _FakeBroker(
            open_orders=[
                {
                    "orderId": "sl-1003",
                    "orderRef": "sl_grp-1",
                    "role": "stop_loss",
                    "status": "Submitted",
                }
            ]
        )
        modifier = _FakeOrderModifier(pb, broker=broker)

        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("adjust_bracket_confirmed", updated["reason"])
        self.assertEqual([("sl-1003", 181.25)], modifier.stop_updates)
        self.assertEqual(1, len(broker.calls))
        self.assertFalse(extra["child_order_resolution"]["gateway_request_blocked"])
        self.assertTrue(extra["child_order_resolution"]["gateway_lookup_allowed"])
        self.assertEqual("broker_open_orders", extra["child_order_resolution"]["sources"]["stop_loss"])

    def test_tv_risk_update_blocks_stale_sequence_without_modifying_orders(self):
        reverse = {
            "id": "rev-risk-stale-seq",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "origin_signal_id": "tv-entry-1",
                "risk_update_seq": 3,
                "previous_stop_loss": 180.0,
                "new_sl": 181.25,
            },
        }
        signals = [
            {
                "id": "sig-row-1",
                "signal_id": "tv-entry-1",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "submitted",
                "extra": {"last_risk_update_seq": 4},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        broker = _FakeBroker(open_orders=[{"orderId": "sl-1003", "orderRef": "sl_grp-1", "orderType": "STP", "status": "Submitted"}])
        modifier = _FakeOrderModifier(pb, broker=broker)
        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("risk_update_seq_stale", updated["reason"])
        self.assertEqual("skipped_stale_seq", extra["adjust_bracket"])
        self.assertTrue(extra["risk_update_sequence"]["guarded"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual([], broker.calls)
        self.assertEqual("confirmed", pb.acks[0]["status"])

    def test_tv_risk_update_sequence_uses_data_environment_for_paper_broker_signal(self):
        reverse = {
            "id": "rev-risk-paper-live-seq",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "paper",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "broker_mode": "paper",
                "data_environment": "live",
                "direction": "long",
                "origin_signal_id": "tv-entry-paper-1",
                "risk_update_seq": 3,
                "previous_stop_loss": 180.0,
                "sl_order_id": "sl-1003",
                "new_sl": 181.25,
            },
        }
        signals = [
            {
                "id": "sig-row-paper-1",
                "signal_id": "tv-entry-paper-1",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "status": "submitted",
                "extra": {"last_risk_update_seq": 4},
            }
        ]
        pb = _FakePB(reverse_rows=[reverse], signal_rows=signals)
        modifier = _FakeOrderModifier(pb)
        ReverseSignalHandler(pb, order_modifier=modifier, environment="paper").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("risk_update_seq_stale", updated["reason"])
        self.assertEqual("origin_signal.extra.last_risk_update_seq", extra["risk_update_sequence"]["prior_source"])
        self.assertEqual([], modifier.stop_updates)

    def test_tv_risk_update_never_widens_stop_by_default(self):
        reverse = {
            "id": "rev-risk-widen-stop",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "event_type": "risk_update",
                "reverse_kind": "tv_risk_update",
                "direction": "long",
                "origin_signal_id": "tv-entry-1",
                "risk_update_seq": 5,
                "previous_stop_loss": 181.0,
                "sl_order_id": "sl-1003",
                "new_sl": 180.5,
            },
        }
        pb = _FakePB(reverse_rows=[reverse], order_rows=_submitted_child_orders("sl-1003"))
        modifier = _FakeOrderModifier(pb)
        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("risk_update_stop_widen_blocked", updated["reason"])
        self.assertEqual("stop_widen_blocked", extra["adjust_results"]["stop_loss"]["reason"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual("cancelled", pb.acks[0]["status"])

    def test_cancel_unconfirmed_stays_pending_and_self_heals_without_second_cancel(self):
        reverse = {
            "id": "rev-cancel-unconfirmed",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "cancel",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {"trade_group_id": "grp-1", "new_direction": "short"},
        }
        orders = [
            {
                "id": "entry",
                "broker_order_id": "1001",
                "order_id": "1001",
                "trade_group_id": "grp-1",
                "status": "Submitted",
                "environment": "live",
            }
        ]
        broker = _FakeBroker(open_orders=[{"orderId": "1001", "status": "Submitted"}])
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders)
        modifier = _FakeOrderModifier(pb, broker=broker)
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        first_update = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        first_extra = first_update["extra"]
        self.assertEqual("pending", first_update["status"])
        self.assertEqual(["1001"], modifier.cancelled)
        self.assertEqual("unconfirmed", first_extra["cancel_old_order"])
        self.assertEqual("pending_retry", first_extra["result_status"])
        self.assertEqual("pending", pb.acks[0]["status"])

        broker.open_orders = []
        handler.check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual(["1001"], modifier.cancelled)
        self.assertEqual("confirmed", extra["cancel_old_order"])
        self.assertTrue(extra["cancel_confirmation"]["confirmed"])
        self.assertTrue(extra["async_self_heal"]["duplicate_submit_blocked"])
        self.assertEqual("confirmed", pb.acks[1]["status"])

    def test_adjust_bracket_pending_price_confirmation_self_heals_without_second_modify(self):
        reverse = {
            "id": "rev-adjust-confirm-pending",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "adjust_bracket",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "sl_order_id": "sl-1003",
                "new_sl": 181.25,
                "adjust_results": {
                    "stop_loss": {
                        "requested": True,
                        "order_id": "sl-1003",
                        "new_price": 181.25,
                        "skipped": False,
                        "reason": "adjust_price_not_confirmed",
                        "result": {
                            "ok": True,
                            "order_id": "sl-1003",
                            "order": {"orderId": "sl-1003", "status": "Submitted", "auxPrice": 180.0},
                        },
                    },
                    "take_profit": {"requested": False, "skipped": True, "reason": "not_requested"},
                },
                "adjust_bracket_result": {
                    "attempted_sides": ["stop_loss"],
                    "succeeded_sides": [],
                    "failed_sides": ["stop_loss"],
                    "unconfirmed_sides": ["stop_loss"],
                },
            },
        }
        orders = _submitted_child_orders("sl-1003")
        orders[0]["auxPrice"] = 180.0
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders)
        modifier = _FakeOrderModifier(pb)
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        first_update = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        first_extra = first_update["extra"]
        self.assertEqual("pending", first_update["status"])
        self.assertEqual("pending_retry", first_extra["result_status"])
        self.assertEqual("adjust_price_not_confirmed", first_extra["adjust_results"]["stop_loss"]["reason"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual("pending", pb.acks[0]["status"])
        self.assertEqual(1, first_extra["adjust_bracket_retry_attempts"])
        self.assertIn("adjust_bracket_next_retry_at", first_extra)

        handler.check_and_process()
        self.assertEqual(1, len(pb.acks))

        pb.set_order_price("sl-1003", "stop_loss", 181.25)
        retry_extra = pb.records[REVERSE_SIGNAL_COLLECTION][0]["extra"]
        retry_extra["adjust_bracket_next_retry_at"] = "2000-01-01T00:00:00+00:00"
        retry_extra["adjust_bracket_retry_backoff"]["next_retry_at"] = "2000-01-01T00:00:00+00:00"
        retry_extra["reverse_runtime_detail"]["adjust_bracket_retry_backoff"]["next_retry_at"] = "2000-01-01T00:00:00+00:00"
        handler.check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("adjust_bracket_confirmed", updated["reason"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual(["stop_loss"], extra["adjust_bracket_result"]["succeeded_sides"])
        self.assertTrue(extra["adjust_results"]["stop_loss"]["confirmation"]["confirmed"])
        self.assertTrue(extra["async_self_heal"]["duplicate_submit_blocked"])
        self.assertEqual("confirmed", pb.acks[1]["status"])

    def test_historical_cancelled_flat_not_confirmed_tv_close_self_heals_when_flat(self):
        reverse = {
            "id": "rev-historical-flat",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "close",
            "status": "cancelled",
            "reason": "reverse blocked: flat_not_confirmed",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "close_old_position": "submitted",
                "wait_flat": "unconfirmed",
                "reentry_blocked": {"reason": "flat_not_confirmed"},
            },
        }
        pb = _FakePB(reverse_rows=[reverse])
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 0}]])

        ReverseSignalHandler(pb, order_lifecycle=lifecycle, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("confirmed", extra["close_old_position"])
        self.assertEqual("confirmed", extra["wait_flat"])
        self.assertTrue(extra["flat_confirmation"]["confirmed"])
        self.assertTrue(extra["historical_self_heal"]["enabled"])
        self.assertEqual("confirmed", pb.acks[0]["status"])

    def test_historical_cancelled_terminal_cancel_tv_close_self_heals_when_safe(self):
        reverse = {
            "id": "rev-historical-terminal",
            "symbol": "AAPL",
            "source": "tradingview",
            "action_type": "close",
            "status": "cancelled",
            "reason": "reverse blocked: cancel_order_failed",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {
                "cancel_old_order": "failed",
                "cancel_target": {"trade_group_id": "grp-1", "order_ids": ["1001", "1002"]},
                "cancel_results": [
                    {"order_id": "1001", "ok": False, "error": "order_filled_during_cancel"},
                    {"order_id": "1002", "ok": True, "status": "CANCELLED"},
                ],
                "reentry_blocked": {"reason": "cancel_order_failed"},
            },
        }
        orders = [
            {"id": "entry", "broker_order_id": "1001", "order_id": "1001", "trade_group_id": "grp-1", "role": "entry", "status": "Filled", "environment": "live"},
            {"id": "tp", "broker_order_id": "1002", "order_id": "1002", "trade_group_id": "grp-1", "role": "take_profit", "status": "Cancelled", "environment": "live"},
        ]
        pb = _FakePB(reverse_rows=[reverse], order_rows=orders)
        modifier = _FakeOrderModifier(pb, broker=_FakeBroker(open_orders=[]))
        lifecycle = _FakeOrderLifecycle([[{"ticker": "AAPL", "position": 0}]])

        ReverseSignalHandler(
            pb,
            order_modifier=modifier,
            order_lifecycle=lifecycle,
            environment="live",
        ).check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual([], modifier.cancelled)
        self.assertTrue(extra["historical_self_heal"]["enabled"])
        self.assertEqual("cancelled_terminal_during_cancel_tv_close", extra["historical_self_heal"]["source"])
        self.assertTrue(extra["terminal_conflict_safe"])
        self.assertEqual("confirmed", extra["cancel_old_order"])
        self.assertEqual("confirmed", extra["wait_flat"])
        self.assertEqual("confirmed", pb.acks[0]["status"])

    def test_non_tv_pending_execution_action_expires_without_broker_actions(self):
        reverse = {
            "id": "rev-legacy-indicator",
            "symbol": "AAPL",
            "source": "indicator",
            "action_type": "cancel",
            "status": "pending",
            "environment": "live",
            "priority": 10,
            "bar_time_ms": 1,
            "extra": {"trade_group_id": "grp-legacy"},
        }
        pb = _FakePB(reverse_rows=[reverse])
        modifier = _FakeOrderModifier(pb)

        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        self.assertEqual("expired", updated["status"])
        self.assertEqual("legacy_non_tv_action_disabled", updated["reason"])
        self.assertEqual("expired_non_tv_action", updated["extra"]["result_status"])
        self.assertTrue(updated["extra"]["tv_primary_only"])
        self.assertEqual([], modifier.cancelled)
        self.assertEqual("expired", pb.acks[0]["status"])


if __name__ == "__main__":
    unittest.main()
