import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.realtime_quote_book import RealtimeQuoteBook


class RealtimeQuoteBookTest(unittest.TestCase):
    def test_tick_close_field_sets_prev_close_for_index_day_change(self):
        book = RealtimeQuoteBook(conid_to_symbol={13455763: "VIX"})

        book.on_tick({"conid": 13455763, "prev_close": 17.39, "31": 17.44, "_updated": 1778175166000})

        quote = book.get_quote("VIX")
        self.assertIsNotNone(quote)
        self.assertEqual(quote["last_price"], 17.44)
        self.assertEqual(quote["prev_close"], 17.39)
        self.assertEqual(quote["day_change"], 0.05)
        self.assertAlmostEqual(quote["day_change_pct"], 0.2875, places=4)

    def test_get_stale_quotes_returns_only_requested_old_quotes(self):
        book = RealtimeQuoteBook(conid_to_symbol={756733: "SPY", 265598: "AAPL"})
        book.on_tick({"conid": 756733, "31": 733.33, "_updated": 1000})
        book.on_tick({"conid": 265598, "31": 292.66, "_updated": 4000})

        stale = book.get_stale_quotes(["SPY", "AAPL"], max_age_s=2, now_ts=5)

        self.assertEqual([item["symbol"] for item in stale], ["SPY"])
        self.assertEqual(stale[0]["quote_age_s"], 4.0)


if __name__ == "__main__":
    unittest.main()
