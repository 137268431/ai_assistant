import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.timeframe_utils import build_bar_close_timestamps, latest_safe_closed_bucket_ms


class BarWindowMetadataTest(unittest.TestCase):
    def test_build_bar_close_timestamps_for_5m_bar(self):
        meta = build_bar_close_timestamps(1776358800000, "5m")

        self.assertEqual(meta["bar_time_semantics"], "start")
        self.assertEqual(meta["bar_close_time_ms"], 1776359100000)
        self.assertEqual(meta["bar_close_us_time"], "2026-04-16 13:05:00")
        self.assertEqual(meta["bar_close_cn_time"], "2026-04-17 01:05:00")

    def test_build_bar_close_timestamps_normalizes_interval_alias(self):
        meta = build_bar_close_timestamps(1776358800000, "60")

        self.assertEqual(meta["bar_close_time_ms"], 1776362400000)
        self.assertEqual(meta["bar_close_us_time"], "2026-04-16 14:00:00")

    def test_latest_safe_closed_bucket_ms_applies_close_delay(self):
        safe_ms = latest_safe_closed_bucket_ms(
            "5m",
            delay_seconds=8,
            now_ms=1776361717325,
        )

        self.assertEqual(safe_ms, 1776361200000)


if __name__ == "__main__":
    unittest.main()
