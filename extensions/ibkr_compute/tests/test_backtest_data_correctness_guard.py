import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest import request_utils
from ibkr_compute.backtest.runtime_service import BacktestService


class BacktestDataCorrectnessGuardTest(unittest.TestCase):
    def _request(self, **overrides):
        payload = {
            "name": "truth guard test",
            "symbol_source": "manual",
            "symbols": "AAPL,NVDA",
            "date_from": "2026-04-01",
            "date_to": "2026-04-01",
            "source_environment": "live",
            "session_mode": "regular",
            "initial_capital": 10000,
            "execution_model": "portfolio_stream",
            "compare_with_tv": False,
            "compare_tv_signals": False,
            "persist_backtest_indicators": False,
        }
        payload.update(overrides)
        return request_utils.normalize_request(payload)

    def _create_truth_db(self, path: str, truth_rows: list[tuple], integrity_rows: list[tuple] | None = None):
        with sqlite3.connect(path) as conn:
            conn.execute(
                """
                CREATE TABLE ibkr_bar_integrity (
                    environment TEXT,
                    market_date TEXT,
                    symbol TEXT,
                    interval TEXT,
                    status TEXT,
                    needs_repair INTEGER,
                    gap_count INTEGER,
                    duplicate_count INTEGER,
                    bad_ohlc_count INTEGER,
                    last_scan_at TEXT,
                    updated TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE ibkr_bar_truth_audit (
                    environment TEXT,
                    market_date TEXT,
                    symbol TEXT,
                    interval TEXT,
                    status TEXT,
                    matched_bar_count INTEGER,
                    missing_stored_bar_count INTEGER,
                    missing_ibkr_bar_count INTEGER,
                    bar_mismatch_count INTEGER,
                    last_checked_at TEXT,
                    updated TEXT
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO ibkr_bar_truth_audit (
                    environment, market_date, symbol, interval, status, matched_bar_count,
                    missing_stored_bar_count, missing_ibkr_bar_count, bar_mismatch_count,
                    last_checked_at, updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                truth_rows,
            )
            conn.executemany(
                """
                INSERT INTO ibkr_bar_integrity (
                    environment, market_date, symbol, interval, status, needs_repair,
                    gap_count, duplicate_count, bad_ohlc_count, last_scan_at, updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                integrity_rows
                if integrity_rows is not None
                else [
                    (row[0], row[1], row[2], row[3], "ok", 0, 0, 0, 0, row[9], row[10])
                    for row in truth_rows
                ],
            )
            conn.commit()

    def test_truth_proof_gate_allows_green_rows(self):
        service = BacktestService(None)
        rows = [
            ("live", "2026-04-01", "AAPL", "5m", "ok", 78, 0, 0, 0, "2026-04-01 20:00:00", "2026-04-01 20:00:00"),
            ("live", "2026-04-01", "NVDA", "5m", "ok", 78, 0, 0, 0, "2026-04-01 20:00:00", "2026-04-01 20:00:00"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "data.db")
            self._create_truth_db(db_path, rows)
            with mock.patch("ibkr_compute.backtest.runtime_service.BACKTEST_SQLITE_PATH", db_path):
                result = service._build_backtest_truth_proof_gate(["AAPL", "NVDA"], self._request())

        self.assertTrue(result["ok"])
        self.assertEqual("green", result["status"])
        self.assertEqual(2, result["audited_pair_count"])

    def test_truth_proof_gate_blocks_missing_or_bad_rows(self):
        service = BacktestService(None)
        rows = [
            ("live", "2026-04-01", "AAPL", "5m", "ok", 78, 0, 0, 0, "2026-04-01 20:00:00", "2026-04-01 20:00:00"),
            ("live", "2026-04-01", "NVDA", "5m", "error", 77, 0, 0, 1, "2026-04-01 20:00:00", "2026-04-01 20:00:00"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "data.db")
            self._create_truth_db(db_path, rows)
            with mock.patch("ibkr_compute.backtest.runtime_service.BACKTEST_SQLITE_PATH", db_path):
                result = service._build_backtest_truth_proof_gate(["AAPL", "NVDA", "MSFT"], self._request(symbols="AAPL,NVDA,MSFT"))

        self.assertFalse(result["ok"])
        self.assertEqual("red", result["status"])
        self.assertEqual(1, result["bad_pair_count"])
        self.assertEqual(2, result["missing_pair_count"])
        self.assertEqual(1, result["missing_truth_pair_count"])
        self.assertEqual(1, result["missing_integrity_pair_count"])
        self.assertEqual("NVDA", result["bad_examples"][0]["symbol"])
        self.assertEqual("MSFT", result["missing_examples"][0]["symbol"])

    def test_truth_proof_gate_can_be_disabled_explicitly(self):
        service = BacktestService(None)
        result = service._build_backtest_truth_proof_gate(
            ["AAPL"],
            self._request(backtest_require_truth_proof=False),
        )

        self.assertTrue(result["ok"])
        self.assertEqual("disabled", result["status"])


if __name__ == "__main__":
    unittest.main()
