import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.ops.tv_indicator_audit import (
    build_tv_indicator_audit_payload,
    normalize_audit_interval,
    normalize_audit_intervals,
    normalize_audit_symbols,
)


def _connect_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = sqlite3.Row
    return tmp.name, conn


def _create_tables(conn, *, include_tv=True, include_bars=True, include_indicators=True):
    if include_tv:
        conn.execute(
            """
            CREATE TABLE tv_indicator_audit_snapshots (
                id TEXT,
                symbol TEXT,
                exchange TEXT,
                interval TEXT,
                script_tag TEXT,
                us_time TEXT,
                cn_time TEXT,
                bar_time_ms INTEGER,
                bar_index INTEGER,
                extra TEXT,
                environment TEXT,
                created TEXT,
                updated TEXT
            )
            """
        )
    if include_bars:
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
    if include_indicators:
        conn.execute(
            """
            CREATE TABLE ibkr_indicators (
                id TEXT,
                symbol TEXT,
                exchange TEXT,
                interval TEXT,
                script_tag TEXT,
                us_time TEXT,
                cn_time TEXT,
                bar_time_ms INTEGER,
                bar_index INTEGER,
                extra TEXT,
                environment TEXT,
                created TEXT,
                updated TEXT
            )
            """
        )


def _insert_tv(conn, *, close=100.0, extra_patch=None, interval="5", environment="live"):
    extra = {
        "open": 99.5,
        "high": 101.0,
        "low": 99.0,
        "close": close,
        "volume": 12345,
        "session_type": "regular",
        "vwap": 100.25,
        "ema_fast": 100.5,
        "atr_pct": 0.42,
        "ema_bullish": True,
        "dtp_phase": "confirmed",
    }
    extra.update(extra_patch or {})
    conn.execute(
        """
        INSERT INTO tv_indicator_audit_snapshots
        (id, symbol, exchange, interval, script_tag, us_time, cn_time, bar_time_ms, bar_index, extra, environment, created, updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "tv1",
            "SPY",
            "ARCA",
            interval,
            "IAC_test",
            "2026-05-28 09:35",
            "2026-05-28 21:35",
            1779975300000,
            10,
            json.dumps(extra),
            environment,
            "2026-05-28 13:35:00Z",
            "2026-05-28 13:35:00Z",
        ),
    )


def _insert_bar(conn, *, close=100.0, interval="5m", environment="live"):
    conn.execute(
        """
        INSERT INTO ibkr_bars
        (id, symbol, exchange, interval, open, high, low, close, volume, session_type, us_time, cn_time, bar_time_ms, extra, environment, created, updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "bar1",
            "SPY",
            "ARCA",
            interval,
            99.5,
            101.0,
            99.0,
            close,
            12345,
            "regular",
            "2026-05-28 09:35",
            "2026-05-28 21:35",
            1779975300000,
            "{}",
            environment,
            "2026-05-28 13:35:01Z",
            "2026-05-28 13:35:01Z",
        ),
    )


def _insert_indicator(conn, *, extra_patch=None, interval="5", environment="live"):
    extra = {
        "vwap": 100.25,
        "ema_fast": 100.5,
        "atr_pct": 0.42,
        "ema_bullish": True,
        "dtp_phase": "confirmed",
    }
    extra.update(extra_patch or {})
    conn.execute(
        """
        INSERT INTO ibkr_indicators
        (id, symbol, exchange, interval, script_tag, us_time, cn_time, bar_time_ms, bar_index, extra, environment, created, updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ind1",
            "SPY",
            "ARCA",
            interval,
            "IBKR_test",
            "2026-05-28 09:35",
            "2026-05-28 21:35",
            1779975300000,
            10,
            json.dumps(extra),
            environment,
            "2026-05-28 13:35:01Z",
            "2026-05-28 13:35:01Z",
        ),
    )


class TvIndicatorAuditTest(unittest.TestCase):
    def test_normalizers_accept_tradingview_and_storage_intervals(self):
        self.assertEqual(normalize_audit_symbols("spy, qqq SPY"), ["SPY", "QQQ"])
        self.assertEqual(normalize_audit_interval("60").storage_interval, "1h")
        self.assertEqual(normalize_audit_interval("1d").chart_interval, "D")
        self.assertEqual([item.chart_interval for item in normalize_audit_intervals(["5m", "5", "240", "D"])], ["5", "240", "D"])

    def test_passes_when_tv_bar_and_indicator_match_ibkr_with_tolerance(self):
        path, conn = _connect_db()
        try:
            _create_tables(conn)
            _insert_tv(conn, close=100.0)
            _insert_bar(conn, close=100.00005)
            _insert_indicator(conn, extra_patch={"ema_fast": 100.50005, "atr_pct": 0.421})
            conn.commit()

            payload = build_tv_indicator_audit_payload(symbols=["SPY"], intervals=["5m"], environment="live", db_path=path)

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["summary"]["matched_row_count"], 1)
            self.assertEqual(payload["summary"]["mismatch_count"], 0)
            self.assertEqual(payload["rows"][0]["status"], "match")
        finally:
            conn.close()
            Path(path).unlink(missing_ok=True)

    def test_reports_bar_and_indicator_mismatches_and_calls_alert(self):
        path, conn = _connect_db()
        alerts = []
        try:
            _create_tables(conn)
            _insert_tv(conn, close=100.0, extra_patch={"ema_bullish": True})
            _insert_bar(conn, close=100.25)
            _insert_indicator(conn, extra_patch={"ema_fast": 101.0, "ema_bullish": False})
            conn.commit()

            payload = build_tv_indicator_audit_payload(
                symbols="SPY",
                intervals="5",
                environment="live",
                db_path=path,
                emit_alert=alerts.append,
            )

            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "error")
            self.assertGreaterEqual(payload["summary"]["bar_field_mismatch_count"], 1)
            self.assertGreaterEqual(payload["summary"]["indicator_field_mismatch_count"], 2)
            fields = {item["field"] for item in payload["mismatches"]}
            self.assertIn("close", fields)
            self.assertIn("ema_fast", fields)
            self.assertIn("ema_bullish", fields)
            self.assertEqual(alerts[0]["type"], "tv_indicator_audit")
        finally:
            conn.close()
            Path(path).unlink(missing_ok=True)

    def test_unavailable_when_tv_snapshots_missing(self):
        path, conn = _connect_db()
        try:
            _create_tables(conn)
            conn.commit()

            payload = build_tv_indicator_audit_payload(symbols=["SPY"], intervals=["5"], environment="live", db_path=path)

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "unavailable")
            self.assertEqual(payload["summary"]["error"], "no_tv_snapshots")
        finally:
            conn.close()
            Path(path).unlink(missing_ok=True)

    def test_unavailable_when_required_table_missing(self):
        path, conn = _connect_db()
        try:
            _create_tables(conn, include_indicators=False)
            conn.commit()

            payload = build_tv_indicator_audit_payload(symbols=["SPY"], intervals=["5"], environment="live", db_path=path)

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "unavailable")
            self.assertIn("ibkr_indicators", payload["summary"]["missing_tables"])
        finally:
            conn.close()
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
