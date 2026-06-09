import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.runtime import common, restore


class RuntimeRestoreRaceTest(unittest.TestCase):
    def test_get_ibkr_service_initializes_singleton_once_under_concurrency(self):
        created = []
        created_lock = threading.Lock()
        start_event = threading.Event()
        fake_app = types.SimpleNamespace(
            _ibkr_service=None,
            _ibkr_service_lock=threading.Lock(),
        )

        class FakeService:
            def __init__(self):
                with created_lock:
                    created.append(time.time())
                time.sleep(0.05)

        fake_module = types.ModuleType("ibkr_compute.ibkr_service")
        fake_module.IBKRTradingService = FakeService

        results = []

        def worker():
            start_event.wait()
            results.append(common.get_ibkr_service())

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(12)]
        for thread in threads:
            thread.start()

        with mock.patch.object(common, "_api_app", return_value=fake_app):
            with mock.patch.dict(sys.modules, {"ibkr_compute.ibkr_service": fake_module}):
                start_event.set()
                for thread in threads:
                    thread.join(timeout=2)

        self.assertEqual(len(created), 1)
        self.assertEqual(len(results), 12)
        self.assertTrue(all(item is results[0] for item in results))

    def test_auto_restore_requests_background_start_once_under_concurrency(self):
        start_event = threading.Event()
        restore_calls = []
        fake_app = types.SimpleNamespace(
            _ibkr_restore_attempted=False,
            _ibkr_restore_lock=threading.Lock(),
            build_runtime_timestamps=lambda: {"us": "2026-04-17 15:04:00"},
        )

        class FakeSessionKeeper:
            def check_auth_status(self):
                time.sleep(0.05)
                return {"authenticated": True}

        class FakeService:
            is_busy = False
            is_starting = False
            is_running = False

            def __init__(self):
                self.session_keeper = FakeSessionKeeper()
                self.clear_calls = 0

            def clear_stale_startup_cycle(self, reason: str, source: str):
                self.clear_calls += 1

            def auto_restore_guard(self):
                return {"blocked": False}

            def status(self):
                return {"environment": "live"}

        service = FakeService()

        def worker():
            start_event.wait()
            restore._maybe_restore_ibkr_service(service)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(12)]
        for thread in threads:
            thread.start()

        with mock.patch.object(restore, "_api_app", return_value=fake_app):
            with mock.patch.object(restore, "get_ibkr_runtime_control", return_value={"desired_running": True}):
                with mock.patch.object(restore, "set_ibkr_runtime_control", return_value={"ok": True}):
                    with mock.patch.object(restore, "_background_start_ibkr_service", side_effect=lambda *args, **kwargs: restore_calls.append((args, kwargs))):
                        start_event.set()
                        for thread in threads:
                            thread.join(timeout=2)

        self.assertTrue(fake_app._ibkr_restore_attempted)
        self.assertEqual(len(restore_calls), 1)
        self.assertEqual(service.clear_calls, 1)

    def test_status_restore_can_skip_blocking_auth_refresh(self):
        restore_calls = []
        fake_app = types.SimpleNamespace(
            _ibkr_restore_attempted=False,
            _ibkr_restore_lock=threading.Lock(),
            build_runtime_timestamps=lambda: {"us": "2026-04-28 10:00:00"},
        )

        class FakeSessionKeeper:
            def __init__(self):
                self.check_called = False

            def status(self):
                return {"authenticated": False}

            def check_auth_status(self):
                self.check_called = True
                raise AssertionError("status endpoint must not run a blocking auth probe")

        class FakeService:
            is_busy = False
            is_starting = False
            is_running = False

            def __init__(self):
                self.session_keeper = FakeSessionKeeper()

            def auto_restore_guard(self):
                return {"blocked": False}

            def status(self):
                return {"environment": "live"}

        service = FakeService()

        with mock.patch.object(restore, "_api_app", return_value=fake_app):
            with mock.patch.object(restore, "get_ibkr_runtime_control", return_value={"desired_running": True}):
                with mock.patch.object(restore, "set_ibkr_runtime_control", return_value={"ok": True}):
                    with mock.patch.object(restore, "_background_start_ibkr_service", side_effect=lambda *args, **kwargs: restore_calls.append((args, kwargs))):
                        restore._maybe_restore_ibkr_service(service, refresh_auth=False, block=False)

        self.assertFalse(service.session_keeper.check_called)
        self.assertTrue(fake_app._ibkr_restore_attempted)
        self.assertEqual(len(restore_calls), 1)

    def test_attempted_restore_self_heals_stopped_authenticated_runtime(self):
        restore_calls = []
        control_updates = []
        fake_app = types.SimpleNamespace(
            _ibkr_restore_attempted=True,
            _ibkr_restore_lock=threading.Lock(),
            build_runtime_timestamps=lambda: {
                "computed_at_ms": 1780000000000,
                "computed_at_us": "2026-06-09 00:00:00",
            },
        )

        class FakeSessionKeeper:
            is_authenticated = True

            def status(self):
                return {"authenticated": True}

        class FakeService:
            is_busy = False
            is_starting = False
            is_running = False

            def __init__(self):
                self.session_keeper = FakeSessionKeeper()
                self.clear_calls = []
                self.events = []

            def clear_stale_startup_cycle(self, reason: str, source: str):
                self.clear_calls.append((reason, source))

            def auto_restore_guard(self):
                return {"blocked": False}

            def _emit_system_event(self, *args, **kwargs):
                self.events.append((args, kwargs))

            def status(self):
                return {"environment": "paper"}

        service = FakeService()

        with mock.patch.object(restore, "_api_app", return_value=fake_app):
            with mock.patch.object(
                restore,
                "get_ibkr_runtime_control",
                return_value={"desired_running": True, "self_heal_attempt_count": 0},
            ):
                with mock.patch.object(
                    restore,
                    "set_ibkr_runtime_control",
                    side_effect=lambda *args, **kwargs: control_updates.append((args, kwargs)) or {"ok": True},
                ):
                    with mock.patch.object(restore, "patch_ibkr_runtime_control"):
                        with mock.patch.object(
                            restore,
                            "_background_start_ibkr_service",
                            side_effect=lambda *args, **kwargs: restore_calls.append((args, kwargs)),
                        ):
                            restore._maybe_restore_ibkr_service(service, refresh_auth=False)

        self.assertTrue(fake_app._ibkr_restore_attempted)
        self.assertEqual(len(restore_calls), 1)
        self.assertEqual(restore_calls[0][1]["reason"], "runtime_stopped_self_heal")
        self.assertEqual(restore_calls[0][1]["source"], "runtime_self_heal")
        self.assertEqual(service.clear_calls, [("authenticated_before_restore", "runtime_self_heal")])
        self.assertEqual(len(control_updates), 1)
        self.assertEqual(control_updates[0][0], ("paper", True))
        self.assertEqual(control_updates[0][1]["source"], "runtime_self_heal")
        self.assertEqual(control_updates[0][1]["reason"], "runtime_stopped_self_heal")
        self.assertEqual(control_updates[0][1]["extra"]["self_heal_attempt_count"], 1)
        self.assertEqual(control_updates[0][1]["extra"]["last_self_heal_attempt_ms"], 1780000000000)
        self.assertEqual(len(service.events), 1)

    def test_attempted_restore_does_not_self_heal_when_desired_stopped(self):
        restore_calls = []
        fake_app = types.SimpleNamespace(
            _ibkr_restore_attempted=True,
            _ibkr_restore_lock=threading.Lock(),
            build_runtime_timestamps=lambda: {
                "computed_at_ms": 1780000000000,
                "computed_at_us": "2026-06-09 00:00:00",
            },
        )

        class FakeSessionKeeper:
            is_authenticated = True

        class FakeService:
            is_busy = False
            is_starting = False
            is_running = False

            def __init__(self):
                self.session_keeper = FakeSessionKeeper()

            def status(self):
                return {"environment": "paper"}

        with mock.patch.object(restore, "_api_app", return_value=fake_app):
            with mock.patch.object(restore, "get_ibkr_runtime_control", return_value={"desired_running": False}):
                with mock.patch.object(
                    restore,
                    "_background_start_ibkr_service",
                    side_effect=lambda *args, **kwargs: restore_calls.append((args, kwargs)),
                ):
                    restore._maybe_restore_ibkr_service(FakeService(), refresh_auth=False)

        self.assertEqual(restore_calls, [])

    def test_attempted_restore_self_heal_respects_cooldown(self):
        restore_calls = []
        patch_updates = []
        fake_app = types.SimpleNamespace(
            _ibkr_restore_attempted=True,
            _ibkr_restore_lock=threading.Lock(),
            build_runtime_timestamps=lambda: {
                "computed_at_ms": 1780000030000,
                "computed_at_us": "2026-06-09 00:00:30",
            },
        )

        class FakeSessionKeeper:
            is_authenticated = True

        class FakeService:
            is_busy = False
            is_starting = False
            is_running = False

            def __init__(self):
                self.session_keeper = FakeSessionKeeper()

            def auto_restore_guard(self):
                return {"blocked": False}

            def clear_stale_startup_cycle(self, reason: str, source: str):
                return None

            def status(self):
                return {"environment": "paper"}

        control = {
            "desired_running": True,
            "last_self_heal_attempt_ms": 1780000000000,
            "self_heal_attempt_count": 1,
        }
        with mock.patch.dict("os.environ", {"IBKR_RUNTIME_SELF_HEAL_COOLDOWN_SECONDS": "90"}):
            with mock.patch.object(restore, "_api_app", return_value=fake_app):
                with mock.patch.object(restore, "get_ibkr_runtime_control", return_value=control):
                    with mock.patch.object(
                        restore,
                        "patch_ibkr_runtime_control",
                        side_effect=lambda *args, **kwargs: patch_updates.append((args, kwargs)) or {"ok": True},
                    ):
                        with mock.patch.object(
                            restore,
                            "_background_start_ibkr_service",
                            side_effect=lambda *args, **kwargs: restore_calls.append((args, kwargs)),
                        ):
                            restore._maybe_restore_ibkr_service(FakeService(), refresh_auth=False)

        self.assertEqual(restore_calls, [])
        self.assertEqual(patch_updates[-1][0][1]["last_self_heal_blocked_reason"], "self_heal_cooldown")

    def test_running_runtime_records_websocket_unready_before_restart_grace(self):
        patch_updates = []
        fake_app = types.SimpleNamespace(
            _ibkr_restore_attempted=True,
            _ibkr_restore_lock=threading.Lock(),
            build_runtime_timestamps=lambda: {
                "computed_at_ms": 1780000000000,
                "computed_at_us": "2026-06-09 00:00:00",
            },
        )

        class FakeSessionKeeper:
            is_authenticated = True

        class FakeWebSocket:
            def status(self):
                return {"connected": False, "ready": False, "running": False}

        class FakeService:
            is_busy = True
            is_starting = False
            is_running = True

            def __init__(self):
                self.session_keeper = FakeSessionKeeper()
                self.ws_client = FakeWebSocket()
                self.restart_calls = []

            def auto_restore_guard(self):
                return {"blocked": False}

            def _schedule_auth_restart(self, **kwargs):
                self.restart_calls.append(kwargs)
                return True

            def status(self):
                return {"environment": "paper"}

        service = FakeService()
        with mock.patch.object(restore, "_api_app", return_value=fake_app):
            with mock.patch.object(
                restore,
                "get_ibkr_runtime_control",
                return_value={"desired_running": True, "self_heal_attempt_count": 0},
            ):
                with mock.patch.object(
                    restore,
                    "patch_ibkr_runtime_control",
                    side_effect=lambda *args, **kwargs: patch_updates.append((args, kwargs)) or {"ok": True},
                ):
                    restore._maybe_restore_ibkr_service(service, refresh_auth=False)

        self.assertEqual(service.restart_calls, [])
        self.assertEqual(patch_updates[0][0][1]["websocket_unready_since_ms"], 1780000000000)
        self.assertEqual(patch_updates[0][0][1]["last_self_heal_reason"], "websocket_not_ready_observed")

    def test_running_runtime_restarts_after_websocket_unready_grace(self):
        control_updates = []
        fake_app = types.SimpleNamespace(
            _ibkr_restore_attempted=True,
            _ibkr_restore_lock=threading.Lock(),
            build_runtime_timestamps=lambda: {
                "computed_at_ms": 1780000400000,
                "computed_at_us": "2026-06-09 00:06:40",
            },
        )

        class FakeSessionKeeper:
            is_authenticated = True

        class FakeWebSocket:
            def status(self):
                return {"connected": False, "ready": False, "running": False}

        class FakeService:
            is_busy = True
            is_starting = False
            is_running = True

            def __init__(self):
                self.session_keeper = FakeSessionKeeper()
                self.ws_client = FakeWebSocket()
                self.restart_calls = []

            def auto_restore_guard(self):
                return {"blocked": False}

            def _schedule_auth_restart(self, **kwargs):
                self.restart_calls.append(kwargs)
                return True

            def _emit_system_event(self, *args, **kwargs):
                return {"ok": True}

            def status(self):
                return {"environment": "paper"}

        control = {
            "desired_running": True,
            "websocket_unready_since_ms": 1780000000000,
            "last_self_heal_attempt_ms": 0,
            "self_heal_attempt_count": 0,
        }
        service = FakeService()
        with mock.patch.dict("os.environ", {"IBKR_RUNTIME_WEBSOCKET_SELF_HEAL_GRACE_SECONDS": "300"}):
            with mock.patch.object(restore, "_api_app", return_value=fake_app):
                with mock.patch.object(restore, "get_ibkr_runtime_control", return_value=control):
                    with mock.patch.object(
                        restore,
                        "set_ibkr_runtime_control",
                        side_effect=lambda *args, **kwargs: control_updates.append((args, kwargs)) or {"ok": True},
                    ):
                        restore._maybe_restore_ibkr_service(service, refresh_auth=False)

        self.assertEqual(service.restart_calls, [{
            "reason": "websocket_not_ready_self_heal",
            "source": "runtime_self_heal",
            "trigger_login": False,
        }])
        self.assertEqual(control_updates[0][0], ("paper", True))
        self.assertEqual(control_updates[0][1]["source"], "runtime_self_heal")
        self.assertEqual(control_updates[0][1]["reason"], "websocket_not_ready_self_heal")
        self.assertEqual(control_updates[0][1]["extra"]["self_heal_attempt_count"], 1)
        self.assertEqual(control_updates[0][1]["extra"]["websocket_unready_since_ms"], 0)

    def test_nonblocking_restore_returns_when_lock_is_busy(self):
        fake_app = types.SimpleNamespace(
            _ibkr_restore_attempted=False,
            _ibkr_restore_lock=threading.Lock(),
            build_runtime_timestamps=lambda: {"us": "2026-04-28 10:00:00"},
        )
        fake_app._ibkr_restore_lock.acquire()

        class FakeService:
            is_busy = False
            is_starting = False
            is_running = False

            def status(self):
                return {"environment": "live"}

        try:
            with mock.patch.object(restore, "_api_app", return_value=fake_app):
                started = time.time()
                restore._maybe_restore_ibkr_service(FakeService(), refresh_auth=False, block=False)
                elapsed = time.time() - started
        finally:
            fake_app._ibkr_restore_lock.release()

        self.assertLess(elapsed, 0.1)
        self.assertFalse(fake_app._ibkr_restore_attempted)


if __name__ == "__main__":
    unittest.main()
