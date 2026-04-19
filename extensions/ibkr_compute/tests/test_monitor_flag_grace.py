import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.monitor.flags import _build_monitor_flags


class MonitorFlagGraceTest(unittest.TestCase):
    def test_session_unauthenticated_suppressed_during_restart_grace(self):
        flags = _build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": False},
                "websocket": {"connected": True, "ready": True},
                "auth_recovery": {
                    "recovery_phase": "silent_probe",
                    "recovery_class": "scheduled_restart",
                    "recovery_reason": "session_expired",
                    "interruption_kind": "session_expired",
                    "probe_result": "pending",
                    "probe_started_at": datetime.now(timezone.utc).isoformat(),
                    "auto_restart_scheduled": False,
                },
            },
            {},
            {},
            {},
        )

        flag_codes = {item["code"] for item in flags}
        self.assertNotIn("session_unauthenticated", flag_codes)

    def test_session_unauthenticated_returns_after_restart_grace_expires(self):
        flags = _build_monitor_flags(
            {
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": False},
                "websocket": {"connected": True, "ready": True},
                "auth_recovery": {
                    "recovery_phase": "silent_probe",
                    "recovery_class": "scheduled_restart",
                    "recovery_reason": "session_expired",
                    "interruption_kind": "session_expired",
                    "probe_result": "pending",
                    "probe_started_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                    "auto_restart_scheduled": False,
                },
            },
            {},
            {},
            {},
        )

        flag_codes = {item["code"] for item in flags}
        self.assertIn("session_unauthenticated", flag_codes)


if __name__ == "__main__":
    unittest.main()
