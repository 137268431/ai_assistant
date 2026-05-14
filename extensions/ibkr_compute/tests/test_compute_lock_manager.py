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
from ibkr_compute.api.compute import prime as compute_prime
from ibkr_compute.api.compute import request as compute_request
from ibkr_compute.api.compute.lock_manager import (
    ComputeLockManager,
    ComputeLockRequest,
    ComputeSlot,
    build_compute_plan_lock_request,
    normalize_compute_slot,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = dict(payload)

    def get_json(self):
        return dict(self._payload)


def _hold_lock_in_thread(manager: ComputeLockManager, request: ComputeLockRequest):
    ready = threading.Event()
    release = threading.Event()
    errors = []

    def target():
        lease = manager.acquire(request, timeout=1.0)
        if lease is None:
            errors.append("lock_not_acquired")
            ready.set()
            return
        with lease:
            ready.set()
            release.wait(2.0)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    if not ready.wait(1.0):
        raise AssertionError("lock holder did not start")
    if errors:
        raise AssertionError(errors[0])
    return release, thread


def _slot(symbol: str, interval: str = "5m") -> ComputeSlot:
    return normalize_compute_slot("live", symbol, interval)


def _targeted_request(symbol: str, interval: str = "5m") -> ComputeLockRequest:
    return ComputeLockRequest(reason="test", slots=(_slot(symbol, interval),))


class ComputeLockManagerTest(unittest.TestCase):
    def test_disjoint_slots_can_run_while_overlap_and_global_wait(self):
        manager = ComputeLockManager()
        release, thread = _hold_lock_in_thread(manager, _targeted_request("AAPL"))
        try:
            self.assertIsNone(manager.acquire(_targeted_request("AAPL"), timeout=0.01))

            disjoint = manager.acquire(_targeted_request("MSFT"), timeout=0.01)
            self.assertIsNotNone(disjoint)
            disjoint.release()

            self.assertIsNone(manager.acquire(ComputeLockRequest.global_lock("maintenance"), timeout=0.01))
        finally:
            release.set()
            thread.join(1.0)

        global_lease = manager.acquire(ComputeLockRequest.global_lock("maintenance"), timeout=0.01)
        self.assertIsNotNone(global_lease)
        global_lease.release()

    def test_global_lock_blocks_targeted_slots(self):
        manager = ComputeLockManager()
        release, thread = _hold_lock_in_thread(manager, ComputeLockRequest.global_lock("maintenance"))
        try:
            self.assertIsNone(manager.acquire(_targeted_request("AAPL"), timeout=0.01))
        finally:
            release.set()
            thread.join(1.0)

        targeted = manager.acquire(_targeted_request("AAPL"), timeout=0.01)
        self.assertIsNotNone(targeted)
        targeted.release()

    def test_plan_request_locks_symbols_across_compute_and_rollup_intervals(self):
        request = build_compute_plan_lock_request(
            {
                "enabled_environments": ["live"],
                "requested_symbols": ["AAPL", "MSFT"],
                "intervals": ["5m"],
                "rollup_intervals": ["15m", "1h"],
                "source": "targeted_recompute",
                "targeted_rollup": True,
            }
        )

        self.assertFalse(request.global_scope)
        self.assertEqual(request.reason, "targeted_recompute")
        self.assertEqual(
            set(request.slots),
            {
                ("live", "AAPL", "5m"),
                ("live", "AAPL", "15m"),
                ("live", "AAPL", "1h"),
                ("live", "MSFT", "5m"),
                ("live", "MSFT", "15m"),
                ("live", "MSFT", "1h"),
            },
        )

    def test_plan_without_symbols_uses_global_lock(self):
        request = build_compute_plan_lock_request(
            {
                "enabled_environments": ["live"],
                "requested_symbols": [],
                "intervals": ["5m"],
                "rollup_intervals": [],
            }
        )

        self.assertTrue(request.global_scope)
        self.assertEqual(request.reason, "compute_plan_unbounded")


def _compute_plan(symbol: str) -> dict:
    return {
        "payload": {"symbols": [symbol]},
        "source": "targeted_recompute",
        "enabled_environments": ["live"],
        "requested_environments": ["live"],
        "requested_symbols": [symbol],
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


def _fake_pipeline_app(manager: ComputeLockManager):
    return SimpleNamespace(
        INDICATOR_BATCH_SIZE=10,
        SIGNAL_BATCH_SIZE=10,
        bootstrap_engine_state=mock.Mock(return_value=0),
        build_indicator_payload=mock.Mock(),
        build_signal_payload=mock.Mock(),
        cfg=SimpleNamespace(refresh=mock.Mock()),
        compute_count=0,
        compute_lock=threading.RLock(),
        compute_lock_manager=manager,
        engines={},
        ensure_higher_timeframe_bars=mock.Mock(return_value={}),
        error_count=0,
        fetch_interval_bars=mock.Mock(return_value=[]),
        flush_indicator_batch=mock.Mock(return_value={"ok": True, "written": 0, "errors": 0}),
        flush_signal_batch=mock.Mock(return_value={"ok": True, "written": 0, "errors": 0}),
        get_or_create_engine=mock.Mock(),
        get_signal_generator_params=mock.Mock(return_value={}),
        is_recent_signal_bar=mock.Mock(return_value=True),
        last_compute_time=0.0,
        last_processed_ms={},
        load_persisted_compute_cursors=mock.Mock(return_value=0),
        normalize_symbol_csv=lambda raw: [],
        persist_compute_cursors=mock.Mock(),
        refresh_daily_close_cache=mock.Mock(),
        refresh_symbol_metadata=mock.Mock(),
        reset_compute_state_for_symbols=mock.Mock(),
        signal_bootstrap_checked=set(),
        signal_gens={},
    )


class ComputePipelineManagedLockTest(unittest.TestCase):
    def _run_compute(self, fake_app, plan):
        with mock.patch.object(pipeline_views, "_api_app", return_value=fake_app), \
                mock.patch.object(pipeline_views, "build_compute_execution_plan", return_value=plan), \
                mock.patch.object(pipeline_views, "COMPUTE_LOCK_TIMEOUT_SECONDS", 0.01), \
                mock.patch.object(pipeline_views, "jsonify", side_effect=lambda payload: _FakeResponse(payload)):
            return pipeline_views.build_compute_response(plan["payload"])

    def test_targeted_compute_uses_slot_lock_not_global_lock(self):
        manager = ComputeLockManager()
        release, thread = _hold_lock_in_thread(manager, _targeted_request("AAPL"))
        fake_app = _fake_pipeline_app(manager)
        try:
            disjoint_response = self._run_compute(fake_app, _compute_plan("MSFT"))
            self.assertTrue(disjoint_response.get_json()["ok"])

            overlapping_response, status = self._run_compute(fake_app, _compute_plan("AAPL"))
            self.assertEqual(status, 503)
            payload = overlapping_response.get_json()
            self.assertEqual(payload["error"], "compute_busy")
            self.assertEqual(payload["lock_scope"], "slots")
            self.assertEqual(payload["lock_slot_count"], 1)
        finally:
            release.set()
            thread.join(1.0)


def _fake_prime_app(manager: ComputeLockManager):
    return SimpleNamespace(
        SUPPORTED_COMPUTE_ENVIRONMENTS=["live", "paper", "backtest"],
        DEFAULT_COMPUTE_ENVIRONMENTS=["live"],
        cfg=SimpleNamespace(
            refresh=mock.Mock(),
            has_environment_override=lambda key, environment: False,
            get_bool_for_environment=lambda key, environment, default: default,
        ),
        compute_lock=threading.RLock(),
        compute_lock_manager=manager,
        normalize_symbols=lambda values: [
            str(value or "").strip().upper()
            for value in (values if isinstance(values, list) else [values])
            if str(value or "").strip()
        ],
        ensure_higher_timeframe_bars=mock.Mock(return_value={"live": {"errors": 0, "written": 1}}),
        materialize_engines_from_storage=mock.Mock(return_value={"MSFT": {"is_ready": True}}),
        engines={},
    )


class ComputePrimeManagedLockTest(unittest.TestCase):
    def _run_prime(self, fake_app, payload):
        with mock.patch.object(compute_prime, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_request, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_prime, "build_multi_timeframe_readiness", return_value={"status": "ready"}), \
                mock.patch.object(pipeline_views, "COMPUTE_LOCK_TIMEOUT_SECONDS", 0.01), \
                mock.patch.object(compute_prime, "COMPUTE_LOCK_TIMEOUT_SECONDS", 0.01), \
                mock.patch.object(compute_prime, "jsonify", side_effect=lambda payload: _FakeResponse(payload)):
            return compute_prime.build_compute_prime_response(payload)

    def test_prime_uses_requested_slots(self):
        manager = ComputeLockManager()
        release, thread = _hold_lock_in_thread(manager, _targeted_request("AAPL", "15m"))
        fake_app = _fake_prime_app(manager)
        try:
            disjoint_response = self._run_prime(
                fake_app,
                {"environments": ["live"], "symbols": ["MSFT"], "intervals": ["15m"]},
            )
            self.assertTrue(disjoint_response.get_json()["ok"])

            overlapping_response, status = self._run_prime(
                fake_app,
                {"environments": ["live"], "symbols": ["AAPL"], "intervals": ["15m"]},
            )
            self.assertEqual(status, 503)
            payload = overlapping_response.get_json()
            self.assertEqual(payload["error"], "compute_busy")
            self.assertEqual(payload["lock_scope"], "slots")
            self.assertEqual(payload["symbols"], ["AAPL"])
        finally:
            release.set()
            thread.join(1.0)


if __name__ == "__main__":
    unittest.main()
