import unittest
from datetime import datetime, timezone, timedelta

from ibkr_compute.market.data_backfill import _regular_session_gap_summary
from ibkr_compute.core.indicator_engine import indicator_ready_bar_count


ET = timezone(timedelta(hours=-4))


def regular_bar(us_time: str) -> dict:
    dt = datetime.strptime(us_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    return {
        "bar_time_ms": int(dt.timestamp() * 1000),
        "us_time": us_time,
        "session_type": "regular",
    }


class RegularSessionGapSummaryTest(unittest.TestCase):
    def test_indicator_ready_bar_count_matches_engine_readiness(self):
        self.assertEqual(indicator_ready_bar_count(), 212)

    def test_counts_large_same_day_gap(self):
        rows = [
            regular_bar("2026-04-10 09:30:00"),
            regular_bar("2026-04-10 09:35:00"),
            regular_bar("2026-04-10 10:35:00"),
        ]

        summary = _regular_session_gap_summary(rows, "5m", same_day_only=True)

        self.assertEqual(summary["gap_count"], 1)
        self.assertEqual(summary["gap_examples"][0]["missing_points"], 11)

    def test_ignores_overnight_gap_when_same_day_only(self):
        rows = [
            regular_bar("2026-04-09 15:55:00"),
            regular_bar("2026-04-10 09:30:00"),
        ]

        summary = _regular_session_gap_summary(rows, "5m", same_day_only=True)

        self.assertEqual(summary["gap_count"], 0)
        self.assertEqual(summary["gap_examples"], [])


if __name__ == "__main__":
    unittest.main()
