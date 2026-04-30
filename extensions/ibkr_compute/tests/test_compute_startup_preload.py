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

        with mock.patch.dict(os.environ, {}, clear=True):
            environments = startup_preload.resolve_compute_startup_preload_environments(fake_app)

        self.assertEqual(environments, ["live"])

        with mock.patch.dict(
            os.environ,
            {"IBKR_COMPUTE_STARTUP_PRELOAD_ENVS": "paper, live, paper,backtest"},
            clear=False,
        ):
            environments = startup_preload.resolve_compute_startup_preload_environments(fake_app)

        self.assertEqual(environments, ["paper", "live", "backtest"])

        with mock.patch.dict(os.environ, {"IBKR_COMPUTE_STARTUP_PRELOAD_ENVS": "all"}, clear=True):
            environments = startup_preload.resolve_compute_startup_preload_environments(fake_app)

        self.assertEqual(environments, ["live", "paper", "backtest"])

    def test_resolve_preload_intervals_defaults_to_all_and_allows_override(self):
        fake_app = SimpleNamespace(INTERVALS=["5m", "15m", "30m", "1h", "4h", "1d"])

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                startup_preload.resolve_compute_startup_preload_intervals(fake_app),
                ["5m", "15m", "30m", "1h", "4h", "1d"],
            )

        with mock.patch.dict(
            os.environ,
            {"IBKR_COMPUTE_STARTUP_PRELOAD_INTERVALS": "1h, 5m, 1h,unknown"},
            clear=True,
        ):
            self.assertEqual(startup_preload.resolve_compute_startup_preload_intervals(fake_app), ["1h", "5m"])

        with mock.patch.dict(os.environ, {"IBKR_COMPUTE_STARTUP_PRELOAD_INTERVALS": "all"}, clear=True):
            self.assertEqual(
                startup_preload.resolve_compute_startup_preload_intervals(fake_app),
                ["5m", "15m", "30m", "1h", "4h", "1d"],
            )

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
                results[symbol] = {"is_ready": True, "indicator_seeded": persist_latest_indicator}
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
            clear=True,
        ):
            result = startup_preload.run_compute_startup_preload(fake_app)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["env_completed"], 2)
        self.assertEqual(result["intervals"], ["5m", "15m", "30m", "1h", "4h", "1d"])
        self.assertEqual(result["symbol_total"], 4)
        self.assertEqual(result["symbol_completed"], 4)
        self.assertEqual(result["ready_count"], 4)
        self.assertEqual(result["indicator_seeded"], 0)
        self.assertEqual(result["environments"], ["live", "paper"])
        self.assertEqual(result["results"]["live"]["cursor_applied"], 3)
        self.assertEqual(result["results"]["live"]["cursor_count"], 3)
        self.assertEqual(result["results"]["live"]["status"], "completed")
        self.assertEqual(result["results"]["live"]["interval_total"], 2)
        self.assertEqual(result["results"]["live"]["symbol_total"], 3)
        self.assertEqual(result["results"]["live"]["symbol_completed"], 3)
        self.assertEqual(result["results"]["live"]["indicator_seeded"], 0)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["symbol_count"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["symbol_completed"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["indicator_seeded"], 0)
        self.assertIn("1h", result["results"]["live"]["intervals"])
        self.assertEqual(result["results"]["live"]["intervals"]["1h"]["symbol_count"], 1)
        self.assertEqual(result["results"]["live"]["intervals"]["1h"]["symbol_completed"], 1)
        self.assertEqual(result["results"]["paper"]["indicator_seeded"], 0)
        self.assertEqual(result["results"]["paper"]["intervals"]["5m"]["ready_count"], 1)
        self.assertIn(("cfg_refresh",), call_log)
        self.assertIn(("metadata", True), call_log)
        self.assertIn(("daily_close", ("live", "paper"), True), call_log)
        self.assertIn(("materialize", "live", ("AAPL",), "5m", True, False), call_log)
        self.assertIn(("materialize", "live", ("MSFT",), "5m", True, False), call_log)
        self.assertIn(("materialize", "paper", ("TSLA",), "5m", True, False), call_log)

        state = startup_preload.get_compute_startup_preload_state(fake_app)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["env_total"], 2)
        self.assertEqual(state["env_completed"], 2)
        self.assertEqual(state["intervals"], ["5m", "15m", "30m", "1h", "4h", "1d"])
        self.assertEqual(state["symbol_total"], 4)
        self.assertEqual(state["symbol_completed"], 4)
        self.assertEqual(state["ready_count"], 4)
        self.assertEqual(state["indicator_seeded"], 0)
        self.assertEqual(state["results"]["live"]["intervals"]["5m"]["ready_count"], 2)
        self.assertEqual(state["results"]["live"]["intervals"]["5m"]["indicator_seeded"], 0)
        self.assertEqual(state["direct_backfill"]["status"], "disabled")
        self.assertIsNotNone(state["started_at"])
        self.assertIsNotNone(state["finished_at"])

    def test_run_preload_can_include_configured_higher_intervals(self):
        call_log = []
        cursor_maps = {
            "live": {
                "AAPL|5m": 100,
                "AAPL|1h": 90,
                "MSFT|15m": 80,
            },
        }

        fake_app = SimpleNamespace(
            cfg=SimpleNamespace(refresh=lambda: None),
            refresh_symbol_metadata=lambda force=False: None,
            refresh_daily_close_cache=lambda environments, force=False: None,
            load_persisted_compute_cursors=lambda environment: len(cursor_maps.get(environment, {})),
            collect_environment_cursor_map=lambda environment: dict(cursor_maps.get(environment, {})),
            parse_compute_cursor_key=lambda raw_key: tuple(str(raw_key).split("|", 1)),
            bootstrap_engine_state=lambda *args, **kwargs: 0,
            materialize_engines_from_storage=lambda environment, symbols, interval, hydrate_signal_state=True, persist_latest_indicator=False: (
                call_log.append(("materialize", environment, tuple(symbols), interval, hydrate_signal_state, persist_latest_indicator))
                or {symbol: {"is_ready": True, "indicator_seeded": persist_latest_indicator} for symbol in symbols}
            ),
            pb=SimpleNamespace(get_all_records=lambda *args, **kwargs: []),
            engines={},
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
                "IBKR_COMPUTE_STARTUP_PRELOAD_INTERVALS": "5m,1h",
            },
            clear=True,
        ):
            result = startup_preload.run_compute_startup_preload(fake_app)

        self.assertEqual(result["intervals"], ["5m", "1h"])
        self.assertEqual(result["symbol_total"], 2)
        self.assertEqual(result["results"]["live"]["interval_total"], 2)
        self.assertIn(("materialize", "live", ("AAPL",), "5m", True, False), call_log)
        self.assertIn(("materialize", "live", ("AAPL",), "1h", True, False), call_log)
        self.assertNotIn(("materialize", "live", ("MSFT",), "15m", True, False), call_log)

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
                "AAPL": {"is_ready": True, "indicator_seeded": persist_latest_indicator},
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
            clear=True,
        ):
            result = startup_preload.run_compute_startup_preload(fake_app)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["symbol_total"], 2)
        self.assertEqual(result["symbol_completed"], 2)
        self.assertEqual(result["ready_count"], 1)
        self.assertEqual(result["indicator_seeded"], 0)
        self.assertEqual(result["results"]["live"]["storage_fallback_symbols"], 2)
        self.assertEqual(result["results"]["live"]["storage_fallback_indicator_seeded"], 0)
        self.assertEqual(result["results"]["live"]["indicator_seeded"], 0)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["symbol_count"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["symbol_completed"], 2)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["ready_count"], 1)
        self.assertEqual(result["results"]["live"]["intervals"]["5m"]["indicator_seeded"], 0)
        self.assertIn(("materialize", "live", ("AAPL",), "5m", True, False), call_log)
        self.assertIn(("materialize", "live", ("MSFT",), "5m", True, False), call_log)

    def test_startup_direct_backfill_fetches_missing_ibkr_history_and_materializes(self):
        call_log = []

        class FakeConfig:
            def refresh(self):
                call_log.append(("cfg_refresh",))

            def get_bool_for_environment(self, key, environment, default=False):
                del environment, default
                return key in {
                    "ibkr_startup_direct_backfill_enabled",
                }

            def get_for_environment(self, key, environment, default=None):
                del environment
                if key == "ibkr_startup_direct_backfill_intervals":
                    return "4h,1d"
                if key == "ibkr_startup_direct_backfill_period_4h":
                    return "120d"
                if key == "ibkr_startup_direct_backfill_period_1d":
                    return "2y"
                return default

            def get_int_for_environment(self, key, environment, default=0):
                del environment
                if key == "ibkr_startup_direct_backfill_required_bars":
                    return 2
                return default

        class FakeWriter:
            def __init__(self, **kwargs):
                call_log.append(("writer", kwargs.get("environment")))

            def flush(self):
                call_log.append(("flush",))
                return True

            def close(self):
                call_log.append(("close",))

        class FakeBackfill:
            def __init__(self, **kwargs):
                self.request_count = 0
                call_log.append(("backfill", kwargs.get("environment")))

            def backfill_all(self, conid_map, symbol_meta=None, intervals=None, repair_symbols=None, period_overrides=None):
                interval = intervals[0]
                self.request_count += len(conid_map)
                call_log.append(
                    (
                        "backfill_all",
                        tuple(sorted(conid_map)),
                        interval,
                        tuple(sorted(repair_symbols or [])),
                        {symbol: dict(periods) for symbol, periods in (period_overrides or {}).items()},
                    )
                )
                return {symbol: {interval: 3} for symbol in conid_map}

            def status(self):
                return {"request_count": self.request_count}

        def materialize_engines_from_storage(
            environment,
            symbols,
            interval,
            hydrate_signal_state=True,
            persist_latest_indicator=False,
        ):
            call_log.append(("materialize", environment, tuple(symbols), interval, hydrate_signal_state, persist_latest_indicator))
            return {symbol: {"is_ready": True, "indicator_seeded": False} for symbol in symbols}

        def get_all_records(collection, **kwargs):
            if collection == "watchlist":
                return [
                    {"symbol": "AAPL", "environment": "live"},
                    {"symbol": "MSFT", "environment": "live"},
                ]
            if collection == "ibkr_bars":
                if "symbol =" in str(kwargs.get("filter") or ""):
                    return []
                return [
                    {"symbol": "AAPL", "extra": {"conid": 1}},
                    {"symbol": "MSFT", "extra": {"conid": 2}},
                ]
            if collection == "ibkr_conid_cache":
                return []
            return []

        fake_app = SimpleNamespace(
            cfg=FakeConfig(),
            refresh_symbol_metadata=lambda force=False: call_log.append(("metadata", force)),
            refresh_daily_close_cache=lambda environments, force=False: call_log.append(("daily_close", tuple(environments), force)),
            load_persisted_compute_cursors=lambda environment: 0,
            collect_environment_cursor_map=lambda environment: {},
            parse_compute_cursor_key=lambda raw_key: tuple(str(raw_key).split("|", 1)),
            bootstrap_engine_state=lambda *args, **kwargs: 0,
            materialize_engines_from_storage=materialize_engines_from_storage,
            conid_resolver=SimpleNamespace(resolve_bulk=lambda symbols: (_ for _ in ()).throw(AssertionError("resolver should not run"))),
            pb=SimpleNamespace(get_all_records=get_all_records),
            engines={},
            symbol_metadata_cache={"AAPL": {"exchange": "NASDAQ"}, "MSFT": {"exchange": "NASDAQ"}},
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
                "IBKR_COMPUTE_STARTUP_PRELOAD_INTERVALS": "5m",
            },
            clear=True,
        ), mock.patch("ibkr_compute.market.data_writer.DataWriter", FakeWriter), mock.patch(
            "ibkr_compute.market.data_backfill.DataBackfill",
            FakeBackfill,
        ):
            result = startup_preload.run_compute_startup_preload(fake_app)

        direct = result["direct_backfill"]
        self.assertEqual(direct["status"], "completed")
        self.assertEqual(direct["intervals"], ["4h", "1d"])
        self.assertEqual(direct["required_bars"], 2)
        self.assertEqual(direct["planned_total"], 4)
        self.assertEqual(direct["written"], 12)
        self.assertEqual(direct["request_count"], 4)
        self.assertEqual(direct["ready_count"], 4)
        self.assertEqual(direct["results"]["live"]["intervals"]["4h"]["backfill_symbols_total"], 2)
        self.assertEqual(direct["results"]["live"]["intervals"]["1d"]["backfill_symbols_total"], 2)
        self.assertIn(("backfill_all", ("AAPL", "MSFT"), "4h", ("AAPL", "MSFT"), {"AAPL": {"4h": "120d"}, "MSFT": {"4h": "120d"}}), call_log)
        self.assertIn(("backfill_all", ("AAPL", "MSFT"), "1d", ("AAPL", "MSFT"), {"AAPL": {"1d": "2y"}, "MSFT": {"1d": "2y"}}), call_log)
        self.assertIn(("materialize", "live", ("AAPL", "MSFT"), "4h", True, False), call_log)
        self.assertIn(("materialize", "live", ("AAPL", "MSFT"), "1d", True, False), call_log)


if __name__ == "__main__":
    unittest.main()
