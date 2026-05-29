import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration.market_universe_support import _target_row_is_tradingview_active
from ibkr_compute.universe.target_execution import build_target_execution_metadata


class TargetExecutionDataQualityTest(unittest.TestCase):
    def test_tradingview_active_target_is_execution_eligible_source(self):
        row = {
            "status": "active",
            "direction_bias": "short",
            "extra": {"source": "tradingview", "activity_rank": 1},
        }
        payload = build_target_execution_metadata(row["extra"], direction_bias=row["direction_bias"], status=row["status"])

        self.assertTrue(_target_row_is_tradingview_active(row))
        self.assertTrue(payload["execution_eligible"])
        self.assertEqual("execution", payload["target_layer"])

    def test_truth_proof_red_blocks_execution_metadata(self):
        payload = build_target_execution_metadata(
            {
                "source": "daily_scan",
                "active_gate_passed": True,
                "data_quality": {
                    "status": "ready",
                    "needs_repair": False,
                    "proof_status": "red",
                },
            },
            direction_bias="long",
            status="active",
        )

        self.assertFalse(payload["execution_eligible"])
        self.assertIn("data_quality_not_ready", payload["execution_blockers"])


if __name__ == "__main__":
    unittest.main()
