import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, name, *args, **kwargs):
            self.name = name

        def route(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

        def add_url_rule(self, *args, **kwargs):
            return None

    flask_stub.Flask = _FakeFlask
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = SimpleNamespace(
        args={},
        headers={},
        method="GET",
        get_data=lambda cache=True: b"",
        get_json=lambda silent=True: {},
        values={},
    )
    sys.modules["flask"] = flask_stub

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.compute import request as compute_request
from ibkr_compute.api.compute import runtime_ops
from ibkr_compute.core.time_utils import ET


class _FakeResponse:
    def __init__(self, payload):
        self._payload = dict(payload)

    def get_json(self):
        return dict(self._payload)


class _FakeConfig:
    def __init__(self, scan_time="08:20", values=None, defaults=None):
        self.scan_time = scan_time
        self.values = dict(values or {})
        if defaults is not None:
            self.DEFAULTS = dict(defaults)

    def refresh(self):
        return None

    def get_for_environment(self, key, environment, default=None):
        value_key = (key, str(environment or "").strip().lower())
        if value_key in self.values:
            return self.values[value_key]
        if key in self.values:
            return self.values[key]
        if key == "ibkr_daily_scan_time_et":
            return self.scan_time
        if key == "ibkr_scan_schedule":
            return "08:20-09:20"
        return default


class _FakePB:
    def __init__(self):
        self.states = []

    def upsert_state(self, state_key, environment, data, date="global"):
        payload = {
            "state_key": state_key,
            "environment": environment,
            "data": dict(data or {}),
            "date": date,
        }
        self.states.append(payload)
        return payload


def _fake_app(scan_time="08:20", market_date="2026-04-28", config_values=None, config_defaults=None):
    return SimpleNamespace(
        SUPPORTED_COMPUTE_ENVIRONMENTS=["live", "paper", "backtest"],
        DEFAULT_COMPUTE_ENVIRONMENTS=["live"],
        cfg=_FakeConfig(scan_time=scan_time, values=config_values, defaults=config_defaults),
        engines={},
        pb=object(),
        last_scan_time=0,
        current_market_date=lambda: market_date,
        is_environment_compute_enabled=lambda environment: str(environment or "").strip().lower() in {"live", "paper"},
    )


class ScanRuntimeGateTest(unittest.TestCase):
    def test_scan_window_state_uses_configured_et_time(self):
        fake_app = _fake_app(scan_time="08:20")

        before = runtime_ops._scan_window_state(
            fake_app,
            "live",
            datetime(2026, 4, 28, 8, 19, tzinfo=ET),
        )
        at_open = runtime_ops._scan_window_state(
            fake_app,
            "live",
            datetime(2026, 4, 28, 8, 20, tzinfo=ET),
        )
        after_close = runtime_ops._scan_window_state(
            fake_app,
            "live",
            datetime(2026, 4, 28, 9, 21, tzinfo=ET),
        )

        self.assertFalse(before["open"])
        self.assertEqual(before["scan_time_et"], "08:20")
        self.assertEqual(before["window_end_et"], "09:20")
        self.assertTrue(at_open["open"])
        self.assertFalse(after_close["open"])

    def test_build_scan_response_skips_before_window_without_force(self):
        fake_app = _fake_app()
        blocked_window = {
            "environment": "live",
            "open": False,
            "scan_time_et": "08:20",
            "configured_scan_time_et": "08:20",
            "current_time_et": "05:55",
            "current_datetime_et": "2026-04-28T05:55:00-04:00",
        }

        with mock.patch.object(runtime_ops, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_request, "_api_app", return_value=fake_app), \
                mock.patch.object(runtime_ops, "_scan_window_state", return_value=blocked_window), \
                mock.patch.object(runtime_ops, "DailyScanner") as scanner_cls, \
                mock.patch.object(runtime_ops, "jsonify", side_effect=lambda payload: _FakeResponse(payload)):
            response = runtime_ops.build_scan_response({"environment": "live"})

        scanner_cls.assert_not_called()
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "scan_window_not_open")
        self.assertTrue(payload["requires_force"])
        self.assertEqual(payload["scan_windows"][0]["current_time_et"], "05:55")

    def test_build_scan_response_force_bypasses_time_gate(self):
        fake_app = _fake_app()
        fake_app.pb = _FakePB()
        blocked_window = {
            "environment": "live",
            "open": False,
            "scan_time_et": "08:20",
            "configured_scan_time_et": "08:20",
            "current_time_et": "05:55",
            "current_datetime_et": "2026-04-28T05:55:00-04:00",
        }
        scanner = mock.Mock()
        scanner.run_scan.return_value = {"scanned": 2, "eligible": 1, "active": 1, "candidates": 0, "errors": 0}

        with mock.patch.object(runtime_ops, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_request, "_api_app", return_value=fake_app), \
                mock.patch.object(runtime_ops, "_scan_window_state", return_value=blocked_window), \
                mock.patch.object(runtime_ops, "DailyScanner", return_value=scanner), \
                mock.patch.object(runtime_ops, "jsonify", side_effect=lambda payload: _FakeResponse(payload)), \
                mock.patch.object(runtime_ops.time, "time", return_value=123.0):
            response = runtime_ops.build_scan_response({"environment": "live", "force": True})

        scanner.run_scan.assert_called_once_with("2026-04-28", environments=["live"], mode="seed")
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["force"])
        self.assertEqual(payload["active"], 1)
        self.assertEqual(fake_app.last_scan_time, 123.0)
        legacy_states = [
            item for item in fake_app.pb.states
            if item["state_key"] == "ibkr_daily_scan_state"
        ]
        self.assertEqual(len(legacy_states), 1)
        self.assertEqual(legacy_states[0]["date"], "global")
        self.assertEqual(legacy_states[0]["data"]["status"], "completed")
        self.assertEqual(legacy_states[0]["data"]["market_date"], "2026-04-28")
        self.assertEqual(legacy_states[0]["data"]["result"]["active"], 1)

    def test_build_scan_response_skips_tv_primary_slim_without_scanning(self):
        fake_app = _fake_app(
            config_values={
                ("ibkr_signal_source", "live"): "tradingview",
                ("ibkr_tv_primary_runtime_slim_enabled", "live"): "TRUE",
            }
        )
        fake_app.pb = _FakePB()
        open_window = {
            "environment": "live",
            "open": True,
            "scan_time_et": "08:20",
            "configured_scan_time_et": "08:20",
            "current_time_et": "08:25",
            "current_datetime_et": "2026-04-28T08:25:00-04:00",
        }

        with mock.patch.object(runtime_ops, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_request, "_api_app", return_value=fake_app), \
                mock.patch.object(runtime_ops, "_scan_window_state", return_value=open_window), \
                mock.patch.object(runtime_ops, "DailyScanner") as scanner_cls, \
                mock.patch.object(runtime_ops, "jsonify", side_effect=lambda payload: _FakeResponse(payload)):
            response = runtime_ops.build_scan_response({"environment": "live"})

        scanner_cls.assert_not_called()
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "tv_primary_slim_mode")
        legacy_states = [item for item in fake_app.pb.states if item["state_key"] == "ibkr_daily_scan_state"]
        self.assertEqual(legacy_states[0]["data"]["status"], "skipped")

    def test_build_scan_response_skips_closed_market_without_scanning(self):
        fake_app = _fake_app(market_date="2026-05-31")
        fake_app.pb = _FakePB()
        open_window = {
            "environment": "live",
            "open": True,
            "scan_time_et": "08:20",
            "configured_scan_time_et": "08:20",
            "current_time_et": "08:25",
            "current_datetime_et": "2026-05-31T08:25:00-04:00",
        }

        with mock.patch.object(runtime_ops, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_request, "_api_app", return_value=fake_app), \
                mock.patch.object(runtime_ops, "_scan_window_state", return_value=open_window), \
                mock.patch.object(runtime_ops, "DailyScanner") as scanner_cls, \
                mock.patch.object(runtime_ops, "jsonify", side_effect=lambda payload: _FakeResponse(payload)):
            response = runtime_ops.build_scan_response({"environment": "live"})

        scanner_cls.assert_not_called()
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "market_closed")
        self.assertEqual(payload["closed_reason"], "weekend")


if __name__ == "__main__":
    unittest.main()
