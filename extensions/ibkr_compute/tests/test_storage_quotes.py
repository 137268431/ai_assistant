import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.market.storage_quotes import merge_quote_with_storage


class StorageQuotesTest(unittest.TestCase):
    def test_merge_storage_preserves_live_snapshot_quote_flag(self):
        existing = {
            "symbol": "AAPL",
            "conid": 265598,
            "bid": 308.5,
            "ask": 308.61,
            "last_price": None,
            "quote_age_s": 0.0,
            "quote_fallback": False,
            "snapshot_ok": True,
            "snapshot_requested": True,
            "snapshot_source": "ibkr_market_data_snapshot",
        }
        snapshot = {
            "symbol": "AAPL",
            "last_price": 307.9,
            "day_change_pct": 0.42,
            "bar_time_ms": 1770000000000,
            "bar_close_time_ms": 1770000000000,
            "source": "canonical_5m",
            "day_change_pct_source": "indicator",
            "updated": "2026-06-08 08:35:00",
            "data_age_s": 120.0,
        }

        merged = merge_quote_with_storage(existing, snapshot)

        self.assertFalse(merged["quote_fallback"])
        self.assertTrue(merged["snapshot_ok"])
        self.assertEqual(308.5, merged["bid"])
        self.assertEqual(308.61, merged["ask"])
        self.assertEqual(307.9, merged["last_price"])
        self.assertEqual("canonical_5m", merged["last_price_source"])
        self.assertEqual(0.42, merged["day_change_pct"])

    def test_merge_storage_marks_fallback_when_no_live_quote_exists(self):
        snapshot = {
            "symbol": "TSLA",
            "last_price": 399.1,
            "bar_time_ms": 1770000000000,
            "bar_close_time_ms": 1770000000000,
            "source": "canonical_5m",
            "updated": "2026-06-08 08:35:00",
            "data_age_s": 120.0,
        }

        merged = merge_quote_with_storage(None, snapshot)

        self.assertTrue(merged["quote_fallback"])
        self.assertEqual("TSLA", merged["symbol"])
        self.assertEqual(399.1, merged["last_price"])
        self.assertIsNone(merged["bid"])
        self.assertIsNone(merged["ask"])


if __name__ == "__main__":
    unittest.main()
