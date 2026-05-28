import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.ops.truth_repair import (
    TRUTH_REPAIR_ACTION_DELETE,
    TRUTH_REPAIR_ACTION_REPLACE,
    TRUTH_REPAIR_ACTION_UPSERT,
    apply_truth_repair_plan,
    build_truth_repair_payload,
    build_truth_repair_plan,
    confirm_delete_actions,
    truth_summary_ok,
)


def compare_payload():
    return {
        "comparison": {
            "summary": {
                "matched_bar_count": 1,
                "missing_stored_bar_count": 1,
                "missing_ibkr_bar_count": 1,
                "bar_mismatch_count": 1,
            },
            "timeline": [
                {
                    "bar_time_ms": 100,
                    "us_time": "2026-05-27 09:30:00",
                    "status": {"bar": "missing_stored"},
                    "stored": {"bar": None},
                    "ibkr": {"bar": {"bar_time_ms": 100, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100}},
                    "diff": {"bar": {"fields": []}},
                },
                {
                    "bar_time_ms": 200,
                    "us_time": "2026-05-27 09:35:00",
                    "status": {"bar": "mismatch"},
                    "stored": {"bar": {"bar_time_ms": 200, "open": 20, "high": 21, "low": 19, "close": 20, "volume": 100}},
                    "ibkr": {"bar": {"bar_time_ms": 200, "open": 20, "high": 21, "low": 19, "close": 20.5, "volume": 100}},
                    "diff": {"bar": {"fields": ["close"]}},
                },
                {
                    "bar_time_ms": 300,
                    "us_time": "2026-05-27 09:40:00",
                    "status": {"bar": "missing_ibkr"},
                    "stored": {"bar": {"bar_time_ms": 300, "open": 30, "high": 31, "low": 29, "close": 30, "volume": 100}},
                    "ibkr": {"bar": None},
                    "diff": {"bar": {"fields": []}},
                },
                {
                    "bar_time_ms": 400,
                    "us_time": "2026-05-27 09:45:00",
                    "status": {"bar": "match"},
                    "stored": {"bar": {"bar_time_ms": 400}},
                    "ibkr": {"bar": {"bar_time_ms": 400}},
                    "diff": {"bar": {"fields": []}},
                },
            ],
        }
    }


def create_repair_sqlite(path: str, *, with_events: bool = False):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ibkr_bars (
            id TEXT PRIMARY KEY,
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
            updated TEXT,
            UNIQUE(symbol, interval, bar_time_ms, environment)
        )
        """
    )
    conn.execute("CREATE TABLE ibkr_indicators (environment TEXT, symbol TEXT, interval TEXT, bar_time_ms INTEGER)")
    conn.execute(
        "CREATE TABLE ibkr_signals (environment TEXT, symbol TEXT, interval TEXT, bar_time_ms INTEGER, status TEXT, signal_id TEXT)"
    )
    conn.execute("CREATE TABLE orders (environment TEXT, symbol TEXT, signal_id TEXT)")
    if with_events:
        conn.execute(
            """
            CREATE TABLE ibkr_bar_truth_repair_events (
                id TEXT PRIMARY KEY,
                operation_id TEXT,
                environment TEXT,
                market_date TEXT,
                symbol TEXT,
                interval TEXT,
                bar_time_ms INTEGER,
                us_time TEXT,
                action TEXT,
                reason TEXT,
                fields TEXT,
                old_bar TEXT,
                new_bar TEXT,
                applied INTEGER,
                verified INTEGER,
                error TEXT,
                created TEXT,
                updated TEXT
            )
            """
        )
    conn.commit()
    conn.close()


def patched_sqlite_opener(path: str):
    def opener(*, readonly=False, timeout=30.0):
        uri = f"file:{path}?mode=ro" if readonly else path
        conn = sqlite3.connect(uri, uri=readonly, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn

    return opener


class TruthRepairHelpersTest(unittest.TestCase):
    def test_build_truth_repair_plan_classifies_bar_actions(self):
        plan = build_truth_repair_plan(
            compare_payload(),
            operation_id="op-1",
            environment="live",
            market_date="2026-05-27",
            symbol="AAPL",
            interval="5m",
        )

        self.assertEqual(3, plan["summary"]["total"])
        self.assertEqual(
            {
                TRUTH_REPAIR_ACTION_UPSERT: 1,
                TRUTH_REPAIR_ACTION_REPLACE: 1,
                TRUTH_REPAIR_ACTION_DELETE: 1,
            },
            plan["summary"]["action_counts"],
        )
        self.assertTrue(plan["summary"]["requires_delete_extra_bars"])
        self.assertEqual(["close"], plan["items"][1]["fields"])

    def test_confirm_delete_actions_requires_second_missing_ibkr(self):
        plan = build_truth_repair_plan(
            compare_payload(),
            operation_id="op-1",
            environment="live",
            market_date="2026-05-27",
            symbol="AAPL",
            interval="5m",
        )
        confirmed = confirm_delete_actions(
            plan,
            {
                "comparison": {
                    "timeline": [
                        {"bar_time_ms": 300, "status": {"bar": "match"}},
                    ]
                }
            },
        )

        delete_item = [item for item in confirmed["items"] if item["action"] == TRUTH_REPAIR_ACTION_DELETE][0]
        self.assertFalse(delete_item["delete_confirmed"])
        self.assertEqual("delete_not_confirmed_by_refetch", delete_item["error"])

        confirmed = confirm_delete_actions(
            plan,
            {
                "comparison": {
                    "timeline": [
                        {"bar_time_ms": 300, "status": {"bar": "missing_ibkr"}},
                    ]
                }
            },
        )
        delete_item = [item for item in confirmed["items"] if item["action"] == TRUTH_REPAIR_ACTION_DELETE][0]
        self.assertTrue(delete_item["delete_confirmed"])

    def test_truth_summary_ok_requires_zero_bar_errors_and_matches(self):
        self.assertTrue(truth_summary_ok({"matched_bar_count": 78}))
        self.assertFalse(truth_summary_ok({"matched_bar_count": 78, "bar_mismatch_count": 1}))
        self.assertFalse(truth_summary_ok({"matched_bar_count": 0}))

    def test_final_proof_red_when_final_compare_has_zero_matched_bars(self):
        service = SimpleNamespace(pb=SimpleNamespace(upsert_bar_truth_audit_items=lambda *_args, **_kwargs: {}))
        empty_compare = {
            "comparison": {
                "summary": {
                    "matched_bar_count": 0,
                    "missing_stored_bar_count": 0,
                    "missing_ibkr_bar_count": 0,
                    "bar_mismatch_count": 0,
                },
                "timeline": [],
                "mismatch_examples": [],
            },
            "meta": {},
        }

        with mock.patch(
            "ibkr_compute.api.ops.truth_repair.build_bar_truth_compare_payload",
            return_value=empty_compare,
        ):
            result = build_truth_repair_payload(
                service=service,
                symbols=["AAPL"],
                environment="live",
                market_date="2026-05-27",
                operation_id="op-empty",
                apply_changes=False,
                persist_truth=False,
            )

        self.assertFalse(result["ok"])
        self.assertEqual("red", result["proof_status"])
        self.assertEqual(["AAPL"], result["blocked_symbols"])

    def test_apply_skips_event_write_when_repair_event_table_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "data.db")
            create_repair_sqlite(db_path, with_events=False)
            plan = {
                "items": [
                    {
                        "operation_id": "op-1",
                        "environment": "live",
                        "market_date": "2026-05-27",
                        "symbol": "AAPL",
                        "interval": "5m",
                        "bar_time_ms": 100,
                        "action": TRUTH_REPAIR_ACTION_UPSERT,
                        "new_bar": {
                            "bar_time_ms": 100,
                            "open": 10,
                            "high": 11,
                            "low": 9,
                            "close": 10.5,
                            "volume": 1000,
                        },
                    }
                ]
            }
            with mock.patch(
                "ibkr_compute.api.ops.truth_repair.open_pb_sqlite",
                patched_sqlite_opener(db_path),
            ):
                result = apply_truth_repair_plan(
                    plan,
                    environment="live",
                    window_end_ms=200,
                    apply_changes=True,
                    delete_extra_bars=True,
                )

            self.assertTrue(result["applied"])
            self.assertFalse(result["event_table_available"])
            self.assertEqual(0, result["events_written"])
            self.assertEqual(1, result["upserted_bars"])
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT close FROM ibkr_bars WHERE symbol = 'AAPL'").fetchone()
            self.assertEqual(10.5, row[0])

    def test_delete_requires_explicit_refetch_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "data.db")
            create_repair_sqlite(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO ibkr_bars (
                        id, symbol, exchange, interval, open, high, low, close, volume,
                        session_type, us_time, cn_time, bar_time_ms, extra, environment, created, updated
                    ) VALUES ('bar1', 'AAPL', 'SMART', '5m', 10, 11, 9, 10, 100, 'regular', '', '', 300, '{}', 'live', '', '')
                    """
                )
                conn.commit()
            plan = {
                "items": [
                    {
                        "operation_id": "op-1",
                        "environment": "live",
                        "market_date": "2026-05-27",
                        "symbol": "AAPL",
                        "interval": "5m",
                        "bar_time_ms": 300,
                        "action": TRUTH_REPAIR_ACTION_DELETE,
                        "old_bar": {"bar_time_ms": 300, "environment": "live"},
                    }
                ]
            }

            with mock.patch(
                "ibkr_compute.api.ops.truth_repair.open_pb_sqlite",
                patched_sqlite_opener(db_path),
            ):
                result = apply_truth_repair_plan(
                    plan,
                    environment="live",
                    window_end_ms=400,
                    apply_changes=True,
                    delete_extra_bars=True,
                )

            self.assertEqual(0, result["deleted_bars"])
            self.assertEqual(0, result["applied_items"])
            self.assertEqual("delete_not_confirmed_by_refetch", result["errors"][0]["error"])
            with sqlite3.connect(db_path) as conn:
                total = conn.execute("SELECT COUNT(*) FROM ibkr_bars").fetchone()[0]
            self.assertEqual(1, total)

    def test_live_delete_extra_bar_removes_legacy_empty_environment_when_confirmed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "data.db")
            create_repair_sqlite(db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO ibkr_bars (
                        id, symbol, exchange, interval, open, high, low, close, volume,
                        session_type, us_time, cn_time, bar_time_ms, extra, environment, created, updated
                    ) VALUES ('bar1', 'AAPL', 'SMART', '5m', 10, 11, 9, 10, 100, 'regular', '', '', 300, '{}', '', '', '')
                    """
                )
                conn.commit()
            plan = {
                "items": [
                    {
                        "operation_id": "op-1",
                        "environment": "live",
                        "market_date": "2026-05-27",
                        "symbol": "AAPL",
                        "interval": "5m",
                        "bar_time_ms": 300,
                        "action": TRUTH_REPAIR_ACTION_DELETE,
                        "delete_confirmed": True,
                        "old_bar": {"bar_time_ms": 300, "environment": ""},
                    }
                ]
            }

            with mock.patch(
                "ibkr_compute.api.ops.truth_repair.open_pb_sqlite",
                patched_sqlite_opener(db_path),
            ):
                result = apply_truth_repair_plan(
                    plan,
                    environment="live",
                    window_end_ms=400,
                    apply_changes=True,
                    delete_extra_bars=True,
                )

            self.assertEqual(1, result["deleted_bars"])
            with sqlite3.connect(db_path) as conn:
                total = conn.execute("SELECT COUNT(*) FROM ibkr_bars").fetchone()[0]
            self.assertEqual(0, total)


if __name__ == "__main__":
    unittest.main()
