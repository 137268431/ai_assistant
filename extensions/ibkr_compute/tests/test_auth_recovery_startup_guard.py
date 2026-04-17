import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration.auth_recovery import TradingServiceAuthRecoveryMixin


class _DummyAuthRecoveryService(TradingServiceAuthRecoveryMixin):
    def __init__(self, *, starting: bool, running: bool):
        self._starting = starting
        self._running = running
        self._auth_required_reason = "auto_restore"
        self._auth_cycle_seq = 0
        self._auth_recovery_lock = threading.Lock()
        self._auth_restart_thread = None
        self._auth_recovery_state = {
            "cycle_id": "cycle-1",
            "recovery_phase": "resume_waiting_manual",
            "recovery_reason": "auto_restore",
            "interruption_kind": "server_boot_resume",
            "manual_takeover_active": False,
            "probe_started_at": "2026-04-17T15:19:00-04:00",
            "probe_last_checked_at": "2026-04-17T15:19:02-04:00",
            "probe_attempts": 1,
            "probe_result": "authenticated",
            "auto_restart_scheduled": False,
            "last_runtime_authenticated_at": "",
            "last_gateway_status_code": 200,
            "last_recovery_source": "server_boot",
            "lock_owner": "",
            "lock_expires_at": "",
            "updated_at": "",
        }
        self.report_calls = []
        self.schedule_calls = []
        self.auth_handler = SimpleNamespace(
            _report_2fa_status=lambda **kwargs: self.report_calls.append(kwargs),
        )

    def _now_iso(self) -> str:
        return "2026-04-17T15:19:02-04:00"

    def _build_2fa_detail(self, reason: str = "") -> dict:
        return {"reason": reason}

    def _next_auth_cycle_id(self) -> str:
        self._auth_cycle_seq += 1
        return f"cycle-{self._auth_cycle_seq}"

    def _copy_auth_recovery_state(self, source: dict | None = None) -> dict:
        return dict(source if source is not None else self._auth_recovery_state)

    def _set_auth_recovery_state(self, sync_pb: bool = True, **updates) -> dict:
        next_state = self._copy_auth_recovery_state()
        next_state.update(updates)
        next_state["updated_at"] = self._now_iso()
        self._auth_recovery_state = next_state
        return dict(next_state)

    def _schedule_auth_restart(self, reason: str, source: str, trigger_login: bool = False):
        self.schedule_calls.append(
            {
                "reason": reason,
                "source": source,
                "trigger_login": bool(trigger_login),
            }
        )
        return True


class AuthRecoveryStartupGuardTest(unittest.TestCase):
    def _service_mod(self):
        logger = SimpleNamespace(
            info=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
        )
        return SimpleNamespace(logger=logger)

    def test_mark_auth_recovered_skips_restart_during_startup(self):
        service = _DummyAuthRecoveryService(starting=True, running=False)

        with mock.patch("ibkr_compute.orchestration.auth_recovery._service_mod", return_value=self._service_mod()):
            service._mark_auth_recovered(source="server_boot_resume_probe", reason="auto_restore")

        self.assertEqual(service.schedule_calls, [])
        self.assertFalse(service._auth_recovery_state["auto_restart_scheduled"])
        self.assertEqual(service._auth_recovery_state["recovery_phase"], "recovered")
        self.assertEqual(service._auth_required_reason, "")

    def test_mark_auth_recovered_still_restarts_when_runtime_is_idle(self):
        service = _DummyAuthRecoveryService(starting=False, running=False)

        with mock.patch("ibkr_compute.orchestration.auth_recovery._service_mod", return_value=self._service_mod()):
            service._mark_auth_recovered(source="server_boot_resume_probe", reason="auto_restore")

        self.assertEqual(len(service.schedule_calls), 1)
        scheduled = service.schedule_calls[0]
        self.assertEqual(scheduled["reason"], "auto_restore")
        self.assertEqual(scheduled["source"], "server_boot")
        self.assertFalse(scheduled["trigger_login"])
        self.assertTrue(service._auth_recovery_state["auto_restart_scheduled"])
        self.assertEqual(
            service._auth_recovery_state["probe_result"],
            "authenticated_resume_restart_scheduled",
        )


if __name__ == "__main__":
    unittest.main()
