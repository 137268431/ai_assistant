import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.universe.activity_gate import DEFAULT_ACTIVITY_GATE_STAGES_JSON, evaluate_activity_gate
from ibkr_compute.universe.dynamic_admission import evaluate_dynamic_admission


class ActivityGateTest(unittest.TestCase):
    def test_preopen_stage_uses_premarket_volume(self):
        failed = evaluate_activity_gate(
            {"premarket_volume": 2999, "today_volume": 2999, "avg_10d_volume": 1_000_000},
            now_et="08:30",
        )
        passed = evaluate_activity_gate(
            {"premarket_volume": 3000, "today_volume": 3000, "avg_10d_volume": 1_000_000},
            now_et="08:30",
        )

        self.assertEqual(failed["stage_id"], "preopen_early")
        self.assertFalse(failed["passed"])
        self.assertTrue(passed["passed"])
        self.assertEqual(passed["passed_keys"], ["premarket_volume_gte"])

    def test_open_stage_accepts_regular_volume_or_elapsed_rvol(self):
        regular_passed = evaluate_activity_gate(
            {"premarket_volume": 0, "regular_volume": 10_000, "today_volume": 10_000, "avg_10d_volume": 1_000_000},
            now_et="09:40",
        )
        rvol_passed = evaluate_activity_gate(
            {"premarket_volume": 0, "regular_volume": 40_000, "today_volume": 40_000, "avg_10d_volume": 1_000_000},
            now_et="09:40",
        )

        self.assertEqual(regular_passed["stage_id"], "open_discovery")
        self.assertTrue(regular_passed["passed"])
        self.assertIn("regular_volume_gte", regular_passed["passed_keys"])
        self.assertGreaterEqual(rvol_passed["actuals"]["elapsed_rvol"], 1.5)
        self.assertIn("elapsed_rvol_gte", rvol_passed["passed_keys"])

    def test_late_morning_ignores_premarket_only_activity(self):
        result = evaluate_activity_gate(
            {"premarket_volume": 500_000, "regular_volume": 0, "today_volume": 500_000, "avg_10d_volume": 1_000_000},
            now_et="10:35",
        )

        self.assertEqual(result["stage_id"], "late_morning")
        self.assertFalse(result["passed"])
        self.assertEqual(set(result["thresholds"]), {"regular_volume_gte", "elapsed_rvol_gte"})

    def test_dynamic_admission_blocks_failed_stage_activity_gate(self):
        result = evaluate_dynamic_admission(
            "AAPL",
            metrics={
                "price": 100,
                "avg_10d_volume": 1_000_000,
                "premarket_volume": 500_000,
                "today_volume": 500_000,
                "regular_volume": 0,
                "atr_pct": 1.0,
                "day_change_pct": 3.0,
                "has_live_bar": True,
            },
            settings={
                "activity_gate_current_time_et": "10:35",
                "activity_gate_stages_json": DEFAULT_ACTIVITY_GATE_STAGES_JSON,
            },
        )

        self.assertFalse(result["quality_gate_passed"])
        self.assertTrue(any(gate["metric"] == "premarket_volume|today_volume|regular_volume|elapsed_rvol|rvol_20" for gate in result["failed_gates"]))


if __name__ == "__main__":
    unittest.main()
