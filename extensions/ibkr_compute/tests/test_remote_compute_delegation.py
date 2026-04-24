import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration.runtime_pipeline import TradingServiceRuntimePipelineMixin
from ibkr_compute.orchestration.warmup_cycle import TradingServiceWarmupCycleMixin


class _DummyWarmupCycle(TradingServiceWarmupCycleMixin):
    def _normalize_symbol_list(self, symbols):
        return [
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        ]

    def _non_monitor_pending_symbols(self, pending_symbols, monitor_symbols=None):
        monitor_set = set(self._normalize_symbol_list(monitor_symbols or []))
        return [
            symbol
            for symbol in self._normalize_symbol_list(pending_symbols or [])
            if symbol not in monitor_set
        ]


class _DummyRuntimePipeline(TradingServiceRuntimePipelineMixin):
    def __init__(self):
        self._interval_prime_lock = threading.Lock()
        self._interval_prime_state = {
            "running": False,
            "completed_intervals": [],
            "symbol_count": 0,
            "last_started_at": "",
            "last_finished_at": "",
            "last_duration_s": 0.0,
            "last_error": "",
            "last_source": "",
        }
        self._interval_prime_thread = None
        self._running = True

    def _now_iso(self) -> str:
        return "2026-04-25T10:00:00Z"


class RemoteWarmupReadinessTest(unittest.TestCase):
    def test_collect_warmup_readiness_uses_remote_compute_status_for_runtime_service(self):
        cycle = _DummyWarmupCycle()
        snapshot = {
            "symbols": ["AAPL", "VIX"],
            "scan_symbols": [],
            "subscription_symbols": ["AAPL", "VIX"],
            "trade_symbols": ["AAPL"],
            "trade_symbols_total": 1,
            "monitor_symbols": ["VIX"],
            "monitor_symbols_total": 1,
        }
        remote_status = {
            "service_profile": "compute",
            "engines": {
                "live/AAPL/5m": {
                    "is_ready": True,
                    "bar_count": 288,
                    "last_bar_time_ms": 1776793800000,
                },
                "live/VIX/5m": {
                    "is_ready": False,
                    "bar_count": 12,
                    "last_bar_time_ms": 1776793500000,
                },
            },
        }

        with mock.patch(
            "ibkr_compute.orchestration.warmup_cycle._service_mod",
            return_value=SimpleNamespace(
                ENVIRONMENT="live",
                DEFAULT_WARMUP_REQUIRED_INTERVAL="5m",
            ),
        ):
            with mock.patch(
                "ibkr_compute.api.service_topology.uses_remote_compute_service",
                return_value=True,
            ):
                with mock.patch(
                    "ibkr_compute.api.compute_status_client.get_remote_compute_status",
                    return_value=remote_status,
                ):
                    readiness = cycle._collect_warmup_readiness(snapshot)

        self.assertEqual(readiness["required_interval"], "5m")
        self.assertEqual(readiness["ready_symbols"], 1)
        self.assertEqual(readiness["ready_trade_symbols"], 1)
        self.assertEqual(readiness["ready_monitor_symbols"], 0)
        self.assertEqual(readiness["pending_symbols"], ["VIX"])
        self.assertTrue(readiness["trading_gate_open"])
        self.assertEqual(readiness["trading_gate_reason"], "ready")
        self.assertEqual(readiness["symbol_status"][0]["source"], "remote_compute_status")

    def test_remote_warmup_skips_local_cursor_and_materialize_bootstrap(self):
        cycle = _DummyWarmupCycle()

        with mock.patch(
            "ibkr_compute.orchestration.warmup_cycle._service_mod",
            return_value=SimpleNamespace(
                ENVIRONMENT="live",
                DEFAULT_WARMUP_REQUIRED_INTERVAL="5m",
            ),
        ):
            with mock.patch(
                "ibkr_compute.api.service_topology.uses_remote_compute_service",
                return_value=True,
            ):
                cursor_result = cycle._load_warmup_compute_cursors()
                bootstrap_result = cycle._materialize_warmup_compute_symbols(
                    ["AAPL", "MSFT"],
                    hydrate_signal_state=False,
                )

        self.assertEqual(cursor_result, 0)
        self.assertEqual(bootstrap_result, {})


class RemoteRealtimeComputeTriggerTest(unittest.TestCase):
    def test_trigger_realtime_compute_posts_to_remote_compute_service(self):
        pipeline = _DummyRuntimePipeline()
        remote_result = {"ok": True, "processed": 5, "signals": 1, "errors": 0}

        with mock.patch(
            "ibkr_compute.orchestration.runtime_pipeline._service_mod",
            return_value=SimpleNamespace(ENVIRONMENT="live", logger=mock.Mock()),
        ):
            with mock.patch(
                "ibkr_compute.api.service_topology.uses_remote_compute_service",
                return_value=True,
            ):
                with mock.patch(
                    "ibkr_compute.api.compute_status_client.trigger_remote_compute",
                    return_value=remote_result,
                ) as trigger_mock:
                    result = pipeline._trigger_realtime_compute(
                        source="canonical_close",
                        symbols=["msft", "AAPL", "msft"],
                    )

        self.assertEqual(result, remote_result)
        trigger_mock.assert_called_once_with(
            {
                "source": "canonical_close",
                "environments": ["live"],
                "symbols": ["AAPL", "MSFT"],
            }
        )

    def test_schedule_interval_prime_seeds_indicators_for_materialized_intervals(self):
        pipeline = _DummyRuntimePipeline()
        call_log = []
        fake_server = types.ModuleType("ibkr_compute.api.server")
        fake_server.compute_lock = threading.Lock()
        fake_server.load_persisted_compute_cursors = lambda environment: 0

        def materialize_engines_from_storage(
            environment,
            symbols,
            interval,
            hydrate_signal_state=True,
            persist_latest_indicator=False,
        ):
            call_log.append(
                (
                    environment,
                    tuple(symbols),
                    interval,
                    hydrate_signal_state,
                    persist_latest_indicator,
                )
            )
            return {}

        fake_server.materialize_engines_from_storage = materialize_engines_from_storage

        class _ImmediateThread:
            def __init__(self, target=None, daemon=None, name=None):
                self._target = target
                self._alive = False
                del daemon, name

            def start(self):
                self._alive = True
                if self._target:
                    self._target()
                self._alive = False

            def is_alive(self):
                return self._alive

        with mock.patch.dict(sys.modules, {"ibkr_compute.api.server": fake_server}):
            with mock.patch(
                "ibkr_compute.orchestration.runtime_pipeline.threading.Thread",
                _ImmediateThread,
            ):
                with mock.patch(
                    "ibkr_compute.orchestration.runtime_pipeline._service_mod",
                    return_value=SimpleNamespace(
                        ENVIRONMENT="live",
                        STARTUP_BACKGROUND_PRIME_INTERVALS=["1h", "4h"],
                        STARTUP_BACKGROUND_PRIME_CHUNK_SIZE=2,
                        logger=mock.Mock(),
                    ),
                ):
                    scheduled = pipeline._schedule_interval_prime(
                        ["AAPL", "MSFT", "TSLA"],
                        source="startup_ready",
                    )

        self.assertTrue(scheduled)
        self.assertEqual(
            call_log,
            [
                ("live", ("AAPL", "MSFT"), "1h", True, True),
                ("live", ("TSLA",), "1h", True, True),
                ("live", ("AAPL", "MSFT"), "4h", True, True),
                ("live", ("TSLA",), "4h", True, True),
            ],
        )


if __name__ == "__main__":
    unittest.main()
