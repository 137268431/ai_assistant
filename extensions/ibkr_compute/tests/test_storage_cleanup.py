import os
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("PB_SQLITE_PATH", "/tmp/nonexistent_storage_cleanup_test.db")

from ibkr_compute.market.storage_cleanup import DEFAULT_PROTECTED_BATCH_IDS, DEFAULT_PROTECTED_RUN_IDS, StorageCleanup


ET = ZoneInfo("America/New_York")


class _FakeConfig:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}

    def get_for_environment(self, key, environment, default=None):
        return self.overrides.get((key, environment), self.overrides.get(key, default))

    def get_bool_for_environment(self, key, environment, default=False):
        value = self.get_for_environment(key, environment, str(default).lower())
        return str(value).strip().lower() in {"true", "1", "yes", "on"}

    def get_int_for_environment(self, key, environment, default=0):
        try:
            return int(self.get_for_environment(key, environment, default))
        except Exception:
            return default


def _strip_group(text):
    stripped = text.strip()
    while stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    return stripped


def _match_condition(row, condition):
    condition = _strip_group(condition)
    if not condition or condition == "1 = 1":
        return True
    if " || " in condition:
        return any(_match_condition(row, part) for part in condition.split(" || "))

    match = re.match(r"^([A-Za-z0-9_]+)\s*!=\s*\"(.*)\"$", condition)
    if match:
        return str(row.get(match.group(1), "")) != match.group(2)
    match = re.match(r"^([A-Za-z0-9_]+)\s*=\s*\"(.*)\"$", condition)
    if match:
        return str(row.get(match.group(1), "")) == match.group(2)
    match = re.match(r"^([A-Za-z0-9_]+)\s*<\s*'?([^']+)'?$", condition)
    if match:
        actual = row.get(match.group(1), "")
        expected = match.group(2)
        try:
            return float(actual) < float(expected)
        except Exception:
            return str(actual) < str(expected)
    match = re.match(r"^([A-Za-z0-9_]+)\s*>\s*'?([^']+)'?$", condition)
    if match:
        actual = row.get(match.group(1), "")
        expected = match.group(2)
        try:
            return float(actual) > float(expected)
        except Exception:
            return str(actual) > str(expected)
    return True


def _matches_filter(row, filter_str):
    text = str(filter_str or "").strip()
    if not text:
        return True
    return all(_match_condition(row, part) for part in text.split(" && "))


class _FakePB:
    def __init__(self, records=None):
        self.records = {key: [dict(item) for item in value] for key, value in (records or {}).items()}
        self.deleted = []
        self.states = {}
        self.created = []

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        rows = [row for row in self.records.get(collection, []) if _matches_filter(row, filter)]
        sort_key = str(sort or "").strip()
        reverse = sort_key.startswith("-")
        sort_field = sort_key[1:] if reverse else sort_key
        if sort_field:
            rows = sorted(rows, key=lambda row: str(row.get(sort_field, "")), reverse=reverse)
        start = (int(page) - 1) * int(per_page)
        return [dict(row) for row in rows[start:start + int(per_page)]]

    def delete_record(self, collection, record_id):
        before = len(self.records.get(collection, []))
        self.records[collection] = [row for row in self.records.get(collection, []) if row.get("id") != record_id]
        if len(self.records.get(collection, [])) != before:
            self.deleted.append((collection, record_id))
        return True

    def get_state(self, state_key, environment, date="global"):
        return self.states.get((state_key, environment, date))

    def upsert_state(self, state_key, environment, data, date="global"):
        self.states[(state_key, environment, date)] = {"data": data}
        return self.states[(state_key, environment, date)]

    def create_record(self, collection, data):
        payload = {"id": f"created_{len(self.created)}", **dict(data)}
        self.created.append((collection, payload))
        self.records.setdefault(collection, []).append(payload)
        return payload


class StorageCleanupTest(unittest.TestCase):
    def test_dry_run_does_not_delete_records(self):
        pb = _FakePB(
            {
                "ibkr_indicators": [
                    {"id": "old_ind", "environment": "live", "bar_time_ms": 1700000000000, "status": ""},
                ]
            }
        )
        cleanup = StorageCleanup(pb, _FakeConfig(), default_environments=["live"])

        result = cleanup.cleanup(dry_run=True, now=datetime(2026, 5, 9, 12, 0, tzinfo=ET))

        self.assertTrue(result["ok"])
        self.assertEqual(result["total_deleted"], 0)
        self.assertEqual(pb.deleted, [])
        self.assertEqual(len(pb.records["ibkr_indicators"]), 1)

    def test_active_signal_statuses_are_protected(self):
        old_ms = 1700000000000
        pb = _FakePB(
            {
                "ibkr_signals": [
                    {"id": "old_expired", "environment": "live", "bar_time_ms": old_ms, "status": "expired"},
                    {"id": "old_pending", "environment": "live", "bar_time_ms": old_ms, "status": "pending"},
                ]
            }
        )
        cleanup = StorageCleanup(pb, _FakeConfig(), default_environments=["live"])

        result = cleanup.cleanup(dry_run=False, now=datetime(2026, 5, 9, 12, 0, tzinfo=ET))

        self.assertTrue(result["ok"])
        remaining_ids = {row["id"] for row in pb.records["ibkr_signals"]}
        self.assertNotIn("old_expired", remaining_ids)
        self.assertIn("old_pending", remaining_ids)

    def test_backtest_cleanup_keeps_protected_and_recent_runs(self):
        protected_batch = DEFAULT_PROTECTED_BATCH_IDS[0]
        protected_run = DEFAULT_PROTECTED_RUN_IDS[0]
        recent_batches = [
            {"id": f"recent_batch_{index}", "best_run_id": f"recent_run_{index}", "created": f"2026-05-0{index + 1} 00:00:00.000Z"}
            for index in range(5)
        ]
        recent_runs = [
            {"id": f"recent_run_{index}", "created": f"2026-05-0{index + 1} 00:00:00.000Z"}
            for index in range(5)
        ]
        pb = _FakePB(
            {
                "ibkr_backtest_batches": [
                    {"id": protected_batch, "best_run_id": protected_run, "created": "2026-04-01 00:00:00.000Z"},
                    *recent_batches,
                    {"id": "stale_batch", "best_run_id": "stale_run", "created": "2026-01-01 00:00:00.000Z"},
                ],
                "ibkr_backtest_runs": [
                    {"id": protected_run, "created": "2026-04-01 00:00:00.000Z"},
                    *recent_runs,
                    {"id": "stale_run", "created": "2026-01-01 00:00:00.000Z"},
                ],
                "ibkr_backtest_trades": [
                    {"id": "protected_trade", "run_id": protected_run, "created": "2026-05-01 00:00:00.000Z"},
                    {"id": "stale_trade", "run_id": "stale_run", "created": "2026-01-01 00:00:00.000Z"},
                ],
            }
        )
        cleanup = StorageCleanup(
            pb,
            _FakeConfig({"storage_cleanup_backtest_recent_limit": "5"}),
            default_environments=["live"],
        )

        result = cleanup.cleanup(dry_run=False, now=datetime(2026, 5, 9, 12, 0, tzinfo=ET))

        self.assertTrue(result["ok"])
        self.assertEqual(
            {row["id"] for row in pb.records["ibkr_backtest_runs"]},
            {protected_run, *(f"recent_run_{index}" for index in range(5))},
        )
        self.assertEqual(
            {row["id"] for row in pb.records["ibkr_backtest_batches"]},
            {protected_batch, *(f"recent_batch_{index}" for index in range(5))},
        )
        self.assertEqual({row["id"] for row in pb.records["ibkr_backtest_trades"]}, {"protected_trade"})


if __name__ == "__main__":
    unittest.main()
