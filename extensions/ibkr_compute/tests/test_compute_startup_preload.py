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

        def bootstrap_engine_state(environment, symbol, interval, target_ms, inclusive=True):
            call_log.append(("bootstrap", environment, interval, symbol, target_ms, inclusive))
            engines[(environment, symbol, interval)] = SimpleNamespace(is_ready=lambda: True)
            return 1

        fake_app = SimpleNamespace(
            cfg=SimpleNamespace(refresh=lambda: call_log.append(("cfg_refresh",))),
            refresh_symbol_metadata=lambda force=False: call_log.append(("metadata", force)),
            refresh_daily_close_cache=lambda environments, force=False: call_log.append(("daily_close", tuple(environments), force)),
            load_persisted_compute_cursors=load_persisted_compute_cursors,
            collect_environment_cursor_map=collect_environment_cursor_map,
            parse_compute_cursor_key=parse_compute_cursor_key,
            bootstrap_engine_state=bootstrap_engine_state,
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
        self.assertEqual(result["environments"], ["live", "paper"])
        self.assertEqual(result["results"]["live"]["cursor_applied"], 3)
        self.assertEqual(result["results"]["live"]["cursor_count"], 3)
        self.assertEqual(result["results"]["live"]["status"], "completed")
        self.assertEqual(result["results"]["live"]["symbol_total"], 3)
        self.assertEqual(result["results"]["live"]["symbol_completed"], 3)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["symbol_count"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["symbol_completed"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["1h"]["symbol_count"], 1)
        self.assertEqual(result["results"]["paper"]["intervals"]["5m"]["ready_count"], 1)
        self.assertIn(("cfg_refresh",), call_log)
        self.assertIn(("metadata", True), call_log)
        self.assertIn(("daily_close", ("live", "paper"), True), call_log)
        self.assertIn(("bootstrap", "live", "5m", "AAPL", 100, True), call_log)
        self.assertIn(("bootstrap", "live", "5m", "MSFT", 80, True), call_log)
        self.assertIn(("bootstrap", "live", "1h", "AAPL", 90, True), call_log)
        self.assertIn(("bootstrap", "paper", "5m", "TSLA", 70, True), call_log)

        state = startup_preload.get_compute_startup_preload_state(fake_app)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["env_total"], 2)
        self.assertEqual(state["env_completed"], 2)
        self.assertEqual(state["symbol_total"], 4)
        self.assertEqual(state["symbol_completed"], 4)
        self.assertEqual(state["ready_count"], 4)
        self.assertEqual(state["results"]["live"]["intervals"]["5m"]["ready_count"], 2)
        self.assertIsNotNone(state["started_at"])
        self.assertIsNotNone(state["finished_at"])

    def test_get_preload_state_reports_disabled_when_schedule_guard_blocks(self):
        fake_app = SimpleNamespace(
            DEFAULT_COMPUTE_ENVIRONMENTS=["live", "paper"],
            SUPPORTED_COMPUTE_ENVIRONMENTS=["live", "paper", "backtest"],
            normalize_symbol_csv=lambda value: [item.strip().upper() for item in str(value).split(",") if item.strip()],
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


if __name__ == "__main__":
    unittest.main()
