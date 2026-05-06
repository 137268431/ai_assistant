import copy
import os
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

os.environ.setdefault("PYTHONHASHSEED", "0")

from ibkr_api.signal_webhooks import build_signal_cancel_webhook_response, build_signal_confirm_webhook_response


class _FakePB:
    def __init__(self, signal_rows, order_rows=None, detail_rows=None):
        self.signals = {str(row["id"]): copy.deepcopy(row) for row in signal_rows}
        self.orders = {str(row["id"]): copy.deepcopy(row) for row in (order_rows or [])}
        self.order_details = [copy.deepcopy(row) for row in (detail_rows or [])]
        self.updated = []
        self.created = []

    def get_first_record(self, collection, filter=None, sort=None):
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
        return copy.deepcopy(rows[0]) if rows else None

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        if collection == "ibkr_signals":
            return [copy.deepcopy(row) for row in self._filter_signals(filter)]
        if collection == "orders":
            return [copy.deepcopy(row) for row in self._filter_orders(filter)]
        if collection == "ibkr_order_details":
            return [copy.deepcopy(row) for row in self._filter_order_details(filter)]
        return []

    def update_record(self, collection, record_id, patch):
        if collection == "ibkr_signals":
            current = copy.deepcopy(self.signals[str(record_id)])
            current.update(copy.deepcopy(patch))
            self.signals[str(record_id)] = current
        elif collection == "orders":
            current = copy.deepcopy(self.orders[str(record_id)])
            current.update(copy.deepcopy(patch))
            self.orders[str(record_id)] = current
        else:
            raise AssertionError(f"unsupported collection: {collection}")
        self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
        return copy.deepcopy(current)

    def create_record(self, collection, data):
        row = copy.deepcopy(data)
        row["id"] = f"{collection}-{len(self.created) + 1}"
        self.created.append((collection, copy.deepcopy(row)))
        if collection == "ibkr_order_details":
            self.order_details.append(copy.deepcopy(row))
        return copy.deepcopy(row)

    def _filter_signals(self, filter_value):
        environment = self._extract_environment(filter_value)
        targets = re.findall(r'(?:id|signal_id)\s*=\s*"([^"]*)"', str(filter_value or ""))
        values = {value for value in targets if value}
        rows = []
        for row in self.signals.values():
            if environment and str(row.get("environment") or "") != environment:
                continue
            if values and str(row.get("id") or "") not in values and str(row.get("signal_id") or "") not in values:
                continue
            rows.append(row)
        return rows

    def _filter_orders(self, filter_value):
        environment = self._extract_environment(filter_value)
        match = re.search(r'signal_id\s*=\s*"([^"]*)"', str(filter_value or ""))
        signal_id = match.group(1) if match else ""
        rows = []
        for row in self.orders.values():
            if environment and str(row.get("environment") or "") != environment:
                continue
            if signal_id and str(row.get("signal_id") or "") != signal_id:
                continue
            rows.append(row)
        return rows

    def _filter_order_details(self, filter_value):
        environment = self._extract_environment(filter_value)
        match = re.search(r'order_id\s*=\s*"([^"]*)"', str(filter_value or ""))
        order_id = match.group(1) if match else ""
        rows = []
        for row in self.order_details:
            if environment and str(row.get("environment") or "") != environment:
                continue
            if order_id and str(row.get("order_id") or "") != order_id:
                continue
            rows.append(row)
        return rows

    @staticmethod
    def _extract_environment(filter_value):
        match = re.search(r'environment\s*=\s*"([^"]*)"', str(filter_value or ""))
        return match.group(1) if match else ""


class SignalWebhookBuildersTest(unittest.TestCase):
    def setUp(self):
        self.normalize_environment = lambda value, default: str(value or default).strip().lower() or default
        self.escape_filter_string = lambda value: str(value or "").replace('\\', '\\\\').replace('"', '\\"')
        self.now_provider = lambda: datetime(2026, 4, 23, 1, 2, 3, tzinfo=timezone.utc)

    def test_confirm_webhook_requires_signal_id(self):
        page, status_code = build_signal_confirm_webhook_response(
            _FakePB([]),
            payload={"environment": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
        )

        self.assertEqual(status_code, 400)
        self.assertEqual(page["page_kind"], "fail")
        self.assertIn("缺少信号ID", page["body"])

    def test_confirm_webhook_updates_signal_and_syncs_notification_metadata(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "awaiting_confirm",
                    "note": "wait_user",
                    "extra": {"foo": "bar"},
                }
            ]
        )
        notify_calls = []

        page, status_code = build_signal_confirm_webhook_response(
            pb,
            payload={"id": "sig-1", "environment": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            now_provider=self.now_provider,
            notify_signal_status=lambda status, signal_row, options: notify_calls.append((status, signal_row["id"], options)) or {"success": True, "message_id": "sig-msg-1"},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "ok")
        self.assertEqual(page["title"], "信号已确认")
        self.assertIn("<!DOCTYPE html>", page["body"])
        self.assertEqual(pb.signals["sig-row-1"]["status"], "pending")
        self.assertEqual(pb.signals["sig-row-1"]["note"], "")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["foo"], "bar")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["confirmed_by"], "manual")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["confirmed_at"], "2026-04-23T01:02:03Z")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["status_reason"], "confirmed_by_user")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["feishu_signal_message_id"], "sig-msg-1")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["feishu_signal_card_version"], 1)
        self.assertEqual(len(notify_calls), 1)
        self.assertEqual(notify_calls[0][0], "pending")
        self.assertEqual(notify_calls[0][2]["message"], "信号已确认，等待执行")

    def test_confirm_webhook_expires_signal_when_confirmation_is_too_late(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "awaiting_confirm",
                    "note": "wait_user",
                    "bar_time_ms": 1778060100000,
                    "extra": {"foo": "bar"},
                }
            ]
        )
        now_provider = lambda: datetime.fromtimestamp((1778060100000 + 31 * 60 * 1000) / 1000, tz=timezone.utc)

        page, status_code = build_signal_confirm_webhook_response(
            pb,
            payload={"id": "sig-1", "environment": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            now_provider=now_provider,
            config_value=lambda key, default, environment: "30" if key == "signal_validity_minutes" else default,
            notify_signal_status=lambda *_args, **_kwargs: {"success": True, "message_id": "sig-msg-1"},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "fail")
        self.assertEqual(page["title"], "信号已过期")
        self.assertEqual(pb.signals["sig-row-1"]["status"], "expired")
        self.assertEqual(pb.signals["sig-row-1"]["note"], "confirm_too_late")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["foo"], "bar")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["status_reason"], "confirm_too_late")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["signal_validity_minutes"], 30)

    def test_confirm_webhook_returns_terminal_status_page_without_updates(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "executed",
                    "note": "done",
                    "extra": {},
                }
            ]
        )

        page, status_code = build_signal_confirm_webhook_response(
            pb,
            payload={"id": "sig-1", "environment": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "ok")
        self.assertEqual(page["title"], "信号已执行")
        self.assertEqual(pb.updated, [])

    def test_confirm_webhook_reports_submitted_status_without_updates(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "submitted",
                    "note": "order_submitted",
                    "extra": {},
                }
            ]
        )

        page, status_code = build_signal_confirm_webhook_response(
            pb,
            payload={"id": "sig-1", "environment": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "ok")
        self.assertEqual(page["title"], "订单已提交")
        self.assertEqual(pb.updated, [])

    def test_cancel_webhook_reports_protected_active_without_updates(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "protected_active",
                    "note": "entry_filled",
                    "extra": {},
                }
            ]
        )

        page, status_code = build_signal_cancel_webhook_response(
            pb,
            payload={"id": "sig-1", "environment": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            cancel_broker_order=lambda *_args, **_kwargs: {"ok": True},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "warn")
        self.assertEqual(page["title"], "保护单已生效")
        self.assertEqual(pb.updated, [])

    def test_cancel_webhook_rejects_awaiting_confirm_without_canceling_orders(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "awaiting_confirm",
                    "note": "wait_user",
                    "extra": {},
                }
            ],
            order_rows=[
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "101",
                    "broker_order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                }
            ],
        )
        cancel_calls = []

        page, status_code = build_signal_cancel_webhook_response(
            pb,
            payload={"id": "sig-1", "environment": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            cancel_broker_order=lambda *_args, **_kwargs: cancel_calls.append(True) or {"ok": True},
            now_provider=self.now_provider,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "fail")
        self.assertEqual(page["title"], "信号已拒绝")
        self.assertIn("拒绝成功", page["body"])
        self.assertEqual(cancel_calls, [])
        self.assertEqual(pb.signals["sig-row-1"]["status"], "rejected")
        self.assertEqual(pb.signals["sig-row-1"]["note"], "manual_rejected")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["status_reason"], "manual_rejected")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["cancelled_order_ids"], [])
        self.assertEqual(pb.orders["order-1"]["status"], "Submitted")

    def test_cancel_webhook_cancels_related_orders_by_signal_id_and_appends_details(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "pending",
                    "note": "",
                    "extra": {"feishu_signal_message_id": "sig-msg-old"},
                }
            ],
            order_rows=[
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
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
                    "signal_id": "sig-1",
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
                {
                    "id": "order-3",
                    "unique_id": "sig-2_entry",
                    "signal_id": "sig-2",
                    "symbol": "MSFT",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "201",
                    "broker_order_id": "201",
                    "trade_group_id": "sig-2_entry",
                    "entry_order_unique_id": "sig-2_entry",
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-2_entry"},
                },
            ],
        )
        cancel_calls = []
        signal_notify_calls = []
        order_notify_calls = []

        page, status_code = build_signal_cancel_webhook_response(
            pb,
            payload={"id": "sig-1", "environment": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            cancel_broker_order=lambda environment, order_id, payload: cancel_calls.append((environment, order_id, payload["source"], payload["signal_id"])) or {"ok": True},
            now_provider=self.now_provider,
            notify_signal_status=lambda status, signal_row, options: signal_notify_calls.append((status, signal_row["id"], options)) or {"success": True, "message_id": "sig-msg-2"},
            notify_order_status=lambda status, order_row, options: order_notify_calls.append((status, order_row["id"], options)) or {"success": True, "message_id": "order-msg-1"},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "fail")
        self.assertIn("信号已拒绝", page["body"])
        self.assertEqual(cancel_calls, [("live", "101", "webhook/signal/cancel", "sig-1"), ("live", "102", "webhook/signal/cancel", "sig-1")])
        self.assertEqual(pb.signals["sig-row-1"]["status"], "rejected")
        self.assertEqual(pb.signals["sig-row-1"]["note"], "manual_rejected")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["cancelled_order_ids"], ["101", "102"])
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["cancel_order_failures"], [])
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["feishu_signal_message_id"], "sig-msg-2")
        self.assertEqual(pb.orders["order-1"]["status"], "Canceled")
        self.assertEqual(pb.orders["order-1"]["relation_status"], "closed")
        self.assertEqual(pb.orders["order-1"]["extra"]["feishu_order_message_id"], "order-msg-1")
        self.assertEqual(pb.orders["order-2"]["status"], "Canceled")
        self.assertEqual(pb.orders["order-3"]["status"], "Submitted")
        created_details = [row for collection, row in pb.created if collection == "ibkr_order_details"]
        self.assertEqual(len(created_details), 2)
        self.assertEqual([row["extra"]["source"] for row in created_details], ["webhook/signal/cancel", "webhook/signal/cancel"])
        self.assertEqual(len(signal_notify_calls), 1)
        self.assertEqual(signal_notify_calls[0][0], "rejected")
        self.assertEqual(signal_notify_calls[0][2]["message"], "信号已拒绝，暂不执行")
        self.assertEqual(len(order_notify_calls), 1)
        self.assertEqual(order_notify_calls[0][0], "canceled")

    def test_cancel_webhook_returns_failure_when_related_order_cancel_fails(self):
        pb = _FakePB(
            [
                {
                    "id": "sig-row-1",
                    "signal_id": "sig-1",
                    "environment": "live",
                    "symbol": "AAPL",
                    "status": "pending",
                    "note": "",
                    "extra": {},
                }
            ],
            order_rows=[
                {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "signal_id": "sig-1",
                    "symbol": "AAPL",
                    "environment": "live",
                    "status": "Submitted",
                    "role": "entry",
                    "order_type": "Entry",
                    "order_id": "101",
                    "broker_order_id": "101",
                    "trade_group_id": "sig-1_entry",
                    "entry_order_unique_id": "sig-1_entry",
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                }
            ],
        )

        page, status_code = build_signal_cancel_webhook_response(
            pb,
            payload={"id": "sig-1", "environment": "live"},
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            cancel_broker_order=lambda *_args, **_kwargs: {"ok": False, "error": "broker_down", "status_code": 503},
            now_provider=self.now_provider,
        )

        self.assertEqual(status_code, 500)
        self.assertEqual(page["page_kind"], "fail")
        self.assertIn("账户撤单失败 1 条", page["body"])
        self.assertEqual(pb.signals["sig-row-1"]["status"], "rejected")
        self.assertEqual(pb.signals["sig-row-1"]["note"], "manual_rejected_with_cancel_failures:1")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["status_reason"], "manual_rejected_with_cancel_failures")
        self.assertEqual(pb.signals["sig-row-1"]["extra"]["cancel_order_failures"][0]["order_id"], "101")
        self.assertEqual(pb.orders["order-1"]["status"], "Submitted")
        self.assertEqual(pb.created, [])


if __name__ == "__main__":
    unittest.main()
