import logging
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("PB_RETRY_ATTEMPTS", "1")
os.environ.setdefault("PB_RETRY_BACKOFF_SECONDS", "0.01")

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def route(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

    def fake_jsonify(*args, **kwargs):
        if len(args) == 1 and not kwargs:
            return args[0]
        if args and kwargs:
            return {"args": args, **kwargs}
        if args:
            return {"args": args}
        return kwargs

    fake_flask.Flask = FakeFlask
    fake_flask.Response = dict
    fake_flask.jsonify = fake_jsonify
    fake_flask.redirect = lambda url, code=302: {"redirect": url, "code": code}
    fake_flask.request = types.SimpleNamespace(get_json=lambda silent=True: {}, args={}, values={})
    sys.modules["flask"] = fake_flask

logging.disable(logging.CRITICAL)
from ibkr_compute.api import server
logging.disable(logging.NOTSET)


class FakeService:
    def __init__(self):
        self._subscription_lock = threading.RLock()
        self._active_subscription_map = {
            "AAPL": 265598,
            "MSFT": 272093,
        }

    def status(self) -> dict:
        return {
            "environment": "live",
            "gateway": {"running": True, "reachable": True},
            "session": {"authenticated": True},
            "websocket": {
                "connected": True,
                "ready": True,
                "subscribed_conids": [265598],
                "pending_conids": [272093],
                "subscribed_count": 1,
                "pending_count": 1,
                "message_count": 120,
                "order_update_count": 7,
                "last_message": "10:30:05",
                "last_message_age_s": 12.0,
                "last_tic": "10:30:03",
                "last_tic_age_s": 14.0,
            },
            "bar_aggregator": {
                "active_bars": {
                    "AAPL": {
                        "last_update_age_s": 18.0,
                        "tick_count": 9,
                        "volume_updates": 3,
                        "interval_start": "10:25",
                    }
                }
            },
            "data_backfill": {
                "request_count": 24,
                "retry_count": 3,
                "throttle_count": 1,
                "request_spacing_s": 0.2,
                "max_concurrency": 4,
                "total_backfilled": 180,
            },
            "order_tracker": {
                "tracked_orders": 2,
                "last_poll": "2026-04-10T14:31:00+00:00",
            },
            "signal_processor": {
                "active_positions": 1,
                "trading_gate_open": True,
                "trading_gate_reason": "ready",
            },
            "realtime_compute": {
                "queue_size": 1,
                "last_run": "2026-04-10T14:30:20+00:00",
                "last_bar_close": "2026-04-10T14:30:08+00:00",
                "last_result": {"elapsed_s": 2.8},
            },
            "warmup": {
                "phase": "ready",
                "monitor_symbols": ["MSFT"],
                "pending_symbols": ["NVDA"],
            },
            "market_universe": {
                "active_subscription_count": 2,
                "active_target_count": 1,
                "active_trade_symbols": ["AAPL"],
                "last_active_repair_reasons": {"MSFT": "stale"},
            },
        }


class MonitorSnapshotTest(unittest.TestCase):
    def test_parse_meminfo_text_to_bytes(self):
        parsed = server._parse_meminfo_text(
            "MemTotal:       1024 kB\n"
            "MemAvailable:    256 kB\n"
            "Buffers:          12 kB\n"
        )

        self.assertEqual(parsed["MemTotal"], 1024 * 1024)
        self.assertEqual(parsed["MemAvailable"], 256 * 1024)
        self.assertEqual(parsed["Buffers"], 12 * 1024)

    def test_build_monitor_snapshot_includes_roles_and_utilization(self):
        fake_service = FakeService()
        host_snapshot = {
            "hostname": "compute-1",
            "platform": "Linux-6.8.0",
            "cpu_count": 8,
            "loadavg": {"1": 8.4, "5": 6.1, "15": 5.2, "per_cpu_1": 1.05},
            "memory": {
                "total_bytes": 1024,
                "available_bytes": 128,
                "used_bytes": 896,
                "used_pct": 87.5,
                "source": "/proc/meminfo",
            },
            "disk": {
                "path": "/",
                "total_bytes": 2048,
                "free_bytes": 512,
                "used_bytes": 1536,
                "used_pct": 75.0,
            },
            "process": {
                "pid": 123,
                "uptime_s": 42.0,
                "rss_bytes": 4096,
                "threads": 6,
                "fd_count": 14,
            },
        }

        with mock.patch.object(server.cfg, "get_for_environment", return_value="10"):
            with mock.patch.object(server, "_collect_host_snapshot", return_value=host_snapshot):
                with mock.patch.object(server, "get_ibkr_runtime_control", return_value={"environment": "live", "desired_running": True}):
                    payload = server._build_ibkr_monitor_snapshot(fake_service)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "live")
        self.assertEqual(payload["api_utilization"]["active_subscription_count"], 2)
        self.assertEqual(payload["api_utilization"]["subscription_limit"], 10)
        self.assertEqual(payload["api_utilization"]["utilization_pct"], 20.0)
        self.assertEqual(payload["samples"]["stale_symbols"], ["MSFT"])

        subscriptions = {item["symbol"]: item for item in payload["samples"]["active_subscriptions"]}
        self.assertEqual(subscriptions["AAPL"]["role"], "trade")
        self.assertEqual(subscriptions["MSFT"]["role"], "market_monitor")

        flag_codes = {item["code"] for item in payload["flags"]}
        self.assertIn("host_memory_high", flag_codes)
        self.assertIn("host_load_high", flag_codes)
        self.assertIn("pending_subscriptions", flag_codes)
        self.assertIn("history_throttle_detected", flag_codes)

    def test_monitor_route_returns_payload(self):
        fake_service = FakeService()
        host_snapshot = {
            "hostname": "compute-1",
            "platform": "Linux",
            "cpu_count": 4,
            "loadavg": {"1": 0.4, "5": 0.3, "15": 0.2, "per_cpu_1": 0.1},
            "memory": {
                "total_bytes": 1024,
                "available_bytes": 512,
                "used_bytes": 512,
                "used_pct": 50.0,
                "source": "/proc/meminfo",
            },
            "disk": {
                "path": "/",
                "total_bytes": 4096,
                "free_bytes": 2048,
                "used_bytes": 2048,
                "used_pct": 50.0,
            },
            "process": {
                "pid": 321,
                "uptime_s": 10.0,
                "rss_bytes": 2048,
                "threads": 4,
                "fd_count": 9,
            },
        }

        with mock.patch.object(server, "get_ibkr_service", return_value=fake_service):
            with mock.patch.object(server, "_maybe_restore_ibkr_service") as restore_mock:
                with mock.patch.object(server.cfg, "get_for_environment", return_value="60"):
                    with mock.patch.object(server, "_collect_host_snapshot", return_value=host_snapshot):
                        with mock.patch.object(server, "get_ibkr_runtime_control", return_value={"environment": "live", "desired_running": True}):
                            payload = server.ibkr_monitor()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["api_utilization"]["subscription_limit"], 60)
        restore_mock.assert_called_once()

    def test_monitor_route_returns_503_without_service(self):
        with mock.patch.object(server, "get_ibkr_service", return_value=None):
            payload, status_code = server.ibkr_monitor()

        self.assertEqual(status_code, 503)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
