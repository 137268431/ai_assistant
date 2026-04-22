import copy
import sys
import unittest
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


class _FakePB:
    def __init__(self, signal=None):
        self.signal = copy.deepcopy(signal)
        self.updated = []

    def get_first_record(self, collection, filter=None, sort=None):
        if collection != "ibkr_signals":
            raise AssertionError(f"unexpected collection lookup: {collection}")
        return copy.deepcopy(self.signal)

    def update_record(self, collection, record_id, patch):
        self.updated.append((collection, str(record_id), copy.deepcopy(patch)))
        if isinstance(self.signal, dict):
            self.signal.update(copy.deepcopy(patch))
        return copy.deepcopy(self.signal)


class FeishuCallbacksTest(unittest.TestCase):
    def setUp(self):
        self.as_dict = lambda value: dict(value) if isinstance(value, dict) else {}
        self.normalize_environment = lambda value, default: str(value or default).strip().lower() or default
        self.escape_filter_string = lambda value: str(value or "").replace("\\", "\\\\").replace('"', '\\"')

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
        pb = _FakePB(signal={"id": "sig-row-1", "status": "awaiting_confirm"})

        payload, status_code = dispatch_feishu_signal_callback(
            "confirm",
            "sig-1",
            "live",
            pb=pb,
            escape_filter_string=self.escape_filter_string,
            callback_toast_fn=callback_toast,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["toast"]["type"], "success")
        self.assertEqual(pb.updated[0][2]["status"], "pending")

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
            dispatch_feishu_order_callback_fn=lambda action, order_id, environment: order_calls.append((action, order_id, environment)) or ({"ok": True}, 200),
            dispatch_feishu_signal_callback_fn=lambda action, signal_id, environment: signal_calls.append((action, signal_id, environment)) or ({"ok": True}, 200),
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
