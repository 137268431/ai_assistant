import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.universe.target_execution import build_target_execution_metadata


class TargetExecutionDataQualityTest(unittest.TestCase):
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
