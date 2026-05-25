import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker.ib_gateway_service import GatewayServiceManager


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
