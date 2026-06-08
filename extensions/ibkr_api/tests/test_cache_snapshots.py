import copy
import re
import sys
import time
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.app_core.cache_snapshots import (  # noqa: E402
    build_snapshot_cache_key,
    cached_snapshot_response,
    clear_cached_snapshots,
    get_cached_snapshot,
    upsert_cached_snapshot,
)


CACHE_COLLECTION = "ibkr_cache_snapshots"


def _now_ms():
    return int(time.time() * 1000)


class _FakePB:
    def __init__(self):
        self.records = {CACHE_COLLECTION: []}
        self.created = []
        self.updated = []
        self.deleted = []
        self.get_calls = []
        self._next_id = 1

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        self.get_calls.append(
            {
                "collection": collection,
                "filter": filter or "",
                "sort": sort,
                "per_page": per_page,
                "page": page,
            }
        )
        rows = [row for row in self.records.get(collection, []) if self._matches(row, filter or "")]
        start = max(0, int(page - 1) * int(per_page))
        return [copy.deepcopy(row) for row in rows[start : start + int(per_page)]]

    def create_record(self, collection, data):
        row = {"id": f"rec-{self._next_id}", **copy.deepcopy(data)}
        self._next_id += 1
        self.records.setdefault(collection, []).append(row)
        self.created.append((collection, copy.deepcopy(data)))
        return copy.deepcopy(row)

    def update_record(self, collection, record_id, patch):
        for row in self.records.get(collection, []):
            if str(row.get("id")) == str(record_id):
                row.update(copy.deepcopy(patch))
                self.updated.append((collection, record_id, copy.deepcopy(patch)))
                return copy.deepcopy(row)
        raise KeyError(f"missing record {collection}/{record_id}")

    def delete_record(self, collection, record_id):
        rows = self.records.get(collection, [])
        before = len(rows)
        self.records[collection] = [row for row in rows if str(row.get("id")) != str(record_id)]
        if len(self.records[collection]) == before:
            raise KeyError(f"missing record {collection}/{record_id}")
        self.deleted.append((collection, record_id))
        return True

    def _matches(self, row, filter_text):
        if not filter_text:
            return True
        for field, value in re.findall(r'([A-Za-z0-9_]+)\s*=\s*"([^"]*)"', filter_text):
            if str(row.get(field, "")) != value:
                return False
        return True


def _seed_snapshot(pb, cache_key, *, payload=None, computed_at_ms=None, fresh_for_ms=0, stale_for_ms=60_000):
    now = _now_ms() if computed_at_ms is None else int(computed_at_ms)
    return upsert_cached_snapshot(
        pb,
        cache_key,
        scope="home.dashboard",
        environment="live",
        market_date="2026-06-09",
        payload=payload or {"value": "cached"},
        ttl_seconds=fresh_for_ms / 1000,
        stale_seconds=stale_for_ms / 1000,
        now=now,
    )


def _assert_eventually(assertion, timeout_seconds=1.0):
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            assertion()
            return
        except AssertionError as exc:
            last_error = exc
            time.sleep(0.01)
    if last_error is not None:
        raise last_error
    assertion()


class SnapshotCacheHelperTest(unittest.TestCase):
    def test_fresh_hit_returns_cached_payload_without_calling_builder(self):
        pb = _FakePB()
        cache_key = "home.dashboard:fresh-hit"
        _seed_snapshot(pb, cache_key, payload={"value": "cached"}, fresh_for_ms=60_000)

        def builder():
            raise AssertionError("fresh cache hit must not call builder")

        payload, status_code = cached_snapshot_response(
            pb,
            scope="home.dashboard",
            cache_key=cache_key,
            builder=builder,
            ttl_seconds=30,
            stale_seconds=300,
            environment="live",
            market_date="2026-06-09",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual("cached", payload["value"])
        self.assertFalse(payload["_snapshot_cache"]["stale"])
        self.assertEqual("hit", payload["_snapshot_cache"]["state"])
        self.assertEqual(cache_key, payload["_snapshot_cache"]["cache_key"])
        self.assertEqual([], pb.updated)

    def test_stale_hit_returns_cached_payload_and_background_refresh_updates_snapshot(self):
        pb = _FakePB()
        cache_key = "home.dashboard:stale-hit"
        _seed_snapshot(
            pb,
            cache_key,
            payload={"value": "stale-cached"},
            computed_at_ms=_now_ms() - 90_000,
            fresh_for_ms=10_000,
            stale_for_ms=300_000,
        )
        calls = []

        def builder():
            calls.append("builder")
            return {"value": "refreshed"}, 200

        payload, status_code = cached_snapshot_response(
            pb,
            scope="home.dashboard",
            cache_key=cache_key,
            builder=builder,
            ttl_seconds=30,
            stale_seconds=300,
            environment="live",
            market_date="2026-06-09",
            background_refresh=True,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual("stale-cached", payload["value"])
        self.assertTrue(payload["_snapshot_cache"]["stale"])
        self.assertEqual("stale", payload["_snapshot_cache"]["state"])

        def refreshed_record_is_saved():
            self.assertEqual(["builder"], calls)
            record = get_cached_snapshot(pb, cache_key)
            self.assertEqual("refreshed", record["payload"]["value"])
            self.assertGreater(record["fresh_until_ms"], _now_ms())

        _assert_eventually(refreshed_record_is_saved)

    def test_expired_snapshot_runs_builder_and_returns_new_payload(self):
        pb = _FakePB()
        cache_key = "home.dashboard:expired"
        _seed_snapshot(
            pb,
            cache_key,
            payload={"value": "expired-cached"},
            computed_at_ms=_now_ms() - 600_000,
            fresh_for_ms=10_000,
            stale_for_ms=20_000,
        )
        calls = []

        def builder():
            calls.append("builder")
            return {"value": "rebuilt"}, 200

        payload, status_code = cached_snapshot_response(
            pb,
            scope="home.dashboard",
            cache_key=cache_key,
            builder=builder,
            ttl_seconds=30,
            stale_seconds=300,
            environment="live",
            market_date="2026-06-09",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(["builder"], calls)
        self.assertEqual("rebuilt", payload["value"])
        self.assertFalse(payload["_snapshot_cache"]["stale"])
        self.assertEqual("miss", payload["_snapshot_cache"]["state"])
        self.assertEqual("rebuilt", get_cached_snapshot(pb, cache_key)["payload"]["value"])

    def test_expired_builder_failure_falls_back_to_last_known_payload(self):
        pb = _FakePB()
        cache_key = "home.dashboard:expired-error"
        _seed_snapshot(
            pb,
            cache_key,
            payload={"value": "expired-but-last-known"},
            computed_at_ms=_now_ms() - 600_000,
            fresh_for_ms=10_000,
            stale_for_ms=20_000,
        )

        def builder():
            raise RuntimeError("runtime unavailable")

        payload, status_code = cached_snapshot_response(
            pb,
            scope="home.dashboard",
            cache_key=cache_key,
            builder=builder,
            ttl_seconds=30,
            stale_seconds=300,
            environment="live",
            market_date="2026-06-09",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual("expired-but-last-known", payload["value"])
        self.assertTrue(payload["_snapshot_cache"]["stale"])
        self.assertEqual("stale_error", payload["_snapshot_cache"]["state"])
        self.assertIn("runtime unavailable", payload["_snapshot_cache"].get("error", ""))
        self.assertEqual("expired-but-last-known", get_cached_snapshot(pb, cache_key)["payload"]["value"])

    def test_force_refresh_updates_even_when_record_is_fresh(self):
        pb = _FakePB()
        cache_key = "home.dashboard:force"
        _seed_snapshot(pb, cache_key, payload={"value": "cached"}, fresh_for_ms=60_000)

        payload, status_code = cached_snapshot_response(
            pb,
            scope="home.dashboard",
            cache_key=cache_key,
            builder=lambda: ({"value": "forced"}, 200),
            ttl_seconds=30,
            stale_seconds=300,
            environment="live",
            market_date="2026-06-09",
            force=True,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual("forced", payload["value"])
        self.assertEqual("forced", get_cached_snapshot(pb, cache_key)["payload"]["value"])

    def test_upsert_creates_then_updates_same_cache_key_with_freshness_windows(self):
        pb = _FakePB()
        cache_key = "home.market:live"

        created = upsert_cached_snapshot(
            pb,
            cache_key,
            scope="home.market",
            environment="live",
            market_date="2026-06-09",
            payload={"value": "first"},
            ttl_seconds=10,
            stale_seconds=50,
            now=1_000_000,
        )
        updated = upsert_cached_snapshot(
            pb,
            cache_key,
            scope="home.market",
            environment="live",
            market_date="2026-06-09",
            payload={"value": "second"},
            ttl_seconds=20,
            stale_seconds=60,
            now=2_000_000,
        )

        self.assertEqual(created["id"], updated["id"])
        self.assertEqual(1, len(pb.records[CACHE_COLLECTION]))
        self.assertEqual("second", get_cached_snapshot(pb, cache_key)["payload"]["value"])
        self.assertEqual(2_000_000, updated["computed_at_ms"])
        self.assertEqual(2_020_000, updated["fresh_until_ms"])
        self.assertEqual(2_080_000, updated["stale_until_ms"])
        self.assertEqual("fresh", updated["status"])

    def test_cache_key_ignores_refresh_noise_and_separates_dimensions(self):
        include_keys = ("broker_mode", "data_environment", "market_date")
        live_key = build_snapshot_cache_key(
            "home.dashboard",
            {
                "broker_mode": "live",
                "data_environment": "live",
                "market_date": "2026-06-09",
                "cache_bust": "ignored",
            },
            include_keys=include_keys,
        )
        same_live_key = build_snapshot_cache_key(
            "home.dashboard",
            {
                "market_date": "2026-06-09",
                "data_environment": "live",
                "broker_mode": "live",
                "cache": "0",
                "_": "noise",
            },
            include_keys=include_keys,
        )
        paper_key = build_snapshot_cache_key(
            "home.dashboard",
            {
                "broker_mode": "paper",
                "data_environment": "live",
                "market_date": "2026-06-09",
            },
            include_keys=include_keys,
        )

        self.assertEqual(live_key, same_live_key)
        self.assertNotEqual(live_key, paper_key)
        self.assertTrue(live_key.startswith("home.dashboard:"))

    def test_clear_cached_snapshots_deletes_matching_scope(self):
        pb = _FakePB()
        keep_key = "home.market:keep"
        drop_key = "home.dashboard:drop"
        upsert_cached_snapshot(pb, keep_key, "home.market", "live", "2026-06-09", {"value": "keep"}, 30, 300)
        upsert_cached_snapshot(pb, drop_key, "home.dashboard", "live", "2026-06-09", {"value": "drop"}, 30, 300)

        invalidated = clear_cached_snapshots(pb, scopes=("home.dashboard",))

        self.assertEqual(1, invalidated)
        self.assertIsNone(get_cached_snapshot(pb, drop_key))
        self.assertEqual("keep", get_cached_snapshot(pb, keep_key)["payload"]["value"])


if __name__ == "__main__":
    unittest.main()
