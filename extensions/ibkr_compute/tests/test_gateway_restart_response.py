import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.runtime import gateway_views


class _FakeGatewayManager:
    def __init__(self):
        self.restart_calls = 0

    def restart(self):
        self.restart_calls += 1
        return True

    def status(self):
        return {"running": True, "reachable": True, "status_code": 401}


class _FakeSessionKeeper:
    def check_auth_status(self):
        return {"authenticated": False}


class _FakeService:
    def __init__(self, *, running=False, starting=False, startup_active=False, status_payload=None):
        self.is_running = running
        self.is_starting = starting
        self.gateway_manager = _FakeGatewayManager()
        self.session_keeper = _FakeSessionKeeper()
        self.panic_reset_calls = []
        self._startup_active = startup_active
        self._status_payload = status_payload or {}

    def panic_reset_auth(self, **kwargs):
        self.panic_reset_calls.append(dict(kwargs))
        return {"gateway_restarted": True, "runtime_started": True}

    def startup_progress_snapshot(self):
        return {"active": self._startup_active, "status": "active" if self._startup_active else "idle"}

    def status(self):
        return {"environment": "live", **self._status_payload}


class GatewayRestartResponseTest(unittest.TestCase):
    def _build_action_payload(self, service, action, *, ok, message, reason="", source="", extra=None):
        payload = {
            "ok": bool(ok),
            "action": action,
            "message": message,
            "environment": "live",
            "reason": reason,
            "source": source,
            "gateway": service.gateway_manager.status(),
            "runtime_running": bool(getattr(service, "is_running", False)),
            "runtime_starting": bool(getattr(service, "is_starting", False)),
            "startup": service.startup_progress_snapshot(),
        }
        if extra:
            payload.update(extra)
        return payload

    def test_running_gateway_restart_is_accepted_and_backgrounded(self):
        service = _FakeService(running=True)
        fake_app = SimpleNamespace(_ibkr_restore_attempted=True)
        background_calls = []
        control_calls = []

        def fake_background(target_service, **kwargs):
            background_calls.append((target_service, kwargs))
            return threading.Thread(target=lambda: None, daemon=True)

        with mock.patch.object(gateway_views, "_api_app", return_value=fake_app):
            with mock.patch.object(gateway_views, "get_ibkr_service", return_value=service):
                with mock.patch.object(gateway_views, "_ibkr_service_environment", return_value="live"):
                    with mock.patch.object(
                        gateway_views,
                        "set_ibkr_runtime_control",
                        side_effect=lambda *args, **kwargs: control_calls.append((args, kwargs)) or {"ok": True},
                    ):
                        with mock.patch.object(gateway_views, "_background_panic_reset_auth", side_effect=fake_background):
                            with mock.patch.object(
                                gateway_views,
                                "_build_gateway_action_payload",
                                side_effect=self._build_action_payload,
                            ):
                                payload, status_code = gateway_views._build_ibkr_gateway_restart_response(
                                    {"reason": "manual_gateway_restart", "source": "runtime_page"}
                                )

        self.assertEqual(202, status_code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["accepted"])
        self.assertEqual("gateway_restart_fresh_cycle", payload["operation"])
        self.assertTrue(payload["startup_cycle_planned"])
        self.assertTrue(payload["background"])
        self.assertTrue(payload["runtime_restart_requested"])
        self.assertFalse(payload["gateway_restarted"])
        self.assertEqual([], service.panic_reset_calls)
        self.assertEqual(0, service.gateway_manager.restart_calls)
        self.assertEqual(1, len(background_calls))
        self.assertIs(background_calls[0][0], service)
        self.assertEqual(
            {
                "restart_gateway": True,
                "restart_runtime": True,
                "trigger_login": False,
                "reason": "manual_gateway_restart",
                "source": "runtime_page",
            },
            background_calls[0][1],
        )
        self.assertFalse(fake_app._ibkr_restore_attempted)
        self.assertEqual(1, len(control_calls))
        self.assertEqual(("live", True), control_calls[0][0][:2])

    def test_idle_gateway_restart_stays_synchronous(self):
        service = _FakeService(running=False, starting=False, startup_active=False)

        with mock.patch.object(gateway_views, "_api_app", return_value=SimpleNamespace(_ibkr_restore_attempted=True)):
            with mock.patch.object(gateway_views, "get_ibkr_service", return_value=service):
                with mock.patch.object(gateway_views, "_ibkr_service_environment", return_value="live"):
                    with mock.patch.object(gateway_views, "_background_panic_reset_auth") as background:
                        with mock.patch.object(
                            gateway_views,
                            "_build_gateway_action_payload",
                            side_effect=self._build_action_payload,
                        ):
                            payload, status_code = gateway_views._build_ibkr_gateway_restart_response(
                                {"reason": "manual_gateway_restart", "source": "runtime_page"}
                            )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["startup_cycle_planned"])
        self.assertTrue(payload["gateway_restarted"])
        self.assertEqual(1, service.gateway_manager.restart_calls)
        background.assert_not_called()

    def test_running_gateway_restart_blocks_recent_market_data_session_conflict(self):
        service = _FakeService(
            running=True,
            status_payload={
                "data_backfill": {
                    "last_trace": {
                        "error": "Historical Market Data Service error message:Trading TWS session is connected from a different IP address",
                        "finished_at_ms": int(time.time() * 1000),
                        "trace_id": "trace-1",
                    }
                }
            },
        )
        fake_app = SimpleNamespace(_ibkr_restore_attempted=True)

        with mock.patch.object(gateway_views, "_api_app", return_value=fake_app):
            with mock.patch.object(gateway_views, "get_ibkr_service", return_value=service):
                with mock.patch.object(gateway_views, "_ibkr_service_environment", return_value="live"):
                    with mock.patch.object(gateway_views, "set_ibkr_runtime_control") as control:
                        with mock.patch.object(gateway_views, "_background_panic_reset_auth") as background:
                            with mock.patch.object(
                                gateway_views,
                                "_build_gateway_action_payload",
                                side_effect=self._build_action_payload,
                            ):
                                payload, status_code = gateway_views._build_ibkr_gateway_restart_response(
                                    {"reason": "manual_gateway_restart", "source": "runtime_page"}
                                )

        self.assertEqual(409, status_code)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["restart_blocked"])
        self.assertEqual("market_data_session_conflict", payload["blocker_code"])
        self.assertEqual("gateway_restart_blocked", payload["operation"])
        self.assertFalse(payload["gateway_restarted"])
        self.assertFalse(payload["startup_cycle_planned"])
        self.assertEqual(0, service.gateway_manager.restart_calls)
        self.assertTrue(fake_app._ibkr_restore_attempted)
        background.assert_not_called()
        control.assert_not_called()

    def test_idle_gateway_restart_blocks_recent_market_data_session_conflict(self):
        service = _FakeService(
            running=False,
            status_payload={
                "gateway": {
                    "broker": {
                        "last_error": "Trading TWS session is connected from a different IP address",
                    }
                }
            },
        )

        with mock.patch.object(gateway_views, "_api_app", return_value=SimpleNamespace(_ibkr_restore_attempted=True)):
            with mock.patch.object(gateway_views, "get_ibkr_service", return_value=service):
                with mock.patch.object(gateway_views, "_ibkr_service_environment", return_value="live"):
                    with mock.patch.object(gateway_views, "_background_panic_reset_auth") as background:
                        with mock.patch.object(
                            gateway_views,
                            "_build_gateway_action_payload",
                            side_effect=self._build_action_payload,
                        ):
                            payload, status_code = gateway_views._build_ibkr_gateway_restart_response(
                                {"reason": "manual_gateway_restart", "source": "runtime_page"}
                            )

        self.assertEqual(409, status_code)
        self.assertTrue(payload["restart_blocked"])
        self.assertEqual(0, service.gateway_manager.restart_calls)
        background.assert_not_called()

    def test_gateway_restart_blocks_10197_competing_live_session(self):
        service = _FakeService(
            running=True,
            status_payload={
                "gateway": {
                    "broker": {
                        "last_error_code": 10197,
                        "last_error": "No market data during competing live session",
                        "last_error_at": time.time(),
                    }
                }
            },
        )

        with mock.patch.object(gateway_views, "_api_app", return_value=SimpleNamespace(_ibkr_restore_attempted=True)):
            with mock.patch.object(gateway_views, "get_ibkr_service", return_value=service):
                with mock.patch.object(gateway_views, "_ibkr_service_environment", return_value="live"):
                    with mock.patch.object(gateway_views, "_background_panic_reset_auth") as background:
                        with mock.patch.object(
                            gateway_views,
                            "_build_gateway_action_payload",
                            side_effect=self._build_action_payload,
                        ):
                            payload, status_code = gateway_views._build_ibkr_gateway_restart_response(
                                {"reason": "manual_gateway_restart", "source": "runtime_page"}
                            )

        self.assertEqual(409, status_code)
        self.assertTrue(payload["restart_blocked"])
        self.assertEqual("market_data_session_conflict", payload["blocker_code"])
        self.assertEqual(0, service.gateway_manager.restart_calls)
        background.assert_not_called()

    def test_gateway_restart_force_bypasses_market_data_session_conflict(self):
        service = _FakeService(
            running=True,
            status_payload={
                "data_backfill": {
                    "last_trace": {
                        "error": "Trading TWS session is connected from a different IP address",
                        "finished_at_ms": int(time.time() * 1000),
                    }
                }
            },
        )
        background_calls = []

        def fake_background(target_service, **kwargs):
            background_calls.append((target_service, kwargs))
            return threading.Thread(target=lambda: None, daemon=True)

        with mock.patch.object(gateway_views, "_api_app", return_value=SimpleNamespace(_ibkr_restore_attempted=True)):
            with mock.patch.object(gateway_views, "get_ibkr_service", return_value=service):
                with mock.patch.object(gateway_views, "_ibkr_service_environment", return_value="live"):
                    with mock.patch.object(gateway_views, "set_ibkr_runtime_control", return_value={"ok": True}):
                        with mock.patch.object(gateway_views, "_background_panic_reset_auth", side_effect=fake_background):
                            with mock.patch.object(
                                gateway_views,
                                "_build_gateway_action_payload",
                                side_effect=self._build_action_payload,
                            ):
                                payload, status_code = gateway_views._build_ibkr_gateway_restart_response(
                                    {"reason": "manual_gateway_restart", "source": "runtime_page", "force_restart": True}
                                )

        self.assertEqual(202, status_code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["forced"])
        self.assertEqual(1, len(background_calls))

    def test_gateway_restart_ignores_old_data_backfill_session_conflict_trace(self):
        service = _FakeService(
            running=False,
            status_payload={
                "data_backfill": {
                    "last_trace": {
                        "error": "Trading TWS session is connected from a different IP address",
                        "finished_at_ms": int((time.time() - 3600) * 1000),
                    }
                }
            },
        )

        with mock.patch.object(gateway_views, "_api_app", return_value=SimpleNamespace(_ibkr_restore_attempted=True)):
            with mock.patch.object(gateway_views, "get_ibkr_service", return_value=service):
                with mock.patch.object(gateway_views, "_ibkr_service_environment", return_value="live"):
                    with mock.patch.object(
                        gateway_views,
                        "_build_gateway_action_payload",
                        side_effect=self._build_action_payload,
                    ):
                        payload, status_code = gateway_views._build_ibkr_gateway_restart_response(
                            {"reason": "manual_gateway_restart", "source": "runtime_page"}
                        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["gateway_restarted"])
        self.assertEqual(1, service.gateway_manager.restart_calls)


if __name__ == "__main__":
    unittest.main()
