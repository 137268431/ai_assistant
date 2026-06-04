import copy
import os
import re
import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

os.environ.setdefault("PYTHONHASHSEED", "0")

from ibkr_api.order_group_cancel import build_order_cancel_group_response
from ibkr_api.order_group_close import build_order_close_group_response
from ibkr_api.order_values import escape_filter_string, to_text


class _FakePB:
    def __init__(self, order_rows, detail_rows=None, signal_rows=None):
        self.orders = {str(row["id"]): copy.deepcopy(row) for row in order_rows}
        self.order_details = [copy.deepcopy(row) for row in (detail_rows or [])]
        self.signals = {str(row["id"]): copy.deepcopy(row) for row in (signal_rows or [])}
        self.updated = []
        self.created = []

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        if collection == "orders":
            return [copy.deepcopy(row) for row in self._filter_orders(filter)]
        if collection == "ibkr_order_details":
            return [copy.deepcopy(row) for row in self._filter_order_details(filter)]
        if collection == "ibkr_signals":
            return [copy.deepcopy(row) for row in self._filter_signals(filter)]
        return []

    def get_first_record(self, collection, filter=None, sort=None):
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
        return rows[0] if rows else None

    def update_record(self, collection, record_id, patch):
        if collection == "orders":
            store = self.orders
        elif collection == "ibkr_signals":
            store = self.signals
        else:
            raise AssertionError(collection)
        current = copy.deepcopy(store[str(record_id)])
        current.update(copy.deepcopy(patch))
        store[str(record_id)] = current
        self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
        return copy.deepcopy(current)

    def create_record(self, collection, data):
        row = copy.deepcopy(data)
        row["id"] = f"{collection}-{len(self.created) + 1}"
        self.created.append((collection, copy.deepcopy(row)))
        if collection == "ibkr_order_details":
            self.order_details.append(copy.deepcopy(row))
        return copy.deepcopy(row)

    def _filter_orders(self, filter_value):
        fields = self._filter_fields(filter_value)
        values = set(self._filter_values(filter_value))
        environment = self._filter_environment(filter_value)
        rows = []
        for row in self.orders.values():
            if environment and to_text(row.get("environment")) != environment:
                continue
            if not fields:
                rows.append(row)
                continue
            row_extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            if any(to_text(row.get(field) or row_extra.get(field)) in values for field in fields):
                rows.append(row)
        return rows

    def _filter_order_details(self, filter_value):
        environment = self._filter_environment(filter_value)
        values = self._filter_values(filter_value)
        order_id = values[0] if values else ""
        rows = []
        for row in self.order_details:
            if environment and to_text(row.get("environment")) != environment:
                continue
            if order_id and to_text(row.get("order_id")) != order_id:
                continue
            rows.append(row)
        return rows

    def _filter_signals(self, filter_value):
        values = set(self._filter_values(filter_value))
        environment = self._filter_environment(filter_value)
        rows = []
        for row in self.signals.values():
            if environment and to_text(row.get("environment")) != environment:
                continue
            if not values or to_text(row.get("id")) in values or to_text(row.get("signal_id")) in values:
                rows.append(row)
        return rows

    @staticmethod
    def _filter_values(filter_value):
        matches = re.findall(r'=\s*"([^"]*)"', str(filter_value or ""))
        return matches[:-1] if len(matches) > 1 else matches

    @staticmethod
    def _filter_environment(filter_value):
        match = re.search(r'environment\s*=\s*"([^"]*)"', str(filter_value or ""))
        return match.group(1) if match else ""

    @staticmethod
    def _filter_fields(filter_value):
        return re.findall(r'([a-zA-Z_]+)\s*=\s*"[^"]*"', str(filter_value or ""))[:-1]


class OrderGroupActionsTest(unittest.TestCase):
    def test_cancel_group_cancels_open_orders_and_appends_details(self):
        pb = _FakePB(
            [
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "101",
                    "broker_order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "signal_id": "sig-1",
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                },
                {
                    "id": "order-2",
                    "unique_id": "sig-1_tp",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "take_profit",
                    "order_type": "TakeProfit",
                    "order_id": "102",
                    "broker_order_id": "102",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "parent_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "signal_id": "sig-1",
                    "extra": {"environment": "live", "role": "take_profit", "trade_group_id": "sig-1_entry"},
                },
                {
                    "id": "order-3",
                    "unique_id": "sig-1_sl",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Canceled",
                    "role": "stop_loss",
                    "order_type": "StopLoss",
                    "order_id": "103",
                    "broker_order_id": "103",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "parent_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "signal_id": "sig-1",
                    "extra": {"environment": "live", "role": "stop_loss", "trade_group_id": "sig-1_entry"},
                },
            ]
        )
        cancel_calls = []

        def fake_cancel_broker_order(environment, order_id, payload):
            cancel_calls.append((environment, order_id, payload.get("id")))
            return {"ok": True, "order_id": order_id}

        payload, status_code = build_order_cancel_group_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=fake_cancel_broker_order,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trade_group_id"], "sig-1_entry")
        self.assertEqual(payload["cancelled_order_ids"], ["101", "102"])
        self.assertEqual(cancel_calls, [("live", "101", "sig-1_entry"), ("live", "102", "sig-1_entry")])
        self.assertEqual(pb.orders["order-1"]["status"], "Canceled")
        self.assertEqual(pb.orders["order-2"]["status"], "Canceled")
        self.assertEqual(pb.orders["order-1"]["relation_status"], "closed")
        self.assertEqual(pb.orders["order-2"]["relation_status"], "closed")
        self.assertEqual(pb.orders["order-3"]["status"], "Canceled")
        created_details = [row for collection, row in pb.created if collection == "ibkr_order_details"]
        self.assertEqual(len(created_details), 2)
        self.assertTrue(all(row["extra"]["action"] == "cancel" for row in created_details))
        self.assertTrue(all(row["extra"]["source"] == "orders/cancel_group" for row in created_details))
        self.assertEqual(created_details[0]["extra"]["previous_status"], "Submitted")

    def test_cancel_group_syncs_unfilled_entry_signal_to_cancelled(self):
        pb = _FakePB(
            [
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "101",
                    "broker_order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "signal_id": "sig-1",
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                }
            ],
            signal_rows=[
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "submitted",
                    "note": "order_submitted",
                    "extra": {"execution_by_mode": {"live": {"status": "submitted"}}},
                }
            ],
        )

        payload, status_code = build_order_cancel_group_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=lambda *_args, **_kwargs: {"ok": True},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["signal_result"]["status"], "cancelled")
        signal = pb.signals["sig-row-1"]
        self.assertEqual(signal["status"], "cancelled")
        self.assertEqual(signal["note"], "manual_order_cancelled")
        self.assertEqual(signal["extra"]["status_reason"], "order_cancelled_by_user")
        self.assertEqual(signal["extra"]["cancelled_order_ids"], ["101"])
        self.assertEqual(signal["extra"]["execution_by_mode"]["live"]["status"], "cancelled")

    def test_cancel_group_repairs_signal_when_entry_order_already_cancelled(self):
        pb = _FakePB(
            [
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Canceled",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "101",
                    "broker_order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "quantity": 10,
                    "filled_qty": 0,
                    "signal_id": "sig-1",
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                }
            ],
            signal_rows=[
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "submitted",
                    "extra": {"execution_by_mode": {"live": {"status": "submitted"}}},
                }
            ],
        )

        payload, status_code = build_order_cancel_group_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=lambda *_args, **_kwargs: {"ok": True},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["warning"])
        self.assertEqual(payload["signal_result"]["status"], "cancelled")
        self.assertEqual(pb.signals["sig-row-1"]["status"], "cancelled")

    def test_cancel_group_rejects_child_order_target(self):
        pb = _FakePB(
            [
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "101",
                    "broker_order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                },
                {
                    "id": "order-2",
                    "unique_id": "sig-1_tp",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "take_profit",
                    "order_type": "TakeProfit",
                    "order_id": "102",
                    "broker_order_id": "102",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "parent_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "extra": {"environment": "live", "role": "take_profit", "trade_group_id": "sig-1_entry"},
                },
            ]
        )

        payload, status_code = build_order_cancel_group_response(
            pb,
            payload={"id": "sig-1_tp", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=lambda *_args, **_kwargs: {"ok": True},
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["warning"])
        self.assertIn("子单不能直接取消", payload["message"])
        self.assertEqual(pb.updated, [])
        self.assertEqual(pb.created, [])

    def test_cancel_group_without_broker_ids_still_closes_local_group(self):
        pb = _FakePB(
            [
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "",
                    "broker_order_id": "",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                }
            ]
        )

        payload, status_code = build_order_cancel_group_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=lambda *_args, **_kwargs: {"ok": True},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["cancelled_order_ids"], [])
        self.assertEqual(pb.orders["order-1"]["status"], "Canceled")
        self.assertEqual(pb.created[0][0], "ibkr_order_details")

    def test_cancel_group_matches_entry_alias_and_cancels_all_split_group_legs(self):
        pb = _FakePB(
            [
                {
                    "id": "entry",
                    "unique_id": "NFLX_short_20260423",
                    "symbol": "NFLX",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "201",
                    "broker_order_id": "201",
                    "trade_group_id": "NFLX_short_20260423",
                    "entry_order_unique_id": "NFLX_short_20260423",
                    "quantity": 5,
                    "filled_qty": 0,
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "NFLX_short_20260423"},
                },
                {
                    "id": "tp",
                    "unique_id": "NFLX_short_20260423_tp",
                    "symbol": "NFLX",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "take_profit",
                    "order_type": "TakeProfit",
                    "order_id": "202",
                    "broker_order_id": "202",
                    "trade_group_id": "entry_NFLX_short_20260423",
                    "entry_order_unique_id": "entry_NFLX_short_20260423",
                    "parent_order_unique_id": "NFLX_short_20260423",
                    "quantity": 5,
                    "filled_qty": 0,
                    "extra": {"environment": "live", "role": "take_profit", "trade_group_id": "entry_NFLX_short_20260423"},
                },
                {
                    "id": "sl",
                    "unique_id": "NFLX_short_20260423_sl",
                    "symbol": "NFLX",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "stop_loss",
                    "order_type": "StopLoss",
                    "order_id": "203",
                    "broker_order_id": "203",
                    "trade_group_id": "entry_NFLX_short_20260423",
                    "entry_order_unique_id": "entry_NFLX_short_20260423",
                    "parent_order_unique_id": "NFLX_short_20260423",
                    "quantity": 5,
                    "filled_qty": 0,
                    "extra": {"environment": "live", "role": "stop_loss", "trade_group_id": "entry_NFLX_short_20260423"},
                },
            ]
        )
        cancel_calls = []

        def fake_cancel_broker_order(environment, order_id, payload):
            cancel_calls.append((environment, order_id, payload.get("id")))
            return {"ok": True, "order_id": order_id}

        payload, status_code = build_order_cancel_group_response(
            pb,
            payload={"id": "NFLX_short_20260423", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=fake_cancel_broker_order,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["cancelled_order_ids"], ["201", "202", "203"])
        self.assertEqual(cancel_calls, [("live", "201", "NFLX_short_20260423"), ("live", "202", "NFLX_short_20260423"), ("live", "203", "NFLX_short_20260423")])
        self.assertEqual({row["status"] for row in pb.orders.values()}, {"Canceled"})
        self.assertTrue(all(row["relation_status"] == "closed" for row in pb.orders.values()))
        self.assertEqual(len([row for collection, row in pb.created if collection == "ibkr_order_details"]), 3)

    def test_close_group_closes_entry_and_cancels_children(self):
        pb = _FakePB(
            [
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Filled",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "101",
                    "broker_order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 10,
                    "fill_time": "2026-04-22 09:40:00",
                    "signal_id": "sig-1",
                    "extra": {
                        "environment": "live",
                        "role": "entry",
                        "trade_group_id": "sig-1_entry",
                        "fill_time": "2026-04-22 09:40:00",
                        "filled_us_time": "2026-04-22 09:40:00",
                        "filled_cn_time": "2026-04-22 21:40:00",
                        "filled_bar_time_ms": 1713798000000,
                    },
                },
                {
                    "id": "order-2",
                    "unique_id": "sig-1_tp",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "take_profit",
                    "order_type": "TakeProfit",
                    "order_id": "102",
                    "broker_order_id": "102",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "parent_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "signal_id": "sig-1",
                    "extra": {"environment": "live", "role": "take_profit", "trade_group_id": "sig-1_entry"},
                },
                {
                    "id": "order-3",
                    "unique_id": "sig-1_sl",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "stop_loss",
                    "order_type": "StopLoss",
                    "order_id": "103",
                    "broker_order_id": "103",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "parent_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "signal_id": "sig-1",
                    "extra": {"environment": "live", "role": "stop_loss", "trade_group_id": "sig-1_entry"},
                },
            ]
        )

        payload, status_code = build_order_close_group_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=escape_filter_string,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["closed_order_ids"], ["sig-1_entry"])
        self.assertEqual(payload["cancelled_order_ids"], ["sig-1_tp", "sig-1_sl"])
        self.assertEqual(pb.orders["order-1"]["status"], "Closed")
        self.assertEqual(pb.orders["order-2"]["status"], "Canceled")
        self.assertEqual(pb.orders["order-3"]["status"], "Canceled")
        self.assertEqual(pb.orders["order-1"]["fill_time"], "2026-04-22 09:40:00")
        self.assertEqual(pb.orders["order-1"]["extra"]["filled_us_time"], "2026-04-22 09:40:00")
        created_details = [row for collection, row in pb.created if collection == "ibkr_order_details"]
        self.assertEqual(len(created_details), 3)
        self.assertTrue(all(row["extra"]["action"] == "close_group" for row in created_details))
        self.assertTrue(all(row["extra"]["source"] == "orders/close_group" for row in created_details))
        self.assertEqual(created_details[0]["extra"]["trade_group_id"], "sig-1_entry")

    def test_close_group_preserves_filled_close_order(self):
        pb = _FakePB(
            [
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Filled",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "101",
                    "broker_order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 10,
                    "signal_id": "sig-1",
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                },
                {
                    "id": "order-2",
                    "unique_id": "sig-1_tp",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "take_profit",
                    "order_type": "TakeProfit",
                    "order_id": "102",
                    "broker_order_id": "102",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "parent_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "signal_id": "sig-1",
                    "extra": {"environment": "live", "role": "take_profit", "trade_group_id": "sig-1_entry"},
                },
                {
                    "id": "order-3",
                    "unique_id": "close_sig-1_101500",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Filled",
                    "role": "close",
                    "order_type": "MKT",
                    "order_id": "104",
                    "broker_order_id": "104",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "parent_order_unique_id": "sig-1_entry",
                    "direction": "sell",
                    "position_side": "long",
                    "quantity": 10,
                    "filled_qty": 10,
                    "fill_price": 105,
                    "signal_id": "sig-1",
                    "extra": {"environment": "live", "role": "close", "trade_group_id": "sig-1_entry"},
                },
            ]
        )

        payload, status_code = build_order_close_group_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=escape_filter_string,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["closed_order_ids"], ["sig-1_entry"])
        self.assertEqual(payload["filled_exit_order_ids"], ["close_sig-1_101500"])
        self.assertEqual(payload["cancelled_order_ids"], ["sig-1_tp"])
        self.assertEqual(pb.orders["order-1"]["status"], "Closed")
        self.assertEqual(pb.orders["order-2"]["status"], "Canceled")
        self.assertEqual(pb.orders["order-3"]["status"], "Filled")
        self.assertEqual(pb.orders["order-3"]["relation_status"], "closed")

    def test_close_group_requires_filled_entry(self):
        pb = _FakePB(
            [
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "101",
                    "broker_order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "direction": "long",
                    "quantity": 10,
                    "filled_qty": 0,
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                }
            ]
        )

        payload, status_code = build_order_close_group_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=escape_filter_string,
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["warning"])
        self.assertIn("取消挂单", payload["message"])
        self.assertEqual(pb.updated, [])
        self.assertEqual(pb.created, [])


if __name__ == "__main__":
    unittest.main()
