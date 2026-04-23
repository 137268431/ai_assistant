import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_scheduler" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

os.environ.setdefault("IBKR_SCHEDULER_AUTOSTART", "false")

if "flask" not in sys.modules:
    import types

    flask_stub = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, name):
            self.name = name

        def route(self, _path, methods=None):
            def decorator(func):
                return func

            return decorator

    flask_stub.Flask = _FakeFlask
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = SimpleNamespace(
        args={},
        headers={},
        method="GET",
        get_json=lambda silent=True: {},
        get_data=lambda cache=True: b"",
    )
    sys.modules["flask"] = flask_stub

from ibkr_api import api_app as api_app_mod


class StateRoutesTest(unittest.TestCase):
    def test_signal_state_get_returns_saved_payload(self):
        record = {
            "id": "state-1",
            "date": "2026-04-23",
            "data": {
                "processed_ids": ["sig-1"],
                "confirmed_ids": ["sig-2"],
                "active_signals": [{"id": "sig-3"}],
            },
        }

        with mock.patch.object(api_app_mod.request, "args", {"date": "2026-04-23", "environment": "paper"}):
            with mock.patch.object(api_app_mod.pb, "get_state", return_value=record):
                payload = api_app_mod.custom_ibkr_state_signals_get()

        self.assertTrue(payload["ok"])
        self.assertEqual("paper", payload["environment"])
        self.assertEqual(record["data"], payload["data"])
        self.assertEqual("ibkr-api", payload["source"])

    def test_signal_state_get_requires_date(self):
        with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
            payload, status_code = api_app_mod.custom_ibkr_state_signals_get()

        self.assertEqual(400, status_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("date required", payload["error"])

    def test_order_state_post_upserts_normalized_payload(self):
        request_payload = {
            "date": "2026-04-23",
            "environment": "live",
            "data": {
                "pending": {"AAPL": {"entry": "123"}},
                "positions": {"AAPL": 10},
                "completed_signal_outcomes": {"sig-1": "tp"},
            },
            "closed_today": ["ord-1"],
            "stop_loss_count_today": 2,
            "order_id_map": {"sig-1": "123"},
            "completed_signal_ids": ["sig-1"],
        }

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "upsert_state", return_value={"id": "state-2"}) as upsert_mock:
                payload = api_app_mod.custom_ibkr_state_orders_post()

        self.assertTrue(payload["ok"])
        self.assertEqual("live", payload["environment"])
        upsert_mock.assert_called_once_with(
            "orders",
            "live",
            {
                "closed_today": ["ord-1"],
                "stop_loss_count_today": 2,
                "pending": {"AAPL": {"entry": "123"}},
                "positions": {"AAPL": 10},
                "order_id_map": {"sig-1": "123"},
                "completed_signal_ids": ["sig-1"],
                "completed_signal_outcomes": {"sig-1": "tp"},
            },
            date="2026-04-23",
        )


if __name__ == "__main__":
    unittest.main()
