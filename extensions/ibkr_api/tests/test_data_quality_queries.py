import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.data_quality.queries import build_summary, load_effective_watchlist_symbols, merge_items


class _FakePB:
    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        if collection == "watchlist":
            return [
                {"symbol": "AAPL", "environment": "global"},
                {"symbol": "AAPL", "environment": "live"},
                {"symbol": "MSFT", "environment": ""},
            ]
        return []


class DataQualityQueriesTest(unittest.TestCase):
    def test_load_effective_watchlist_symbols_prefers_runtime_scope(self):
        symbols = load_effective_watchlist_symbols(_FakePB(), "live")
        self.assertEqual(["AAPL", "MSFT"], symbols)

    def test_merge_items_and_summary_track_truth_coverage(self):
        merged = merge_items(
            [
                {
                    "id": "iq-1",
                    "environment": "live",
                    "market_date": "2026-04-23",
                    "symbol": "AAPL",
                    "interval": "5m",
                    "status": "ok",
                    "needs_repair": False,
                    "duplicate_count": 0,
                    "bad_ohlc_count": 0,
                    "last_scan_at": "2026-04-23 10:00:00",
                    "updated": "2026-04-23 10:01:00",
                },
                {
                    "id": "iq-2",
                    "environment": "live",
                    "market_date": "2026-04-23",
                    "symbol": "MSFT",
                    "interval": "5m",
                    "status": "warn",
                    "needs_repair": True,
                    "duplicate_count": 1,
                    "bad_ohlc_count": 0,
                    "scan_scope": "watchlist",
                    "last_scan_at": "2026-04-23 10:05:00",
                    "updated": "2026-04-23 10:06:00",
                },
            ],
            [
                {
                    "id": "truth-1",
                    "environment": "live",
                    "market_date": "2026-04-23",
                    "symbol": "AAPL",
                    "interval": "5m",
                    "status": "ok",
                    "last_checked_at": "2026-04-23 10:07:00",
                    "updated": "2026-04-23 10:07:00",
                }
            ],
        )
        self.assertEqual(2, len(merged))
        summary = build_summary("live", "2026-04-23", "watchlist", merged, ["AAPL", "MSFT"])
        self.assertEqual(2, summary["total"])
        self.assertEqual(1, summary["needs_repair"])
        self.assertEqual(1, summary["manual_review"])
        self.assertEqual(2, summary["scanned_symbols_total"])
        self.assertEqual(1, summary["truth_audited_symbols_total"])
        self.assertFalse(summary["truth_coverage_complete"])
        self.assertEqual(["MSFT"], summary["unaudited_symbols"])
        self.assertEqual("red", summary["database_correctness_status"])


if __name__ == "__main__":
    unittest.main()
