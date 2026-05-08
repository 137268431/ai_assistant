import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
API_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
for path in (SRC_ROOT, API_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ibkr_compute.core.active_window_admission import (  # noqa: E402
    build_active_window_admission_item,
    is_active_window_admitted,
)
from ibkr_api.system.jobs.intraday_window_admission import _window_is_valid  # noqa: E402


class ActiveWindowAdmissionTests(unittest.TestCase):
    def test_live_and_backtest_share_sd_window_admission_predicate(self):
        item = {
            "window_status": "lower_active",
            "trace_stage": "none",
            "sd_upper_valid": False,
            "sd_lower_valid": True,
        }

        self.assertTrue(is_active_window_admitted(item))
        self.assertTrue(_window_is_valid(item))

    def test_blocked_trace_rejects_otherwise_valid_window(self):
        item = {
            "window_status": "blocked",
            "trace_stage": "blocked",
            "sd_upper_valid": True,
            "sd_lower_valid": False,
        }

        self.assertFalse(is_active_window_admitted(item))
        self.assertFalse(_window_is_valid(item))

    def test_trace_payload_builds_flat_admission_fields(self):
        admission = build_active_window_admission_item(
            {
                "signal_state": {"stage": "none"},
                "window_flags": {
                    "sd_upper_valid": True,
                    "sd_upper_active": True,
                    "sd_upper_age_bars": 3,
                },
                "component_flags": {"sd_upper_bear_fractal_seen": True},
            },
            signal_window_max_bars=12,
        )

        self.assertTrue(admission["admitted"])
        self.assertEqual(admission["window_status"], "upper_active")
        self.assertEqual(admission["bars_remaining"], 9)
        self.assertEqual(admission["components"]["best_group"], "type3_short_mr")


if __name__ == "__main__":
    unittest.main()
