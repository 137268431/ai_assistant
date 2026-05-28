import sys
import unittest
from datetime import datetime
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.calendar import (  # noqa: E402
    build_ibkr_calendar_snapshot,
    build_local_nyse_calendar_snapshot,
    parse_ibkr_trading_hours,
)
from ibkr_compute.core.time_utils import ET  # noqa: E402


class MarketCalendarTest(unittest.TestCase):
    def test_parse_ibkr_closed_day_and_next_open(self):
        contract = {
            "symbol": "SPY",
            "conid": 756733,
            "exchange": "SMART",
            "sec_type": "STK",
            "time_zone_id": "America/New_York",
            "trading_hours": "20260525:CLOSED;20260526:0400-2000",
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
        self.assertEqual(snapshot["market_session"]["kind"], "closed")
        self.assertEqual(snapshot["market_session"]["label_zh"], "闭市")

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
        self.assertEqual(snapshot["market_session"]["kind"], "closed")

    def test_gateway_schedule_classifies_market_sessions(self):
        contract = {
            "symbol": "SPY",
            "conid": 756733,
            "exchange": "SMART",
            "sec_type": "STK",
            "time_zone_id": "America/New_York",
            "trading_hours": "20260526:0400-2000",
            "liquid_hours": "20260526:0930-1600",
        }

        cases = [
            ("2026-05-26 08:00:00", "premarket", "盘前"),
            ("2026-05-26 10:00:00", "regular", "盘中"),
            ("2026-05-26 16:05:00", "close_transition", "盘后过渡"),
            ("2026-05-26 17:00:00", "afterhours", "盘后"),
            ("2026-05-26 20:01:00", "overnight", "夜盘"),
            ("2026-05-26 03:00:00", "overnight", "夜盘"),
        ]
        for raw_time, expected_kind, expected_label in cases:
            with self.subTest(raw_time=raw_time):
                now = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
                snapshot = build_ibkr_calendar_snapshot(contract, market_date="2026-05-26", now=now)

                self.assertTrue(snapshot["ok"])
                self.assertEqual(snapshot["session"]["regular_open_us"], "2026-05-26 09:30:00")
                self.assertEqual(snapshot["session"]["extended_open_us"], "2026-05-26 04:00:00")
                self.assertEqual(snapshot["session"]["extended_close_us"], "2026-05-26 20:00:00")
                self.assertEqual(snapshot["market_session"]["kind"], expected_kind)
                self.assertEqual(snapshot["market_session"]["label_zh"], expected_label)


if __name__ == "__main__":
    unittest.main()
