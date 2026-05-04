import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.chart.timeline import payload as chart_payload
from ibkr_compute.api.chart.timeline import rows as chart_rows
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

    def test_build_runtime_timeline_keeps_last_duplicate_bar_time(self):
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
                "bar_time_ms": 1776692100000,
                "us_time": "2026-04-20 09:35:00",
                "cn_time": "2026-04-20 21:35:00",
                "open": 100.5,
                "high": 102.0,
                "low": 100.3,
                "close": 101.8,
                "volume": 1500,
                "exchange": "SMART",
                "preview": True,
            },
        ]

        timeline = build_runtime_timeline("SPY", "5m", bars, params={}, include_trace=True)

        self.assertEqual(len(timeline["rows"]), 2)
        self.assertTrue(timeline["rows"][-1]["is_preview"])
        self.assertEqual(timeline["rows"][-1]["close"], 101.8)
        self.assertEqual(timeline["rows"][-1]["high"], 102.0)

    def test_chart_timeline_payload_marks_duplicate_preview_bar(self):
        fake_app = SimpleNamespace(
            get_signal_generator_params=lambda environment: {},
            get_daily_change_fields=lambda environment, symbol, close, bar_ms: {},
        )
        source = {
            "source_rows": [
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
                    "bar_time_ms": 1776691800000,
                    "us_time": "2026-04-20 09:30:00",
                    "cn_time": "2026-04-20 21:30:00",
                    "open": 100,
                    "high": 101.6,
                    "low": 99.5,
                    "close": 101.4,
                    "volume": 1300,
                    "exchange": "SMART",
                    "is_preview": True,
                },
            ],
            "visible_rows": [{"bar_time_ms": 1776691800000}],
            "meta": {},
        }

        with mock.patch.object(chart_payload, "_api_app", return_value=fake_app):
            with mock.patch.object(chart_rows, "_api_app", return_value=fake_app):
                result = chart_payload.build_chart_timeline_payload_from_source(
                    "live",
                    "SPY",
                    "5m",
                    source,
                    include_trace=True,
                )

        self.assertEqual(len(result["bars"]), 1)
        self.assertEqual(result["bars"][-1]["close"], 101.4)
        self.assertTrue(result["bars"][-1]["is_preview"])
        self.assertTrue(result["trace_timeline"][-1]["is_preview"])

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
