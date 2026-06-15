from __future__ import annotations

import sys
import unittest
from pathlib import Path

API_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(API_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(API_SRC_ROOT))

from ibkr_api.system.jobs.monitor_alert import _detail


class MonitorAlertConflictDetailTest(unittest.TestCase):
    def test_detail_includes_market_data_session_conflict_state(self):
        detail = _detail(
            {
                "status": "error",
                "runtime": {
                    "session": {"authenticated": True},
                    "websocket": {"connected": True},
                    "market_data_session_conflict": {
                        "active": True,
                        "first_seen_at": "2026-06-15T14:17:46+00:00",
                        "last_seen_at": "2026-06-15T15:56:45+00:00",
                        "count": 8,
                    },
                },
                "service_monitor": {"services": {"ibkr-runtime": {}}, "status_counts": {"running": 1}},
                "flags": [],
            },
            [
                {
                    "severity": "error",
                    "code": "market_data_session_conflict",
                    "title": "Market data session conflict",
                    "detail": "No market data during competing live session",
                }
            ],
            timestamp_us="2026-06-15 11:56:45",
        )

        self.assertEqual("active", detail["行情冲突"])
        self.assertEqual("2026-06-15T14:17:46+00:00", detail["冲突首次"])
        self.assertEqual(8, detail["冲突次数"])
        self.assertIn("1-3 分钟", detail["处理建议"])


if __name__ == "__main__":
    unittest.main()
