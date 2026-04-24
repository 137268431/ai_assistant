import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api import startup_preload


class ComputeStartupPreloadTest(unittest.TestCase):
    def setUp(self):
        startup_preload._STARTUP_PRELOAD_THREAD = None

    def test_should_schedule_requires_compute_remote_profile(self):
        with mock.patch.dict(
            os.environ,
            {
                "IBKR_SERVICE_PROFILE": "compute",
                "IBKR_RUNTIME_MODE": "remote",
                "IBKR_COMPUTE_STARTUP_PRELOAD_ENABLED": "true",
            },
            clear=False,
        ):
            self.assertTrue(startup_preload.should_schedule_compute_startup_preload())

        with mock.patch.dict(
            os.environ,
            {
                "IBKR_SERVICE_PROFILE": "runtime",
                "IBKR_RUNTIME_MODE": "remote",
                "IBKR_COMPUTE_STARTUP_PRELOAD_ENABLED": "true",
            },
            clear=False,
        ):
            self.assertFalse(startup_preload.should_schedule_compute_startup_preload())

        with mock.patch.dict(
            os.environ,
            {
                "IBKR_SERVICE_PROFILE": "compute",
                "IBKR_RUNTIME_MODE": "embedded",
                "IBKR_COMPUTE_STARTUP_PRELOAD_ENABLED": "true",
            },
            clear=False,
        ):
            self.assertFalse(startup_preload.should_schedule_compute_startup_preload())

    def test_resolve_preload_environments_uses_configured_csv(self):
        fake_app = SimpleNamespace(
            DEFAULT_COMPUTE_ENVIRONMENTS=["live", "paper"],
            SUPPORTED_COMPUTE_ENVIRONMENTS=["live", "paper", "backtest"],
            normalize_symbol_csv=lambda value: [item.strip().upper() for item in str(value).split(",") if item.strip()],
        )

        with mock.patch.dict(
            os.environ,
            {"IBKR_COMPUTE_STARTUP_PRELOAD_ENVS": "paper, live, paper,backtest"},
            clear=False,
        ):
            environments = startup_preload.resolve_compute_startup_preload_environments(fake_app)

        self.assertEqual(environments, ["paper", "live", "backtest"])

    def test_run_preload_loads_cursors_and_materializes_by_interval(self):
        call_log = []
        cursor_maps = {
            "live": {
                "AAPL|5m": 100,
                "AAPL|1h": 90,
                "MSFT|5m": 80,
            },
            "paper": {
                "TSLA|5m": 70,
            },
        }

        def load_persisted_compute_cursors(environment):
            call_log.append(("load", environment))
            return len(cursor_maps.get(environment, {}))

        def collect_environment_cursor_map(environment):
            return dict(cursor_maps.get(environment, {}))

        def parse_compute_cursor_key(raw_key):
            symbol, interval = raw_key.split("|", 1)
            return symbol, interval

        engines = {}

        def materialize_engines_from_storage(
            environment,
            symbols,
            interval,
            hydrate_signal_state=True,
            persist_latest_indicator=False,
        ):
            normalized_symbols = tuple(symbols)
            call_log.append(
                (
                    "materialize",
                    environment,
                    normalized_symbols,
                    interval,
                    hydrate_signal_state,
                    persist_latest_indicator,
                )
            )
            results = {}
            for symbol in normalized_symbols:
                engines[(environment, symbol, interval)] = SimpleNamespace(is_ready=lambda: True)
                results[symbol] = {"is_ready": True, "indicator_seeded": True}
            return results

        fake_app = SimpleNamespace(
            cfg=SimpleNamespace(refresh=lambda: call_log.append(("cfg_refresh",))),
            refresh_symbol_metadata=lambda force=False: call_log.append(("metadata", force)),
            refresh_daily_close_cache=lambda environments, force=False: call_log.append(("daily_close", tuple(environments), force)),
            load_persisted_compute_cursors=load_persisted_compute_cursors,
            collect_environment_cursor_map=collect_environment_cursor_map,
            parse_compute_cursor_key=parse_compute_cursor_key,
            bootstrap_engine_state=lambda *args, **kwargs: 0,
            materialize_engines_from_storage=materialize_engines_from_storage,
            pb=SimpleNamespace(get_all_records=lambda *args, **kwargs: []),
            engines=engines,
            DEFAULT_COMPUTE_ENVIRONMENTS=["live", "paper"],
            SUPPORTED_COMPUTE_ENVIRONMENTS=["live", "paper", "backtest"],
            INTERVALS=["5m", "15m", "30m", "1h", "4h", "1d"],
            normalize_symbol_csv=lambda value: [item.strip().upper() for item in str(value).split(",") if item.strip()],
        )

        with mock.patch.dict(
            os.environ,
            {
                "IBKR_SERVICE_PROFILE": "compute",
                "IBKR_RUNTIME_MODE": "remote",
                "IBKR_COMPUTE_STARTUP_PRELOAD_ENVS": "live,paper",
            },
            clear=False,
        ):
            result = startup_preload.run_compute_startup_preload(fake_app)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["env_completed"], 2)
        self.assertEqual(result["symbol_total"], 4)
        self.assertEqual(result["symbol_completed"], 4)
        self.assertEqual(result["ready_count"], 4)
        self.assertEqual(result["indicator_seeded"], 4)
        self.assertEqual(result["environments"], ["live", "paper"])
        self.assertEqual(result["results"]["live"]["cursor_applied"], 3)
        self.assertEqual(result["results"]["live"]["cursor_count"], 3)
        self.assertEqual(result["results"]["live"]["status"], "completed")
        self.assertEqual(result["results"]["live"]["symbol_total"], 3)
        self.assertEqual(result["results"]["live"]["symbol_completed"], 3)
        self.assertEqual(result["results"]["live"]["indicator_seeded"], 3)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["symbol_count"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["symbol_completed"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["indicator_seeded"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["1h"]["symbol_count"], 1)
        self.assertEqual(result["results"]["paper"]["indicator_seeded"], 1)
        self.assertEqual(result["results"]["paper"]["intervals"]["5m"]["ready_count"], 1)
        self.assertIn(("cfg_refresh",), call_log)
        self.assertIn(("metadata", True), call_log)
        self.assertIn(("daily_close", ("live", "paper"), True), call_log)
        self.assertIn(("materialize", "live", ("AAPL",), "5m", True, True), call_log)
        self.assertIn(("materialize", "live", ("MSFT",), "5m", True, True), call_log)
        self.assertIn(("materialize", "live", ("AAPL",), "1h", True, True), call_log)
        self.assertIn(("materialize", "paper", ("TSLA",), "5m", True, True), call_log)

        state = startup_preload.get_compute_startup_preload_state(fake_app)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["env_total"], 2)
        self.assertEqual(state["env_completed"], 2)
        self.assertEqual(state["symbol_total"], 4)
        self.assertEqual(state["symbol_completed"], 4)
        self.assertEqual(state["ready_count"], 4)
        self.assertEqual(state["indicator_seeded"], 4)
        self.assertEqual(state["results"]["live"]["intervals"]["5m"]["ready_count"], 2)
        self.assertEqual(state["results"]["live"]["intervals"]["5m"]["indicator_seeded"], 2)
        self.assertIsNotNone(state["started_at"])
        self.assertIsNotNone(state["finished_at"])

    def test_get_preload_state_reports_disabled_when_schedule_guard_blocks(self):
        fake_app = SimpleNamespace(
            DEFAULT_COMPUTE_ENVIRONMENTS=["live", "paper"],
            SUPPORTED_COMPUTE_ENVIRONMENTS=["live", "paper", "backtest"],
            normalize_symbol_csv=lambda value: [item.strip().upper() for item in str(value).split(",") if item.strip()],
            pb=SimpleNamespace(get_all_records=lambda *args, **kwargs: []),
        )

        with mock.patch.dict(
            os.environ,
            {
                "IBKR_SERVICE_PROFILE": "runtime",
                "IBKR_RUNTIME_MODE": "remote",
                "IBKR_COMPUTE_STARTUP_PRELOAD_ENABLED": "false",
            },
            clear=False,
        ):
            state = startup_preload.get_compute_startup_preload_state(fake_app)

        self.assertEqual(state["status"], "disabled")
        self.assertFalse(state["enabled"])
        self.assertFalse(state["should_schedule"])
        self.assertEqual(state["reason"], "schedule_guard_blocked")

    def test_run_preload_falls_back_to_watchlist_materialization_without_cursors(self):
        call_log = []

        def load_persisted_compute_cursors(environment):
            call_log.append(("load", environment))
            return 0

        def collect_environment_cursor_map(environment):
            del environment
            return {}

        def materialize_engines_from_storage(
            environment,
            symbols,
            interval,
            hydrate_signal_state=True,
            persist_latest_indicator=False,
        ):
            call_log.append(
                (
                    "materialize",
                    environment,
                    tuple(symbols),
                    interval,
                    hydrate_signal_state,
                    persist_latest_indicator,
                )
            )
            return {
                "AAPL": {"is_ready": True, "indicator_seeded": True},
                "MSFT": {"is_ready": False},
            }

        fake_app = SimpleNamespace(
            cfg=SimpleNamespace(refresh=lambda: call_log.append(("cfg_refresh",))),
            refresh_symbol_metadata=lambda force=False: call_log.append(("metadata", force)),
            refresh_daily_close_cache=lambda environments, force=False: call_log.append(("daily_close", tuple(environments), force)),
            load_persisted_compute_cursors=load_persisted_compute_cursors,
            collect_environment_cursor_map=collect_environment_cursor_map,
            parse_compute_cursor_key=lambda raw_key: tuple(str(raw_key).split("|", 1)),
            bootstrap_engine_state=lambda *args, **kwargs: 0,
            materialize_engines_from_storage=materialize_engines_from_storage,
            engines={},
            symbol_metadata_cache={},
            pb=SimpleNamespace(
                get_all_records=lambda *args, **kwargs: [
                    {"symbol": "AAPL", "environment": "live"},
                    {"symbol": "MSFT", "environment": "global"},
                ]
            ),
            DEFAULT_COMPUTE_ENVIRONMENTS=["live"],
            SUPPORTED_COMPUTE_ENVIRONMENTS=["live", "paper", "backtest"],
            INTERVALS=["5m", "15m", "30m", "1h", "4h", "1d"],
            normalize_symbol_csv=lambda value: [item.strip().upper() for item in str(value).split(",") if item.strip()],
        )

        with mock.patch.dict(
            os.environ,
            {
                "IBKR_SERVICE_PROFILE": "compute",
                "IBKR_RUNTIME_MODE": "remote",
                "IBKR_COMPUTE_STARTUP_PRELOAD_ENVS": "live",
            },
            clear=False,
        ):
            result = startup_preload.run_compute_startup_preload(fake_app)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["symbol_total"], 2)
        self.assertEqual(result["symbol_completed"], 2)
        self.assertEqual(result["ready_count"], 1)
        self.assertEqual(result["indicator_seeded"], 1)
        self.assertEqual(result["results"]["live"]["storage_fallback_symbols"], 2)
        self.assertEqual(result["results"]["live"]["storage_fallback_indicator_seeded"], 1)
        self.assertEqual(result["results"]["live"]["indicator_seeded"], 1)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["symbol_count"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["symbol_completed"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["ready_count"], 1)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["indicator_seeded"], 1)
        self.assertIn(("materialize", "live", ("AAPL",), "5m", True, True), call_log)
        self.assertIn(("materialize", "live", ("MSFT",), "5m", True, True), call_log)


if __name__ == "__main__":
    unittest.main()
