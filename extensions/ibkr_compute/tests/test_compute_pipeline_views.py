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


def _normalize_csv(raw):
    return [
        str(item or "").strip().upper()
        for item in str(raw or "").split(",")
        if str(item or "").strip()
    ]


def _base_compute_plan(**overrides):
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
    plan.update(overrides)
    return plan


def _base_bar(bar_time_ms=200, symbol="AAPL"):
    return {
        "symbol": symbol,
        "bar_time_ms": bar_time_ms,
        "open": 1,
        "high": 2,
        "low": 1,
        "close": 2,
        "volume": 100,
        "us_time": "2026-04-25 10:00:00",
        "cn_time": "",
        "session_type": "regular",
    }


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


class ComputePipelineSignalFastPathTest(unittest.TestCase):
    def _run_compute(self, fake_app, plan):
        with mock.patch("ibkr_compute.api.compute.pipeline_views._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.pipeline_views.build_compute_execution_plan", return_value=plan):
                with mock.patch(
                    "ibkr_compute.api.compute.pipeline_views.jsonify",
                    side_effect=lambda payload: _FakeResponse(payload),
                ):
                    return pipeline_views.build_compute_response({}).get_json()

    def _fake_app(
        self,
        *,
        bars,
        signal_generator=None,
        signal_params=None,
        signal_batch_sink=None,
        indicator_batch_sink=None,
        event_log=None,
    ):
        symbols = sorted({
            str((bar or {}).get("symbol") or "").strip().upper()
            for bar in (bars or [])
            if str((bar or {}).get("symbol") or "").strip()
        } or {"AAPL"})
        engines = {("live", symbol, "5m"): _FakeEngine() for symbol in symbols}
        key = ("live", "AAPL", "5m")
        engines.setdefault(key, _FakeEngine())
        last_processed_ms = {engine_key: 100 for engine_key in engines}
        persist_calls = []
        bootstrap_calls = []
        signal_batches = signal_batch_sink if signal_batch_sink is not None else []
        indicator_batches = indicator_batch_sink if indicator_batch_sink is not None else []
        events = event_log if event_log is not None else []

        def bootstrap_engine_state(
            environment,
            symbol,
            interval,
            before_bar_time_ms,
            inclusive=True,
            force_rebuild=False,
            hydrate_signal_state=True,
        ):
            engine_key = (environment, symbol, interval)
            engine = engines.setdefault(engine_key, _FakeEngine())
            last_processed_ms.setdefault(engine_key, 100)
            bootstrap_calls.append(
                {
                    "environment": environment,
                    "symbol": symbol,
                    "interval": interval,
                    "before_bar_time_ms": before_bar_time_ms,
                    "inclusive": inclusive,
                    "force_rebuild": force_rebuild,
                    "hydrate_signal_state": hydrate_signal_state,
                }
            )
            if force_rebuild:
                engine.reset()
            return 0

        def get_or_create_engine(environment, symbol, interval, signal_params=None):
            engine_key = (environment, symbol, interval)
            last_processed_ms.setdefault(engine_key, 100)
            return engines.setdefault(engine_key, _FakeEngine())

        def build_indicator_payload(environment, symbol, interval, bar, current_engine, snapshot):
            events.append("indicator_payload")
            return {
                "environment": environment,
                "symbol": symbol,
                "interval": interval,
                "bar_time_ms": int(bar["bar_time_ms"]),
                "close": snapshot.get("close"),
            }

        def build_signal_payload(environment, symbol, interval, bar, current_engine, signal):
            events.append("signal_payload")
            return {
                "environment": environment,
                "symbol": symbol,
                "interval": interval,
                "bar_time_ms": int(bar["bar_time_ms"]),
                "signal": signal.get("signal", "test"),
            }

        def flush_indicator_batch(batch):
            events.append("indicator_flush")
            indicator_batches.append(list(batch))
            return {
                "ok": True,
                "written": len(batch),
                "errors": 0,
            }

        def flush_signal_batch(batch):
            events.append("signal_flush")
            signal_batches.append(list(batch))
            return {
                "ok": True,
                "written": len(batch),
                "errors": 0,
            }

        app = SimpleNamespace(
            INDICATOR_BATCH_SIZE=10,
            SIGNAL_BATCH_SIZE=10,
            build_indicator_payload=build_indicator_payload,
            build_signal_payload=build_signal_payload,
            cfg=SimpleNamespace(refresh=lambda: None),
            compute_count=0,
            compute_lock=threading.Lock(),
            engines=engines,
            ensure_higher_timeframe_bars=lambda *args, **kwargs: {},
            error_count=0,
            fetch_interval_bars=lambda environment, interval, symbols=None, full_scan=False: list(bars),
            flush_indicator_batch=flush_indicator_batch,
            flush_signal_batch=flush_signal_batch,
            get_or_create_engine=get_or_create_engine,
            get_signal_generator_params=lambda environment: dict(signal_params or {"signal_enabled_symbols": "AAPL"}),
            is_recent_signal_bar=lambda bar_time_ms, interval: True,
            last_compute_time=0.0,
            last_processed_ms=last_processed_ms,
            load_persisted_compute_cursors=lambda environment: 0,
            normalize_symbol_csv=_normalize_csv,
            persist_compute_cursors=lambda environment: persist_calls.append(environment),
            refresh_daily_close_cache=lambda environments: None,
            refresh_symbol_metadata=lambda: None,
            reset_compute_state_for_symbols=lambda environment, symbols, intervals=None: None,
            signal_gens={key: signal_generator} if signal_generator is not None else {},
            signal_bootstrap_checked=set(),
            bootstrap_engine_state=bootstrap_engine_state,
        )
        app._test_engine = engines[key]
        app._test_bootstrap_calls = bootstrap_calls
        app._test_persist_calls = persist_calls
        app._test_indicator_batches = indicator_batches
        app._test_signal_batches = signal_batches
        return app

    def test_suppressed_signal_outputs_skip_signal_generator_update_but_advance_indicator_cursor(self):
        signal_generator = SimpleNamespace(update=mock.Mock(return_value={"signal": "test"}))
        fake_app = self._fake_app(
            bars=[_base_bar(200)],
            signal_generator=signal_generator,
        )

        payload = self._run_compute(fake_app, _base_compute_plan(persist_signals=False, capture_signals=False))

        self.assertEqual(payload["errors"], 0)
        self.assertEqual(payload["signals"], 0)
        self.assertEqual(fake_app.last_processed_ms[("live", "AAPL", "5m")], 200)
        self.assertEqual(fake_app._test_persist_calls, ["live"])
        self.assertEqual(len(fake_app._test_indicator_batches), 1)
        self.assertEqual(fake_app._test_indicator_batches[0][0]["bar_time_ms"], 200)
        signal_generator.update.assert_not_called()
        self.assertEqual(fake_app._test_signal_batches, [])
        self.assertFalse(fake_app._test_bootstrap_calls[0]["hydrate_signal_state"])

    def test_suppressed_signal_outputs_mark_signal_bootstrap_stale(self):
        key = ("live", "AAPL", "5m")
        signal_generator = SimpleNamespace(update=mock.Mock(return_value={"signal": "test"}))
        fake_app = self._fake_app(
            bars=[_base_bar(200)],
            signal_generator=signal_generator,
        )
        fake_app.signal_bootstrap_checked.add(key)

        self._run_compute(fake_app, _base_compute_plan(persist_signals=False, capture_signals=False))

        self.assertNotIn(key, fake_app.signal_bootstrap_checked)
        signal_generator.update.assert_not_called()

    def test_capture_signals_true_updates_signal_generator_without_persisting(self):
        signal_generator = SimpleNamespace(update=mock.Mock(return_value={"signal": "test"}))
        fake_app = self._fake_app(
            bars=[_base_bar(200)],
            signal_generator=signal_generator,
        )

        payload = self._run_compute(fake_app, _base_compute_plan(persist_signals=False, capture_signals=True))

        self.assertEqual(payload["captured_signal_count"], 1)
        self.assertEqual(payload["signals"], 0)
        signal_generator.update.assert_called_once()
        self.assertEqual(fake_app._test_signal_batches, [])
        self.assertTrue(fake_app._test_bootstrap_calls[0]["hydrate_signal_state"])

    def test_persist_signals_true_updates_and_flushes_signal_batch(self):
        signal_generator = SimpleNamespace(update=mock.Mock(return_value={"signal": "test"}))
        fake_app = self._fake_app(
            bars=[_base_bar(200)],
            signal_generator=signal_generator,
        )

        payload = self._run_compute(fake_app, _base_compute_plan(persist_signals=True, capture_signals=False))

        self.assertEqual(payload["signals"], 1)
        signal_generator.update.assert_called_once()
        self.assertEqual(len(fake_app._test_signal_batches), 1)
        self.assertEqual(fake_app._test_signal_batches[0][0]["bar_time_ms"], 200)

    def test_persist_signal_symbols_filters_signal_generation_to_subset(self):
        aapl_generator = SimpleNamespace(update=mock.Mock(return_value={"signal": "aapl"}))
        msft_generator = SimpleNamespace(update=mock.Mock(return_value={"signal": "msft"}))
        fake_app = self._fake_app(
            bars=[_base_bar(200, symbol="AAPL"), _base_bar(210, symbol="MSFT")],
            signal_generator=aapl_generator,
            signal_params={"signal_enabled_symbols": "AAPL,MSFT"},
        )
        fake_app.signal_gens[("live", "MSFT", "5m")] = msft_generator

        payload = self._run_compute(
            fake_app,
            _base_compute_plan(
                requested_symbols=["AAPL", "MSFT"],
                persist_signals=True,
                capture_signals=False,
                persist_signal_symbols=["AAPL"],
            ),
        )

        self.assertEqual(payload["signals"], 1)
        self.assertEqual(payload["persist_signal_symbols"], ["AAPL"])
        aapl_generator.update.assert_called_once()
        msft_generator.update.assert_not_called()
        self.assertEqual(len(fake_app._test_signal_batches), 1)
        self.assertEqual([item["symbol"] for item in fake_app._test_signal_batches[0]], ["AAPL"])
        indicator_symbols = [
            item["symbol"]
            for batch in fake_app._test_indicator_batches
            for item in batch
        ]
        self.assertEqual(sorted(indicator_symbols), ["AAPL", "MSFT"])
        hydrate_by_symbol = {
            item["symbol"]: item["hydrate_signal_state"]
            for item in fake_app._test_bootstrap_calls
        }
        self.assertTrue(hydrate_by_symbol["AAPL"])
        self.assertFalse(hydrate_by_symbol["MSFT"])

    def test_latest_signal_update_and_flush_do_not_wait_for_indicator_flush(self):
        events = []

        def update_signal(snapshot):
            del snapshot
            events.append("signal_update")
            return {"signal": "test"}

        signal_generator = SimpleNamespace(update=mock.Mock(side_effect=update_signal))
        fake_app = self._fake_app(
            bars=[_base_bar(200)],
            signal_generator=signal_generator,
            event_log=events,
        )

        payload = self._run_compute(fake_app, _base_compute_plan(persist_signals=True, capture_signals=False))

        self.assertEqual(payload["signals"], 1)
        self.assertLess(events.index("signal_update"), events.index("indicator_flush"))
        self.assertLess(events.index("signal_flush"), events.index("indicator_flush"))

    def test_indicator_flush_sorts_batch_by_latest_bar_time_first(self):
        fake_app = self._fake_app(
            bars=[_base_bar(150), _base_bar(200)],
            signal_generator=None,
        )

        payload = self._run_compute(fake_app, _base_compute_plan(persist_signals=False, capture_signals=False))

        self.assertEqual(payload["errors"], 0)
        self.assertEqual(fake_app.last_processed_ms[("live", "AAPL", "5m")], 200)
        self.assertEqual(len(fake_app._test_indicator_batches), 1)
        self.assertEqual([item["bar_time_ms"] for item in fake_app._test_indicator_batches[0]], [200, 150])

    def test_latest_indicator_flush_runs_per_symbol(self):
        fake_app = self._fake_app(
            bars=[_base_bar(195, symbol="MSFT"), _base_bar(200, symbol="AAPL")],
            signal_generator=None,
        )

        payload = self._run_compute(
            fake_app,
            _base_compute_plan(requested_symbols=["AAPL", "MSFT"], persist_signals=False, capture_signals=False),
        )

        self.assertEqual(payload["errors"], 0)
        self.assertEqual(len(fake_app._test_indicator_batches), 2)
        self.assertEqual([item["bar_time_ms"] for item in fake_app._test_indicator_batches[0]], [195])
        self.assertEqual([item["bar_time_ms"] for item in fake_app._test_indicator_batches[1]], [200])

    def test_signal_flush_sorts_batch_by_latest_bar_time_first(self):
        signal_generator = SimpleNamespace(
            update=mock.Mock(side_effect=[{"signal": "older"}, {"signal": "newer"}])
        )
        fake_app = self._fake_app(
            bars=[_base_bar(150), _base_bar(200)],
            signal_generator=signal_generator,
        )

        payload = self._run_compute(fake_app, _base_compute_plan(persist_signals=True, capture_signals=False))

        self.assertEqual(payload["signals"], 2)
        self.assertEqual(len(fake_app._test_signal_batches), 1)
        self.assertEqual([item["bar_time_ms"] for item in fake_app._test_signal_batches[0]], [200, 150])


if __name__ == "__main__":
    unittest.main()
