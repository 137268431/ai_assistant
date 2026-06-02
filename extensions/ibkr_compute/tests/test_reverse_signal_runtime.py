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
    def __init__(self, pb, *, broker=None, ok=True, update_failures=None):
        self.pb = pb
        self.broker = broker or _FakeBroker([])
        self.ok = ok
        self.cancelled = []
        self.update_failures = set(update_failures or [])
        self.stop_updates = []
        self.take_profit_updates = []

    def cancel_order(self, order_id):
        self.cancelled.append(str(order_id))
        if not self.ok:
            return {"ok": False, "error": "cancel rejected", "order_id": str(order_id)}
        self.pb.set_order_status(order_id, "Canceled")
        return {"ok": True, "order_id": str(order_id)}

    def update_stop_loss(self, order_id, new_sl_price):
        self.stop_updates.append((str(order_id), float(new_sl_price)))
        if "stop_loss" in self.update_failures:
            return {"ok": False, "error": "stop update rejected", "order_id": str(order_id)}
        return {"ok": True, "order_id": str(order_id), "price": float(new_sl_price)}

    def update_take_profit(self, order_id, new_tp_price):
        self.take_profit_updates.append((str(order_id), float(new_tp_price)))
        if "take_profit" in self.update_failures:
            return {"ok": False, "error": "take profit update rejected", "order_id": str(order_id)}
        return {"ok": True, "order_id": str(order_id), "price": float(new_tp_price)}


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
        self.assertFalse(extra["ready_reentry"])
        self.assertFalse(extra["reentry_submitted"])
        self.assertFalse(extra["blocked"])
        self.assertTrue(extra["auto_reentry_disabled"])
        self.assertEqual("tv_exit_confirmed", pb.acks[0]["detail"]["result_status"])
        self.assertEqual("tv_exit_confirmed_no_reentry", pb.acks[0]["reason"])

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
        self.assertFalse(extra["ready_reentry"])
        self.assertFalse(extra["blocked"])
        self.assertTrue(extra["auto_reentry_disabled"])
        self.assertIn("tv_exit_confirmed", extra["reverse_state_path"])

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
        pb = _FakePB(reverse_rows=[reverse])
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
        pb = _FakePB(reverse_rows=[missing_prices])
        modifier = _FakeOrderModifier(pb)
        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("adjust_bracket_prices_missing_or_invalid", updated["reason"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual([], modifier.take_profit_updates)
        self.assertEqual("new_sl_missing_or_invalid", extra["adjust_results"]["stop_loss"]["reason"])
        self.assertEqual("new_tp_missing_or_invalid", extra["adjust_results"]["take_profit"]["reason"])
        self.assertEqual("blocked", extra["ack_status_original"])
        self.assertEqual("cancelled", extra["ack_status_normalized"])

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
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("adjust_bracket_targets_missing_or_invalid", updated["reason"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual([], modifier.take_profit_updates)
        self.assertEqual("sl_order_id_missing", extra["adjust_results"]["stop_loss"]["reason"])
        self.assertEqual("not_requested", extra["adjust_results"]["take_profit"]["reason"])

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
        pb = _FakePB(reverse_rows=[reverse])
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

    def test_tv_risk_update_missing_child_order_ids_stays_pending_and_retries(self):
        reverse = {
            "id": "rev-risk-missing-child",
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
        pb = _FakePB(reverse_rows=[reverse])
        modifier = _FakeOrderModifier(pb)
        handler = ReverseSignalHandler(pb, order_modifier=modifier, environment="live")

        handler.check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("pending", updated["status"])
        self.assertIn("risk_update_child_order_id_missing", updated["reason"])
        self.assertEqual("pending_retry", extra["adjust_bracket"])
        self.assertTrue(extra["reentry_blocked"]["retryable"])
        self.assertEqual(["stop_loss", "take_profit"], extra["adjust_bracket_result"]["failed_sides"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual([], modifier.take_profit_updates)
        self.assertEqual("pending", pb.acks[0]["status"])

        pb.records[REVERSE_SIGNAL_COLLECTION][0]["extra"].update(
            {"sl_order_id": "sl-1003", "tp_order_id": "tp-1002"}
        )
        handler.check_and_process()

        self.assertEqual([("sl-1003", 181.25)], modifier.stop_updates)
        self.assertEqual([("tp-1002", 190.75)], modifier.take_profit_updates)
        retried = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        self.assertEqual("confirmed", retried["status"])
        self.assertEqual("adjust_bracket_confirmed", retried["reason"])

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
                "sl_order_id": "sl-1003",
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
        modifier = _FakeOrderModifier(pb)
        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("confirmed", updated["status"])
        self.assertEqual("risk_update_seq_stale", updated["reason"])
        self.assertEqual("skipped_stale_seq", extra["adjust_bracket"])
        self.assertTrue(extra["risk_update_sequence"]["guarded"])
        self.assertEqual([], modifier.stop_updates)
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
        pb = _FakePB(reverse_rows=[reverse])
        modifier = _FakeOrderModifier(pb)
        ReverseSignalHandler(pb, order_modifier=modifier, environment="live").check_and_process()

        updated = pb.records[REVERSE_SIGNAL_COLLECTION][0]
        extra = updated["extra"]
        self.assertEqual("cancelled", updated["status"])
        self.assertIn("risk_update_stop_widen_blocked", updated["reason"])
        self.assertEqual("stop_widen_blocked", extra["adjust_results"]["stop_loss"]["reason"])
        self.assertEqual([], modifier.stop_updates)
        self.assertEqual("cancelled", pb.acks[0]["status"])

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
