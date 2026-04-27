import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

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
    flask_stub.jsonify = lambda payload: payload
    flask_stub.Response = object
    flask_stub.redirect = lambda url, code=302: {"redirect": url, "code": code}
    flask_stub.request = SimpleNamespace(
        args={},
        headers={},
        method="GET",
        values={},
        get_data=lambda cache=True: b"",
        get_json=lambda silent=True: {},
    )
    sys.modules["flask"] = flask_stub

from ibkr_compute.api.monitor.runtime.compute import _build_compute_summary
from ibkr_compute.api.ops.common import _snapshot_engine_items
from ibkr_compute.api.ops.status_views import build_status_response


class _GuardedLock:
    def __init__(self):
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.entered = False
        return False


class _ExplodingEngines(dict):
    def __init__(self, lock, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = lock

    def items(self):
        if not self._lock.entered:
            raise RuntimeError("dictionary changed size during iteration")
        return list(super().items())

    def values(self):
        if not self._lock.entered:
            raise RuntimeError("dictionary changed size during iteration")
        return list(super().values())


class _FakeEngine:
    def __init__(self, ready, close, last_bar_time_ms, bar_count):
        self._ready = ready
        self._close = close
        self.last_bar_time_ms = last_bar_time_ms
        self.bar_count = bar_count

    def is_ready(self):
        return self._ready

    def get_snapshot(self):
        return {"close": self._close}


def _build_fake_app():
    lock = _GuardedLock()
    engines = _ExplodingEngines(
        lock,
        {
            ("live", "AAPL", "5m"): _FakeEngine(True, 210.5, 1776791100000, 260),
            ("live", "MSFT", "15m"): _FakeEngine(False, 401.2, 1776789900000, 180),
        },
    )
    app = SimpleNamespace(
        compute_lock=lock,
        engines=engines,
        cfg=SimpleNamespace(compute_enabled=True),
        is_environment_compute_enabled=lambda environment: environment == "live",
        SUPPORTED_COMPUTE_ENVIRONMENTS=["live", "paper"],
        DEFAULT_COMPUTE_ENVIRONMENTS=["live"],
        persistent_cursor_envs_loaded={"live"},
        last_processed_ms={("live", "AAPL", "5m"): 1776791100000},
        compute_count=7,
        error_count=0,
        last_compute_time=0,
        last_scan_time=0,
        _start_time=0,
        backtest_service=SimpleNamespace(status=lambda: {"status": "idle"}),
        history_rebuild_manager=SimpleNamespace(status=lambda environment: {"status": "idle", "environment": environment}),
    )
    return app


class OpsStatusViewsTest(unittest.TestCase):
    def test_snapshot_engine_items_uses_compute_lock(self):
        fake_app = _build_fake_app()

        engine_items = _snapshot_engine_items(fake_app)

        self.assertEqual(len(engine_items), 2)
        self.assertFalse(fake_app.compute_lock.entered)

    def test_build_status_response_defaults_to_lite_snapshot(self):
        fake_app = _build_fake_app()

        with mock.patch("ibkr_compute.api.ops.status_views.get_app_module", return_value=fake_app):
            with mock.patch("ibkr_compute.api.ops.status_views.get_requested_environment", return_value="live"):
                with mock.patch("ibkr_compute.api.ops.status_views.get_service_profile", return_value="compute"):
                    with mock.patch("ibkr_compute.api.ops.status_views.get_runtime_mode", return_value="remote"):
                        with mock.patch("ibkr_compute.api.ops.status_views.get_compute_startup_preload_state", return_value={"status": "idle"}):
                            with mock.patch("ibkr_compute.api.ops.status_views._build_topology_payload", return_value={"services": {}}):
                                with mock.patch("ibkr_compute.api.ops.status_views.request", SimpleNamespace(args={})):
                                    with mock.patch("ibkr_compute.api.ops.status_views.jsonify", side_effect=lambda payload: payload):
                                        payload = build_status_response()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total_engines"], 2)
        self.assertEqual(payload["ready_engines"], 1)
        self.assertFalse(payload["engines_included"])
        self.assertEqual(payload["status_mode"], "lite")
        self.assertEqual(payload["engines"], {})

    def test_build_status_response_includes_engines_when_full_requested(self):
        fake_app = _build_fake_app()

        with mock.patch("ibkr_compute.api.ops.status_views.get_app_module", return_value=fake_app):
            with mock.patch("ibkr_compute.api.ops.status_views.get_requested_environment", return_value="live"):
                with mock.patch("ibkr_compute.api.ops.status_views.get_service_profile", return_value="compute"):
                    with mock.patch("ibkr_compute.api.ops.status_views.get_runtime_mode", return_value="remote"):
                        with mock.patch("ibkr_compute.api.ops.status_views.get_compute_startup_preload_state", return_value={"status": "idle"}):
                            with mock.patch("ibkr_compute.api.ops.status_views._build_topology_payload", return_value={"services": {}}):
                                with mock.patch("ibkr_compute.api.ops.status_views.request", SimpleNamespace(args={"full": "1"})):
                                    with mock.patch("ibkr_compute.api.ops.status_views.jsonify", side_effect=lambda payload: payload):
                                        payload = build_status_response()

        self.assertTrue(payload["engines_included"])
        self.assertEqual(payload["status_mode"], "full")
        self.assertEqual(payload["engines"]["live/AAPL/5m"]["last_close"], 210.5)
        self.assertEqual(payload["engines"]["live/MSFT/15m"]["bar_count"], 180)

    def test_monitor_compute_summary_snapshots_engines_before_counting(self):
        fake_app = _build_fake_app()

        with mock.patch("ibkr_compute.api.monitor.runtime.compute._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.monitor.runtime.compute.time.time", return_value=50.0):
                summary = _build_compute_summary()

        self.assertEqual(summary["total_engines"], 2)
        self.assertEqual(summary["ready_engines"], 1)
        self.assertEqual(summary["uptime_s"], 50.0)


if __name__ == "__main__":
    unittest.main()
