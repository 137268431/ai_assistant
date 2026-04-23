import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.storage.helpers import (
    batch_upsert_records,
    build_bar_close_meta,
    merge_bar_integrity_row,
    prepare_bar_integrity_row,
    prepare_indicator_row,
)


class _FakePB:
    def __init__(self):
        self.batch_requests = []
        self.existing_records = {}

    def _find_existing_records(self, collection, items, unique_fields):
        return dict(self.existing_records)

    def _execute_batch_requests(self, requests_payload, timeout=30, batch_size=50):
        self.batch_requests.extend(requests_payload)


class StorageHelpersTest(unittest.TestCase):
    def test_prepare_indicator_row_normalizes_symbol_and_source(self):
        row, error = prepare_indicator_row(
            {
                "symbol": "aapl",
                "interval": "5m",
                "bar_time_ms": 1234567890,
                "extra": {"close": 123.4},
            },
            "live",
        )
        self.assertEqual("", error)
        self.assertIsNotNone(row)
        self.assertEqual("AAPL", row["symbol"])
        self.assertEqual("live", row["environment"])
        self.assertEqual("ibkr_compute", row["extra"]["source"])
        self.assertEqual(123.4, row["extra"]["close"])

    def test_build_bar_close_meta_uses_interval_bucket(self):
        meta = build_bar_close_meta(1_000, "5m")
        self.assertEqual(301_000, meta["bar_close_time_ms"])
        self.assertEqual("start", meta["bar_time_semantics"])

    def test_batch_upsert_records_patches_existing_integrity_row(self):
        pb = _FakePB()
        row, error = prepare_bar_integrity_row(
            {
                "symbol": "AAPL",
                "market_date": "2026-04-23",
                "interval": "5m",
                "increment_repair_attempts": True,
            },
            "live",
        )
        self.assertEqual("", error)
        pb.existing_records = {
            ("live", "2026-04-23", "AAPL", "5m"): {
                "id": "integrity-1",
                "repair_attempts": 2,
                "last_repair_at": "2026-04-22 10:00:00",
                "last_repair_result": {"status": "old"},
            }
        }
        result = batch_upsert_records(
            pb,
            "ibkr_bar_integrity",
            [row],
            ["environment", "market_date", "symbol", "interval"],
            update_transform=merge_bar_integrity_row,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["created"])
        self.assertEqual(1, result["updated"])
        self.assertEqual(1, len(pb.batch_requests))
        request_payload = pb.batch_requests[0]
        self.assertEqual("PATCH", request_payload["method"])
        self.assertEqual("/api/collections/ibkr_bar_integrity/records/integrity-1", request_payload["url"])
        self.assertEqual(3, request_payload["body"]["repair_attempts"])


if __name__ == "__main__":
    unittest.main()
