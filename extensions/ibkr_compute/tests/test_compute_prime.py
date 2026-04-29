import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = SimpleNamespace(get_json=lambda silent=True: {}, args={})
    sys.modules["flask"] = flask_stub

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.compute import prime as compute_prime
from ibkr_compute.api.compute import request as compute_request


class _FakeResponse:
    def __init__(self, payload):
        self._payload = dict(payload)

    def get_json(self):
        return dict(self._payload)


class _FakeEngine:
    def __init__(self, ready=True, bar_count=260, last_bar_time_ms=1000):
        self._ready = ready
        self.bar_count = bar_count
        self.last_bar_time_ms = last_bar_time_ms

    def is_ready(self):
        return self._ready


class _FakePB:
    def __init__(self):
        self.rows = {
            ("ibkr_bars", "5m"): [
                {"symbol": "AAPL", "bar_time_ms": 1000},
                {"symbol": "MSFT", "bar_time_ms": 1000},
            ],
            ("ibkr_indicators", "5"): [
                {"symbol": "AAPL", "bar_time_ms": 1000},
                {"symbol": "MSFT", "bar_time_ms": 1000},
            ],
            ("ibkr_bars", "15m"): [
                {"symbol": "AAPL", "bar_time_ms": 900},
                {"symbol": "MSFT", "bar_time_ms": 900},
            ],
            ("ibkr_indicators", "15"): [
                {"symbol": "AAPL", "bar_time_ms": 900},
            ],
        }

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        del sort, per_page
        interval = ""
        if 'interval = "15m"' in str(filter):
            interval = "15m"
        elif 'interval = "15"' in str(filter):
            interval = "15"
        elif 'interval = "5m"' in str(filter):
            interval = "5m"
        elif 'interval = "5"' in str(filter):
            interval = "5"
        if page > 1:
            return []
        return list(self.rows.get((collection, interval), []))


def _fake_app():
    return SimpleNamespace(
        SUPPORTED_COMPUTE_ENVIRONMENTS=["live", "paper", "backtest"],
        DEFAULT_COMPUTE_ENVIRONMENTS=["live"],
        cfg=SimpleNamespace(
            refresh=lambda: None,
            has_environment_override=lambda key, environment: False,
            get_bool_for_environment=lambda key, environment, default: default,
        ),
        compute_lock=threading.RLock(),
        normalize_symbols=lambda values: [
            str(value or "").strip().upper()
            for value in (values if isinstance(values, list) else [values])
            if str(value or "").strip()
        ],
        ensure_higher_timeframe_bars=mock.Mock(return_value={"live": {"errors": 0, "written": 2}}),
        materialize_engines_from_storage=mock.Mock(
            return_value={
                "AAPL": {"is_ready": True, "indicator_seeded": True},
                "MSFT": {"is_ready": True, "indicator_seeded": True},
            }
        ),
        engines={
            ("live", "AAPL", "5m"): _FakeEngine(True, 260, 1000),
            ("live", "MSFT", "5m"): _FakeEngine(True, 260, 1000),
            ("live", "AAPL", "15m"): _FakeEngine(True, 260, 900),
            ("live", "MSFT", "15m"): _FakeEngine(False, 100, 900),
        },
        pb=_FakePB(),
        build_bar_environment_filter=lambda environment, include_legacy_empty=True: f'environment = "{environment}"',
    )


class ComputePrimeResponseTest(unittest.TestCase):
    def test_prime_materializes_requested_symbols_and_intervals(self):
        fake_app = _fake_app()

        with mock.patch.object(compute_prime, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_request, "_api_app", return_value=fake_app), \
                mock.patch.object(
                    compute_prime,
                    "build_multi_timeframe_readiness",
                    return_value={"status": "ready"},
                ), \
                mock.patch.object(compute_prime, "jsonify", side_effect=lambda payload: _FakeResponse(payload)):
            response = compute_prime.build_compute_prime_response(
                {
                    "environments": ["live"],
                    "symbols": ["aapl", "msft"],
                    "intervals": ["15m"],
                }
            )

        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["intervals"], ["15m"])
        fake_app.ensure_higher_timeframe_bars.assert_called_once_with(
            ["live"],
            force=True,
            symbols=["AAPL", "MSFT"],
            incremental=False,
            intervals=["15m"],
        )
        fake_app.materialize_engines_from_storage.assert_called_once_with(
            "live",
            ["AAPL", "MSFT"],
            "15m",
            hydrate_signal_state=False,
            persist_latest_indicator=False,
        )
        self.assertFalse(payload["persist_latest_indicator"])

    def test_prime_can_explicitly_persist_latest_indicator_mirror(self):
        fake_app = _fake_app()

        with mock.patch.object(compute_prime, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_request, "_api_app", return_value=fake_app), \
                mock.patch.object(
                    compute_prime,
                    "build_multi_timeframe_readiness",
                    return_value={"status": "ready"},
                ), \
                mock.patch.object(compute_prime, "jsonify", side_effect=lambda payload: _FakeResponse(payload)):
            response = compute_prime.build_compute_prime_response(
                {
                    "environments": ["live"],
                    "symbols": ["aapl"],
                    "intervals": ["15m"],
                    "persist_latest_indicator": True,
                }
            )

        payload = response.get_json()
        self.assertTrue(payload["persist_latest_indicator"])
        fake_app.materialize_engines_from_storage.assert_called_once_with(
            "live",
            ["AAPL"],
            "15m",
            hydrate_signal_state=False,
            persist_latest_indicator=True,
        )

    def test_prime_rejects_unsupported_interval(self):
        fake_app = _fake_app()

        with mock.patch.object(compute_prime, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_request, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_prime, "jsonify", side_effect=lambda payload: _FakeResponse(payload)):
            response, status = compute_prime.build_compute_prime_response(
                {
                    "environments": ["live"],
                    "symbols": ["AAPL"],
                    "intervals": ["1d"],
                }
            )

        self.assertEqual(status, 400)
        self.assertEqual(response.get_json()["error"], "unsupported_prime_intervals")
        self.assertEqual(response.get_json()["unsupported_intervals"], ["1d"])

    def test_prime_rejects_mixed_unsupported_intervals(self):
        fake_app = _fake_app()

        with mock.patch.object(compute_prime, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_request, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_prime, "jsonify", side_effect=lambda payload: _FakeResponse(payload)):
            response, status = compute_prime.build_compute_prime_response(
                {
                    "environments": ["live"],
                    "symbols": ["AAPL"],
                    "intervals": ["15m", "4h"],
                }
            )

        self.assertEqual(status, 400)
        payload = response.get_json()
        self.assertEqual(payload["error"], "unsupported_prime_intervals")
        self.assertEqual(payload["unsupported_intervals"], ["4h"])


class MultiTimeframeReadinessTest(unittest.TestCase):
    def test_readiness_keeps_5m_hard_and_high_timeframes_soft(self):
        fake_app = _fake_app()

        readiness = compute_prime.build_multi_timeframe_readiness(
            fake_app,
            environment="live",
            symbols=["AAPL", "MSFT"],
            intervals=["5m", "15m"],
        )

        self.assertEqual(readiness["status"], "degraded")
        self.assertEqual(readiness["intervals"]["5m"]["status"], "ready")
        self.assertTrue(readiness["intervals"]["5m"]["hard_gate"])
        self.assertEqual(readiness["intervals"]["15m"]["status"], "degraded")
        self.assertTrue(readiness["intervals"]["15m"]["soft_gate"])
        self.assertEqual(readiness["intervals"]["15m"]["missing_ready_symbols"], ["MSFT"])
        self.assertEqual(readiness["intervals"]["15m"]["missing_indicator_symbols"], ["MSFT"])
        self.assertTrue(readiness["intervals"]["15m"]["storage_checked"])
        self.assertEqual(readiness["intervals"]["15m"]["readiness_source"], "storage")

    def test_readiness_can_use_fast_engine_snapshot_without_storage(self):
        fake_app = _fake_app()
        fake_app.pb.get_records = mock.Mock(side_effect=AssertionError("storage should not be read"))

        readiness = compute_prime.build_multi_timeframe_readiness(
            fake_app,
            environment="live",
            symbols=["AAPL", "MSFT"],
            intervals=["5m", "15m"],
            use_cache=False,
            include_storage=False,
        )

        self.assertEqual(readiness["status"], "degraded")
        self.assertFalse(readiness["intervals"]["15m"]["storage_checked"])
        self.assertEqual(readiness["intervals"]["15m"]["readiness_source"], "engine")
        self.assertEqual(readiness["intervals"]["15m"]["bar_symbols"], 2)
        self.assertEqual(readiness["intervals"]["15m"]["indicator_symbols"], 1)
        self.assertEqual(readiness["intervals"]["15m"]["missing_indicator_symbols"], ["MSFT"])

    def test_readiness_ignores_missing_indicator_mirror_when_engines_are_ready(self):
        fake_app = _fake_app()
        fake_app.engines[("live", "MSFT", "15m")] = _FakeEngine(True, 260, 900)

        readiness = compute_prime.build_multi_timeframe_readiness(
            fake_app,
            environment="live",
            symbols=["AAPL", "MSFT"],
            intervals=["5m", "15m"],
            use_cache=False,
        )

        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["intervals"]["15m"]["status"], "ready")
        self.assertEqual(readiness["intervals"]["15m"]["missing_ready_symbols"], [])
        self.assertEqual(readiness["intervals"]["15m"]["missing_indicator_symbols"], ["MSFT"])

    def test_readiness_returns_unknown_without_target_symbols(self):
        fake_app = _fake_app()
        fake_app.engines = {}

        readiness = compute_prime.build_multi_timeframe_readiness(
            fake_app,
            environment="live",
            use_cache=False,
            include_storage=False,
        )

        self.assertEqual(readiness["status"], "unknown")
        self.assertEqual(readiness["reason"], "no_target_symbols")
        self.assertEqual(readiness["symbols_total"], 0)


if __name__ == "__main__":
    unittest.main()
