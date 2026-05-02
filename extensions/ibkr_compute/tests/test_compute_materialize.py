import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.compute import materialize


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
        self.bar_count += 1
        self.last_bar_time_ms = int(row.get("bar_time_ms", 0) or 0)
        self._snapshot = {"close": row.get("close", 0), "bar_time_ms": self.last_bar_time_ms}
        return dict(self._snapshot)

    def is_ready(self):
        return self.bar_count > 0

    def get_snapshot(self):
        return dict(self._snapshot)


class _FakeSignalGenerator:
    def __init__(self):
        self.reset_count = 0
        self.updated_bar_times = []

    def daily_reset(self):
        self.reset_count += 1
        self.updated_bar_times = []

    def update(self, snapshot):
        self.updated_bar_times.append(int(snapshot.get("bar_time_ms", 0) or 0))
        return None


class _FakeCfg:
    def __init__(self, *, sqlite_read_enabled=False):
        self.sqlite_read_enabled = sqlite_read_enabled

    def get_bool_for_environment(self, key, _environment, default=False):
        if key == "ibkr_bar_direct_sqlite_read_enabled":
            return self.sqlite_read_enabled
        return default

    def get_float_for_environment(self, _key, _environment, default=0.0):
        return default

    def get_int_for_environment(self, _key, _environment, default=0):
        return default


class _FakeSqliteConnection:
    def __init__(self, rows):
        self.rows = list(rows or [])
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.executions.append((sql, tuple(params or ())))
        return SimpleNamespace(fetchall=lambda: list(self.rows))


class ComputeMaterializeTest(unittest.TestCase):
    def test_bootstrap_does_not_advance_processed_cursor(self):
        engine = _FakeEngine()
        key = ("live", "AAPL", "5m")
        fake_app = SimpleNamespace(
            BOOTSTRAP_LOOKBACK_BARS={"5m": 8},
            build_bar_environment_filter=lambda environment, include_legacy_empty=True: f'environment = "{environment}"',
            cfg=_FakeCfg(sqlite_read_enabled=False),
            engine_bootstrap_checked=set(),
            last_interval_fetch_ms={},
            last_processed_ms={key: 123},
            normalize_bar_environment=lambda row, environment: dict(row, environment=environment),
            pb=SimpleNamespace(
                get_all_records=lambda *args, **kwargs: [
                    {"symbol": "AAPL", "bar_time_ms": 200, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
                    {"symbol": "AAPL", "bar_time_ms": 150, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                ]
            ),
            signal_gens={},
        )

        with mock.patch("ibkr_compute.api.compute.materialize._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.materialize.get_or_create_engine", return_value=engine):
                processed = materialize.bootstrap_engine_state(
                    "live",
                    "AAPL",
                    "5m",
                    200,
                    inclusive=True,
                )

        self.assertEqual(processed, 2)
        self.assertEqual(fake_app.last_processed_ms[key], 123)
        self.assertEqual(fake_app.last_interval_fetch_ms[("live", "5m")], 200)
        self.assertIn(key, fake_app.engine_bootstrap_checked)

    def test_bootstrap_rehydrates_signal_state_when_engine_was_already_checked(self):
        engine = _FakeEngine()
        engine.last_bar_time_ms = 200
        key = ("live", "AAPL", "5m")
        signal_generator = _FakeSignalGenerator()
        query_count = {"value": 0}

        def get_records(*args, **kwargs):
            del args, kwargs
            query_count["value"] += 1
            return [
                {"symbol": "AAPL", "bar_time_ms": 200, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
                {"symbol": "AAPL", "bar_time_ms": 150, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            ]

        fake_app = SimpleNamespace(
            BOOTSTRAP_LOOKBACK_BARS={"5m": 8},
            build_bar_environment_filter=lambda environment, include_legacy_empty=True: f'environment = "{environment}"',
            cfg=_FakeCfg(sqlite_read_enabled=False),
            engine_bootstrap_checked={key},
            signal_bootstrap_checked=set(),
            last_interval_fetch_ms={},
            last_processed_ms={},
            normalize_bar_environment=lambda row, environment: dict(row, environment=environment),
            pb=SimpleNamespace(get_all_records=get_records),
            signal_gens={key: signal_generator},
        )

        with mock.patch("ibkr_compute.api.compute.materialize._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.materialize.get_or_create_engine", return_value=engine):
                processed = materialize.bootstrap_engine_state(
                    "live",
                    "AAPL",
                    "5m",
                    200,
                    inclusive=True,
                    hydrate_signal_state=True,
                )
                skipped = materialize.bootstrap_engine_state(
                    "live",
                    "AAPL",
                    "5m",
                    200,
                    inclusive=True,
                    hydrate_signal_state=True,
                )

        self.assertEqual(processed, 2)
        self.assertEqual(skipped, 0)
        self.assertEqual(query_count["value"], 1)
        self.assertEqual(signal_generator.reset_count, 1)
        self.assertEqual(signal_generator.updated_bar_times, [150, 200])
        self.assertIn(key, fake_app.signal_bootstrap_checked)

    def test_materialize_can_seed_latest_indicator_payloads(self):
        engine = _FakeEngine()
        key = ("live", "AAPL", "5m")
        seeded_batches = []
        latest_row = {
            "symbol": "AAPL",
            "bar_time_ms": 200,
            "open": 2,
            "high": 2,
            "low": 2,
            "close": 2,
            "volume": 2,
            "exchange": "NASDAQ",
            "us_time": "2026-04-24 16:00:00",
            "cn_time": "2026-04-25 04:00:00",
            "session_type": "regular",
        }
        fake_app = SimpleNamespace(
            MATERIALIZE_MAX_WORKERS=1,
            build_bar_environment_filter=lambda environment, include_legacy_empty=True: f'environment = "{environment}"',
            build_indicator_payload=lambda environment, symbol, interval, bar, current_engine, snapshot: {
                "environment": environment,
                "symbol": symbol,
                "interval": interval,
                "bar_time_ms": bar["bar_time_ms"],
                "close": snapshot.get("close"),
            },
            cfg=_FakeCfg(sqlite_read_enabled=False),
            engines={key: engine},
            last_processed_ms={},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
            normalize_bar_environment=lambda row, environment: dict(row, environment=environment),
            normalize_symbols=lambda symbols: [str(symbol).upper() for symbol in symbols],
            pb=SimpleNamespace(get_all_records=lambda *args, **kwargs: [latest_row]),
            persist_compute_cursors=mock.Mock(return_value=None),
            signal_gens={},
            flush_indicator_batch=lambda batch: seeded_batches.append(list(batch)) or {"ok": True, "written": len(batch), "errors": 0},
        )

        def bootstrap(environment, symbol, interval, target_ms, inclusive=True, force_rebuild=False, hydrate_signal_state=True):
            del environment, symbol, interval, target_ms, inclusive, force_rebuild, hydrate_signal_state
            engine.reset()
            engine.update({"bar_time_ms": 150, "close": 1})
            engine.update({"bar_time_ms": 200, "close": 2})
            return 2

        with mock.patch("ibkr_compute.api.compute.materialize._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.materialize.bootstrap_engine_state", side_effect=bootstrap):
                results = materialize.materialize_engines_from_storage(
                    "live",
                    ["AAPL"],
                    "5m",
                    hydrate_signal_state=True,
                    persist_latest_indicator=True,
                )

        self.assertTrue(results["AAPL"]["is_ready"])
        self.assertTrue(results["AAPL"]["indicator_seeded"])
        self.assertEqual(len(seeded_batches), 1)
        self.assertEqual(seeded_batches[0][0]["bar_time_ms"], 200)
        self.assertEqual(seeded_batches[0][0]["symbol"], "AAPL")
        self.assertEqual(fake_app.last_processed_ms[key], 200)
        fake_app.persist_compute_cursors.assert_called_once_with("live")

    def test_bootstrap_prefers_direct_sqlite_read_and_preserves_filters(self):
        engine = _FakeEngine()
        key = ("live", "AAPL", "5m")
        sqlite_conn = _FakeSqliteConnection(
            [
                {"symbol": "AAPL", "bar_time_ms": 150, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            ]
        )
        fake_app = SimpleNamespace(
            BOOTSTRAP_LOOKBACK_BARS={"5m": 8},
            build_bar_environment_filter=lambda environment, include_legacy_empty=True: f'environment = "{environment}"',
            cfg=_FakeCfg(sqlite_read_enabled=True),
            engine_bootstrap_checked=set(),
            last_interval_fetch_ms={},
            last_processed_ms={key: 123},
            logger=SimpleNamespace(debug=lambda *args, **kwargs: None),
            normalize_bar_environment=lambda row, environment: dict(row, environment=environment),
            pb=SimpleNamespace(get_all_records=mock.Mock(side_effect=AssertionError("PB API should not be read"))),
            signal_gens={},
        )

        with mock.patch("ibkr_compute.api.compute.materialize._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.materialize.get_or_create_engine", return_value=engine):
                with mock.patch("ibkr_compute.api.compute.materialize.open_pb_sqlite", return_value=sqlite_conn) as open_sqlite:
                    with mock.patch("ibkr_compute.api.compute.materialize._safe_storage_upper_bound_ms", return_value=175):
                        processed = materialize.bootstrap_engine_state(
                            "live",
                            "AAPL",
                            "5m",
                            200,
                            inclusive=True,
                        )

        self.assertEqual(processed, 1)
        self.assertEqual(engine.last_bar_time_ms, 150)
        open_sqlite.assert_called_once_with(readonly=True, timeout=30.0)
        self.assertEqual(len(sqlite_conn.executions), 1)
        sql, params = sqlite_conn.executions[0]
        self.assertIn("FROM ibkr_bars", sql)
        self.assertIn("ORDER BY bar_time_ms DESC", sql)
        self.assertEqual(params, ("AAPL", "5m", "live", "", 175, 200, 8))

    def test_bootstrap_falls_back_to_pb_api_when_sqlite_read_fails(self):
        engine = _FakeEngine()
        key = ("live", "AAPL", "5m")
        pb_get_all_records = mock.Mock(
            return_value=[
                {"symbol": "AAPL", "bar_time_ms": 200, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
                {"symbol": "AAPL", "bar_time_ms": 150, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            ]
        )
        fake_app = SimpleNamespace(
            BOOTSTRAP_LOOKBACK_BARS={"5m": 8},
            build_bar_environment_filter=lambda environment, include_legacy_empty=True: f'environment = "{environment}"',
            cfg=_FakeCfg(sqlite_read_enabled=True),
            engine_bootstrap_checked=set(),
            last_interval_fetch_ms={},
            last_processed_ms={key: 123},
            logger=SimpleNamespace(debug=lambda *args, **kwargs: None),
            normalize_bar_environment=lambda row, environment: dict(row, environment=environment),
            pb=SimpleNamespace(get_all_records=pb_get_all_records),
            signal_gens={},
        )

        with mock.patch("ibkr_compute.api.compute.materialize._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.materialize.get_or_create_engine", return_value=engine):
                with mock.patch(
                    "ibkr_compute.api.compute.materialize.open_pb_sqlite",
                    side_effect=RuntimeError("sqlite busy"),
                ):
                    processed = materialize.bootstrap_engine_state(
                        "live",
                        "AAPL",
                        "5m",
                        200,
                        inclusive=True,
                    )

        self.assertEqual(processed, 2)
        self.assertEqual(engine.last_bar_time_ms, 200)
        pb_get_all_records.assert_called_once()

    def test_materialize_prefers_direct_sqlite_latest_rows(self):
        key = ("live", "AAPL", "5m")
        engine = _FakeEngine()
        sqlite_conn = _FakeSqliteConnection(
            [
                {
                    "symbol": "AAPL",
                    "bar_time_ms": 200,
                    "open": 2,
                    "high": 2,
                    "low": 2,
                    "close": 2,
                    "volume": 2,
                    "exchange": "NASDAQ",
                    "us_time": "2026-04-24 16:00:00",
                    "cn_time": "2026-04-25 04:00:00",
                    "session_type": "regular",
                }
            ]
        )
        fake_app = SimpleNamespace(
            MATERIALIZE_MAX_WORKERS=1,
            build_bar_environment_filter=lambda environment, include_legacy_empty=True: f'environment = "{environment}"',
            cfg=_FakeCfg(sqlite_read_enabled=True),
            engines={key: engine},
            logger=SimpleNamespace(debug=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None),
            normalize_bar_environment=lambda row, environment: dict(row, environment=environment),
            normalize_symbols=lambda symbols: [str(symbol).upper() for symbol in symbols],
            pb=SimpleNamespace(get_all_records=mock.Mock(side_effect=AssertionError("PB API should not be read"))),
            signal_gens={},
        )

        def bootstrap(environment, symbol, interval, target_ms, inclusive=True, force_rebuild=False, hydrate_signal_state=True):
            del environment, symbol, interval, inclusive, force_rebuild, hydrate_signal_state
            engine.reset()
            engine.update({"bar_time_ms": target_ms, "close": 2})
            return 1

        with mock.patch("ibkr_compute.api.compute.materialize._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.materialize.bootstrap_engine_state", side_effect=bootstrap) as bootstrap_mock:
                with mock.patch("ibkr_compute.api.compute.materialize.open_pb_sqlite", return_value=sqlite_conn) as open_sqlite:
                    results = materialize.materialize_engines_from_storage(
                        "live",
                        ["AAPL"],
                        "5m",
                    )

        self.assertEqual(results["AAPL"]["last_bar_time_ms"], 200)
        self.assertTrue(results["AAPL"]["is_ready"])
        open_sqlite.assert_called_once_with(readonly=True, timeout=30.0)
        bootstrap_mock.assert_called_once()
        self.assertEqual(bootstrap_mock.call_args.args[:4], ("live", "AAPL", "5m", 200))
        self.assertEqual(len(sqlite_conn.executions), 1)
        sql, params = sqlite_conn.executions[0]
        self.assertIn("FROM ibkr_bars", sql)
        self.assertIn("ORDER BY bar_time_ms DESC", sql)
        self.assertEqual(params[:4], ("5m", "live", "", "AAPL"))

    def test_materialize_falls_back_to_pb_api_when_sqlite_latest_read_fails(self):
        key = ("live", "AAPL", "5m")
        engine = _FakeEngine()
        pb_get_all_records = mock.Mock(
            return_value=[
                {"symbol": "AAPL", "bar_time_ms": 200, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
            ]
        )
        fake_app = SimpleNamespace(
            MATERIALIZE_MAX_WORKERS=1,
            build_bar_environment_filter=lambda environment, include_legacy_empty=True: f'environment = "{environment}"',
            cfg=_FakeCfg(sqlite_read_enabled=True),
            engines={key: engine},
            logger=SimpleNamespace(debug=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None),
            normalize_bar_environment=lambda row, environment: dict(row, environment=environment),
            normalize_symbols=lambda symbols: [str(symbol).upper() for symbol in symbols],
            pb=SimpleNamespace(get_all_records=pb_get_all_records),
            signal_gens={},
        )

        def bootstrap(environment, symbol, interval, target_ms, inclusive=True, force_rebuild=False, hydrate_signal_state=True):
            del environment, symbol, interval, inclusive, force_rebuild, hydrate_signal_state
            engine.reset()
            engine.update({"bar_time_ms": target_ms, "close": 2})
            return 1

        with mock.patch("ibkr_compute.api.compute.materialize._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.materialize.bootstrap_engine_state", side_effect=bootstrap):
                with mock.patch(
                    "ibkr_compute.api.compute.materialize.open_pb_sqlite",
                    side_effect=RuntimeError("sqlite busy"),
                ):
                    results = materialize.materialize_engines_from_storage(
                        "live",
                        ["AAPL"],
                        "5m",
                    )

        self.assertEqual(results["AAPL"]["last_bar_time_ms"], 200)
        self.assertTrue(results["AAPL"]["is_ready"])
        pb_get_all_records.assert_called_once()


if __name__ == "__main__":
    unittest.main()
