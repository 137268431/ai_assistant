import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker.disconnect_reason import classify_ibkr_disconnect
from ibkr_compute.broker.ib_gateway_session import SocketSessionKeeper
from ibkr_compute.core.time_utils import ET


class DisconnectReasonClassifierTest(unittest.TestCase):
    def test_daily_reset_window_prefers_reset_over_later_client_id_conflict(self):
        result = classify_ibkr_disconnect(
            {
                "gateway_running": True,
                "status_code": 503,
                "last_error_code": 326,
                "recent_errors": [
                    {"code": 1100, "message": "Connectivity between IBKR and Trader Workstation has been lost."},
                    {"code": 326, "message": "Unable to connect as the client id is already in use."},
                ],
            },
            interruption_kind="session_expired",
            now=datetime(2026, 5, 24, 0, 16, tzinfo=ET),
        )

        self.assertEqual(result["reason_code"], "ibkr_daily_reset")
        self.assertEqual(result["level"], "info")
        self.assertEqual(result["confidence"], "high")

    def test_ibkr_disconnect_outside_reset_window_is_upstream_warning(self):
        result = classify_ibkr_disconnect(
            {
                "gateway_running": True,
                "status_code": 503,
                "recent_errors": [
                    {"code": 1100, "message": "Connectivity between IBKR and Trader Workstation has been lost."},
                ],
            },
            now=datetime(2026, 5, 24, 12, 16, tzinfo=ET),
        )

        self.assertEqual(result["reason_code"], "ibkr_upstream_disconnect")
        self.assertEqual(result["level"], "warning")

    def test_gateway_down_is_local_error(self):
        result = classify_ibkr_disconnect(
            {
                "gateway_running": False,
                "status_code": 503,
            },
            now=datetime(2026, 5, 24, 0, 16, tzinfo=ET),
        )

        self.assertEqual(result["reason_code"], "local_gateway_down")
        self.assertEqual(result["level"], "error")

    def test_socket_errors_are_local_socket_unreachable(self):
        result = classify_ibkr_disconnect(
            {
                "gateway_running": True,
                "status_code": 503,
                "last_error_code": 502,
                "last_error": "Couldn't connect to TWS",
            },
            now=datetime(2026, 5, 24, 3, 0, tzinfo=ET),
        )

        self.assertEqual(result["reason_code"], "local_socket_unreachable")
        self.assertEqual(result["level"], "warning")

    def test_client_id_conflict_without_disconnect_codes_is_distinct(self):
        result = classify_ibkr_disconnect(
            {
                "gateway_running": True,
                "status_code": 503,
                "last_error_code": 326,
                "last_error": "Unable to connect as the client id is already in use.",
            },
            now=datetime(2026, 5, 24, 0, 16, tzinfo=ET),
        )

        self.assertEqual(result["reason_code"], "client_id_conflict")
        self.assertEqual(result["level"], "warning")

    def test_unauthenticated_ready_timeout_is_auth_not_ready(self):
        result = classify_ibkr_disconnect(
            {
                "gateway_running": True,
                "status_code": 401,
                "last_error_code": 0,
            },
            now=datetime(2026, 5, 24, 3, 0, tzinfo=ET),
        )

        self.assertEqual(result["reason_code"], "auth_not_ready")


class SessionKeeperTransitionPayloadTest(unittest.TestCase):
    def test_session_expired_callback_receives_status_snapshot(self):
        snapshots = []
        broker = SimpleNamespace(health=lambda: {"ready": False, "status_code": 401})
        gateway = SimpleNamespace(is_running=True)
        keeper = SocketSessionKeeper(
            broker=broker,
            gateway_manager=gateway,
            on_session_expired=snapshots.append,
        )
        keeper.is_authenticated = True

        status = keeper.check_auth_status()

        self.assertFalse(status["authenticated"])
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["status_code"], 401)
        self.assertFalse(snapshots[0]["authenticated"])

    def test_zero_argument_callback_remains_supported(self):
        calls = []
        broker = SimpleNamespace(health=lambda: {"ready": False, "status_code": 401})
        gateway = SimpleNamespace(is_running=True)
        keeper = SocketSessionKeeper(
            broker=broker,
            gateway_manager=gateway,
            on_session_expired=lambda: calls.append("called"),
        )
        keeper.is_authenticated = True

        keeper.check_auth_status()

        self.assertEqual(calls, ["called"])


if __name__ == "__main__":
    unittest.main()
