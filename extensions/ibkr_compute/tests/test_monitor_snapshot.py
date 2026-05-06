import logging
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
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

        def add_url_rule(self, *args, **kwargs):
            return None

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
    def __init__(self, subscription_limit=60):
        self._subscription_lock = threading.RLock()
        self._active_subscription_map = {
            "AAPL": 265598,
            "MSFT": 272093,
        }
        self.config = types.SimpleNamespace(
            refresh=lambda: None,
            get_int_for_environment=lambda key, environment, default=0: (
                int(subscription_limit) if key == "ibkr_target_subscription_limit" else int(default)
            ),
        )

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
    def test_build_cpu_usage_snapshot_percent(self):
        payload = server._build_cpu_usage_snapshot(
            {"total": 200, "idle": 80, "sampled_at": 10.0},
            {"total": 260, "idle": 92, "sampled_at": 11.5},
        )

        self.assertEqual(payload["used_pct"], 80.0)
        self.assertEqual(payload["idle_pct"], 20.0)
        self.assertEqual(payload["sample_span_s"], 1.5)
        self.assertEqual(payload["source"], "/proc/stat")

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
        fake_service = FakeService(subscription_limit=10)
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
        self.assertNotIn("history_throttle_detected", flag_codes)
        self.assertNotIn("history_request_retry_or_error", flag_codes)

    def test_build_monitor_snapshot_keeps_recent_warmup_symbol_out_of_stale_list(self):
        fake_service = FakeService(subscription_limit=10)
        fake_service._active_subscription_map["VIX"] = 13455763
        status_payload = fake_service.status()
        status_payload["canonical_5m"] = {
            "last_completed_bucket_ms": 1773700500000,
        }
        status_payload["warmup"]["monitor_symbols"] = ["MSFT", "VIX"]
        status_payload["warmup"]["symbol_status"] = [
            {
                "symbol": "VIX",
                "role": "monitor",
                "ready": False,
                "bar_count": 271,
                "last_bar_time_ms": 1773700500000,
                "integrity_ready": False,
                "integrity_reason": "today_regular_incomplete=1",
            }
        ]
        status_payload["market_universe"]["active_subscription_count"] = 3
        host_snapshot = {
            "hostname": "compute-1",
            "platform": "Linux-6.8.0",
            "cpu_count": 8,
            "loadavg": {"1": 1.4, "5": 1.1, "15": 0.9, "per_cpu_1": 0.18},
            "memory": {
                "total_bytes": 1024,
                "available_bytes": 256,
                "used_bytes": 768,
                "used_pct": 75.0,
                "source": "/proc/meminfo",
            },
            "disk": {
                "path": "/",
                "total_bytes": 2048,
                "free_bytes": 1024,
                "used_bytes": 1024,
                "used_pct": 50.0,
            },
            "process": {
                "pid": 123,
                "uptime_s": 42.0,
                "rss_bytes": 4096,
                "threads": 6,
                "fd_count": 14,
            },
        }

        with mock.patch.object(fake_service, "status", return_value=status_payload):
            with mock.patch.object(server, "_collect_host_snapshot", return_value=host_snapshot):
                with mock.patch.object(server, "get_ibkr_runtime_control", return_value={"environment": "live", "desired_running": True}):
                    payload = server._build_ibkr_monitor_snapshot(fake_service)

        self.assertEqual(payload["samples"]["stale_symbols"], ["MSFT"])
        subscriptions = {item["symbol"]: item for item in payload["samples"]["active_subscriptions"]}
        self.assertTrue(subscriptions["VIX"]["visible"])
        self.assertFalse(subscriptions["VIX"]["stale"])
        self.assertFalse(subscriptions["VIX"]["warmup_ready"])
        self.assertIn("warmup", subscriptions["VIX"]["visibility_sources"])

    def test_monitor_route_returns_payload(self):
        fake_service = FakeService(subscription_limit=60)
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
                with mock.patch.object(server, "_collect_host_snapshot", return_value=host_snapshot):
                    with mock.patch.object(server, "get_ibkr_runtime_control", return_value={"environment": "live", "desired_running": True}):
                        payload = server.ibkr_monitor()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["api_utilization"]["subscription_limit"], 60)
        restore_mock.assert_called_once()

    def test_subscription_utilization_does_not_warn_before_limit(self):
        flags = server._build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            {
                "subscription_limit": 70,
                "active_subscription_count": 59,
                "utilization_pct": 84.29,
                "pending_subscription_count": 0,
            },
            {},
            {},
        )

        flag_codes = {item["code"] for item in flags}
        self.assertNotIn("subscription_utilization_high", flag_codes)
        self.assertNotIn("subscription_utilization_critical", flag_codes)

    def test_history_cumulative_throttle_is_display_only(self):
        flags = server._build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            {
                "subscription_limit": 70,
                "active_subscription_count": 2,
                "utilization_pct": 2.86,
                "pending_subscription_count": 0,
                "retry_count": 3,
                "throttle_count": 123,
                "last_trace_retry_count": 0,
                "last_trace_throttle_count": 0,
                "last_trace_error": "",
            },
            {},
            {},
        )

        flag_codes = {item["code"] for item in flags}
        self.assertNotIn("history_throttle_detected", flag_codes)
        self.assertNotIn("history_request_retry_or_error", flag_codes)

    def test_recent_history_retry_or_error_warns(self):
        flags = server._build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            {
                "subscription_limit": 70,
                "active_subscription_count": 2,
                "utilization_pct": 2.86,
                "pending_subscription_count": 0,
                "retry_count": 3,
                "throttle_count": 123,
                "last_trace_retry_count": 1,
                "last_trace_throttle_count": 2,
                "last_trace_error": "history_fetch_failed",
            },
            {},
            {},
        )

        flag_codes = {item["code"] for item in flags}
        self.assertIn("history_request_retry_or_error", flag_codes)
        self.assertNotIn("history_throttle_detected", flag_codes)

    def test_no_active_targets_warns_when_trade_watchlist_has_no_active_target(self):
        flags = server._build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
                "market_universe": {
                    "watchlist_trade_count": 2,
                    "active_target_count": 0,
                    "no_active_targets": True,
                    "inactive_trade_symbols_total": 2,
                    "inactive_trade_symbols_sample": ["AAPL", "MSFT"],
                },
            },
            {
                "subscription_limit": 70,
                "active_subscription_count": 0,
                "utilization_pct": 0.0,
                "pending_subscription_count": 0,
            },
            {},
            {},
        )

        warning = next(item for item in flags if item["code"] == "no_active_targets")
        self.assertEqual(warning["severity"], "warning")
        self.assertIn("AAPL", warning["detail"])

    def test_subscription_utilization_warns_at_limit(self):
        flags = server._build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            {
                "subscription_limit": 70,
                "active_subscription_count": 70,
                "utilization_pct": 100.0,
                "pending_subscription_count": 0,
            },
            {},
            {},
        )

        warning = next(item for item in flags if item["code"] == "subscription_utilization_high")
        self.assertEqual(warning["severity"], "warning")
        self.assertIn("70/70", warning["detail"])

    def test_subscription_utilization_critical_after_limit(self):
        flags = server._build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            {
                "subscription_limit": 70,
                "active_subscription_count": 71,
                "utilization_pct": 101.43,
                "pending_subscription_count": 0,
            },
            {},
            {},
        )

        critical = next(item for item in flags if item["code"] == "subscription_utilization_critical")
        self.assertEqual(critical["severity"], "error")
        self.assertIn("71/70", critical["detail"])

    def test_monitor_route_returns_offline_snapshot_without_service(self):
        host_snapshot = {
            "hostname": "compute-1",
            "platform": "Linux",
            "cpu_count": 4,
            "cpu": {
                "used_pct": 22.5,
                "idle_pct": 77.5,
                "sample_span_s": 1.0,
                "source": "/proc/stat",
            },
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
        with mock.patch.object(server, "get_ibkr_service", return_value=None):
            with mock.patch.object(server, "_collect_host_snapshot", return_value=host_snapshot):
                with mock.patch.object(server, "get_ibkr_runtime_control", return_value={"environment": "live", "desired_running": True}):
                    with mock.patch.object(server.cfg, "refresh", return_value=None):
                        with mock.patch.object(server.cfg, "get_int_for_environment", return_value=70):
                            payload = server.ibkr_monitor()

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["environment"], "live")
        self.assertEqual(payload["api_utilization"]["subscription_limit"], 70)
        self.assertEqual(payload["host"]["cpu"]["used_pct"], 22.5)
        flag_codes = {item["code"] for item in payload["flags"]}
        self.assertIn("gateway_offline", flag_codes)
        self.assertIn("websocket_not_ready", flag_codes)


if __name__ == "__main__":
    unittest.main()
