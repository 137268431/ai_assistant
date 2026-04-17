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


if __name__ == "__main__":
    unittest.main()
