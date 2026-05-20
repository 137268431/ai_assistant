import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.callbacks.feishu import (
    callback_toast,
    dispatch_feishu_2fa_callback,
    dispatch_feishu_order_callback,
    dispatch_feishu_signal_callback,
    handle_feishu_callback,
)
from ibkr_api.integrations.runtime_orders import cancel_broker_order_via_runtime
from ibkr_api.signals.webhooks import build_signal_cancel_webhook_response, build_signal_confirm_webhook_response


class _FakePB:
    def __init__(self, signal=None, order_rows=None):
        self.signal = copy.deepcopy(signal)
        self.orders = {str(row["id"]): copy.deepcopy(row) for row in (order_rows or [])}
        self.updated = []

    def get_first_record(self, collection, filter=None, sort=None):
        if collection == "ibkr_signals":
            return copy.deepcopy(self.signal)
        if collection == "orders":
            rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
            return copy.deepcopy(rows[0]) if rows else None
        raise AssertionError(f"unexpected collection lookup: {collection}")

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        if collection == "orders":
            return [copy.deepcopy(row) for row in self.orders.values()][:per_page]
        raise AssertionError(f"unexpected collection lookup: {collection}")

    def update_record(self, collection, record_id, patch):
        self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
        if collection == "orders":
            self.orders[str(record_id)].update(copy.deepcopy(patch))
            return copy.deepcopy(self.orders[str(record_id)])
        if isinstance(self.signal, dict):
            self.signal.update(copy.deepcopy(patch))
        return copy.deepcopy(self.signal)


class FeishuCallbacksTest(unittest.TestCase):
    def setUp(self):
        self.as_dict = lambda value: dict(value) if isinstance(value, dict) else {}
        self.normalize_environment = lambda value, default: str(value or default).strip().lower() or default
        self.escape_filter_string = lambda value: str(value or "").replace("\\", "\\\\").replace('"', '\\"')
        self.config_value = lambda key, default, environment: default
        self.now_provider = lambda: datetime(2026, 4, 23, 1, 2, 3, tzinfo=timezone.utc)

    def _confirm_builder(self, *args, **kwargs):
        kwargs.setdefault("now_provider", self.now_provider)
        return build_signal_confirm_webhook_response(*args, **kwargs)

    def _cancel_builder(self, *args, **kwargs):
        kwargs.setdefault("now_provider", self.now_provider)
        return build_signal_cancel_webhook_response(*args, **kwargs)

    def test_callback_toast_with_card(self):
        payload = callback_toast("success", "ok", card={"hello": "world"})

        self.assertEqual(payload["toast"]["type"], "success")
        self.assertEqual(payload["card"]["data"]["hello"], "world")

    def test_dispatch_feishu_2fa_callback_posts_manual_request(self):
        calls = []

        payload = dispatch_feishu_2fa_callback(
            "ibkr_2fa_start",
            "paper",
            request_json_request=lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True, "payload": {"message": "done"}},
            pb_base_url="http://pb.test",
            as_dict=self.as_dict,
            callback_toast_fn=callback_toast,
        )

        self.assertEqual(payload["toast"]["type"], "success")
        self.assertEqual(calls[0][0][1], "http://pb.test")
        self.assertEqual(calls[0][0][2], "/api/custom/ibkr/2fa/request")
        self.assertEqual(calls[0][1]["json_body"]["environment"], "paper")

    def test_dispatch_feishu_signal_callback_confirms_signal(self):
        pb = _FakePB(signal={"id": "sig-row-1", "signal_id": "sig-1", "environment": "live", "symbol": "AAPL", "status": "awaiting_confirm", "extra": {}})

        payload, status_code = dispatch_feishu_signal_callback(
            "confirm",
            "sig-1",
            "live",
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            cancel_broker_order=lambda environment, order_id, payload=None: {"ok": True},
            build_signal_confirm_webhook_response_fn=self._confirm_builder,
            build_signal_cancel_webhook_response_fn=self._cancel_builder,
            callback_toast_fn=callback_toast,
            console_base_url="https://console.example.com",
            config_value=self.config_value,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["toast"]["type"], "success")
        self.assertEqual(payload["toast"]["content"], "信号已确认，等待执行")
        self.assertIn("card", payload)
        self.assertEqual(pb.updated[0][2]["status"], "pending")
        self.assertEqual(pb.signal["extra"]["confirmed_by"], "manual")
        self.assertEqual(pb.signal["extra"]["confirmed_at"], "2026-04-23T01:02:03Z")
        self.assertEqual(pb.signal["extra"]["status_reason"], "confirmed_by_user")

    def test_dispatch_feishu_signal_callback_rejects_signal(self):
        pb = _FakePB(signal={"id": "sig-row-1", "signal_id": "sig-1", "environment": "live", "symbol": "AAPL", "status": "awaiting_confirm", "extra": {}})

        payload, status_code = dispatch_feishu_signal_callback(
            "reject",
            "sig-1",
            "live",
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            cancel_broker_order=lambda environment, order_id, payload=None: {"ok": True},
            build_signal_confirm_webhook_response_fn=self._confirm_builder,
            build_signal_cancel_webhook_response_fn=self._cancel_builder,
            callback_toast_fn=callback_toast,
            console_base_url="https://console.example.com",
            config_value=self.config_value,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["toast"]["type"], "success")
        self.assertEqual(payload["toast"]["content"], "信号已拒绝，暂不执行")
        self.assertIn("card", payload)
        self.assertEqual(pb.updated[0][2]["status"], "rejected")
        self.assertEqual(pb.signal["extra"]["rejected_by"], "manual")
        self.assertEqual(pb.signal["extra"]["rejected_at"], "2026-04-23T01:02:03Z")
        self.assertEqual(pb.signal["extra"]["status_reason"], "manual_rejected")

    def test_dispatch_feishu_signal_callback_treats_submitted_as_terminal(self):
        pb = _FakePB(signal={"id": "sig-row-1", "signal_id": "sig-1", "environment": "live", "symbol": "AAPL", "status": "submitted", "extra": {}})

        payload, status_code = dispatch_feishu_signal_callback(
            "confirm",
            "sig-1",
            "live",
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            cancel_broker_order=lambda environment, order_id, payload=None: {"ok": True},
            build_signal_confirm_webhook_response_fn=self._confirm_builder,
            build_signal_cancel_webhook_response_fn=self._cancel_builder,
            callback_toast_fn=callback_toast,
            console_base_url="https://console.example.com",
            config_value=self.config_value,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["toast"]["type"], "info")
        self.assertIn("card", payload)
        self.assertEqual(pb.updated, [])

    def test_dispatch_feishu_order_callback_wraps_builder_response(self):
        payload, status_code = dispatch_feishu_order_callback(
            "cancel",
            "group-1",
            "live",
            pb=object(),
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            cancel_broker_order=lambda environment, order_id, payload=None: {"ok": True},
            build_order_cancel_group_response_fn=lambda *args, **kwargs: ({"message": "cancelled"}, 202),
            build_order_close_group_response_fn=lambda *args, **kwargs: ({"message": "closed"}, 200),
            callback_toast_fn=callback_toast,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["toast"]["content"], "cancelled")

    def test_dispatch_feishu_order_callback_card_includes_lifecycle_button(self):
        notify_calls = []
        pb = _FakePB(
            order_rows=[
                {
                    "id": "order-row-1",
                    "unique_id": "entry-1",
                    "order_id": "12345",
                    "symbol": "AAPL",
                    "environment": "live",
                    "signal_id": "sig-order",
                    "trade_group_id": "tg-order",
                    "status": "Submitted",
                    "role": "entry",
                    "order_type": "LMT",
                    "quantity": 20,
                    "filled_qty": 5,
                    "us_time": "2026-05-15 09:40:00",
                }
            ]
        )

        payload, status_code = dispatch_feishu_order_callback(
            "cancel",
            "tg-order",
            "live",
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            cancel_broker_order=lambda environment, order_id, payload=None: {"ok": True},
            build_order_cancel_group_response_fn=lambda *args, **kwargs: ({"message": "cancelled"}, 200),
            build_order_close_group_response_fn=lambda *args, **kwargs: ({"message": "closed"}, 200),
            callback_toast_fn=callback_toast,
            notify_order_status=lambda status, order_row, options: notify_calls.append((status, order_row["id"], options))
            or {"success": True, "message_id": "order-msg-1"},
            console_base_url="https://console.example.com",
        )

        self.assertEqual(status_code, 200)
        self.assertIn("card", payload)
        self.assertEqual(len(notify_calls), 1)
        self.assertEqual(notify_calls[0][0], "canceled")
        self.assertEqual(notify_calls[0][1], "order-row-1")
        self.assertEqual(notify_calls[0][2]["message"], "cancelled")
        actions = [
            action
            for element in payload["card"]["data"]["elements"]
            if element.get("tag") == "action"
            for action in element.get("actions", [])
        ]
        urls = [action.get("multi_url", {}).get("url", "") for action in actions]
        self.assertTrue(any("/ibkr_lifecycle_flow.html" in url and "signal_id=sig-order" in url for url in urls))

    def test_handle_feishu_callback_prioritizes_order_id_before_signal_id(self):
        order_calls = []
        signal_calls = []

        response = handle_feishu_callback(
            {
                "event": {
                    "token": "card-token",
                    "action": {"value": {"action": "cancel", "order_id": "ord-1", "signal_id": "sig-1", "environment": "paper"}}
                }
            },
            as_dict=self.as_dict,
            normalize_environment=self.normalize_environment,
            dispatch_feishu_2fa_callback_fn=lambda action, environment: {"toast": {"type": "success"}},
            dispatch_feishu_order_callback_fn=lambda action, order_id, environment, data_environment="": order_calls.append((action, order_id, environment)) or ({"ok": True}, 200),
            dispatch_feishu_signal_callback_fn=lambda action, signal_id, environment, data_environment="": signal_calls.append((action, signal_id, environment)) or ({"ok": True}, 200),
            callback_toast_fn=callback_toast,
            callback_response_fn=lambda payload, update_token="", status_code=200: {"payload": payload, "token": update_token, "status_code": status_code},
        )

        self.assertEqual(order_calls, [("cancel", "ord-1", "paper")])
        self.assertEqual(signal_calls, [])
        self.assertEqual(response["token"], "card-token")

    def test_cancel_broker_order_via_runtime_normalizes_response(self):
        calls = []

        payload = cancel_broker_order_via_runtime(
            "paper",
            "123",
            {"reason": "manual"},
            request_json_request=lambda *args, **kwargs: calls.append((args, kwargs)) or {
                "ok": True,
                "status_code": 200,
                "payload": {"message": "sent"},
                "target_url": "http://runtime/ibkr/orders/cancel",
            },
            runtime_base_url="http://runtime",
            normalize_environment=self.normalize_environment,
            as_dict=self.as_dict,
        )

        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["message"], "sent")
        self.assertEqual(calls[0][0][1], "http://runtime")
        self.assertEqual(calls[0][1]["json_body"]["environment"], "paper")


if __name__ == "__main__":
    unittest.main()
