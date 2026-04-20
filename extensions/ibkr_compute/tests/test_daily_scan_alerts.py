import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration import market_universe as market_universe_mod
from ibkr_compute.orchestration.market_universe import TradingServiceMarketUniverseMixin


class DummyDailyScanAlerts(TradingServiceMarketUniverseMixin):
    def __init__(self):
        self._current_market_date = "2026-04-20"
        self._scan_state_lock = threading.Lock()
        self._daily_scan_state = self._initial_daily_scan_state(self._current_market_date)
        self._daily_scan_alert_market_date = ""
        self._daily_scan_alert_error = ""
        self._daily_scan_alert_title = ""
        self._daily_scan_alert_at = 0.0
        self._daily_scan_alert_active = False
        self._daily_scan_failure_count = 0
        self.events = []

    def _market_date(self) -> str:
        return self._current_market_date

    def _now_et(self) -> str:
        return "2026-04-20T09:25:45-04:00"

    def _runtime_phase_label(self) -> str:
        return "running"

    def _runtime_page_url(self) -> str:
        return "https://pb.example/ibkr_runtime.html?environment=live"

    def _emit_system_event(self, event_type: str, level: str, title: str, detail: dict, *, message_id: str = ""):
        self.events.append(
            {
                "event_type": event_type,
                "level": level,
                "title": title,
                "detail": dict(detail),
                "message_id": message_id,
            }
        )
        return {"ok": True}


class DailyScanAlertStateTest(unittest.TestCase):
    def setUp(self):
        self.service = DummyDailyScanAlerts()
        self.service_mod = types.SimpleNamespace(
            ENVIRONMENT="live",
            DAILY_SCAN_EVENT_ALERT_COOLDOWN_SECONDS=1800,
            logger=types.SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        self.patcher = mock.patch.object(market_universe_mod, "_service_mod", return_value=self.service_mod)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_failure_alert_is_sent_once_within_cooldown(self):
        failed_state = {
            "market_date": "2026-04-20",
            "status": "failed",
            "reason": "poll",
            "last_error": "interval_to_ms missing",
            "result": {},
        }

        with mock.patch.object(market_universe_mod.time, "time", side_effect=[100.0, 110.0]):
            self.service._notify_daily_scan_failed(failed_state)
            self.service._notify_daily_scan_failed(failed_state)

        self.assertEqual(len(self.service.events), 1)
        self.assertEqual(self.service.events[0]["level"], "error")
        self.assertEqual(self.service.events[0]["title"], "IBKR 盘前日筛失败")
        self.assertEqual(self.service.events[0]["detail"]["错误信息"], "interval_to_ms missing")
        self.assertEqual(self.service._daily_scan_failure_count, 2)
        self.assertTrue(self.service._daily_scan_alert_active)

    def test_recovery_alert_clears_active_failure_state(self):
        failed_state = {
            "market_date": "2026-04-20",
            "status": "failed",
            "reason": "poll",
            "last_error": "interval_to_ms missing",
            "result": {},
        }
        completed_state = {
            "market_date": "2026-04-20",
            "status": "completed",
            "reason": "manual_reconcile_after_scan",
            "last_error": "",
            "result": {"scanned": 117, "active": 8, "candidates": 0, "errors": 0},
        }

        with mock.patch.object(market_universe_mod.time, "time", return_value=100.0):
            self.service._notify_daily_scan_failed(failed_state)
        self.service._notify_daily_scan_recovered(completed_state)

        self.assertEqual(len(self.service.events), 2)
        self.assertEqual(self.service.events[1]["level"], "info")
        self.assertEqual(self.service.events[1]["title"], "IBKR 盘前日筛已恢复")
        self.assertEqual(
            self.service.events[1]["detail"]["恢复结果"],
            "scanned=117 active=8 candidate=0 errors=0",
        )
        self.assertFalse(self.service._daily_scan_alert_active)
        self.assertEqual(self.service._daily_scan_failure_count, 0)


if __name__ == "__main__":
    unittest.main()
