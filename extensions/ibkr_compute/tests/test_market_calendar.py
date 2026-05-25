import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.calendar import (  # noqa: E402
    build_ibkr_calendar_snapshot,
    build_local_nyse_calendar_snapshot,
    parse_ibkr_trading_hours,
)


class MarketCalendarTest(unittest.TestCase):
    def test_parse_ibkr_closed_day_and_next_open(self):
        contract = {
            "symbol": "SPY",
            "conid": 756733,
            "exchange": "SMART",
            "sec_type": "STK",
            "time_zone_id": "America/New_York",
            "liquid_hours": "20260525:CLOSED;20260526:0930-1600",
        }

        snapshot = build_ibkr_calendar_snapshot(contract, market_date="2026-05-25")

        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["source"], "ibkr_schedule")
        self.assertFalse(snapshot["is_trading_day"])
        self.assertTrue(snapshot["is_closed"])
        self.assertEqual(snapshot["closed_reason"], "ibkr_closed")
        self.assertEqual(snapshot["next_open_us"], "2026-05-26 09:30:00")
        self.assertEqual(snapshot["next_open_beijing"], "2026-05-26 21:30:00")

    def test_parse_ibkr_regular_session_with_dated_endpoints(self):
        days = parse_ibkr_trading_hours(
            "20260526:20260526:0930-20260526:1600",
            time_zone_id="America/New_York",
        )

        self.assertEqual(days["2026-05-26"]["open_us"], "2026-05-26 09:30:00")
        self.assertEqual(days["2026-05-26"]["close_us"], "2026-05-26 16:00:00")
        self.assertFalse(days["2026-05-26"]["is_closed"])

    def test_local_fallback_marks_memorial_day_closed(self):
        snapshot = build_local_nyse_calendar_snapshot("2026-05-25", source_error="gateway_down")

        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["source"], "local_nyse_fallback")
        self.assertTrue(snapshot["is_closed"])
        self.assertEqual(snapshot["closed_reason"], "nyse_holiday")
        self.assertEqual(snapshot["next_open_us"], "2026-05-26 09:30:00")
        self.assertEqual(snapshot["source_error"], "gateway_down")


if __name__ == "__main__":
    unittest.main()
