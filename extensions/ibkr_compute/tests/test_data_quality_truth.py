import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

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
            classify_truth_audit_status({}, error="gateway_not_running"),
            "unavailable",
        )

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
