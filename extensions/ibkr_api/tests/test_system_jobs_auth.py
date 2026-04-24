import os
import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_scheduler" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

os.environ.setdefault("IBKR_SCHEDULER_AUTOSTART", "false")

from ibkr_api.system.jobs.auth import (
    build_auth_edge_guard_response,
    build_auth_pending_guard_response,
    build_two_factor_hourly_check_response,
)


class _AuthStateStore:
    def __init__(self, auth_state=None, extra_states=None):
        self.auth_state = dict(auth_state or {})
        self.extra_states = {key: dict(value) for key, value in (extra_states or {}).items()}
        self.upserts = []

    def get_state_payload(self, state_key, environment):
        if state_key == "ibkr_2fa":
            return {"data": dict(self.auth_state), "environment": environment}
        return {"data": dict(self.extra_states.get((state_key, environment), {})), "environment": environment}

    def upsert_state(self, state_key, environment, data, date):
        payload = dict(data)
        self.extra_states[(state_key, environment)] = payload
        self.upserts.append((state_key, environment, payload, date))
        return {"data": payload, "environment": environment, "date": date}


class AuthJobsTest(unittest.TestCase):
    def test_auth_edge_guard_emits_operational_status_change(self):
        store = _AuthStateStore(
            auth_state={
                "status": "waiting_confirm",
                "request_count": 1,
                "requested_at": "2026-04-24 09:00:00",
                "triggered_at": "2026-04-24 09:01:00",
                "cycle_id": "cycle-1",
            }
        )
        emitted = []

        payload, status_code = build_auth_edge_guard_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            get_state_payload=store.get_state_payload,
            normalize_two_factor_state_with_runtime=lambda state, runtime: dict(state),
            fetch_runtime_status=lambda environment: {
                "payload": {
                    "gateway": {"running": True, "status_code": 200, "uptime_s": 42},
                    "session": {"running": True, "authenticated": False},
                    "websocket": {"running": True},
                }
            },
            time_strings=lambda: {"us": "2026-04-24 09:10:00", "cn": "2026-04-24 21:10:00", "date": "2026-04-24"},
            upsert_state=store.upsert_state,
            emit_system_event=lambda **kwargs: emitted.append(kwargs) or {"notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["issue"]["kind"], "waiting_confirm")
        self.assertTrue(payload["notified"])
        self.assertEqual(emitted[0]["event_type"], "status_change")
        self.assertEqual(emitted[0]["level"], "info")
        self.assertEqual(payload["state"]["last_auth_issue_kind"], "waiting_confirm")

    def test_auth_pending_guard_uses_waiting_response_title(self):
        store = _AuthStateStore(
            auth_state={
                "status": "waiting_response",
                "request_count": 1,
                "requested_at": "2026-04-22 08:40:00",
                "triggered_at": "2026-04-22 08:41:00",
                "cycle_id": "cycle-2",
                "challenge_code": "123456",
                "response_status": "submit_failed",
            }
        )
        emitted = []

        payload, status_code = build_auth_pending_guard_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            get_state_payload=store.get_state_payload,
            normalize_two_factor_state_with_runtime=lambda state, runtime: dict(state),
            fetch_runtime_status=lambda environment: {
                "payload": {
                    "gateway": {"running": True, "status_code": 200, "uptime_s": 300},
                    "session": {"running": True, "authenticated": False},
                    "websocket": {"running": True},
                }
            },
            time_strings=lambda: {"us": "2026-04-24 09:10:00", "cn": "2026-04-24 21:10:00", "date": "2026-04-24"},
            upsert_state=store.upsert_state,
            emit_system_event=lambda **kwargs: emitted.append(kwargs) or {"notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["pending_too_long"])
        self.assertEqual(payload["title"], "IBKR Response Code 浏览器提交失败")
        self.assertTrue(payload["notified"])
        self.assertEqual(emitted[0]["title"], "IBKR Response Code 浏览器提交失败")
        self.assertEqual(emitted[0]["detail"]["Challenge"], "123456")

    def test_two_factor_hourly_check_requests_weekly_reauth_followup(self):
        store = _AuthStateStore(
            auth_state={
                "status": "requested",
                "reason": "weekly_reauth",
                "request_count": 1,
                "requested_at": "2026-04-24 08:00:00",
                "business_deadline_cn": "2026-04-25 20:00:00",
                "business_deadline_at": "2026-04-25 08:00:00",
                "last_request_push_ms": 0,
            }
        )
        requests = []

        payload, status_code = build_two_factor_hourly_check_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower() or default,
            get_state_payload=store.get_state_payload,
            normalize_two_factor_state_with_runtime=lambda state, runtime: dict(state),
            fetch_runtime_status=lambda environment: {
                "payload": {
                    "gateway": {"running": True, "status_code": 401, "uptime_s": 120},
                    "session": {"running": True, "authenticated": False},
                    "websocket": {"running": True},
                }
            },
            request_two_factor_approval=lambda **kwargs: requests.append(kwargs) or {"ok": True, "message_id": "msg-1"},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["requested"])
        self.assertEqual(requests[0]["source"], "ibkr_scheduler")
        self.assertEqual(requests[0]["reason"], "weekly_reauth")
        self.assertIn("最晚请于美股周一盘前前完成", requests[0]["message"])
        self.assertEqual(requests[0]["detail"]["周验证截止"], "2026-04-25 20:00:00 北京时间 / 2026-04-25 08:00:00 美东")


if __name__ == "__main__":
    unittest.main()
