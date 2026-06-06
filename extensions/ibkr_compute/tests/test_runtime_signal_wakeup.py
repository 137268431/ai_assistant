import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.runtime import signal_views


class _FakeWakeupEvent:
    def __init__(self):
        self.set_calls = 0

    def set(self):
        self.set_calls += 1


class _FakeSessionKeeper:
    is_authenticated = True


class _FakeService:
    def __init__(self, *, running=True, starting=False):
        self.is_running = running
        self.is_starting = starting
        self.session_keeper = _FakeSessionKeeper()
        self._signal_wakeup = _FakeWakeupEvent()


class RuntimeSignalWakeupTest(unittest.TestCase):
    def test_signal_wakeup_sets_event_and_reports_will_process(self):
        service = _FakeService(running=True)

        with mock.patch.object(signal_views, "get_ibkr_service", return_value=service):
            with mock.patch.object(signal_views, "_ibkr_service_environment", return_value="paper"):
                payload, status_code = signal_views._build_ibkr_signal_wakeup_response(
                    {
                        "source": "tv_webhook",
                        "event_type": "risk_update",
                        "tv_event_id": "tv-risk-1",
                        "route_target": "ibkr_reverse_signals",
                        "route_record_id": "rev-1",
                        "broker_mode": "paper",
                    }
                )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["woke"])
        self.assertTrue(payload["will_process"])
        self.assertEqual("signal_loop_woken", payload["reason"])
        self.assertEqual("paper", payload["environment"])
        self.assertEqual("risk_update", payload["event_type"])
        self.assertEqual(1, service._signal_wakeup.set_calls)

    def test_signal_wakeup_reports_not_running_but_still_sets_event(self):
        service = _FakeService(running=False, starting=True)

        with mock.patch.object(signal_views, "get_ibkr_service", return_value=service):
            with mock.patch.object(signal_views, "_ibkr_service_environment", return_value="paper"):
                payload, status_code = signal_views._build_ibkr_signal_wakeup_response({"broker_mode": "paper"})

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["woke"])
        self.assertFalse(payload["will_process"])
        self.assertEqual("runtime_not_running", payload["reason"])
        self.assertEqual(1, service._signal_wakeup.set_calls)

    def test_signal_wakeup_returns_503_when_service_missing(self):
        with mock.patch.object(signal_views, "get_ibkr_service", return_value=None):
            payload, status_code = signal_views._build_ibkr_signal_wakeup_response({})

        self.assertEqual(status_code, 503)
        self.assertFalse(payload["ok"])
        self.assertEqual("service_unavailable", payload["reason"])


if __name__ == "__main__":
    unittest.main()
