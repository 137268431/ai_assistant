import json
import os
import sqlite3
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("PB_RETRY_ATTEMPTS", "1")
os.environ.setdefault("PB_RETRY_BACKOFF_SECONDS", "0.01")

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    fake_flask = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, name, *args, **kwargs):
            self.name = name

        def route(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

        def add_url_rule(self, *args, **kwargs):
            return None

    fake_flask.Flask = _FakeFlask
    fake_flask.Response = object
    fake_flask.jsonify = lambda payload: payload
    fake_flask.redirect = lambda url, code=302: {"redirect": url, "code": code}
    fake_flask.request = types.SimpleNamespace(
        get_json=lambda silent=True: {},
        get_data=lambda cache=True: b"",
        args={},
        headers={},
        method="GET",
        values={},
    )
    sys.modules["flask"] = fake_flask

from ibkr_compute.api.compute import request as compute_request
from ibkr_compute.api.compute import rollup as compute_rollup
from ibkr_compute.api.compute.runtime_state import timing as compute_timing


class _FakeConfig:
    def __init__(self, bools=None, ints=None):
        self.bools = dict(bools or {})
        self.ints = dict(ints or {})

    def get_bool_for_environment(self, key, environment, fallback):
        return self.bools.get(key, fallback)

    def get_int_for_environment(self, key, environment, fallback):
        return self.ints.get(key, fallback)

    def get_float_for_environment(self, key, environment, fallback):
        return fallback


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        return None


class _ReusableSqliteConn:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, *args, **kwargs):
        return self.conn.execute(*args, **kwargs)

    def close(self):
        return None


def _normalize_symbols(symbols):
    if isinstance(symbols, str):
        symbols = [symbols]
    normalized = []
    seen = set()
    for item in symbols or []:
        symbol = str(item or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            normalized.append(symbol)
    return normalized


def _build_fake_app():
    fake_app = mock.Mock()
    fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
    fake_app.ROLLUP_BATCH_SIZE = 100
    fake_app.last_interval_fetch_ms = {}
    fake_app.cfg = _FakeConfig()
    fake_app.normalize_symbols.side_effect = _normalize_symbols
    fake_app.build_symbol_filter.side_effect = lambda symbols: (
        "(" + " || ".join(f'symbol = "{symbol}"' for symbol in _normalize_symbols(symbols)) + ")"
        if _normalize_symbols(symbols)
        else ""
    )
    fake_app.build_bar_environment_filter.side_effect = lambda environment, include_legacy_empty=True: (
        '(environment = "live" || environment = "")'
        if include_legacy_empty and str(environment or "").lower() == "live"
        else f'environment = "{str(environment or "live").lower()}"'
    )
    fake_app.normalize_bar_environment.side_effect = lambda bar, environment: {
        **dict(bar),
        "environment": str(environment or "live").strip().lower() or "live",
    }
    return fake_app


def _sqlite_bars(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE ibkr_bars (
            id TEXT,
            symbol TEXT,
            exchange TEXT,
            interval TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            session_type TEXT,
            us_time TEXT,
            cn_time TEXT,
            bar_time_ms INTEGER,
            extra TEXT,
            environment TEXT,
            created TEXT,
            updated TEXT
        )
        """
    )
    for row in rows:
        payload = {
            "id": row.get("id", f"row_{row.get('symbol', 'AAPL')}_{row.get('bar_time_ms', 0)}"),
            "symbol": row.get("symbol", "AAPL"),
            "exchange": row.get("exchange", "SMART"),
            "interval": row.get("interval", "5m"),
            "open": row.get("open", 100.0),
            "high": row.get("high", 101.0),
            "low": row.get("low", 99.0),
            "close": row.get("close", 100.5),
            "volume": row.get("volume", 1000),
            "session_type": row.get("session_type", "regular"),
            "us_time": row.get("us_time", ""),
            "cn_time": row.get("cn_time", ""),
            "bar_time_ms": row.get("bar_time_ms", 0),
            "extra": row.get("extra", "{}") if isinstance(row.get("extra", "{}"), str) else json.dumps(row.get("extra", {})),
            "environment": row.get("environment", "live"),
            "created": row.get("created", ""),
            "updated": row.get("updated", ""),
        }
        conn.execute(
            """
            INSERT INTO ibkr_bars (
                id, symbol, exchange, interval, open, high, low, close, volume,
                session_type, us_time, cn_time, bar_time_ms, extra, environment,
                created, updated
            ) VALUES (
                :id, :symbol, :exchange, :interval, :open, :high, :low, :close, :volume,
                :session_type, :us_time, :cn_time, :bar_time_ms, :extra, :environment,
                :created, :updated
            )
            """,
            payload,
        )
    conn.commit()
    return conn


def _base_bar(symbol="AAPL", bar_time_ms=1713797100000):
    return {
        "symbol": symbol,
        "exchange": "SMART",
        "interval": "5m",
        "environment": "live",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000,
        "session_type": "regular",
        "us_time": "2026-04-22 09:35:00",
        "cn_time": "2026-04-22 21:35:00",
        "bar_time_ms": bar_time_ms,
        "extra": {},
    }


def _base_bar_series(symbols=("AAPL",), start_ms=1713796800000, count=12):
    rows = []
    for index in range(count):
        for symbol in symbols:
            price = 100.0 + index
            rows.append(
                {
                    **_base_bar(symbol, start_ms + (index * 5 * 60 * 1000)),
                    "open": price,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price + 0.5,
                    "volume": 1000 + index,
                }
            )
    return rows


def _compact_written_bars(batches):
    rows = [bar for batch in batches for bar in batch]
    compact = []
    for bar in rows:
        extra = dict(bar.get("extra") or {})
        compact.append(
            (
                str(bar.get("symbol") or ""),
                str(bar.get("interval") or ""),
                int(bar.get("bar_time_ms", 0) or 0),
                float(bar.get("open", 0) or 0),
                float(bar.get("high", 0) or 0),
                float(bar.get("low", 0) or 0),
                float(bar.get("close", 0) or 0),
                float(bar.get("volume", 0) or 0),
                extra.get("source"),
                extra.get("component_interval"),
                int(extra.get("component_count", 0) or 0),
                int(extra.get("last_component_bar_time_ms", 0) or 0),
            )
        )
    return sorted(compact)


class ComputeRollupPlanTest(unittest.TestCase):
    def test_canonical_close_uses_incremental_rollup_for_targeted_symbols(self):
        fake_app = mock.Mock()
        fake_app.SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
        fake_app.DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.INTERVALS = ["5m", "15m", "30m", "1h", "4h", "1d"]
        fake_app.SIGNAL_SUPPRESSED_COMPUTE_SOURCES = {
            "history_repair",
            "history_rebuild",
            "recompute",
            "targeted_recompute",
        }
        fake_app.normalize_symbols.return_value = ["AAPL", "MSFT"]
        fake_app.cfg.has_environment_override.return_value = False
        fake_app.cfg.get_bool_for_environment.return_value = True

        with mock.patch.object(compute_request, "_api_app", return_value=fake_app):
            plan = compute_request.build_compute_execution_plan(
                {
                    "source": "canonical_close",
                    "environments": ["live"],
                    "symbols": ["AAPL", "MSFT"],
                }
            )

        self.assertTrue(plan["targeted_rollup"])
        self.assertTrue(plan["force_rollup"])
        self.assertTrue(plan["incremental_rollup"])
        self.assertEqual(plan["rollup_intervals"], ["15m", "30m", "1h", "4h"])

    def test_history_repair_keeps_full_targeted_rollup(self):
        fake_app = mock.Mock()
        fake_app.SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
        fake_app.DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.INTERVALS = ["5m", "15m", "30m", "1h", "4h", "1d"]
        fake_app.SIGNAL_SUPPRESSED_COMPUTE_SOURCES = {
            "history_repair",
            "history_rebuild",
            "recompute",
            "targeted_recompute",
        }
        fake_app.normalize_symbols.return_value = ["AAPL"]
        fake_app.cfg.has_environment_override.return_value = False
        fake_app.cfg.get_bool_for_environment.return_value = True

        with mock.patch.object(compute_request, "_api_app", return_value=fake_app):
            plan = compute_request.build_compute_execution_plan(
                {
                    "source": "history_repair",
                    "environments": ["live"],
                    "symbols": ["AAPL"],
                }
            )

        self.assertTrue(plan["targeted_rollup"])
        self.assertTrue(plan["force_rollup"])
        self.assertFalse(plan["incremental_rollup"])
        self.assertEqual(plan["rollup_intervals"], ["15m", "30m", "1h", "4h", "1d"])

    def test_scheduler_compute_defaults_to_5m_without_rollup(self):
        fake_app = mock.Mock()
        fake_app.SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
        fake_app.DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.INTERVALS = ["5m", "15m", "30m", "1h", "4h", "1d"]
        fake_app.SIGNAL_SUPPRESSED_COMPUTE_SOURCES = set()
        fake_app.normalize_symbols.return_value = []
        fake_app.cfg.has_environment_override.return_value = False
        fake_app.cfg.get_bool_for_environment.return_value = True

        with mock.patch.object(compute_request, "_api_app", return_value=fake_app):
            plan = compute_request.build_compute_execution_plan(
                {
                    "source": "ibkr_scheduler",
                    "environments": ["live"],
                }
            )

        self.assertEqual(plan["intervals"], ["5m"])
        self.assertEqual(plan["rollup_intervals"], [])

    def test_empty_rollup_interval_override_stays_empty(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]

        self.assertEqual(compute_rollup._normalize_target_intervals(fake_app, []), [])
        self.assertEqual(
            compute_rollup._normalize_target_intervals(fake_app, None),
            ["15m", "30m", "1h", "4h", "1d"],
        )


class IncrementalRollupWindowTest(unittest.TestCase):
    def test_fetch_since_falls_back_to_processed_cursor_when_interval_fetch_empty(self):
        fake_app = mock.Mock()
        fake_app.last_interval_fetch_ms = {}
        fake_app.last_processed_ms = {
            ("live", "AAPL", "5m"): 1777056600000,
            ("live", "MSFT", "5m"): 1777056300000,
            ("paper", "AAPL", "5m"): 1777057200000,
            ("live", "AAPL", "15m"): 1777057200000,
        }

        with mock.patch.object(compute_timing, "_api_app", return_value=fake_app):
            since_ms = compute_timing.get_fetch_since_ms("live", "5m")

        self.assertEqual(since_ms, 1777056600000 - (2 * 5 * 60 * 1000))

    def test_recent_rollup_respects_selected_intervals(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.last_interval_fetch_ms = {("live", "5m"): 1776278100000}

        with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app):
            since_ms = compute_rollup._recent_rollup_since_ms(
                "live",
                ["AAPL", "MSFT"],
                intervals=["15m", "30m", "1h", "4h"],
            )

        self.assertEqual(since_ms, 1776278100000 - (2 * 4 * 60 * 60 * 1000))

    def test_incremental_due_intervals_only_keep_closed_higher_timeframes(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]

        with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app):
            self.assertEqual(
                compute_rollup._incremental_due_intervals(
                    1776794700000,
                    intervals=["15m", "30m", "1h", "4h"],
                ),
                [],
            )
            self.assertEqual(
                compute_rollup._incremental_due_intervals(
                    1776795300000,
                    intervals=["15m", "30m", "1h", "4h"],
                ),
                ["15m"],
            )
            self.assertEqual(
                compute_rollup._incremental_due_intervals(
                    1776798000000,
                    intervals=["15m", "30m", "1h", "4h"],
                ),
                ["15m", "30m", "1h"],
            )

    def test_incremental_targeted_rollup_skips_when_no_higher_interval_closes(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.normalize_symbols.return_value = ["AAPL"]

        with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_rollup, "_latest_targeted_5m_bar_ms", return_value=1776794700000), \
                mock.patch.object(compute_rollup, "rebuild_higher_timeframe_bars") as rebuild:
            results = compute_rollup.ensure_higher_timeframe_bars(
                ["live"],
                force=True,
                symbols=["AAPL"],
                incremental=True,
                intervals=["15m", "30m", "1h", "4h"],
            )

        self.assertEqual(
            results["live"],
            {
                "skipped": True,
                "reason": "no_due_intervals",
                "written": 0,
                "errors": 0,
                "intervals": [],
            },
        )
        rebuild.assert_not_called()

    def test_incremental_targeted_rollup_only_rebuilds_due_intervals(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.normalize_symbols.return_value = ["AAPL", "MSFT"]

        with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_rollup, "_latest_targeted_5m_bar_ms", return_value=1776795300000), \
                mock.patch.object(compute_rollup, "_recent_rollup_since_ms", return_value=1776793500000) as since_mock, \
                mock.patch.object(
                    compute_rollup,
                    "rebuild_higher_timeframe_bars",
                    return_value={"processed_5m": 24, "written": 48, "errors": 0},
                ) as rebuild:
            results = compute_rollup.ensure_higher_timeframe_bars(
                ["live"],
                force=True,
                symbols=["AAPL", "MSFT"],
                incremental=True,
                intervals=["15m", "30m", "1h", "4h"],
            )

        since_mock.assert_called_once_with(
            "live",
            ["AAPL", "MSFT"],
            intervals=["15m"],
        )
        rebuild.assert_called_once_with(
            "live",
            symbols=["AAPL", "MSFT"],
            intervals=["15m"],
            since_ms=1776793500000,
        )
        self.assertTrue(results["live"]["targeted"])
        self.assertTrue(results["live"]["incremental"])


class RollupDirectSqliteTest(unittest.TestCase):
    def test_latest_and_interval_reads_prefer_direct_sqlite(self):
        fake_app = _build_fake_app()
        fake_app.pb.get_records.side_effect = AssertionError("PB API should not be used")
        fake_app.pb.get_all_records.side_effect = AssertionError("PB API should not be used")
        conn = _sqlite_bars(
            [
                _base_bar("AAPL", 1713796800000),
                _base_bar("AAPL", 1713797100000),
                _base_bar("MSFT", 1713797400000),
                {**_base_bar("AAPL", 1713796800000), "interval": "15m", "environment": ""},
            ]
        )

        try:
            with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                    mock.patch.object(compute_rollup, "open_pb_sqlite", return_value=_ReusableSqliteConn(conn)), \
                    mock.patch.object(compute_rollup, "get_fetch_since_ms", return_value=1713797000000):
                self.assertEqual(
                    compute_rollup._latest_targeted_5m_bar_ms("live", ["AAPL"]),
                    1713797100000,
                )
                self.assertTrue(compute_rollup.has_interval_bars("live", "15m", symbols=["AAPL"]))
                rows = compute_rollup.fetch_interval_bars("live", "5m", symbols=["AAPL"])

            self.assertEqual([row["bar_time_ms"] for row in rows], [1713797100000])
            self.assertEqual(fake_app.last_interval_fetch_ms[("live", "5m")], 1713797100000)
            fake_app.pb.get_records.assert_not_called()
            fake_app.pb.get_all_records.assert_not_called()
        finally:
            conn.close()

    def test_fetch_interval_bars_does_not_fallback_to_api_on_empty_sqlite_result(self):
        fake_app = _build_fake_app()
        fake_app.pb.get_all_records.side_effect = AssertionError("empty SQLite result should not hit PB API")
        conn = _sqlite_bars([])

        try:
            with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                    mock.patch.object(compute_rollup, "open_pb_sqlite", return_value=_ReusableSqliteConn(conn)), \
                    mock.patch.object(compute_rollup, "get_fetch_since_ms", return_value=1713797000000):
                rows = compute_rollup.fetch_interval_bars("live", "5m", symbols=["AAPL"])

            self.assertEqual(rows, [])
            fake_app.pb.get_all_records.assert_not_called()
        finally:
            conn.close()

    def test_rebuild_reads_from_sqlite_and_writes_rollup_batch_directly(self):
        fake_app = _build_fake_app()
        fake_app.ROLLUP_BATCH_SIZE = 1
        fake_app.pb.get_all_records.side_effect = AssertionError("PB API read should not be used")
        fake_app.pb.upsert_bars.side_effect = AssertionError("PB API write should not be used")
        read_conn = _sqlite_bars(
            [
                _base_bar("AAPL", 1713796800000),
                _base_bar("AAPL", 1713797100000),
                _base_bar("AAPL", 1713797700000),
            ]
        )
        written_batches = []

        def fake_open_pb_sqlite(*, readonly=False, timeout=30.0):
            return _ReusableSqliteConn(read_conn) if readonly else _FakeConn()

        def fake_upsert_bars(conn, batch):
            written_batches.append(list(batch))
            return len(batch)

        try:
            with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                    mock.patch.object(compute_rollup, "open_pb_sqlite", side_effect=fake_open_pb_sqlite), \
                    mock.patch.object(compute_rollup, "upsert_bars", side_effect=fake_upsert_bars):
                result = compute_rollup.rebuild_higher_timeframe_bars(
                    "live",
                    symbols=["AAPL"],
                    intervals=["15m"],
                )

            self.assertEqual(result["processed_5m"], 3)
            self.assertEqual(result["written"], 1)
            self.assertEqual(result["errors"], 0)
            self.assertEqual(len(written_batches), 1)
            self.assertEqual(written_batches[0][0]["interval"], "15m")
            fake_app.pb.get_all_records.assert_not_called()
            fake_app.pb.upsert_bars.assert_not_called()
        finally:
            read_conn.close()

    def test_parallel_rollup_matches_serial_output(self):
        intervals = ["15m", "30m", "1h", "4h", "1d"]
        rows = _base_bar_series(symbols=("AAPL", "MSFT"), count=600)
        read_conn = _sqlite_bars(rows)

        def run_rebuild(parallel_enabled):
            fake_app = _build_fake_app()
            fake_app.ROLLUP_BATCH_SIZE = 50
            fake_app.cfg = _FakeConfig(
                bools={"ibkr_rollup_parallel_enabled": parallel_enabled},
                ints={"ibkr_rollup_max_workers": 5},
            )
            written_batches = []
            readonly_calls = 0

            def fake_open_pb_sqlite(*, readonly=False, timeout=30.0):
                nonlocal readonly_calls
                if readonly:
                    readonly_calls += 1
                    return _ReusableSqliteConn(read_conn)
                return _FakeConn()

            def fake_upsert_bars(conn, batch):
                written_batches.append(list(batch))
                return len(batch)

            with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                    mock.patch.object(compute_rollup, "open_pb_sqlite", side_effect=fake_open_pb_sqlite), \
                    mock.patch.object(compute_rollup, "upsert_bars", side_effect=fake_upsert_bars):
                result = compute_rollup.rebuild_higher_timeframe_bars(
                    "live",
                    symbols=["AAPL", "MSFT"],
                    intervals=intervals,
                )
            return result, written_batches, readonly_calls

        try:
            serial_result, serial_batches, serial_reads = run_rebuild(False)
            parallel_result, parallel_batches, parallel_reads = run_rebuild(True)
        finally:
            read_conn.close()

        self.assertFalse(serial_result["parallel"])
        self.assertTrue(parallel_result["parallel"])
        self.assertEqual(parallel_result["workers"], 5)
        self.assertEqual(serial_reads, 1)
        self.assertEqual(parallel_reads, 1)
        self.assertEqual(parallel_result["processed_5m"], serial_result["processed_5m"])
        self.assertEqual(parallel_result["written"], serial_result["written"])
        self.assertEqual(parallel_result["errors"], 0)
        self.assertEqual(
            _compact_written_bars(parallel_batches),
            _compact_written_bars(serial_batches),
        )

    def test_parallel_rollup_serializes_writes(self):
        fake_app = _build_fake_app()
        fake_app.ROLLUP_BATCH_SIZE = 20
        fake_app.cfg = _FakeConfig(ints={"ibkr_rollup_max_workers": 5})
        read_conn = _sqlite_bars(_base_bar_series(count=600))
        active_writes = 0
        max_active_writes = 0
        write_count = 0
        active_lock = threading.Lock()

        def fake_open_pb_sqlite(*, readonly=False, timeout=30.0):
            return _ReusableSqliteConn(read_conn) if readonly else _FakeConn()

        def fake_upsert_bars(conn, batch):
            nonlocal active_writes, max_active_writes, write_count
            with active_lock:
                active_writes += 1
                max_active_writes = max(max_active_writes, active_writes)
            try:
                time.sleep(0.002)
                write_count += 1
                return len(batch)
            finally:
                with active_lock:
                    active_writes -= 1

        try:
            with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                    mock.patch.object(compute_rollup, "open_pb_sqlite", side_effect=fake_open_pb_sqlite), \
                    mock.patch.object(compute_rollup, "upsert_bars", side_effect=fake_upsert_bars):
                result = compute_rollup.rebuild_higher_timeframe_bars(
                    "live",
                    symbols=["AAPL"],
                    intervals=["15m", "30m", "1h", "4h", "1d"],
                )
        finally:
            read_conn.close()

        self.assertTrue(result["parallel"])
        self.assertGreater(write_count, 1)
        self.assertEqual(max_active_writes, 1)

    def test_single_interval_rollup_stays_serial(self):
        fake_app = _build_fake_app()
        fake_app.cfg = _FakeConfig(ints={"ibkr_rollup_max_workers": 5})
        read_conn = _sqlite_bars(_base_bar_series(count=12))

        def fake_open_pb_sqlite(*, readonly=False, timeout=30.0):
            return _ReusableSqliteConn(read_conn) if readonly else _FakeConn()

        try:
            with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                    mock.patch.object(compute_rollup, "open_pb_sqlite", side_effect=fake_open_pb_sqlite), \
                    mock.patch.object(compute_rollup, "upsert_bars", return_value=1):
                result = compute_rollup.rebuild_higher_timeframe_bars(
                    "live",
                    symbols=["AAPL"],
                    intervals=["15m"],
                )
        finally:
            read_conn.close()

        self.assertFalse(result["parallel"])
        self.assertEqual(result["workers"], 1)
        self.assertEqual(result["interval_results"], {})

    def test_rebuild_falls_back_to_pb_when_direct_sqlite_read_fails(self):
        fake_app = _build_fake_app()
        fake_app.pb.get_all_records.return_value = []

        with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_rollup, "open_pb_sqlite", side_effect=RuntimeError("sqlite unavailable")):
            result = compute_rollup.rebuild_higher_timeframe_bars(
                "live",
                symbols=["AAPL"],
                intervals=["15m"],
            )

        self.assertEqual(result["processed_5m"], 0)
        fake_app.pb.get_all_records.assert_called_once()


if __name__ == "__main__":
    unittest.main()
