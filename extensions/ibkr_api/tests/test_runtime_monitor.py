import sys
import unittest
from pathlib import Path
from unittest import mock

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

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
    flask_stub.request = types.SimpleNamespace(args={}, headers={}, method="GET", get_json=lambda silent=True: {})
    sys.modules["flask"] = flask_stub

from ibkr_api.system.runtime_monitor import build_system_monitor_payload


class _FakeConfig:
    def refresh(self):
        return None


class _FakePB:
    def get_records(self, *_args, **_kwargs):
        return []


def _normalize_environment(value, default="live"):
    return str(value or default).strip().lower() or default


def _successful_upstream_payload(environment="paper"):
    return {
        "ok": True,
        "status": "running",
        "environment": environment,
        "gateway": {"running": True},
        "session": {"authenticated": True},
        "websocket": {"ready": True, "connected": True},
        "summary": {},
        "positions": [],
        "orders": [],
        "live_open_orders": [],
        "counts": {},
        "errors": {},
    }


class RuntimeMonitorAccountSnapshotProbeTest(unittest.TestCase):
    def _run_account_snapshot_probe(self, request_json_request):
        globals_dict = {
            "pb": _FakePB(),
            "_normalize_environment": _normalize_environment,
            "_request_json_request": request_json_request,
            "RUNTIME_BASE_URL": "http://runtime.internal:5101",
            "_fetch_compute_monitor": lambda *_args, **_kwargs: {},
            "_as_dict": lambda value: dict(value) if isinstance(value, dict) else {},
            "_scheduler_status": lambda *_args, **_kwargs: {},
            "_build_scheduler_summary": lambda *_args, **_kwargs: {},
            "_augment_scheduler_summary": lambda summary, _items: summary,
            "_request_json": lambda *_args, **_kwargs: {},
            "_console_base_url": lambda: "http://console.internal:5104",
            "_probe_console_status": lambda *_args, **_kwargs: {},
            "_load_effective_config_map": lambda *_args, **_kwargs: {},
            "_load_recent_system_events": lambda *_args, **_kwargs: [],
            "_enrich_monitor_payload_with_pocketbase_disk": lambda payload: payload,
            "_derive_monitor_service_map": lambda *_args, **_kwargs: {},
            "_merge_service_topology": lambda *_args, **_kwargs: {"services": {}},
            "BACKTEST_BASE_URL": "http://backtest.internal:5105",
        }

        def support(environment, **kwargs):
            return kwargs["account_snapshot_probe"](environment)

        builder = build_system_monitor_payload(
            globals_dict=globals_dict,
            config=_FakeConfig(),
            build_cron_payload=lambda *_args, **_kwargs: [],
            build_service_topology=lambda: {"services": {}},
            pb_base_url="http://pb.internal:8090",
            monitor_config_keys=(),
            support=support,
        )
        return builder("paper")

    def test_account_snapshot_probe_retries_after_timeout_then_succeeds(self):
        calls = []
        responses = [
            {
                "ok": False,
                "status_code": 0,
                "payload": {},
                "error": "HTTPConnectionPool: Read timed out.",
                "target_url": "http://runtime.internal:5101/ibkr/account",
                "elapsed_ms": 4001.0,
                "timeout_s": 4.0,
            },
            {
                "ok": True,
                "status_code": 200,
                "payload": _successful_upstream_payload(),
                "error": "",
                "target_url": "http://runtime.internal:5101/ibkr/account",
                "elapsed_ms": 75.0,
                "timeout_s": 4.0,
            },
        ]

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            calls.append(
                {
                    "method": method,
                    "base_url": base_url,
                    "path": path,
                    "params": list(params or []),
                    "timeout": timeout,
                }
            )
            return responses.pop(0)

        with mock.patch.dict(
            "os.environ",
            {
                "IBKR_ACCOUNT_SNAPSHOT_MONITOR_TIMEOUT_SEC": "4",
                "IBKR_ACCOUNT_SNAPSHOT_MONITOR_ATTEMPTS": "2",
                "IBKR_ACCOUNT_SNAPSHOT_MONITOR_RETRY_INTERVAL_SEC": "2",
            },
            clear=False,
        ), mock.patch("ibkr_api.system.runtime_monitor.time.sleep") as sleep_mock:
            payload = self._run_account_snapshot_probe(request_json_request)

        self.assertTrue(payload["ok"])
        self.assertEqual(200, payload["status_code"])
        self.assertEqual("", payload["error"])
        self.assertEqual(2, len(calls))
        self.assertEqual(["/ibkr/status", "/ibkr/status"], [call["path"] for call in calls])
        self.assertEqual([("broker_mode", "paper"), ("environment", "paper")], calls[0]["params"])
        self.assertEqual([4.0, 4.0], [call["timeout"] for call in calls])
        sleep_mock.assert_called_once_with(2.0)
        self.assertEqual(2, len(payload["attempts"]))
        self.assertFalse(payload["attempts"][0]["ok"])
        self.assertIn("Read timed out", payload["attempts"][0]["error"])
        self.assertTrue(payload["attempts"][1]["ok"])

    def test_account_snapshot_probe_returns_last_failure_after_retries(self):
        calls = []
        responses = [
            {
                "ok": False,
                "status_code": 0,
                "payload": {},
                "error": "first timeout",
                "target_url": "http://runtime.internal:5101/ibkr/account",
                "elapsed_ms": 4000.0,
                "timeout_s": 4.0,
            },
            {
                "ok": False,
                "status_code": 0,
                "payload": {},
                "error": "second timeout",
                "target_url": "http://runtime.internal:5101/ibkr/account",
                "elapsed_ms": 4000.0,
                "timeout_s": 4.0,
            },
        ]

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            calls.append({"timeout": timeout})
            return responses.pop(0)

        with mock.patch.dict(
            "os.environ",
            {
                "IBKR_ACCOUNT_SNAPSHOT_MONITOR_TIMEOUT_SEC": "4",
                "IBKR_ACCOUNT_SNAPSHOT_MONITOR_ATTEMPTS": "2",
                "IBKR_ACCOUNT_SNAPSHOT_MONITOR_RETRY_INTERVAL_SEC": "0",
            },
            clear=False,
        ), mock.patch("ibkr_api.system.runtime_monitor.time.sleep") as sleep_mock:
            payload = self._run_account_snapshot_probe(request_json_request)

        self.assertFalse(payload["ok"])
        self.assertEqual(502, payload["status_code"])
        self.assertEqual("second timeout", payload["error"])
        self.assertEqual(2, len(calls))
        sleep_mock.assert_not_called()
        self.assertEqual(2, len(payload["attempts"]))
        self.assertEqual("first timeout", payload["attempts"][0]["error"])
        self.assertEqual("second timeout", payload["attempts"][1]["error"])

    def test_account_snapshot_probe_does_not_retry_success(self):
        calls = []

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            calls.append({"timeout": timeout})
            return {
                "ok": True,
                "status_code": 200,
                "payload": _successful_upstream_payload(),
                "error": "",
                "target_url": "http://runtime.internal:5101/ibkr/account",
                "elapsed_ms": 50.0,
                "timeout_s": timeout,
            }

        with mock.patch.dict(
            "os.environ",
            {
                "IBKR_ACCOUNT_SNAPSHOT_MONITOR_TIMEOUT_SEC": "4",
                "IBKR_ACCOUNT_SNAPSHOT_MONITOR_ATTEMPTS": "3",
                "IBKR_ACCOUNT_SNAPSHOT_MONITOR_RETRY_INTERVAL_SEC": "2",
            },
            clear=False,
        ), mock.patch("ibkr_api.system.runtime_monitor.time.sleep") as sleep_mock:
            payload = self._run_account_snapshot_probe(request_json_request)

        self.assertTrue(payload["ok"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(payload["attempts"]))
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
