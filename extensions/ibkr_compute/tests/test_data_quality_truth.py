import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.ops.bar_truth_compare import build_bar_truth_compare_payload
from ibkr_compute.api.ops.data_quality_truth import (
    build_truth_audit_row,
    build_truth_audit_summary,
    classify_truth_audit_status,
    normalize_truth_symbols,
    resolve_truth_audit_window,
)


class DataQualityTruthHelpersTest(unittest.TestCase):
    def test_normalize_truth_symbols_dedupes_and_uppercases(self):
        self.assertEqual(
            normalize_truth_symbols(["spy", "SPY", " qqq ", "", None, "aapl"]),
            ["SPY", "QQQ", "AAPL"],
        )

    def test_resolve_truth_audit_window_uses_regular_session_bounds(self):
        window = resolve_truth_audit_window("2026-04-20")
        self.assertEqual(window["start_ms"], 1776691800000)
        self.assertEqual(window["end_ms"], 1776714900000)

    def test_resolve_truth_audit_window_respects_winter_dst_offset(self):
        window = resolve_truth_audit_window("2026-01-05")
        self.assertEqual(window["start_ms"], 1767623400000)
        self.assertEqual(window["end_ms"], 1767646500000)

    def test_classify_truth_audit_status_marks_mismatch_as_error(self):
        self.assertEqual(
            classify_truth_audit_status({"bar_mismatch_count": 1}),
            "error",
        )
        self.assertEqual(
            classify_truth_audit_status({"matched_bar_count": 78}),
            "ok",
        )
        self.assertEqual(
            classify_truth_audit_status({"matched_bar_count": 0}),
            "unavailable",
        )
        self.assertEqual(
            classify_truth_audit_status({"matched_bar_count": 78, "indicator_mismatch_count": 1, "signal_mismatch_count": 1}),
            "ok",
        )
        self.assertEqual(
            classify_truth_audit_status({}, error="gateway_not_running"),
            "unavailable",
        )

    def test_build_bar_truth_compare_payload_compares_only_bars(self):
        stored_source = {
            "source_rows": [],
            "visible_rows": [
                {"bar_time_ms": 100, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "session_type": "regular"},
                {"bar_time_ms": 200, "open": 20, "high": 21, "low": 19, "close": 20, "volume": 200, "session_type": "regular"},
            ],
        }
        ibkr_source = {
            "source_rows": [],
            "visible_rows": [
                {"bar_time_ms": 100, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "session_type": "regular"},
                {"bar_time_ms": 200, "open": 20, "high": 21, "low": 19, "close": 20.5, "volume": 200, "session_type": "regular"},
                {"bar_time_ms": 300, "open": 30, "high": 31, "low": 29, "close": 30, "volume": 300, "session_type": "regular"},
            ],
            "meta": {"chain": "ibkr_api", "fetched_bar_count": 3},
        }

        with mock.patch(
            "ibkr_compute.api.ops.bar_truth_compare.load_chart_timeline_source_bars",
            return_value=stored_source,
        ):
            with mock.patch(
                "ibkr_compute.api.ops.bar_truth_compare.load_chart_compare_ibkr_source_bars",
                return_value=ibkr_source,
            ):
                with mock.patch(
                    "ibkr_compute.api.ops.bar_truth_compare._api_app",
                    return_value=SimpleNamespace(CHART_COMPARE_MAX_MISMATCH_EXAMPLES=8),
                ):
                    with mock.patch(
                        "ibkr_compute.api.chart.compare.diff._api_app",
                        return_value=SimpleNamespace(coerce_int=lambda value, default=0: int(value or default)),
                    ):
                        payload = build_bar_truth_compare_payload(
                            "live",
                            "AAPL",
                            "5m",
                            start_ms=100,
                            end_ms=300,
                        )

        summary = payload["comparison"]["summary"]
        self.assertEqual(summary["matched_bar_count"], 1)
        self.assertEqual(summary["bar_mismatch_count"], 1)
        self.assertEqual(summary["missing_stored_bar_count"], 1)
        self.assertEqual(summary["missing_ibkr_bar_count"], 0)
        self.assertEqual(summary["indicator_mismatch_count"], 0)
        self.assertEqual(summary["signal_mismatch_count"], 0)
        self.assertEqual(payload["meta"]["audit_mode"], "bar_only")
        self.assertEqual(payload["comparison"]["mismatch_examples"][0]["status"]["bar"], "missing_stored")

    def test_build_truth_audit_summary_tracks_coverage_and_statuses(self):
        rows = [
            build_truth_audit_row(
                environment="live",
                market_date="2026-04-20",
                symbol="SPY",
                interval="5m",
                window_start_ms=1,
                window_end_ms=2,
                comparison_summary={"matched_bar_count": 78},
                mismatch_examples=[],
                source_meta={},
                last_checked_at="2026-04-20T20:15:00-04:00",
            ),
            build_truth_audit_row(
                environment="live",
                market_date="2026-04-20",
                symbol="QQQ",
                interval="5m",
                window_start_ms=1,
                window_end_ms=2,
                comparison_summary={"missing_stored_bar_count": 1},
                mismatch_examples=[],
                source_meta={},
                last_checked_at="2026-04-20T20:16:00-04:00",
            ),
        ]

        summary = build_truth_audit_summary(rows, expected_symbols=["SPY", "QQQ", "AAPL"])

        self.assertFalse(summary["coverage_complete"])
        self.assertEqual(summary["unscanned_symbols"], ["AAPL"])
        self.assertEqual(summary["status_counts"]["ok"], 1)
        self.assertEqual(summary["status_counts"]["error"], 1)
        self.assertEqual(summary["latest_checked_at"], "2026-04-20T20:16:00-04:00")


if __name__ == "__main__":
    unittest.main()
