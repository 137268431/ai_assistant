import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.core.timeline_builder import build_runtime_timeline


class ChartTraceTimelineTest(unittest.TestCase):
    def test_build_runtime_timeline_includes_trace_rows_and_preview_flag(self):
        bars = [
            {
                "bar_time_ms": 1776691800000,
                "us_time": "2026-04-20 09:30:00",
                "cn_time": "2026-04-20 21:30:00",
                "open": 100,
                "high": 101,
                "low": 99.5,
                "close": 100.5,
                "volume": 1000,
                "exchange": "SMART",
            },
            {
                "bar_time_ms": 1776692100000,
                "us_time": "2026-04-20 09:35:00",
                "cn_time": "2026-04-20 21:35:00",
                "open": 100.5,
                "high": 101.2,
                "low": 100.3,
                "close": 101.0,
                "volume": 1200,
                "exchange": "SMART",
            },
            {
                "bar_time_ms": 1776692400000,
                "us_time": "2026-04-20 09:40:00",
                "cn_time": "2026-04-20 21:40:00",
                "open": 101.0,
                "high": 101.6,
                "low": 100.8,
                "close": 101.4,
                "volume": 1400,
                "exchange": "SMART",
                "preview": True,
            },
        ]

        timeline = build_runtime_timeline(
            "SPY",
            "5m",
            bars,
            params={},
            include_signals=True,
            include_trace=True,
        )

        self.assertEqual(len(timeline["rows"]), 3)
        self.assertIn("trace", timeline["rows"][-1])
        self.assertTrue(timeline["rows"][-1]["is_preview"])
        self.assertEqual(timeline["rows"][-1]["trace"]["signal_state"]["stage"], "none")

    def test_signal_generator_trace_label_uses_decision_language(self):
        signal_gen = SignalGenerator("SPY", "5m", params={})
        trace = signal_gen._build_trace_payload(
            {"close": 101.0},
            stage="candidate",
            signal={
                "direction": "long",
                "signal": "trend_sdUpper",
                "reason": "preview",
                "extra": {
                    "signal_window": "sd_upper",
                    "signal_mode": "trend",
                    "ema_touch_line": "fast",
                    "div_source": "crsi",
                },
            },
        )

        self.assertEqual(trace["signal_state"]["label"], "顺势多候选")
        self.assertEqual(trace["signal_state"]["stage"], "candidate")


if __name__ == "__main__":
    unittest.main()
