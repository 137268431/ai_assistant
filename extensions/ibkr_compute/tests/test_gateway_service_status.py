import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker.ib_gateway_service import GatewayServiceManager, _gateway_auth_issue_status


class GatewayServiceStatusTest(unittest.TestCase):
    def _manager(self, broker_status=None):
        broker = SimpleNamespace(status=lambda: dict(broker_status or {}))
        return GatewayServiceManager(service_name="ibkr-gateway", broker=broker)

    def test_active_service_without_api_socket_reports_502_unreachable(self):
        manager = self._manager({"status_code": 401, "host": "127.0.0.1", "port": 4001})

        with mock.patch(
            "ibkr_compute.broker.ib_gateway_service._systemctl_show",
            return_value={"ActiveState": "active", "SubState": "running", "MainPID": "123"},
        ):
            with mock.patch("ibkr_compute.broker.ib_gateway_service._pid_uptime_seconds", return_value=12.3):
                with mock.patch(
                    "ibkr_compute.broker.ib_gateway_service._api_socket_status",
                    return_value={
                        "listening": False,
                        "host": "127.0.0.1",
                        "port": 4001,
                        "source": "ss",
                        "reason": "port_not_listening",
                    },
                ):
                    payload = manager.status()

        self.assertTrue(payload["running"])
        self.assertFalse(payload["reachable"])
        self.assertEqual(payload["status_code"], 502)
        self.assertFalse(payload["api_socket_listening"])
        self.assertEqual(payload["api_socket_port"], 4001)

    def test_auth_issue_detects_expired_security_token(self):
        launcher_log = (
            "The security tokens associated with your login credentials have expired. "
            "Please manually enter your username and password to access IBKR Gateway."
        )
        with mock.patch("ibkr_compute.broker.ib_gateway_service._tail_file", return_value=launcher_log):
            with mock.patch("ibkr_compute.broker.ib_gateway_service._read_file", return_value="1780877119957\n1\n"):
                payload = _gateway_auth_issue_status("/tmp/userdir")

        self.assertTrue(payload["auth_issue"])
        self.assertEqual("security_token_expired", payload["auth_issue_reason"])
        self.assertEqual("manual_gateway_login", payload["auth_issue_action_required"])
        self.assertEqual(1, payload["login_failure_count"])

    def test_status_includes_auth_issue_when_socket_down(self):
        manager = self._manager({"status_code": 401, "host": "127.0.0.1", "port": 4001})

        with mock.patch(
            "ibkr_compute.broker.ib_gateway_service._systemctl_show",
            return_value={"ActiveState": "active", "SubState": "running", "MainPID": "123"},
        ):
            with mock.patch("ibkr_compute.broker.ib_gateway_service._pid_uptime_seconds", return_value=12.3):
                with mock.patch(
                    "ibkr_compute.broker.ib_gateway_service._api_socket_status",
                    return_value={
                        "listening": False,
                        "host": "127.0.0.1",
                        "port": 4001,
                        "source": "ss",
                        "reason": "port_not_listening",
                    },
                ):
                    with mock.patch(
                        "ibkr_compute.broker.ib_gateway_service._gateway_auth_issue_status",
                        return_value={
                            "auth_issue": True,
                            "auth_issue_reason": "security_token_expired",
                            "auth_issue_action_required": "manual_gateway_login",
                            "auth_issue_source": "launcher.log",
                            "login_failure_count": 1,
                        },
                    ):
                        payload = manager.status()

        self.assertTrue(payload["auth_issue"])
        self.assertEqual("security_token_expired", payload["auth_issue_reason"])

    def test_active_service_with_socket_but_no_broker_status_reports_401(self):
        manager = self._manager({"status_code": 0, "host": "127.0.0.1", "port": 4001})

        with mock.patch(
            "ibkr_compute.broker.ib_gateway_service._systemctl_show",
            return_value={"ActiveState": "active", "SubState": "running", "MainPID": "123"},
        ):
            with mock.patch("ibkr_compute.broker.ib_gateway_service._pid_uptime_seconds", return_value=12.3):
                with mock.patch(
                    "ibkr_compute.broker.ib_gateway_service._api_socket_status",
                    return_value={"listening": True, "host": "127.0.0.1", "port": 4001, "source": "ss", "reason": ""},
                ):
                    payload = manager.status()

        self.assertTrue(payload["running"])
        self.assertTrue(payload["reachable"])
        self.assertEqual(payload["status_code"], 401)
        self.assertTrue(payload["api_socket_listening"])


if __name__ == "__main__":
    unittest.main()
