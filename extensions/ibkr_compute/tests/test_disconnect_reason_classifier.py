import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker.disconnect_reason import classify_ibkr_disconnect, classification_detail_fields
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

    def test_socket_errors_during_ibc_auto_restart_window_are_planned(self):
        result = classify_ibkr_disconnect(
            {
                "gateway_running": True,
                "status_code": 503,
                "last_error_code": 1781395514559,
                "last_error": "502",
                "recent_errors": [
                    {"code": 1781395514559, "message": "502"},
                ],
            },
            gateway_status={
                "running": True,
                "status_code": 502,
                "api_socket_listening": False,
                "ibc_auto_restart_time": "08:05 PM",
            },
            now=datetime(2026, 6, 13, 20, 5, tzinfo=ET),
        )

        self.assertEqual(result["reason_code"], "scheduled_gateway_restart")
        self.assertEqual(result["level"], "info")
        self.assertEqual(result["evidence"]["error_codes"], [502])
        self.assertTrue(result["evidence"]["in_ibc_auto_restart_window"])
        detail = classification_detail_fields(result)
        self.assertEqual(detail["IB错误码"], "502")
        self.assertNotIn("1781395514559", detail["最近错误"])

    def test_socket_errors_outside_ibc_auto_restart_window_remain_local(self):
        result = classify_ibkr_disconnect(
            {
                "gateway_running": True,
                "status_code": 503,
                "last_error_code": 502,
                "last_error": "Couldn't connect to TWS",
            },
            gateway_status={
                "running": True,
                "status_code": 502,
                "api_socket_listening": False,
                "ibc_auto_restart_time": "08:05 PM",
            },
            now=datetime(2026, 6, 13, 20, 30, tzinfo=ET),
        )

        self.assertEqual(result["reason_code"], "local_socket_unreachable")
        self.assertEqual(result["level"], "warning")

    def test_missing_gateway_api_socket_is_local_socket_unreachable(self):
        result = classify_ibkr_disconnect(
            {
                "gateway_running": True,
                "status_code": 401,
                "last_error_code": 0,
            },
            gateway_status={
                "running": True,
                "status_code": 502,
                "api_socket_listening": False,
                "api_socket_port": 4001,
            },
            now=datetime(2026, 5, 24, 3, 0, tzinfo=ET),
        )

        self.assertEqual(result["reason_code"], "local_socket_unreachable")
        self.assertEqual(result["confidence"], "high")
        self.assertFalse(result["evidence"]["api_socket_listening"])
        self.assertEqual(result["evidence"]["api_socket_port"], 4001)

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
