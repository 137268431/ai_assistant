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
                    "websocket": {"connected": True, "subscribed_count": 3, "pending_count": 0},
                    "gateway": {"running": True, "broker": {"connected": True, "ready": True}},
                    "order_flow": {"enabled": False},
                    "signal_router": {"signal_source": "tradingview"},
                    "market_data_session_conflict": {
                        "active": True,
                        "first_seen_at": "2026-06-15T14:17:46+00:00",
                        "last_seen_at": "2026-06-15T15:56:45+00:00",
                        "last_error_at": "2026-06-15T15:55:00+00:00",
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
        self.assertEqual("正常", detail["下单通道"])
        self.assertEqual("当前不阻断下单", detail["下单影响"])
        self.assertEqual("tradingview", detail["信号来源"])
        self.assertEqual("disabled", detail["OrderFlow"])
        self.assertEqual("subscribed:3 | pending:0", detail["行情订阅"])
        self.assertIn("live 行情会话冲突", detail["处理建议"])
        self.assertIn("1-3 分钟", detail["处理建议"])

    def test_detail_marks_quote_dependent_order_flow_impact(self):
        detail = _detail(
            {
                "status": "error",
                "runtime": {
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "subscribed_count": 4, "pending_count": 1},
                    "gateway": {"running": True, "broker": {"connected": True, "ready": True}},
                    "order_flow": {"enabled": True},
                    "signal_router": {"signal_source": "tradingview"},
                    "market_data_session_conflict": {
                        "active": True,
                        "first_seen_at": "2026-06-15T14:17:46+00:00",
                        "last_seen_at": "2026-06-15T15:56:45+00:00",
                        "last_error_at": "2026-06-15T15:55:00+00:00",
                        "count": 3,
                    },
                },
                "service_monitor": {"services": {"ibkr-runtime": {}}, "status_counts": {"running": 1}},
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

        self.assertEqual("正常但报价相关逻辑可能受影响", detail["下单通道"])
        self.assertEqual("可能影响依赖实时报价的入场/出场", detail["下单影响"])
        self.assertEqual("enabled", detail["OrderFlow"])
        self.assertEqual("subscribed:4 | pending:1", detail["行情订阅"])

    def test_detail_does_not_claim_order_path_ok_when_session_unavailable(self):
        detail = _detail(
            {
                "status": "error",
                "runtime": {
                    "session": {"authenticated": False},
                    "websocket": {"connected": True, "subscribed_count": 3},
                    "gateway": {"running": True, "broker": {"connected": False, "ready": False}},
                    "order_flow": {"enabled": False},
                    "signal_router": {"signal_source": "tradingview"},
                    "market_data_session_conflict": {
                        "active": True,
                        "first_seen_at": "2026-06-15T14:17:46+00:00",
                        "last_seen_at": "2026-06-15T15:56:45+00:00",
                    },
                },
                "service_monitor": {"services": {"ibkr-runtime": {}}, "status_counts": {"running": 1}},
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

        self.assertEqual("未知/可能受影响", detail["下单通道"])
        self.assertEqual("订单通道可能受影响", detail["下单影响"])


if __name__ == "__main__":
    unittest.main()
