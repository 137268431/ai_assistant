import copy
import sys
import time
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.two_factor.request import build_two_factor_request_response
from ibkr_api.two_factor.respond import build_two_factor_respond_response
from ibkr_api.two_factor.result import build_two_factor_result_response
from ibkr_api.two_factor.delivery import build_two_factor_card
from ibkr_api.runtime.two_factor import normalize_two_factor_state_with_runtime
from ibkr_api.two_factor.deadlines import parse_et_time_ms
from ibkr_api.two_factor.runtime_actions import (
    build_two_factor_panic_reset_response,
    build_two_factor_probe_response,
    build_two_factor_takeover_response,
)


class _FakePB:
    def __init__(self, state_rows=None):
        self.state_rows = {}
        for row in state_rows or []:
            key = (str(row["state_key"]), str(row["environment"]), str(row.get("date") or "global"))
            self.state_rows[key] = copy.deepcopy(row)

    def get_state(self, state_key, environment, date="global"):
        row = self.state_rows.get((str(state_key), str(environment), str(date)))
        return copy.deepcopy(row) if row else None

    def upsert_state(self, state_key, environment, data, date="global"):
        key = (str(state_key), str(environment), str(date))
        current = self.state_rows.get(key) or {"id": f"state-{len(self.state_rows) + 1}"}
        next_row = {
            **copy.deepcopy(current),
            "state_key": str(state_key),
            "environment": str(environment),
            "date": str(date),
            "data": copy.deepcopy(data),
        }
        self.state_rows[key] = next_row
        return copy.deepcopy(next_row)


class TwoFactorBuildersTest(unittest.TestCase):
    def setUp(self):
        self.normalize_environment = lambda value, default: str(value or default).strip().lower() or default
        self.as_dict = lambda value: dict(value) if isinstance(value, dict) else {}
        self.runtime_status = {
            "payload": {
                "environment": "live",
                "session": {"authenticated": False, "running": True},
                "gateway": {"running": True, "reachable": True, "status_code": 401},
            },
            "selected_upstream": "http://runtime/ibkr/status",
            "proxy_upstream": "http://compute/ibkr/status",
            "error": "",
        }
        self.inspect_runtime_environment = lambda environment: {
            "requested_environment": environment,
            "actual_runtime_environment": environment,
            "runtime_environment_mismatch": False,
        }
        self.build_mismatch_payload = lambda info, route: {"ok": False, "route": route, **info}
        self.console_base_url = "https://console.example.com"
        self.config_value = lambda key, default, environment: default
        self.request_json_request = lambda method, base_url, path, params=None, json_body=None, timeout=5.0: {
            "ok": True,
            "status_code": 200,
            "payload": {"ok": True, "status": "starting" if path.endswith("/start") else "stopping"},
            "target_url": f"{base_url}{path}",
            "error": "",
        }
        self.emit_system_event = lambda **kwargs: {"ok": True, "message_id": "evt-1"}
        self.merge_startup_steps = lambda existing, patch, trigger_login: dict(existing or {}) | dict(patch or {})
        self.deliver_startup_progress_card = lambda state, environment: {"success": True, "message_id": "startup-1"}

    def test_two_factor_card_uses_vertical_compact_action_rows(self):
        card = build_two_factor_card(
            {
                "status": "success",
                "reason": "manual_start",
                "requested_at": "2026-05-20 12:19:21",
                "result_at": "2026-05-20 12:19:27",
                "last_result": "会话恢复成功。",
            },
            "paper",
            normalize_environment=self.normalize_environment,
            console_base_url=self.console_base_url,
        )

        action_blocks = [element for element in card["elements"] if element.get("tag") == "action"]
        actions = [block["actions"][0] for block in action_blocks]

        self.assertEqual(["查看 Runtime", "查看 System"], [action["text"]["content"] for action in actions])
        self.assertTrue(all(len(block.get("actions", [])) == 1 for block in action_blocks))
        self.assertFalse(any("width" in action for action in actions))

    def test_request_builder_reuses_active_card(self):
        pb = _FakePB(
            state_rows=[
                {
                    "id": "state-1",
                    "state_key": "ibkr_2fa",
                    "environment": "live",
                    "date": "global",
                    "data": {
                        "status": "waiting_confirm",
                        "message_id": "msg-keep",
                        "last_request_push_ms": int(time.time() * 1000),
                        "reason": "manual_reauth",
                        "requested_at": "2026-04-23 09:30:00",
                    },
                }
            ]
        )
        send_calls = []
        update_calls = []

        payload, status_code = build_two_factor_request_response(
            pb,
            payload={"environment": "live", "reason": "manual_reauth"},
            normalize_environment=self.normalize_environment,
            as_dict=self.as_dict,
            request_json_request=self.request_json_request,
            runtime_base_url="http://runtime",
            fetch_runtime_status=lambda environment: self.runtime_status,
            inspect_runtime_environment=self.inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=self.build_mismatch_payload,
            console_base_url=self.console_base_url,
            config_value=self.config_value,
            send_interactive=lambda *args, **kwargs: send_calls.append((args, kwargs)) or {"success": True, "message_id": "msg-new"},
            update_interactive=lambda *args, **kwargs: update_calls.append((args, kwargs)) or {"success": True, "message_id": "msg-keep"},
            emit_system_event=self.emit_system_event,
            merge_startup_steps=self.merge_startup_steps,
            deliver_startup_progress_card=self.deliver_startup_progress_card,
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("msg-keep", payload["message_id"])
        self.assertEqual([], send_calls)
        self.assertGreaterEqual(payload["renotify_remaining_ms"], 0)
        self.assertIn(len(update_calls), {0, 1})

    def test_result_builder_marks_success_and_clears_challenge_fields(self):
        pb = _FakePB(
            state_rows=[
                {
                    "id": "state-1",
                    "state_key": "ibkr_2fa",
                    "environment": "live",
                    "date": "global",
                    "data": {
                        "status": "waiting_response",
                        "message_id": "msg-2",
                        "challenge_code": "AB12",
                        "response_status": "received",
                        "detail": {"foo": "bar"},
                    },
                }
            ]
        )
        payload, status_code = build_two_factor_result_response(
            pb,
            payload={"environment": "live", "status": "success", "message": "done"},
            normalize_environment=self.normalize_environment,
            as_dict=self.as_dict,
            console_base_url=self.console_base_url,
            config_value=self.config_value,
            send_interactive=lambda *args, **kwargs: {"success": True, "message_id": "msg-new"},
            update_interactive=lambda *args, **kwargs: {"success": True, "message_id": "msg-2"},
            emit_system_event=self.emit_system_event,
            merge_startup_steps=self.merge_startup_steps,
            deliver_startup_progress_card=self.deliver_startup_progress_card,
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("success", payload["status"])
        self.assertEqual("", payload["state"]["challenge_code"])
        self.assertEqual("", payload["state"]["response_status"])
        self.assertEqual("msg-2", payload["message_id"])

    def test_result_builder_suppresses_duplicate_auto_restore_success_event(self):
        pb = _FakePB(
            state_rows=[
                {
                    "id": "state-1",
                    "state_key": "ibkr_2fa",
                    "environment": "live",
                    "date": "global",
                    "data": {
                        "status": "waiting_response",
                        "message_id": "msg-2",
                        "reason": "auto_restore",
                    },
                }
            ]
        )
        emit_calls = []

        common_kwargs = {
            "normalize_environment": self.normalize_environment,
            "as_dict": self.as_dict,
            "console_base_url": self.console_base_url,
            "config_value": self.config_value,
            "send_interactive": lambda *args, **kwargs: {"success": True, "message_id": "msg-new"},
            "update_interactive": lambda *args, **kwargs: {"success": True, "message_id": "msg-2"},
            "emit_system_event": lambda **kwargs: emit_calls.append(kwargs) or {"ok": True, "message_id": "evt-1"},
            "merge_startup_steps": self.merge_startup_steps,
            "deliver_startup_progress_card": self.deliver_startup_progress_card,
        }

        first_payload, first_status_code = build_two_factor_result_response(
            pb,
            payload={
                "environment": "live",
                "status": "success",
                "last_result": "会话恢复成功。",
                "state_patch": {"recovery_reason": "auto_restore"},
            },
            **common_kwargs,
        )
        second_payload, second_status_code = build_two_factor_result_response(
            pb,
            payload={
                "environment": "live",
                "status": "success",
                "last_result": "复用现有认证会话。",
                "state_patch": {"reason": "auto_restore"},
            },
            **common_kwargs,
        )

        self.assertEqual(200, first_status_code)
        self.assertEqual(200, second_status_code)
        self.assertTrue(first_payload["ok"])
        self.assertTrue(second_payload["ok"])
        self.assertEqual(1, len(emit_calls))
        self.assertEqual("IBKR 2FA 完成", emit_calls[0]["title"])
        self.assertEqual("复用现有认证会话。", second_payload["state"]["last_result"])

    def test_respond_builder_validates_and_records_response_code(self):
        pb = _FakePB(
            state_rows=[
                {
                    "id": "state-1",
                    "state_key": "ibkr_2fa",
                    "environment": "live",
                    "date": "global",
                    "data": {
                        "status": "waiting_response",
                        "message_id": "msg-3",
                        "challenge_code": "XYZ123",
                        "reason": "manual_reauth",
                    },
                }
            ]
        )
        payload, status_code = build_two_factor_respond_response(
            pb,
            payload={"environment": "live", "response_code": " ab-12 ", "challenge_code": "XYZ123"},
            normalize_environment=self.normalize_environment,
            as_dict=self.as_dict,
            inspect_runtime_environment=self.inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=self.build_mismatch_payload,
            console_base_url=self.console_base_url,
            config_value=self.config_value,
            send_interactive=lambda *args, **kwargs: {"success": True, "message_id": "msg-new"},
            update_interactive=lambda *args, **kwargs: {"success": True, "message_id": "msg-3"},
            merge_startup_steps=self.merge_startup_steps,
            deliver_startup_progress_card=self.deliver_startup_progress_card,
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("waiting_response", payload["status"])
        self.assertEqual("AB12", payload["state"]["response_code"])
        self.assertEqual("received", payload["state"]["response_status"])
        self.assertEqual("msg-3", payload["message_id"])

    def test_takeover_builder_returns_runtime_normalized_state(self):
        pb = _FakePB(
            state_rows=[
                {
                    "id": "state-1",
                    "state_key": "ibkr_2fa",
                    "environment": "live",
                    "date": "global",
                    "data": {
                        "status": "waiting_response",
                        "message_id": "msg-4",
                        "challenge_code": "XYZ123",
                    },
                }
            ]
        )
        runtime_status = {
            **copy.deepcopy(self.runtime_status),
            "payload": {
                **copy.deepcopy(self.runtime_status["payload"]),
                "auth_recovery": {
                    "manual_takeover_active": True,
                    "manual_takeover_started_at": "2026-04-23 09:40:00",
                    "manual_takeover_until": "2026-04-23 09:50:00",
                },
            },
        }
        payload, status_code = build_two_factor_takeover_response(
            pb,
            payload={"environment": "live", "enabled": True, "ttl_sec": 120},
            normalize_environment=self.normalize_environment,
            as_dict=self.as_dict,
            request_json_request=self.request_json_request,
            runtime_base_url="http://runtime",
            fetch_runtime_status=lambda environment: runtime_status,
            inspect_runtime_environment=self.inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=self.build_mismatch_payload,
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["state"]["manual_takeover_active"])
        self.assertEqual("manual_takeover", payload["state"]["operator_action"])

    def test_probe_builder_uses_runtime_probe_path(self):
        pb = _FakePB(
            state_rows=[
                {
                    "id": "state-1",
                    "state_key": "ibkr_2fa",
                    "environment": "live",
                    "date": "global",
                    "data": {"status": "recovering"},
                }
            ]
        )
        captured = {}

        def request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            captured.update({
                "method": method,
                "base_url": base_url,
                "path": path,
                "json_body": copy.deepcopy(json_body),
                "timeout": timeout,
            })
            return {
                "ok": True,
                "status_code": 200,
                "payload": {"ok": True, "probe_started": True},
                "target_url": f"{base_url}{path}",
                "error": "",
            }

        payload, status_code = build_two_factor_probe_response(
            pb,
            payload={"environment": "live", "reason": "manual_probe", "source": "runtime_page"},
            normalize_environment=self.normalize_environment,
            as_dict=self.as_dict,
            request_json_request=request_json_request,
            runtime_base_url="http://runtime",
            fetch_runtime_status=lambda environment: self.runtime_status,
            inspect_runtime_environment=self.inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=self.build_mismatch_payload,
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("/ibkr/2fa/probe", captured["path"])
        self.assertEqual("manual_probe", captured["json_body"]["reason"])

    def test_panic_reset_builder_rejects_runtime_environment_mismatch(self):
        pb = _FakePB()
        payload, status_code = build_two_factor_panic_reset_response(
            pb,
            payload={"environment": "paper"},
            normalize_environment=self.normalize_environment,
            as_dict=self.as_dict,
            request_json_request=self.request_json_request,
            runtime_base_url="http://runtime",
            fetch_runtime_status=lambda environment: self.runtime_status,
            inspect_runtime_environment=lambda environment: {
                "requested_environment": environment,
                "actual_runtime_environment": "live",
                "runtime_environment_mismatch": True,
            },
            build_runtime_environment_mismatch_payload=self.build_mismatch_payload,
        )

        self.assertEqual(409, status_code)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["runtime_environment_mismatch"])

    def test_waiting_confirm_downgrades_when_gateway_never_reaches_2fa(self):
        state = {
            "status": "waiting_confirm",
            "message": "等待手机确认 IBKR 2FA。",
            "last_result": "waiting_mobile_approval",
            "triggered_at": "2026-04-28 10:30:16",
        }
        runtime_status = {
            "session": {"authenticated": False, "running": False},
            "gateway": {"running": True, "reachable": False, "status_code": 503},
        }

        normalized = normalize_two_factor_state_with_runtime(
            state,
            runtime_status,
            as_dict=self.as_dict,
            parse_et_time_ms=parse_et_time_ms,
        )

        self.assertEqual("triggered", normalized["status"])
        self.assertTrue(normalized["gateway_2fa_not_reached"])
        self.assertFalse(normalized["push_confirmed"])
        self.assertIn("暂未确认", normalized["message"])

    def test_gateway_socket_missing_recommends_panic_reset_not_push_wait(self):
        state = {
            "status": "triggered",
            "message": "等待手机确认 IBKR 2FA。",
            "last_result": "waiting_mobile_approval",
            "triggered_at": "2026-04-28 10:30:16",
        }
        runtime_status = {
            "session": {"authenticated": False, "running": False},
            "gateway": {
                "running": True,
                "reachable": False,
                "status_code": 502,
                "api_socket_listening": False,
                "api_socket_port": 4001,
            },
        }

        normalized = normalize_two_factor_state_with_runtime(
            state,
            runtime_status,
            as_dict=self.as_dict,
            parse_et_time_ms=parse_et_time_ms,
        )

        self.assertEqual("triggered", normalized["status"])
        self.assertEqual("panic_reset", normalized["operator_action"])
        self.assertTrue(normalized["reset_recommended"])
        self.assertEqual("gateway_socket_unreachable", normalized["reset_reason"])
        self.assertEqual("local_socket_unreachable", normalized["disconnect_reason_code"])
        self.assertTrue(normalized["gateway_2fa_not_reached"])
        self.assertIn("API 端口未开放", normalized["message"])


if __name__ == "__main__":
    unittest.main()
