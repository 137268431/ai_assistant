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

from ibkr_api.order_values import to_text
from ibkr_api.order_webhooks import build_order_cancel_webhook_response, build_order_close_webhook_response


class _FakePB:
    def __init__(self, order_rows, detail_rows=None):
        self.orders = {str(row["id"]): copy.deepcopy(row) for row in order_rows}
        self.order_details = [copy.deepcopy(row) for row in (detail_rows or [])]
        self.updated = []
        self.created = []

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        if collection == "orders":
            return [copy.deepcopy(row) for row in self._filter_orders(filter)]
        if collection == "ibkr_order_details":
            return [copy.deepcopy(row) for row in self._filter_order_details(filter)]
        return []

    def update_record(self, collection, record_id, patch):
        assert collection == "orders"
        current = copy.deepcopy(self.orders[str(record_id)])
        current.update(copy.deepcopy(patch))
        self.orders[str(record_id)] = current
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


class OrderWebhookPagesTest(unittest.TestCase):
    def test_cancel_webhook_requires_order_id(self):
        page, status_code = build_order_cancel_webhook_response(
            _FakePB([]),
            payload={"environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=lambda value: str(value or "").replace('\\', '\\\\').replace('"', '\\"'),
            cancel_broker_order=lambda *_args, **_kwargs: {"ok": True},
        )

        self.assertEqual(status_code, 400)
        self.assertEqual(page["page_kind"], "fail")
        self.assertIn("缺少订单ID", page["body"])

    def test_cancel_webhook_success_renders_ok_page_and_webhook_detail_source(self):
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
        cancel_calls = []

        page, status_code = build_order_cancel_webhook_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=lambda value: str(value or "").replace('\\', '\\\\').replace('"', '\\"'),
            cancel_broker_order=lambda environment, order_id, payload: cancel_calls.append((environment, order_id, payload["source"])) or {"ok": True},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "ok")
        self.assertEqual(page["content_type"], "text/html; charset=utf-8")
        self.assertIn("<!DOCTYPE html>", page["body"])
        self.assertIn("订单已取消", page["body"])
        self.assertEqual(cancel_calls, [("live", "101", "webhook/order/cancel"), ("live", "102", "webhook/order/cancel")])
        created_details = [row for collection, row in pb.created if collection == "ibkr_order_details"]
        self.assertEqual([row["extra"]["source"] for row in created_details], ["webhook/order/cancel", "webhook/order/cancel"])

    def test_cancel_webhook_child_order_warning_uses_warning_page(self):
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
                    "extra": {"environment": "live", "role": "take_profit", "trade_group_id": "sig-1_entry"},
                },
            ]
        )

        page, status_code = build_order_cancel_webhook_response(
            pb,
            payload={"id": "sig-1_tp", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=lambda value: str(value or "").replace('\\', '\\\\').replace('"', '\\"'),
            cancel_broker_order=lambda *_args, **_kwargs: {"ok": True},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "warn")
        self.assertEqual(page["title"], "只能取消主单")
        self.assertIn("止盈/止损等子单不能直接取消", page["body"])

    def test_cancel_webhook_runtime_failure_renders_fail_page(self):
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
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                }
            ]
        )

        page, status_code = build_order_cancel_webhook_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=lambda value: str(value or "").replace('\\', '\\\\').replace('"', '\\"'),
            cancel_broker_order=lambda *_args, **_kwargs: {"ok": False, "error": "broker_down", "status_code": 503},
        )

        self.assertEqual(status_code, 500)
        self.assertEqual(page["page_kind"], "fail")
        self.assertEqual(page["title"], "订单取消失败")
        self.assertIn("账户撤单失败 1 条", page["body"])

    def test_close_webhook_success_renders_ok_page_and_group_reason(self):
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
                    "filled_qty": 10,
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
                    "filled_qty": 0,
                    "extra": {"environment": "live", "role": "take_profit", "trade_group_id": "sig-1_entry"},
                },
            ]
        )

        page, status_code = build_order_close_webhook_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=lambda value: str(value or "").replace('\\', '\\\\').replace('"', '\\"'),
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "ok")
        self.assertIn("交易组已平仓", page["body"])
        created_details = [row for collection, row in pb.created if collection == "ibkr_order_details"]
        self.assertEqual(created_details[0]["extra"]["source"], "webhook/order/close")
        self.assertEqual(created_details[0]["reason"], "页面平仓交易组 sig-1_entry")

    def test_close_webhook_warning_uses_landing_page_copy(self):
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
                    "filled_qty": 0,
                    "extra": {"environment": "live", "role": "entry", "trade_group_id": "sig-1_entry"},
                }
            ]
        )

        page, status_code = build_order_close_webhook_response(
            pb,
            payload={"id": "sig-1_entry", "environment": "live"},
            normalize_environment=lambda value, default: to_text(value or default) or default,
            escape_filter_string=lambda value: str(value or "").replace('\\', '\\\\').replace('"', '\\"'),
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(page["page_kind"], "warn")
        self.assertEqual(page["title"], "订单未成交")
        self.assertIn("请先取消挂单", page["body"])


if __name__ == "__main__":
    unittest.main()
