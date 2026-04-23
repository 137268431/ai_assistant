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


class ControlRuntimeActionsTest(unittest.TestCase):
    def test_emergency_stop_route_updates_config_and_stops_runtime(self):
        created = []

        def fake_create_record(collection, data):
            created.append((collection, dict(data)))
            return {"id": f"cfg-{len(created)}", **data}

        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live", "action": "compute"}):
            with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=None):
                with mock.patch.object(api_app_mod.pb, "create_record", side_effect=fake_create_record):
                    with mock.patch.object(
                        api_app_mod,
                        "_fetch_runtime_status",
                        return_value={
                            "payload": {"environment": "live"},
                            "selected_upstream": "http://runtime/ibkr/status",
                            "proxy_upstream": "http://compute/ibkr/status",
                            "error": "",
                        },
                    ):
                        with mock.patch.object(
                            api_app_mod,
                            "_request_json_request",
                            return_value={"ok": True, "status_code": 200, "payload": {"ok": True, "status": "stopping"}},
                        ):
                            with mock.patch.object(api_app_mod, "_deliver_system_event_notification", return_value={"success": True, "message_id": "evt-1"}):
                                with mock.patch.object(api_app_mod, "_write_system_event_record", return_value=True):
                                    payload = api_app_mod.custom_ibkr_emergency_stop()

        self.assertTrue(payload["ok"])
        self.assertEqual("compute", payload["action"])
        self.assertEqual("ibkr_compute_enabled", payload["updated"][0]["key"])
        self.assertEqual("FALSE", created[0][1]["value"])
        self.assertEqual("stopping", payload["runtime_stop"]["status"])

    def test_emergency_stop_route_rejects_runtime_environment_mismatch(self):
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "paper", "action": "runtime"}):
            with mock.patch.object(
                api_app_mod,
                "_fetch_runtime_status",
                return_value={
                    "payload": {"environment": "live"},
                    "selected_upstream": "http://runtime/ibkr/status",
                    "proxy_upstream": "http://compute/ibkr/status",
                    "error": "",
                },
            ):
                payload, status_code = api_app_mod.custom_ibkr_emergency_stop()

        self.assertEqual(409, status_code)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["runtime_environment_mismatch"])
        self.assertEqual("paper", payload["requested_environment"])
        self.assertEqual("live", payload["actual_runtime_environment"])

    def test_recover_route_updates_config_without_runtime_call(self):
        created = []

        def fake_create_record(collection, data):
            created.append((collection, dict(data)))
            return {"id": f"cfg-{len(created)}", **data}

        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live", "action": "trading"}):
            with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=None):
                with mock.patch.object(api_app_mod.pb, "create_record", side_effect=fake_create_record):
                    with mock.patch.object(api_app_mod, "_deliver_system_event_notification", return_value={"success": True, "message_id": "evt-2"}):
                        with mock.patch.object(api_app_mod, "_write_system_event_record", return_value=True):
                            payload = api_app_mod.custom_ibkr_recover()

        self.assertTrue(payload["ok"])
        self.assertEqual("trading", payload["action"])
        self.assertEqual("ibkr_trading_enabled", payload["updated"][0]["key"])
        self.assertEqual("TRUE", created[0][1]["value"])

    def test_reauth_route_stops_then_starts_runtime(self):
        request_results = [
            {"ok": True, "status_code": 200, "payload": {"ok": True, "status": "stopping"}},
            {"ok": True, "status_code": 200, "payload": {"ok": True, "status": "starting"}},
        ]

        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live"}):
            with mock.patch.object(
                api_app_mod,
                "_fetch_runtime_status",
                return_value={
                    "payload": {"environment": "live"},
                    "selected_upstream": "http://runtime/ibkr/status",
                    "proxy_upstream": "http://compute/ibkr/status",
                    "error": "",
                },
            ):
                with mock.patch.object(api_app_mod, "_request_json_request", side_effect=request_results) as request_mock:
                    payload = api_app_mod.custom_ibkr_reauth()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["reauth"])
        self.assertEqual("starting", payload["status"])
        self.assertEqual("stopping", payload["stop_before_start"]["status"])
        self.assertEqual(2, request_mock.call_count)


if __name__ == "__main__":
    unittest.main()
