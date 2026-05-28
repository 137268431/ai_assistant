import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.system.storage_health import collect_storage_health


def _create_table(conn, name, extra_fields=""):
    conn.execute(
        f"""
        create table {name} (
            id text primary key,
            environment text default '' not null,
            created text default '' not null,
            updated text default '' not null
            {extra_fields}
        )
        """
    )


class StorageHealthTest(unittest.TestCase):
    def test_collect_storage_health_reports_tables_and_retention_lag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "data.db"
            conn = sqlite3.connect(db_path)
            _create_table(
                conn,
                "ibkr_bars",
                ", symbol text default '' not null, interval text default '' not null, bar_time_ms numeric default 0 not null",
            )
            _create_table(
                conn,
                "ibkr_indicators",
                ", symbol text default '' not null, interval text default '' not null, bar_time_ms numeric default 0 not null",
            )
            _create_table(conn, "ibkr_signals", ", bar_time_ms numeric default 0 not null, status text default '' not null")
            _create_table(conn, "ibkr_reverse_signals", ", bar_time_ms numeric default 0 not null")
            _create_table(conn, "ibkr_targets", ", bar_time_ms numeric default 0 not null")
            _create_table(conn, "orders", ", status text default '' not null")
            _create_table(conn, "ibkr_order_details", ", status text default '' not null")
            _create_table(conn, "ibkr_state", ", state_key text default '' not null")
            _create_table(conn, "config", ", key text default '' not null")
            _create_table(conn, "watchlist", ", symbol text default '' not null")
            _create_table(conn, "system_events", ", event_type text default '' not null")
            for name in ("ibkr_bar_integrity", "ibkr_bar_coverage_daily", "ibkr_bar_truth_audit", "ibkr_bar_truth_repair_events"):
                _create_table(conn, name, ", market_date text default '' not null")
            for name in (
                "ibkr_backtest_runs",
                "ibkr_backtest_batches",
                "ibkr_backtest_trades",
                "ibkr_backtest_signals",
                "ibkr_backtest_reverse_signals",
                "ibkr_backtest_targets",
                "ibkr_backtest_daily_selection_cache",
                "ibkr_backtest_indicators",
                "tv_signals",
                "tv_indicators",
                "tv_indicator_audit_snapshots",
            ):
                _create_table(conn, name, ", bar_time_ms numeric default 0 not null")
            conn.execute(
                "create index idx_ibkr_bars_env_interval_bar_time_ms on ibkr_bars(environment, interval, bar_time_ms)"
            )
            conn.execute(
                "insert into ibkr_bars(id, environment, symbol, interval, bar_time_ms, created, updated) values(?, ?, ?, ?, ?, ?, ?)",
                ("bar1", "live", "SPY", "5m", 1600000000000, "2020-09-13 00:00:00", "2020-09-13 00:00:00"),
            )
            conn.execute(
                "insert into orders(id, environment, status, created, updated) values(?, ?, ?, ?, ?)",
                ("order1", "live", "submitted", "2026-05-07 10:00:00", "2026-05-07 10:00:00"),
            )
            conn.execute("analyze")
            conn.commit()
            conn.close()

            with mock.patch.dict(os.environ, {"PB_DB_PATH": str(db_path)}):
                payload = collect_storage_health(
                    "live",
                    config_map={"ibkr_history_retention_days": "30"},
                    force_refresh=True,
                )

        self.assertEqual(payload["environment"], "live")
        self.assertIn(payload["status"], {"warning", "error"})
        self.assertEqual(payload["summary"]["monitored_tables"], 26)
        self.assertTrue(any(table["name"] == "ibkr_bars" for table in payload["tables"]))
        self.assertTrue(any(flag["code"] == "ibkr_bars_retention_lag" for flag in payload["flags"]))
        orders = next(table for table in payload["tables"] if table["name"] == "orders")
        self.assertEqual(orders["status_counts"]["submitted"], 1)

    def test_collect_storage_health_handles_missing_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.db"
            with mock.patch.dict(os.environ, {"PB_DB_PATH": str(db_path)}):
                payload = collect_storage_health("live", force_refresh=True)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["flags"][0]["code"], "pb_db_missing")


if __name__ == "__main__":
    unittest.main()
