import copy
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = SimpleNamespace(args={}, get_json=lambda silent=True: {})
    sys.modules["flask"] = flask_stub

from ibkr_api.orders.group_cancel import build_order_cancel_group_response
from ibkr_api.orders.routes import register_order_routes
from ibkr_api.orders.values import escape_filter_string, to_text
from ibkr_api.orders.webhooks import build_order_cancel_webhook_response


class _FakeApp:
    def __init__(self):
        self.routes = {}

    def route(self, path, methods=None):
        def decorator(func):
            self.routes[(path, tuple(methods or []))] = func
            return func

        return decorator


class _RoutesPB:
    def __init__(self):
        self.orders = {
            "order-1": {
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
        }
        self.order_details = []
        self.signals = {
            "sig-row-1": {
                "id": "sig-row-1",
                "signal_id": "sig-1",
                "environment": "live",
                "symbol": "AAPL",
                "status": "submitted",
                "note": "order_submitted",
                "extra": {
                    "feishu_signal_message_id": "sig-msg-old",
                    "execution_by_mode": {"live": {"status": "submitted"}},
                },
            }
        }
        self.updated = []
        self.created = []

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        if collection == "orders":
            return [copy.deepcopy(row) for row in self._filter_rows(self.orders.values(), filter)]
        if collection == "ibkr_signals":
            return [copy.deepcopy(row) for row in self._filter_rows(self.signals.values(), filter)]
        if collection == "ibkr_order_details":
            return [copy.deepcopy(row) for row in self.order_details]
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

    def _filter_rows(self, rows, filter_value):
        filter_text = str(filter_value or "")
        environment = self._extract_string(filter_text, "environment")
        values = set(self._filter_values(filter_text))
        fields = self._filter_fields(filter_text)
        matched = []
        for row in rows:
            if environment and to_text(row.get("environment")) != environment:
                continue
            if not values or not fields:
                matched.append(row)
                continue
            extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            if any(to_text(row.get(field) or extra.get(field)) in values for field in fields):
                matched.append(row)
        return matched

    @staticmethod
    def _extract_string(filter_text, field_name):
        match = re.search(rf'{re.escape(field_name)}\s*=\s*"([^"]*)"', filter_text)
        return match.group(1) if match else ""

    @staticmethod
    def _filter_values(filter_text):
        matches = re.findall(r'=\s*"([^"]*)"', str(filter_text or ""))
        return matches[:-1] if len(matches) > 1 else matches

    @staticmethod
    def _filter_fields(filter_text):
        fields = re.findall(r'([a-zA-Z_]+)\s*=\s*"[^"]*"', str(filter_text or ""))
        return fields[:-1] if len(fields) > 1 else fields


class OrderRoutesNotificationTest(unittest.TestCase):
    def _route_deps(self, pb, *, updated_cards, cancel_calls=None):
        def cancel_broker_order(environment, order_id, payload):
            if cancel_calls is not None:
                cancel_calls.append((environment, order_id, payload))
            return {"ok": True}

        return {
            "pb": pb,
            "normalize_environment": lambda value, default="live": to_text(value or default) or default,
            "escape_filter_string": escape_filter_string,
            "cancel_broker_order": cancel_broker_order,
            "build_order_upsert_response": lambda *_args, **_kwargs: ({"ok": True}, 200),
            "build_orders_reconcile_response": lambda *_args, **_kwargs: ({"ok": True}, 200),
            "build_order_cancel_sync_response": build_order_cancel_group_response,
            "build_order_cancel_group_response": build_order_cancel_group_response,
            "build_order_close_group_response": lambda *_args, **_kwargs: ({"ok": True}, 200),
            "build_order_cancel_webhook_response": build_order_cancel_webhook_response,
            "build_order_close_webhook_response": lambda *_args, **_kwargs: ({"ok": True, "body": ""}, 200),
            "signal_chat_id": lambda environment: f"signal-chat-{environment}",
            "feishu_send_interactive": lambda *_args, **_kwargs: {"success": True, "message_id": "sig-msg-new"},
            "feishu_update_interactive": lambda message_id, card, environment: updated_cards.append((message_id, card, environment))
            or {"success": True, "message_id": message_id},
            "console_base_url": lambda: "https://console.example.com",
        }

    def test_cancel_group_updates_signal_feishu_card(self):
        pb = _RoutesPB()
        app = _FakeApp()
        updated_cards = []

        exports = register_order_routes(app, deps=self._route_deps(pb, updated_cards=updated_cards))

        import ibkr_api.orders.routes as routes_mod

        routes_mod.request = SimpleNamespace(
            get_json=lambda silent=True: {"id": "sig-1_entry", "environment": "live"},
            args={},
        )
        payload = exports["custom_ibkr_orders_cancel_group"]()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["signal_result"]["status"], "cancelled")
        self.assertTrue(payload["signal_notification"]["success"])
        self.assertEqual(payload["signal_notification"]["message_id"], "sig-msg-old")
        self.assertEqual(updated_cards[0][0], "sig-msg-old")
        self.assertEqual(updated_cards[0][2], "live")
        content = updated_cards[0][1]["elements"][0]["content"]
        self.assertIn("**状态**: 已取消", content)
        self.assertIn("订单已取消，关联信号已同步取消", content)
        signal_extra = pb.signals["sig-row-1"]["extra"]
        self.assertEqual(pb.signals["sig-row-1"]["status"], "cancelled")
        self.assertEqual(signal_extra["feishu_signal_notify_last_action"], "cancelled")

    def test_webhook_order_cancel_already_cancelled_entry_updates_signal_feishu_card(self):
        pb = _RoutesPB()
        pb.orders["order-1"]["status"] = "Canceled"
        pb.orders["order-1"]["filled_qty"] = 0
        app = _FakeApp()
        updated_cards = []
        cancel_calls = []
        exports = register_order_routes(
            app,
            deps=self._route_deps(pb, updated_cards=updated_cards, cancel_calls=cancel_calls),
        )

        import ibkr_api.orders.routes as routes_mod

        routes_mod.request = SimpleNamespace(
            get_json=lambda silent=True: {},
            args={"id": "sig-1_entry", "environment": "live"},
        )
        body, status_code, headers = exports["webhook_order_cancel"]()

        self.assertEqual(status_code, 200)
        self.assertIn("订单已取消", body)
        self.assertIn("无需重复操作", body)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(cancel_calls, [])
        signal = pb.signals["sig-row-1"]
        signal_extra = signal["extra"]
        self.assertEqual(signal["status"], "cancelled")
        self.assertEqual(signal["note"], "manual_order_cancelled")
        self.assertEqual(signal_extra["status_reason"], "order_cancelled_by_user")
        self.assertEqual(signal_extra["cancelled_by"], "webhook/order/cancel")
        self.assertEqual(signal_extra["cancelled_order_ids"], ["101"])
        self.assertEqual(len(updated_cards), 1)
        self.assertEqual(updated_cards[0][0], "sig-msg-old")
        self.assertEqual(updated_cards[0][2], "live")
        content = updated_cards[0][1]["elements"][0]["content"]
        self.assertIn("**状态**: 已取消", content)
        self.assertIn("订单已取消，关联信号已同步取消", content)
        self.assertIn("**取消原因**: 订单已取消，同步信号为已取消", content)
        self.assertIn("**已撤订单**: 101", content)
        self.assertEqual(signal_extra["feishu_signal_notify_last_action"], "cancelled")
        self.assertEqual(signal_extra["feishu_signal_notify_last_status"], "cancelled")
        self.assertEqual(signal_extra["feishu_signal_message_id"], "sig-msg-old")


if __name__ == "__main__":
    unittest.main()
