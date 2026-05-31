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

from ibkr_compute.orchestration import market_universe as market_universe_mod
from ibkr_compute.orchestration.market_universe import TradingServiceMarketUniverseMixin
from ibkr_compute.orchestration.runtime_pipeline import TradingServiceRuntimePipelineMixin
from ibkr_compute.orchestration.warmup_cycle import TradingServiceWarmupCycleMixin


class _DummyWarmupCycle(TradingServiceWarmupCycleMixin):
    def _copy_warmup_state(self, source=None):
        copied = {}
        for key, value in dict(source or {}).items():
            if isinstance(value, dict):
                copied[key] = dict(value)
            elif isinstance(value, list):
                copied[key] = [dict(item) if isinstance(item, dict) else item for item in value]
            else:
                copied[key] = value
        return copied

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


class _DummyStatePB:
    def __init__(self):
        self.states = []

    def upsert_state(self, state_key, environment, data, date="global"):
        self.states.append((state_key, environment, dict(data), date))
        return {"ok": True}


class _DummyConfig:
    def get_for_environment(self, key, environment, default=None):
        if key == "ibkr_scan_schedule":
            return "00:00-23:59"
        if key == "ibkr_daily_scan_retry_delays_sec":
            return "1,2,3"
        return default

    def get_bool_for_environment(self, key, environment, default=False):
        return default

    def get_int_for_environment(self, key, environment, default=0):
        return default


class _DummyMarketUniverse(TradingServiceMarketUniverseMixin):
    def __init__(self):
        self._current_market_date = "2026-04-27"
        self._scan_state_lock = threading.Lock()
        self._daily_scan_state = self._initial_daily_scan_state(self._current_market_date)
        self._watchlist_trade_symbols = ["AAPL"]
        self._last_target_refresh_at = 10.0
        self._daily_scan_alert_market_date = ""
        self._daily_scan_alert_error = ""
        self._daily_scan_alert_title = ""
        self._daily_scan_alert_at = 0.0
        self._daily_scan_alert_active = False
        self._daily_scan_failure_count = 0
        self.pb = _DummyStatePB()
        self.config = _DummyConfig()
        self.events = []

    def _market_date(self) -> str:
        return self._current_market_date

    def _now_iso(self) -> str:
        return "2026-04-27T09:20:03-04:00"

    def _now_et(self) -> str:
        return "2026-04-27T09:20:03-04:00"

    def _runtime_phase_label(self) -> str:
        return "running"

    def _runtime_page_url(self) -> str:
        return "https://quant.example/ibkr_runtime.html"

    def _emit_system_event(self, event_type: str, level: str, title: str, detail: dict, *, message_id: str = ""):
        self.events.append(
            {
                "event_type": event_type,
                "level": level,
                "title": title,
                "detail": dict(detail),
                "message_id": message_id,
            }
        )
        return {"ok": True}

    def _refresh_watchlist_pool(self):
        return None

    def _scan_window_open(self) -> bool:
        return True

    def _copy_warmup_state(self) -> dict:
        return {"symbols_total": 1, "pending_symbols": [], "monitor_symbols": []}

    def _non_monitor_pending_symbols(self, pending_symbols, monitor_symbols=None):
        return []


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
                ) as status_mock:
                    readiness = cycle._collect_warmup_readiness(snapshot)

        status_mock.assert_called_once()
        self.assertFalse(status_mock.call_args.kwargs["include_engines"])
        self.assertEqual(readiness["required_interval"], "5m")
        self.assertEqual(readiness["ready_symbols"], 1)
        self.assertEqual(readiness["ready_trade_symbols"], 1)
        self.assertEqual(readiness["ready_monitor_symbols"], 0)
        self.assertEqual(readiness["pending_symbols"], ["VIX"])
        self.assertTrue(readiness["trading_gate_open"])
        self.assertEqual(readiness["trading_gate_reason"], "ready")
        self.assertEqual(readiness["symbol_status"][0]["source"], "remote_compute_status")

    def test_collect_warmup_readiness_falls_back_to_remote_readiness_summary(self):
        cycle = _DummyWarmupCycle()
        snapshot = {
            "symbols": ["AAPL", "AMD"],
            "scan_symbols": ["AMD"],
            "subscription_symbols": ["AAPL", "AMD"],
            "trade_symbols": ["AAPL"],
            "trade_symbols_total": 1,
            "monitor_symbols": [],
            "monitor_symbols_total": 0,
        }
        remote_status = {
            "service_profile": "compute",
            "engines": {},
            "multi_timeframe_readiness": {
                "intervals": {
                    "5m": {
                        "status": "ready",
                        "latest_bar_time_ms": 1776793800000,
                        "latest_indicator_time_ms": 1776793800000,
                        "missing_ready_symbols_total": 0,
                        "missing_indicator_symbols_total": 2,
                    }
                }
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

        self.assertEqual(readiness["ready_symbols"], 2)
        self.assertEqual(readiness["ready_trade_symbols"], 1)
        self.assertTrue(readiness["trading_gate_open"])
        self.assertEqual(readiness["pending_symbols"], [])
        self.assertEqual(readiness["symbol_status"][0]["source"], "remote_compute_bar_readiness")

    def test_collect_warmup_readiness_uses_storage_missing_list_without_engines(self):
        cycle = _DummyWarmupCycle()
        snapshot = {
            "symbols": ["AAPL", "AMD", "VIX"],
            "scan_symbols": [],
            "subscription_symbols": ["AAPL", "AMD", "VIX"],
            "trade_symbols": ["AAPL", "AMD"],
            "trade_symbols_total": 2,
            "monitor_symbols": ["VIX"],
            "monitor_symbols_total": 1,
        }
        remote_status = {
            "service_profile": "compute",
            "engines": {},
            "multi_timeframe_readiness": {
                "intervals": {
                    "5m": {
                        "status": "blocked",
                        "storage_checked": True,
                        "latest_bar_time_ms": 1776793800000,
                        "latest_indicator_time_ms": 1776793800000,
                        "missing_ready_symbols": ["AMD"],
                        "missing_ready_symbols_total": 1,
                        "missing_bar_symbols": [],
                    }
                }
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

        self.assertEqual(readiness["ready_symbols_list"], ["AAPL", "VIX"])
        self.assertEqual(readiness["pending_symbols"], ["AMD"])
        self.assertEqual(readiness["ready_trade_symbols"], 1)
        self.assertFalse(readiness["trading_gate_open"])
        self.assertEqual(readiness["symbol_status"][1]["source"], "remote_compute_status_missing")

    def test_transient_remote_status_preserves_previous_open_gate(self):
        cycle = _DummyWarmupCycle()
        snapshot = {
            "symbols": ["AAPL", "AMD", "SPY"],
            "scan_symbols": [],
            "subscription_symbols": ["AAPL", "AMD", "SPY"],
            "trade_symbols": ["AAPL", "AMD"],
            "trade_symbols_total": 2,
            "monitor_symbols": ["SPY"],
            "monitor_symbols_total": 1,
        }
        readiness = {
            "phase": "degraded",
            "data_ready": False,
            "trade_allowed": False,
            "ready_symbols": 0,
            "ready_symbols_list": [],
            "ready_trade_symbols": 0,
            "ready_monitor_symbols": 0,
            "pending_symbols": [],
            "integrity_pending_symbols": [],
            "symbol_status": [
                {"symbol": "AAPL", "role": "trade", "ready": False, "source": "remote_compute_status_unavailable"},
                {"symbol": "AMD", "role": "trade", "ready": False, "source": "remote_compute_status_unavailable"},
                {"symbol": "SPY", "role": "monitor", "ready": False, "source": "remote_compute_status_unavailable"},
            ],
            "trading_gate_open": False,
            "trading_gate_reason": "remote_compute_status_unavailable",
        }
        previous = {
            "trading_gate_open": True,
            "ready_symbols_list": ["AAPL", "AMD", "SPY"],
            "symbol_status": [
                {"symbol": "AAPL", "bar_count": 288, "last_bar_time_ms": 1776793800000},
                {"symbol": "AMD", "bar_count": 288, "last_bar_time_ms": 1776793800000},
                {"symbol": "SPY", "bar_count": 288, "last_bar_time_ms": 1776793800000},
            ],
        }

        preserved = cycle._preserve_previous_gate_for_transient_remote_readiness(
            snapshot,
            readiness,
            previous,
        )

        self.assertTrue(preserved["trading_gate_open"])
        self.assertEqual(preserved["trading_gate_reason"], "ready")
        self.assertEqual(preserved["ready_trade_symbols"], 2)
        self.assertEqual(preserved["ready_monitor_symbols"], 1)
        self.assertEqual(preserved["pending_symbols"], [])
        self.assertEqual(preserved["symbol_status"][0]["source"], "previous_warmup_snapshot")

    def test_collect_warmup_readiness_marks_no_trade_data_ready(self):
        cycle = _DummyWarmupCycle()
        snapshot = {
            "symbols": ["AAPL", "QQQ", "SPY", "VIX"],
            "scan_symbols": ["AAPL"],
            "subscription_symbols": ["QQQ", "SPY", "VIX"],
            "trade_symbols": [],
            "trade_symbols_total": 0,
            "monitor_symbols": ["QQQ", "SPY", "VIX"],
            "monitor_symbols_total": 3,
        }
        remote_status = {
            "service_profile": "compute",
            "engines": {},
            "multi_timeframe_readiness": {
                "symbols_total": 4,
                "intervals": {
                    "5m": {
                        "status": "ready",
                        "symbols_total": 4,
                        "latest_bar_time_ms": 1776793800000,
                        "latest_indicator_time_ms": 1776793800000,
                        "missing_ready_symbols_total": 0,
                    }
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

        self.assertEqual(readiness["phase"], "ready")
        self.assertTrue(readiness["data_ready"])
        self.assertFalse(readiness["trading_gate_open"])
        self.assertEqual(readiness["trading_gate_reason"], "no_trade_symbols")
        self.assertEqual(readiness["pending_symbols"], [])

    def test_collect_warmup_readiness_does_not_mark_all_pending_when_remote_status_unavailable(self):
        cycle = _DummyWarmupCycle()
        snapshot = {
            "symbols": ["AAPL", "AMD"],
            "scan_symbols": [],
            "subscription_symbols": ["AAPL", "AMD"],
            "trade_symbols": ["AAPL"],
            "trade_symbols_total": 1,
            "monitor_symbols": [],
            "monitor_symbols_total": 0,
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
                    return_value={},
                ):
                    readiness = cycle._collect_warmup_readiness(snapshot)

        self.assertEqual(readiness["phase"], "degraded")
        self.assertEqual(readiness["pending_symbols"], [])
        self.assertFalse(readiness["trading_gate_open"])
        self.assertEqual(readiness["trading_gate_reason"], "remote_compute_status_unavailable")
        self.assertEqual(readiness["symbol_status"][0]["source"], "remote_compute_status_unavailable")

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

    def test_trigger_realtime_compute_can_suppress_startup_signals(self):
        pipeline = _DummyRuntimePipeline()

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
                    return_value={"ok": True},
                ) as trigger_mock:
                    pipeline._trigger_realtime_compute(
                        source="canonical_close",
                        symbols=["AAPL"],
                        persist_signals=False,
                    )

        trigger_mock.assert_called_once_with(
            {
                "source": "canonical_close",
                "environments": ["live"],
                "persist_signals": False,
                "symbols": ["AAPL"],
            }
        )

    def test_schedule_interval_prime_delegates_to_remote_compute_service(self):
        pipeline = _DummyRuntimePipeline()
        calls = []

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

        def trigger_remote_prime(payload):
            calls.append(dict(payload))
            return {"ok": True}

        with mock.patch(
            "ibkr_compute.orchestration.runtime_pipeline.threading.Thread",
            _ImmediateThread,
        ):
            with mock.patch(
                "ibkr_compute.orchestration.runtime_pipeline._service_mod",
                return_value=SimpleNamespace(
                    ENVIRONMENT="live",
                    STARTUP_BACKGROUND_PRIME_INTERVALS=["15m", "30m"],
                    STARTUP_BACKGROUND_PRIME_CHUNK_SIZE=2,
                    logger=mock.Mock(),
                ),
            ):
                with mock.patch(
                    "ibkr_compute.api.service_topology.uses_remote_compute_service",
                    return_value=True,
                ):
                    with mock.patch(
                        "ibkr_compute.api.compute_status_client.trigger_remote_prime",
                        side_effect=trigger_remote_prime,
                    ):
                        scheduled = pipeline._schedule_interval_prime(
                            ["AAPL", "MSFT", "TSLA"],
                            source="startup_ready",
                        )

        self.assertTrue(scheduled)
        self.assertEqual(
            calls,
            [
                {
                    "environments": ["live"],
                    "symbols": ["AAPL", "MSFT"],
                    "intervals": ["15m"],
                    "persist_latest_indicator": False,
                },
                {
                    "environments": ["live"],
                    "symbols": ["TSLA"],
                    "intervals": ["15m"],
                    "persist_latest_indicator": False,
                },
                {
                    "environments": ["live"],
                    "symbols": ["AAPL", "MSFT"],
                    "intervals": ["30m"],
                    "persist_latest_indicator": False,
                },
                {
                    "environments": ["live"],
                    "symbols": ["TSLA"],
                    "intervals": ["30m"],
                    "persist_latest_indicator": False,
                },
            ],
        )
        self.assertEqual(pipeline._interval_prime_state["completed_intervals"], ["15m", "30m"])

    def test_schedule_interval_prime_materializes_intervals_without_indicator_seed(self):
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
                    with mock.patch(
                        "ibkr_compute.api.service_topology.uses_remote_compute_service",
                        return_value=False,
                    ):
                        scheduled = pipeline._schedule_interval_prime(
                            ["AAPL", "MSFT", "TSLA"],
                            source="startup_ready",
                        )

        self.assertTrue(scheduled)
        self.assertEqual(
            call_log,
            [
                ("live", ("AAPL", "MSFT"), "1h", True, False),
                ("live", ("TSLA",), "1h", True, False),
                ("live", ("AAPL", "MSFT"), "4h", True, False),
                ("live", ("TSLA",), "4h", True, False),
            ],
        )


class RemoteDailyScanDelegationTest(unittest.TestCase):
    def setUp(self):
        self.service_mod = SimpleNamespace(
            ENVIRONMENT="live",
            DAILY_SCAN_STATE_KEY="ibkr_daily_scan_state",
            DAILY_SCAN_STATE_DATE="global",
            DAILY_SCAN_EVENT_ALERT_COOLDOWN_SECONDS=1800,
            logger=mock.Mock(),
        )
        self.service_patch = mock.patch.object(
            market_universe_mod,
            "_service_mod",
            return_value=self.service_mod,
        )
        self.service_patch.start()

    def tearDown(self):
        self.service_patch.stop()

    def test_daily_scan_posts_to_remote_compute_service_for_runtime_profile(self):
        service = _DummyMarketUniverse()
        remote_result = {
            "ok": True,
            "accepted": True,
            "async": True,
            "run_id": "scan-live-2026-04-27-test",
            "status": "accepted",
        }

        with mock.patch(
            "ibkr_compute.api.service_topology.uses_remote_compute_service",
            return_value=True,
        ):
            with mock.patch("ibkr_compute.api.compute_status_client.get_remote_compute_status", return_value={}):
                with mock.patch(
                    "ibkr_compute.api.compute_status_client.trigger_remote_scan",
                    return_value=remote_result,
                ) as trigger_mock:
                    result = service._run_daily_scan_if_due(reason="poll")

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        self.assertEqual(result["state"]["status"], "pending")
        self.assertEqual(result["state"]["run_id"], "scan-live-2026-04-27-test")
        self.assertEqual(service._last_target_refresh_at, 10.0)
        trigger_payload = trigger_mock.call_args.args[0]
        self.assertEqual(trigger_payload["environment"], "live")
        self.assertTrue(trigger_payload["async"])
        self.assertEqual(trigger_payload["trigger_source"], "poll")
        self.assertTrue(str(trigger_payload["run_id"]).startswith("daily-scan-live-2026-04-27-"))

    def test_daily_scan_poll_completes_pending_remote_attempt(self):
        service = _DummyMarketUniverse()
        service._daily_scan_state = {
            **service._initial_daily_scan_state("2026-04-27"),
            "status": "pending",
            "run_id": "scan-live-2026-04-27-test",
            "started_at": "2026-04-27T09:20:03-04:00",
        }
        status_result = {
            "ok": True,
            "status": "completed",
            "run_id": "scan-live-2026-04-27-test",
            "result": {
                "ok": True,
                "date": "2026-04-27",
                "scanned": 2,
                "eligible": 1,
                "active": 1,
                "candidates": 0,
                "errors": 0,
            },
        }

        with mock.patch(
            "ibkr_compute.api.service_topology.uses_remote_compute_service",
            return_value=True,
        ):
            with mock.patch(
                "ibkr_compute.api.compute_status_client.get_remote_scan_status",
                return_value=status_result,
            ) as status_mock:
                result = service._run_daily_scan_if_due(reason="poll")

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["status"], "completed")
        self.assertEqual(service._last_target_refresh_at, 0.0)
        status_mock.assert_called_once()

    def test_daily_scan_all_snapshotless_result_schedules_retry(self):
        service = _DummyMarketUniverse()
        snapshotless_result = {
            "ok": True,
            "date": "2026-04-27",
            "scanned": 2,
            "eligible": 0,
            "active": 0,
            "candidates": 0,
            "errors": 0,
            "rejection_summary": {"no_snapshot": 2},
        }

        with mock.patch(
            "ibkr_compute.api.service_topology.uses_remote_compute_service",
            return_value=True,
        ):
            with mock.patch("ibkr_compute.api.compute_status_client.get_remote_compute_status", return_value={}):
                with mock.patch(
                    "ibkr_compute.api.compute_status_client.trigger_remote_scan",
                    return_value=snapshotless_result,
                ):
                    result = service._run_daily_scan_if_due(reason="poll")

        self.assertFalse(result["ok"])
        self.assertTrue(result["retry_scheduled"])
        self.assertEqual(result["state"]["status"], "retry_wait")
        self.assertEqual(result["state"]["last_error"], "All scanned symbols are missing technical snapshots")
        self.assertEqual(result["state"]["failure"]["code"], "all_scanned_symbols_missing_technical_snapshots")
        self.assertFalse(result["state"]["result"]["ok"])
        self.assertEqual(result["state"]["result"]["error"], "all_scanned_symbols_missing_technical_snapshots")
        self.assertEqual(service.events[0]["title"], "IBKR 盘前日筛重试中")

    def test_daily_scan_skips_tv_primary_slim_without_retry_alert(self):
        service = _DummyMarketUniverse()
        service._runtime_tv_primary_slim_enabled = lambda: True

        with mock.patch("ibkr_compute.api.compute_status_client.trigger_remote_scan") as trigger_mock:
            result = service._run_daily_scan_if_due(reason="poll")

        trigger_mock.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "tv_primary_slim_mode")
        self.assertEqual(result["state"]["status"], "skipped")
        self.assertEqual(service.events, [])

    def test_daily_scan_skips_closed_market_and_clears_retry_state(self):
        service = _DummyMarketUniverse()
        service._current_market_date = "2026-05-31"
        service._daily_scan_state = {
            **service._initial_daily_scan_state("2026-05-31"),
            "status": "retry_wait",
            "last_error": "compute_scan_stalled",
            "next_retry_at": "2026-05-31T08:52:00-04:00",
            "retry_count": 1,
        }

        with mock.patch("ibkr_compute.api.compute_status_client.trigger_remote_scan") as trigger_mock:
            result = service._run_daily_scan_if_due(reason="poll")

        trigger_mock.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "market_closed")
        self.assertEqual(result["closed_reason"], "weekend")
        self.assertEqual(result["state"]["status"], "skipped")
        self.assertEqual(result["state"]["last_error"], "")
        self.assertEqual(result["state"]["next_retry_at"], "")
        self.assertEqual(service.events, [])

    def test_daily_scan_waits_when_compute_preload_is_running(self):
        service = _DummyMarketUniverse()
        compute_status = {
            "ok": True,
            "compute_startup_preload": {
                "status": "running",
                "running": True,
                "symbol_completed": 14,
                "symbol_total": 712,
                "ready_count": 14,
                "elapsed_s": 815,
            },
        }

        with mock.patch(
            "ibkr_compute.api.service_topology.uses_remote_compute_service",
            return_value=True,
        ):
            with mock.patch("ibkr_compute.api.compute_status_client.get_remote_compute_status", return_value=compute_status):
                with mock.patch("ibkr_compute.api.compute_status_client.trigger_remote_scan") as trigger_mock:
                    result = service._run_daily_scan_if_due(reason="poll")

        self.assertFalse(result["ok"])
        self.assertTrue(result["retry_scheduled"])
        self.assertEqual(result["state"]["status"], "retry_wait")
        self.assertEqual(result["state"]["failure"]["code"], "compute_preload_running")
        self.assertEqual(result["state"]["failure"]["evidence"]["compute_startup_preload"]["symbol_total"], 712)
        trigger_mock.assert_not_called()

    def test_daily_scan_submit_timeout_checks_status_then_schedules_retry(self):
        service = _DummyMarketUniverse()
        timeout_result = {
            "ok": False,
            "error_code": "compute_scan_submit_timeout",
            "error": "HTTPConnectionPool(host='127.0.0.1', port=5100): Read timed out.",
            "retryable": True,
        }
        not_found = {"ok": False, "status": "not_found", "error": "scan_attempt_not_found"}

        with mock.patch(
            "ibkr_compute.api.service_topology.uses_remote_compute_service",
            return_value=True,
        ):
            with mock.patch("ibkr_compute.api.compute_status_client.get_remote_compute_status", return_value={}):
                with mock.patch("ibkr_compute.api.compute_status_client.trigger_remote_scan", return_value=timeout_result):
                    with mock.patch("ibkr_compute.api.compute_status_client.get_remote_scan_status", return_value=not_found) as status_mock:
                        result = service._run_daily_scan_if_due(reason="poll")

        self.assertFalse(result["ok"])
        self.assertTrue(result["retry_scheduled"])
        self.assertEqual(result["state"]["failure"]["code"], "compute_scan_submit_timeout")
        self.assertEqual(result["state"]["failure"]["evidence"]["error_code"], "compute_scan_submit_timeout")
        status_mock.assert_called_once()

    def test_stale_running_daily_scan_is_retried(self):
        service = _DummyMarketUniverse()
        service._daily_scan_state.update({
            "status": "running",
            "started_at": "2000-01-01T00:00:00Z",
            "reason": "poll",
            "result": {},
        })
        remote_result = {
            "ok": True,
            "date": "2026-04-27",
            "scanned": 2,
            "eligible": 1,
            "active": 1,
            "candidates": 0,
            "errors": 0,
        }

        with mock.patch(
            "ibkr_compute.api.service_topology.uses_remote_compute_service",
            return_value=True,
        ):
            with mock.patch("ibkr_compute.api.compute_status_client.get_remote_compute_status", return_value={}):
                with mock.patch(
                    "ibkr_compute.api.compute_status_client.trigger_remote_scan",
                    return_value=remote_result,
                ) as trigger_mock:
                    result = service._run_daily_scan_if_due(reason="poll")

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["status"], "completed")
        self.assertEqual(result["state"]["result"], remote_result)
        trigger_payload = trigger_mock.call_args.args[0]
        self.assertEqual(trigger_payload["environment"], "live")
        self.assertTrue(trigger_payload["async"])
        persisted_statuses = [row[2]["status"] for row in service.pb.states]
        self.assertIn("failed", persisted_statuses)
        self.assertEqual(persisted_statuses[-1], "completed")


if __name__ == "__main__":
    unittest.main()
