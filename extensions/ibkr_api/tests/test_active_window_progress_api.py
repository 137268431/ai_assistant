import sys
import unittest
from pathlib import Path


SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))


from ibkr_api.universe.active_window_progress import build_active_window_progress_response


class FakePocketBase:
    def get_runtime_config(self, **kwargs):
        return [
            {"key": "signal_window_max_bars", "value": "12", "environment": "global"},
            {"key": "signal_window_max_bars", "value": "9", "environment": "live"},
        ]

    def get_records(self, collection, **kwargs):
        if collection == "ibkr_targets":
            return [
                {
                    "symbol": "AAPL",
                    "status": "active",
                    "score": 1,
                    "direction_bias": "long",
                }
            ]
        return []

    def get_all_records(self, collection, **kwargs):
        return []


class ActiveWindowProgressApiTest(unittest.TestCase):
    def test_progress_response_builds_active_window_summary_without_bars(self):
        payload, status_code = build_active_window_progress_response(
            FakePocketBase(),
            payload={"environment": "live", "status": "active", "date": "2026-04-28"},
            normalize_environment=lambda value, default="live": str(value or default).strip().lower(),
            time_strings=lambda: {
                "date": "2026-04-28",
                "us": "2026-04-28 10:00:00",
                "cn": "2026-04-28 22:00:00",
            },
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["signal_window_max_bars"], 9)
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["items"][0]["window_status"], "no_window")
        self.assertEqual(payload["items"][0]["window_max_bars"], 9)
        self.assertEqual(payload["items"][0]["trace_error"], "no_bars")


if __name__ == "__main__":
    unittest.main()
