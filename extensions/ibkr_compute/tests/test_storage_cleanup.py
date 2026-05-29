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

from ibkr_compute.market.data_retention import (
    DEFAULT_RETENTION_POLICIES,
    DataRetention,
    PROTECTED_CORE_COLLECTIONS as RETENTION_PROTECTED_COLLECTIONS,
)
from ibkr_compute.market.storage_cleanup import (
    DEFAULT_PROFILE,
    DEFAULT_PROTECTED_BATCH_IDS,
    DEFAULT_PROTECTED_RUN_IDS,
    PROTECTED_CORE_COLLECTIONS as STORAGE_PROTECTED_COLLECTIONS,
    StorageCleanup,
)


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

    def test_default_profile_keeps_signal_and_order_tables(self):
        old_ms = 1700000000000
        pb = _FakePB(
            {
                "ibkr_signals": [{"id": "old_expired", "environment": "live", "bar_time_ms": old_ms, "status": "expired"}],
                "orders": [{"id": "old_order", "environment": "live", "bar_time_ms": old_ms, "status": "Filled"}],
                "ibkr_order_details": [{"id": "old_detail", "environment": "live", "bar_time_ms": old_ms}],
                "ibkr_reverse_signals": [{"id": "old_reverse", "environment": "live", "bar_time_ms": old_ms}],
                "ibkr_targets": [{"id": "old_target", "environment": "live", "bar_time_ms": old_ms}],
                "watchlist": [{"id": "watch", "environment": "live", "symbol": "AAPL"}],
                "config": [{"id": "cfg", "environment": "live", "key": "storage_cleanup_profile"}],
                "ibkr_state": [{"id": "state", "environment": "live", "state_key": "important"}],
            }
        )
        cleanup = StorageCleanup(pb, _FakeConfig(), default_environments=["live"])

        result = cleanup.cleanup(dry_run=False, now=datetime(2026, 5, 9, 12, 0, tzinfo=ET))

        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"], DEFAULT_PROFILE)
        self.assertEqual({row["id"] for row in pb.records["ibkr_signals"]}, {"old_expired"})
        self.assertEqual({row["id"] for row in pb.records["orders"]}, {"old_order"})
        self.assertEqual({row["id"] for row in pb.records["ibkr_order_details"]}, {"old_detail"})
        self.assertEqual({row["id"] for row in pb.records["ibkr_reverse_signals"]}, {"old_reverse"})
        self.assertEqual({row["id"] for row in pb.records["ibkr_targets"]}, {"old_target"})
        self.assertEqual({row["id"] for row in pb.records["watchlist"]}, {"watch"})
        self.assertEqual({row["id"] for row in pb.records["config"]}, {"cfg"})
        self.assertEqual({row["id"] for row in pb.records["ibkr_state"]}, {"state"})

    def test_balanced_50g_profile_aliases_to_tv_primary_lean(self):
        pb = _FakePB(
            {
                "ibkr_indicators": [{"id": "ind", "environment": "live", "bar_time_ms": 1760000000000}],
            }
        )
        cleanup = StorageCleanup(pb, _FakeConfig(), default_environments=["live"])

        result = cleanup.cleanup(dry_run=False, profile="balanced_50g", now=datetime(2026, 5, 9, 12, 0, tzinfo=ET))

        self.assertTrue(result["ok"])
        self.assertEqual(result["requested_profile"], "balanced_50g")
        self.assertEqual(result["profile"], DEFAULT_PROFILE)
        self.assertEqual(result["profile_alias"], "balanced_50g")
        self.assertEqual(pb.records["ibkr_indicators"], [])

    def test_custom_storage_policy_cannot_delete_protected_core_collection(self):
        pb = _FakePB(
            {
                "ibkr_signals": [{"id": "signal", "environment": "live", "bar_time_ms": 1700000000000}],
            }
        )
        cleanup = StorageCleanup(
            pb,
            _FakeConfig(),
            default_environments=["live"],
            policies=[
                {
                    "collection": "ibkr_signals",
                    "mode": "truncate_collection",
                    "include_legacy_empty": True,
                    "sort": "created",
                }
            ],
        )

        dry_result = cleanup.cleanup(dry_run=True, now=datetime(2026, 5, 9, 12, 0, tzinfo=ET))
        real_result = cleanup.cleanup(dry_run=False, force=True, now=datetime(2026, 5, 9, 12, 0, tzinfo=ET))

        self.assertTrue(dry_result["ok"])
        self.assertTrue(real_result["ok"])
        self.assertEqual({row["id"] for row in pb.records["ibkr_signals"]}, {"signal"})
        self.assertEqual(pb.deleted, [])
        policy_result = real_result["environments"][0]["policies"][0]
        self.assertTrue(policy_result["skipped"])
        self.assertEqual(policy_result["reason"], "protected_core_collection")

    def test_tv_primary_lean_clears_indicators_and_uses_short_retention(self):
        pb = _FakePB(
            {
                "ibkr_indicators": [{"id": "ind", "environment": "live", "bar_time_ms": 1760000000000}],
                "ibkr_bars": [
                    {"id": "old_bar", "environment": "live", "bar_time_ms": 1700000000000},
                    {"id": "new_bar", "environment": "live", "bar_time_ms": 1788000000000},
                ],
                "system_events": [{"id": "old_event", "environment": "live", "created": "2026-03-01 00:00:00"}],
            }
        )
        cleanup = StorageCleanup(pb, _FakeConfig(), default_environments=["live"])

        result = cleanup.cleanup(dry_run=False, now=datetime(2026, 5, 9, 12, 0, tzinfo=ET))

        self.assertTrue(result["ok"])
        self.assertEqual(pb.records["ibkr_indicators"], [])
        self.assertEqual({row["id"] for row in pb.records["ibkr_bars"]}, {"new_bar"})
        self.assertFalse(any(row["id"] == "old_event" for row in pb.records["system_events"]))

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

    def test_data_retention_default_policies_exclude_protected_core_tables(self):
        policy_collections = {str(item.get("collection") or "") for item in DEFAULT_RETENTION_POLICIES}

        self.assertFalse(policy_collections & RETENTION_PROTECTED_COLLECTIONS)
        self.assertFalse(policy_collections & STORAGE_PROTECTED_COLLECTIONS)

    def test_data_retention_keeps_protected_core_tables(self):
        old_ms = 1700000000000
        protected_records = {
            "orders": [{"id": "order", "environment": "live", "bar_time_ms": old_ms}],
            "ibkr_order_details": [{"id": "detail", "environment": "live", "bar_time_ms": old_ms}],
            "ibkr_signals": [{"id": "signal", "environment": "live", "bar_time_ms": old_ms}],
            "ibkr_reverse_signals": [{"id": "reverse", "environment": "live", "bar_time_ms": old_ms}],
            "ibkr_targets": [{"id": "target", "environment": "live", "bar_time_ms": old_ms}],
            "watchlist": [{"id": "watch", "environment": "live", "symbol": "AAPL"}],
            "config": [{"id": "cfg", "environment": "live", "key": "ibkr_history_retention_enabled"}],
            "ibkr_state": [{"id": "state", "environment": "live", "state_key": "important"}],
        }
        pb = _FakePB(
            {
                "ibkr_bars": [{"id": "old_bar", "environment": "live", "bar_time_ms": old_ms}],
                "ibkr_indicators": [{"id": "old_indicator", "environment": "live", "bar_time_ms": old_ms}],
                **protected_records,
            }
        )
        retention = DataRetention(pb, _FakeConfig({"ibkr_history_retention_enabled": "true"}), default_environments=["live"])

        result = retention.cleanup(retention_days=30, force=True, source="unit")

        self.assertTrue(result["ok"])
        self.assertEqual(pb.records["ibkr_bars"], [])
        self.assertEqual(pb.records["ibkr_indicators"], [])
        for collection, rows in protected_records.items():
            self.assertEqual({row["id"] for row in pb.records[collection]}, {row["id"] for row in rows})

    def test_data_retention_custom_policy_cannot_delete_protected_core_collection(self):
        pb = _FakePB(
            {
                "ibkr_targets": [{"id": "target", "environment": "live", "bar_time_ms": 1700000000000}],
            }
        )
        retention = DataRetention(
            pb,
            _FakeConfig({"ibkr_history_retention_enabled": "true"}),
            default_environments=["live"],
            policies=[
                {
                    "collection": "ibkr_targets",
                    "field": "bar_time_ms",
                    "kind": "ms",
                    "include_legacy_empty": True,
                }
            ],
        )

        result = retention.cleanup(retention_days=30, force=True, source="unit")

        self.assertTrue(result["ok"])
        self.assertEqual({row["id"] for row in pb.records["ibkr_targets"]}, {"target"})
        self.assertEqual(pb.deleted, [])
        policy_result = result["environments"][0]["collections"][0]
        self.assertTrue(policy_result["skipped"])
        self.assertEqual(policy_result["reason"], "protected_core_collection")


if __name__ == "__main__":
    unittest.main()
