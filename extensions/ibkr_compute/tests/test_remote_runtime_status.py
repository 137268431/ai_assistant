import types
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = SimpleNamespace(args={}, headers={}, get_json=lambda silent=True: {})
    flask_stub.Response = object
    sys.modules["flask"] = flask_stub

from ibkr_compute.api.compute.runtime_ops import _get_runtime_status_snapshot
from ibkr_compute.api.ops.status_views import _build_topology_payload
from ibkr_compute.api.service_topology import build_service_topology


class RemoteRuntimeTopologyTest(unittest.TestCase):
    def test_compute_topology_uses_remote_runtime_snapshot(self):
        runtime_payload = {
            "ok": True,
            "session": {"authenticated": True},
            "gateway": {"running": True, "pid": 319433, "managed_by": "systemd"},
        }
        with mock.patch.dict(
            os.environ,
            {
                "IBKR_SERVICE_PROFILE": "compute",
                "IBKR_RUNTIME_MODE": "remote",
                "IBKR_RUNTIME_INTERNAL_URL": "http://127.0.0.1:5101",
            },
            clear=False,
        ):
            with mock.patch(
                "ibkr_compute.api.service_topology.get_remote_runtime_status",
                return_value=runtime_payload,
            ):
                topology = build_service_topology(service_status={"ok": True})

        runtime_service = topology["services"]["ibkr-runtime"]
        gateway_service = topology["services"]["ibkr-gateway"]
        self.assertEqual(runtime_service["status"], "running")
        self.assertTrue(runtime_service["session_authenticated"])
        self.assertEqual(gateway_service["status"], "running")
        self.assertEqual(gateway_service["pid"], 319433)
        self.assertEqual(gateway_service["managed_by"], "systemd")

    def test_compute_topology_falls_back_when_remote_snapshot_unavailable(self):
        with mock.patch.dict(
            os.environ,
            {
                "IBKR_SERVICE_PROFILE": "compute",
                "IBKR_RUNTIME_MODE": "remote",
            },
            clear=False,
        ):
            with mock.patch(
                "ibkr_compute.api.service_topology.get_remote_runtime_status",
                return_value={},
            ):
                topology = build_service_topology(service_status={"ok": True})

        runtime_service = topology["services"]["ibkr-runtime"]
        gateway_service = topology["services"]["ibkr-gateway"]
        self.assertEqual(runtime_service["status"], "expected_remote")
        self.assertFalse(runtime_service["session_authenticated"])
        self.assertEqual(gateway_service["status"], "offline")


class RemoteRuntimeSnapshotResolverTest(unittest.TestCase):
    def test_runtime_status_snapshot_uses_remote_runtime_when_compute_has_no_local_service(self):
        runtime_payload = {
            "environment": "live",
            "session": {"authenticated": True},
            "gateway": {"running": True},
            "websocket": {"running": True},
        }
        fake_app = SimpleNamespace(get_ibkr_service=lambda: None)

        with mock.patch("ibkr_compute.api.compute.runtime_ops._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.runtime_ops.is_runtime_remote_mode", return_value=True):
                with mock.patch(
                    "ibkr_compute.api.compute.runtime_ops.get_remote_runtime_status",
                    return_value=runtime_payload,
                ):
                    payload = _get_runtime_status_snapshot("live")

        self.assertEqual(payload, runtime_payload)

    def test_runtime_status_snapshot_prefers_remote_runtime_over_local_service_in_remote_mode(self):
        runtime_payload = {
            "environment": "live",
            "session": {"authenticated": True},
            "gateway": {"running": True},
            "websocket": {"running": True},
        }
        fake_service = SimpleNamespace(status=mock.Mock(return_value={"session": {"authenticated": False}}))
        fake_app = SimpleNamespace(get_ibkr_service=lambda: fake_service)

        with mock.patch("ibkr_compute.api.compute.runtime_ops._api_app", return_value=fake_app):
            with mock.patch("ibkr_compute.api.compute.runtime_ops.is_runtime_remote_mode", return_value=True):
                with mock.patch(
                    "ibkr_compute.api.compute.runtime_ops.get_remote_runtime_status",
                    return_value=runtime_payload,
                ):
                    payload = _get_runtime_status_snapshot("live")

        self.assertEqual(payload, runtime_payload)
        fake_service.status.assert_not_called()

    def test_ops_topology_payload_uses_runtime_snapshot_resolver(self):
        runtime_payload = {
            "ok": True,
            "session": {"authenticated": True},
            "gateway": {"running": True, "pid": 319433},
        }
        fake_app = SimpleNamespace(_get_runtime_status_snapshot=lambda environment: runtime_payload)

        with mock.patch.dict(
            os.environ,
            {
                "IBKR_SERVICE_PROFILE": "compute",
                "IBKR_RUNTIME_MODE": "remote",
            },
            clear=False,
        ):
            topology = _build_topology_payload(fake_app, "live")

        self.assertTrue(topology["services"]["ibkr-runtime"]["session_authenticated"])
        self.assertEqual(topology["services"]["ibkr-gateway"]["status"], "running")


if __name__ == "__main__":
    unittest.main()
