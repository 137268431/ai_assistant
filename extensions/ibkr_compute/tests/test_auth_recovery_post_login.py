import importlib.util
import logging
import threading
import types
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
AUTH_RECOVERY_PATH = SRC_ROOT / "ibkr_compute" / "orchestration" / "auth_recovery.py"

spec = importlib.util.spec_from_file_location("auth_recovery_under_test", AUTH_RECOVERY_PATH)
auth_recovery_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth_recovery_mod)
auth_recovery_mod._service_mod = lambda: types.SimpleNamespace(
    AUTH_PROBE_INTERVAL_SECONDS=5,
    AUTH_PROBE_WINDOW_SECONDS=45,
    AUTH_PROBE_LATE_SESSION_WINDOW_SECONDS=180,
    AUTH_PROBE_SELF_HEAL_GRACE_SECONDS=90,
    AUTH_POST_LOGIN_PROBE_WINDOW_SECONDS=240,
    AUTH_POST_LOGIN_PROBE_GRACE_SECONDS=180,
    AUTH_PROBE_LATE_SESSION_SELF_HEAL_GRACE_SECONDS=300,
    AUTH_RECOVERY_LOCK_TTL_SECONDS=120,
    AUTH_MANUAL_TAKEOVER_TTL_SECONDS=600,
    AUTH_RECOVERY_PB_FIELDS=(
        "cycle_id",
        "recovery_phase",
        "probe_result",
        "auto_restart_scheduled",
        "last_runtime_authenticated_at",
        "last_recovery_source",
    ),
    ET=None,
    logger=logging.getLogger("auth_recovery_test"),
)
TradingServiceAuthRecoveryMixin = auth_recovery_mod.TradingServiceAuthRecoveryMixin


class FakeAuthHandler:
    def __init__(self):
        self.reports = []

    def _report_2fa_status(self, **kwargs):
        self.reports.append(dict(kwargs))


class FakeAuthRecoveryService(TradingServiceAuthRecoveryMixin):
    def __init__(self, *, startup_active=True):
        self._auth_recovery_lock = threading.RLock()
        self._auth_recovery_state = self._initial_auth_recovery_state()
        self._auth_cycle_seq = 0
        self._running = False
        self._starting = False
        self._auth_restart_thread = None
        self._startup_active = bool(startup_active)
        self.auth_handler = FakeAuthHandler()
        self.pb = None
        self.scheduled_restarts = []
        self._auth_required_reason = ""

    def _now_iso(self):
        return "2026-04-28T11:32:30-04:00"

    def _build_2fa_detail(self, reason):
        return {"reason": reason}

    def startup_progress_snapshot(self):
        return {"active": self._startup_active}

    def _schedule_auth_restart(self, reason, source, trigger_login=False, *, allow_panic_reset=False):
        self.scheduled_restarts.append(
            {
                "reason": reason,
                "source": source,
                "trigger_login": bool(trigger_login),
                "allow_panic_reset": bool(allow_panic_reset),
            }
        )
        return True


class AuthRecoveryPostLoginTest(unittest.TestCase):
    def test_post_login_probe_window_covers_ibkr_second_factor_timeout(self):
        service = FakeAuthRecoveryService()

        self.assertGreaterEqual(service._auth_probe_window_seconds("post_login_2fa"), 240)
        self.assertGreaterEqual(service._auth_probe_self_heal_grace_seconds("post_login_2fa"), 180)

    def test_recovered_manual_start_schedules_runtime_resume(self):
        service = FakeAuthRecoveryService(startup_active=True)
        service._set_auth_recovery_state(
            cycle_id="cycle-1",
            recovery_phase="silent_probe",
            recovery_reason="manual_gateway_restart",
            last_recovery_source="runtime_page",
        )

        service._mark_auth_recovered(source="auth_probe", reason="manual_gateway_restart")

        self.assertEqual(1, len(service.scheduled_restarts))
        self.assertEqual("manual_gateway_restart", service.scheduled_restarts[0]["reason"])
        self.assertEqual("runtime_page", service.scheduled_restarts[0]["source"])
        self.assertFalse(service.scheduled_restarts[0]["trigger_login"])
        self.assertEqual(
            "authenticated_runtime_restart_scheduled",
            service._auth_recovery_state["probe_result"],
        )
        self.assertTrue(service._auth_recovery_state["auto_restart_scheduled"])
        self.assertEqual("success", service.auth_handler.reports[0]["status"])

    def test_manual_probe_without_active_startup_does_not_resume_runtime(self):
        service = FakeAuthRecoveryService(startup_active=False)
        service._set_auth_recovery_state(
            cycle_id="cycle-1",
            recovery_phase="silent_probe",
            recovery_reason="manual_probe",
            last_recovery_source="runtime_page_banner",
        )

        service._mark_auth_recovered(source="auth_probe", reason="manual_probe")

        self.assertEqual([], service.scheduled_restarts)


if __name__ == "__main__":
    unittest.main()
