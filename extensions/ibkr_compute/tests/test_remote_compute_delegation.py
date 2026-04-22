import sys
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
    pass


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


if __name__ == "__main__":
    unittest.main()
