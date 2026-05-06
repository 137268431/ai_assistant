import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest import runtime_service
from ibkr_compute.backtest.runtime_service import BacktestService
from ibkr_compute.core.time_utils import ET
from ibkr_compute.market.bar_coverage_daily import (
    build_daily_coverage_row,
    expected_bar_times_for_date,
)


def _bar(symbol, bar_time_ms, *, bad=False):
    return {
        "symbol": symbol,
        "environment": "live",
        "interval": "5m",
        "bar_time_ms": int(bar_time_ms),
        "open": 101 if not bad else 120,
        "high": 102,
        "low": 100,
        "close": 101,
    }


class BarCoverageDailyTests(unittest.TestCase):
    def test_regular_session_missing_bar_is_hard_gap(self):
        expected = expected_bar_times_for_date("2026-05-05", "5m", "regular")
        missing = expected[2]
        row = build_daily_coverage_row(
            symbol="AAPL",
            environment="live",
            market_date="2026-05-05",
            interval="5m",
            session_mode="regular",
            bars=[_bar("AAPL", value) for value in expected if value != missing],
            source="backtest_preflight",
        )

        self.assertEqual(row["expected_count"], 78)
        self.assertEqual(row["missing_count"], 1)
        self.assertEqual(row["status"], "hard_gap")
        self.assertTrue(row["needs_repair"])
        self.assertEqual(row["repair_windows"][0]["start_us"], "2026-05-05 09:40:00")

    def test_extended_session_missing_bar_is_soft_gap(self):
        expected = expected_bar_times_for_date("2026-05-05", "5m", "extended")
        row = build_daily_coverage_row(
            symbol="AAPL",
            environment="live",
            market_date="2026-05-05",
            interval="5m",
            session_mode="extended",
            bars=[_bar("AAPL", value) for value in expected[:-1]],
            source="manual_scan",
        )

        self.assertGreater(row["expected_count"], 0)
        self.assertEqual(row["missing_count"], 1)
        self.assertEqual(row["status"], "soft_gap")
        self.assertFalse(row["hard_gate"])

    def test_early_close_regular_expected_count(self):
        expected = expected_bar_times_for_date("2025-07-03", "5m", "regular")

        self.assertEqual(len(expected), 42)
        self.assertEqual(datetime.fromtimestamp(expected[-1] / 1000, ET).strftime("%H:%M"), "12:55")

    def test_backtest_uses_daily_coverage_fast_path(self):
        first = int(datetime(2026, 5, 5, 9, 30, tzinfo=ET).timestamp() * 1000)
        last = int(datetime(2026, 5, 5, 15, 55, tzinfo=ET).timestamp() * 1000)
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with sqlite3.connect(tmp.name) as conn:
                conn.execute(
                    """
                    CREATE TABLE ibkr_bar_coverage_daily (
                        symbol TEXT,
                        environment TEXT,
                        interval TEXT,
                        session_mode TEXT,
                        market_date TEXT,
                        status TEXT,
                        actual_count INTEGER,
                        duplicate_count INTEGER,
                        bad_ohlc_count INTEGER,
                        first_bar_ms INTEGER,
                        last_bar_ms INTEGER,
                        repair_windows TEXT,
                        missing_windows TEXT,
                        last_checked_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO ibkr_bar_coverage_daily(
                        symbol, environment, interval, session_mode, market_date, status,
                        actual_count, duplicate_count, bad_ohlc_count, first_bar_ms, last_bar_ms,
                        repair_windows, missing_windows, last_checked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("AAPL", "live", "5m", "regular", "2026-05-05", "ok", 78, 0, 0, first, last, "[]", "[]", "now"),
                )
                conn.commit()

            old_path = runtime_service.BACKTEST_SQLITE_PATH
            runtime_service.BACKTEST_SQLITE_PATH = tmp.name
            try:
                summary = BacktestService(None)._symbol_range_coverage_summary(
                    "AAPL",
                    "live",
                    "2026-05-05",
                    "2026-05-05",
                    warmup_bars=0,
                )
            finally:
                runtime_service.BACKTEST_SQLITE_PATH = old_path

        self.assertEqual(summary["diagnostic_source"], "daily_coverage")
        self.assertFalse(summary["needs_backfill"])
        self.assertEqual(summary["daily_coverage"]["covered_trading_days"], 1)

    def test_backtest_rebuilds_missing_daily_rows_from_bars(self):
        expected = expected_bar_times_for_date("2026-05-05", "5m", "regular")
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with sqlite3.connect(tmp.name) as conn:
                conn.execute(
                    """
                    CREATE TABLE ibkr_bar_coverage_daily (
                        id TEXT,
                        environment TEXT,
                        market_date TEXT,
                        symbol TEXT,
                        interval TEXT,
                        session_mode TEXT,
                        status TEXT,
                        hard_gate INTEGER,
                        needs_repair INTEGER,
                        expected_count INTEGER,
                        actual_count INTEGER,
                        missing_count INTEGER,
                        gap_count INTEGER,
                        duplicate_count INTEGER,
                        bad_ohlc_count INTEGER,
                        expected_start_ms INTEGER,
                        expected_end_ms INTEGER,
                        first_bar_ms INTEGER,
                        last_bar_ms INTEGER,
                        last_checked_at TEXT,
                        last_repair_at TEXT,
                        missing_windows TEXT,
                        missing_examples TEXT,
                        repair_windows TEXT,
                        expected_mask_hex TEXT,
                        actual_mask_hex TEXT,
                        missing_mask_hex TEXT,
                        source TEXT,
                        extra TEXT,
                        created TEXT,
                        updated TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE UNIQUE INDEX idx_cov ON ibkr_bar_coverage_daily(environment, market_date, symbol, interval, session_mode)"
                )
                conn.execute(
                    """
                    CREATE TABLE ibkr_bars (
                        symbol TEXT,
                        environment TEXT,
                        interval TEXT,
                        bar_time_ms INTEGER,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO ibkr_bars(symbol, environment, interval, bar_time_ms, open, high, low, close) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [("AAPL", "live", "5m", value, 101, 102, 100, 101) for value in expected],
                )
                conn.commit()

            old_path = runtime_service.BACKTEST_SQLITE_PATH
            runtime_service.BACKTEST_SQLITE_PATH = tmp.name
            try:
                summary = BacktestService(None)._symbol_range_coverage_summary(
                    "AAPL",
                    "live",
                    "2026-05-05",
                    "2026-05-05",
                    warmup_bars=0,
                )
                with sqlite3.connect(tmp.name) as conn:
                    stored = conn.execute("SELECT COUNT(*) FROM ibkr_bar_coverage_daily").fetchone()[0]
            finally:
                runtime_service.BACKTEST_SQLITE_PATH = old_path

        self.assertEqual(summary["diagnostic_source"], "daily_coverage")
        self.assertFalse(summary["needs_backfill"])
        self.assertEqual(summary["daily_coverage"]["rebuilt_dates"], ["2026-05-05"])
        self.assertEqual(stored, 1)


if __name__ == "__main__":
    unittest.main()
