import sys
import threading
import types
import unittest
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
    flask_stub.redirect = lambda url, code=302: {"redirect": url, "code": code}
    flask_stub.request = SimpleNamespace(
        get_json=lambda silent=True: {},
        get_data=lambda cache=True: b"",
        args=SimpleNamespace(
            get=lambda name, default=None: default,
            getlist=lambda name: [],
        ),
        headers={},
        method="GET",
        values={},
    )
    sys.modules["flask"] = flask_stub

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.compute import pipeline_views


class _FakeEngine:
    def __init__(self):
        self.bar_count = 0
        self.last_bar_time_ms = 0
        self._snapshot = {}

    def reset(self):
        self.bar_count = 0
        self.last_bar_time_ms = 0
        self._snapshot = {}

    def update(self, row):
        bar_time_ms = int(row.get("bar_time_ms", 0) or 0)
        if bar_time_ms <= self.last_bar_time_ms:
            return dict(self._snapshot)
        self.bar_count += 1
        self.last_bar_time_ms = bar_time_ms
        self._snapshot = {"bar_time_ms": bar_time_ms, "close": row.get("close", 0)}
        return dict(self._snapshot)

    def is_ready(self):
        return True


class _FakeResponse:
    def __init__(self, payload):
        self._payload = dict(payload)

    def get_json(self):
        return dict(self._payload)


class ComputePipelineGuardTest(unittest.TestCase):
    def test_compute_lock_busy_returns_retryable_error(self):
        lock = threading.Lock()
        lock.acquire()
        fake_app = SimpleNamespace(compute_lock=lock)

        try:
            with mock.patch("ibkr_compute.api.compute.pipeline_views._api_app", return_value=fake_app):
                with mock.patch.object(pipeline_views, "COMPUTE_LOCK_TIMEOUT_SECONDS", 0.01):
                    with mock.patch(
                        "ibkr_compute.api.compute.pipeline_views.jsonify",
                        side_effect=lambda payload: _FakeResponse(payload),
                    ):
                        response, status = pipeline_views.build_compute_response({})
        finally:
            lock.release()

        self.assertEqual(status, 503)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "compute_busy")
        self.assertTrue(payload["retryable"])

    def test_unbounded_daily_rollup_requires_symbols(self):
        fake_app = SimpleNamespace(
            cfg=SimpleNamespace(refresh=lambda: None),
            compute_lock=threading.Lock(),
        )
        plan = {
            "payload": {"source": "manual_daily_rollup_repair", "rollup_intervals": ["1d"]},
            "enabled_environments": ["live"],
            "requested_environments": ["live"],
            "requested_symbols": [],
            "rollup_intervals": ["1d"],
        }

        with mock.patch("ibkr_compute.api.compute.pipeline_views._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.pipeline_views.build_compute_execution_plan", return_value=plan):
                with mock.patch(
                    "ibkr_compute.api.compute.pipeline_views.jsonify",
                    side_effect=lambda payload: _FakeResponse(payload),
                ):
                    response, status = pipeline_views.build_compute_response(plan["payload"])

        self.assertEqual(status, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "unbounded_daily_rollup_requires_symbols")
        self.assertEqual(payload["rollup_intervals"], ["1d"])


class ComputePipelineCursorCommitTest(unittest.TestCase):
    def test_indicator_flush_failure_keeps_cursor_behind_and_retry_forces_rebuild(self):
        engine = _FakeEngine()
        key = ("live", "AAPL", "5m")
        bootstrap_calls = []
        persist_calls = []
        flush_results = [
            {"ok": False, "written": 0, "errors": 1},
            {"ok": True, "written": 1, "errors": 0},
        ]

        def flush_indicator_batch(batch):
            self.assertEqual(len(batch), 1)
            return dict(flush_results.pop(0))

        def get_or_create_engine(environment, symbol, interval, signal_params=None):
            del environment, symbol, interval, signal_params
            return engine

        def bootstrap_engine_state(
            environment,
            symbol,
            interval,
            before_bar_time_ms,
            inclusive=True,
            force_rebuild=False,
            hydrate_signal_state=True,
        ):
            del environment, symbol, interval, before_bar_time_ms, inclusive, hydrate_signal_state
            bootstrap_calls.append({"force_rebuild": force_rebuild})
            if force_rebuild:
                engine.reset()
            return 0

        fake_app = SimpleNamespace(
            INDICATOR_BATCH_SIZE=10,
            SIGNAL_BATCH_SIZE=10,
            build_indicator_payload=lambda environment, symbol, interval, bar, current_engine, snapshot: {
                "environment": environment,
                "symbol": symbol,
                "interval": interval,
                "bar_time_ms": int(bar["bar_time_ms"]),
                "close": snapshot.get("close"),
            },
            cfg=SimpleNamespace(refresh=lambda: None),
            compute_count=0,
            compute_lock=threading.Lock(),
            engines={key: engine},
            ensure_higher_timeframe_bars=lambda *args, **kwargs: {},
            error_count=0,
            fetch_interval_bars=lambda environment, interval, symbols=None, full_scan=False: [
                {
                    "symbol": "AAPL",
                    "bar_time_ms": 200,
                    "open": 1,
                    "high": 2,
                    "low": 1,
                    "close": 2,
                    "volume": 100,
                    "us_time": "2026-04-25 10:00:00",
                    "cn_time": "",
                    "session_type": "regular",
                }
            ],
            flush_indicator_batch=flush_indicator_batch,
            flush_signal_batch=lambda batch: {"ok": True, "written": len(batch), "errors": 0},
            get_or_create_engine=get_or_create_engine,
            get_signal_generator_params=lambda environment: {},
            is_recent_signal_bar=lambda bar_time_ms, interval: True,
            last_compute_time=0.0,
            last_processed_ms={key: 100},
            load_persisted_compute_cursors=lambda environment: 0,
            normalize_symbol_csv=lambda raw: [],
            persist_compute_cursors=lambda environment: persist_calls.append(environment),
            refresh_daily_close_cache=lambda environments: None,
            refresh_symbol_metadata=lambda: None,
            reset_compute_state_for_symbols=lambda environment, symbols, intervals=None: None,
            signal_gens={},
            bootstrap_engine_state=bootstrap_engine_state,
        )

        plan = {
            "enabled_environments": ["live"],
            "requested_environments": ["live"],
            "requested_symbols": ["AAPL"],
            "persist_signals": False,
            "capture_signals": False,
            "force_rollup": False,
            "targeted_rollup": False,
            "incremental_rollup": False,
            "rollup_intervals": [],
            "skip_persisted_cursor": False,
            "targeted_rebuild": False,
            "intervals": ["5m"],
        }

        with mock.patch("ibkr_compute.api.compute.pipeline_views._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.pipeline_views.build_compute_execution_plan", return_value=plan):
                with mock.patch(
                    "ibkr_compute.api.compute.pipeline_views.jsonify",
                    side_effect=lambda payload: _FakeResponse(payload),
                ):
                    first_payload = pipeline_views.build_compute_response({}).get_json()
                    self.assertEqual(first_payload["errors"], 1)
                    self.assertEqual(fake_app.last_processed_ms[key], 100)
                    self.assertEqual(persist_calls, [])
                    second_payload = pipeline_views.build_compute_response({}).get_json()

        self.assertEqual(second_payload["errors"], 0)
        self.assertEqual(fake_app.last_processed_ms[key], 200)
        self.assertEqual(persist_calls, ["live"])
        self.assertGreaterEqual(len(bootstrap_calls), 2)
        self.assertFalse(bootstrap_calls[0]["force_rebuild"])
        self.assertTrue(bootstrap_calls[1]["force_rebuild"])


if __name__ == "__main__":
    unittest.main()
