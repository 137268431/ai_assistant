import sys
import types
import unittest
from pathlib import Path
from unittest import mock

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = types.SimpleNamespace(get_json=lambda silent=True: {})
    sys.modules["flask"] = flask_stub

from ibkr_api.startup.progress import (
    build_startup_card,
    build_startup_label,
    default_startup_steps,
    merge_startup_steps,
    normalize_startup_fields,
    normalize_startup_state,
    resolve_startup_step_label,
)
from ibkr_api.startup.routes import register_startup_routes
from ibkr_api.two_factor.startup_sync import sync_startup_auth_progress
import ibkr_api.startup.progress_routes as progress_routes


class _FakeApp:
    def __init__(self):
        self.routes = {}

    def route(self, path, methods=None):
        def decorator(func):
            self.routes[(path, tuple(methods or []))] = func
            return func

        return decorator


class _FakePB:
    def __init__(self, initial=None):
        self.records = dict(initial or {})

    def get_state(self, state_key, environment, date="global"):
        return self.records.get((state_key, environment, date))

    def upsert_state(self, state_key, environment, data, date="global"):
        record = {"state_key": state_key, "environment": environment, "date": date, "data": dict(data)}
        self.records[(state_key, environment, date)] = record
        return record


def _normalize_environment(value, fallback):
    return str(value or fallback).strip().lower() or fallback


def _time_strings():
    return {"us": "2026-05-18 09:30:00"}


def _build_deps(pb):
    return {
        "pb": pb,
        "normalize_environment": _normalize_environment,
        "time_strings": _time_strings,
        "get_state_payload": lambda state_key, environment, date="global": (
            pb.get_state(state_key, environment, date=date)
            or {"environment": environment, "date": date, "data": {}}
        ),
        "normalize_startup_state": lambda value, environment: normalize_startup_state(
            value,
            environment,
            normalize_environment=_normalize_environment,
            startup_chat_id_fn=lambda env: f"chat-{env}",
        ),
        "build_startup_cycle_id": lambda environment: f"{environment}-cycle",
        "build_startup_label": lambda environment, startup_seq, started_at: build_startup_label(
            environment,
            startup_seq,
            started_at,
            normalize_environment=_normalize_environment,
            time_strings=_time_strings,
        ),
        "startup_chat_id": lambda environment: f"chat-{environment}",
        "default_startup_steps": default_startup_steps,
        "normalize_startup_fields": normalize_startup_fields,
        "merge_startup_steps": merge_startup_steps,
        "deliver_startup_progress_card": lambda state, environment: {"success": True, "message_id": "startup-msg"},
        "resolve_startup_step_label": resolve_startup_step_label,
        "write_system_event_record": lambda *args, **kwargs: True,
        "ibkr_startup_state_key": "ibkr_runtime_startup",
        "ibkr_startup_state_date": "global",
        "as_dict": lambda value: dict(value) if isinstance(value, dict) else {},
    }


class StartupProgressStepTest(unittest.TestCase):
    def test_startup_card_uses_vertical_compact_action_rows(self):
        state = normalize_startup_state(
            {
                "cycle_id": "paper-cycle",
                "startup_seq": 1,
                "startup_label": "PAPER-20260518-093000-001",
                "active": True,
                "status": "active",
                "current_step": "manual_trigger",
                "current_blocker": "等待手动触发 2FA",
                "steps": merge_startup_steps(default_startup_steps(), {"manual_trigger": {"status": "waiting"}}, True),
            },
            "paper",
            normalize_environment=_normalize_environment,
            startup_chat_id_fn=lambda env: f"chat-{env}",
        )

        card = build_startup_card(
            state,
            "paper",
            normalize_environment=_normalize_environment,
            normalize_startup_state_fn=lambda value, environment: normalize_startup_state(
                value,
                environment,
                normalize_environment=_normalize_environment,
                startup_chat_id_fn=lambda env: f"chat-{env}",
            ),
            environment_tag=lambda environment: environment,
            label_title_with_environment=lambda title, environment: f"[{environment.upper()}] {title}",
            runtime_page_url=lambda environment: f"https://console.example.com/ibkr_runtime.html?environment={environment}",
            system_page_url=lambda environment: f"https://console.example.com/ibkr_system.html?environment={environment}",
            console_base_url=lambda: "https://console.example.com",
        )

        action_blocks = [element for element in card["elements"] if element.get("tag") == "action"]
        actions = [block["actions"][0] for block in action_blocks]

        self.assertEqual(["开始 2FA 验证", "查看 Runtime", "查看 System"], [action["text"]["content"] for action in actions])
        self.assertTrue(all(len(block.get("actions", [])) == 1 for block in action_blocks))
        self.assertFalse(any("width" in action for action in actions))

    def test_auth_done_without_trigger_marks_manual_steps_skipped(self):
        steps = merge_startup_steps({}, {"auth": {"status": "done", "detail": "already authenticated"}}, False)

        self.assertEqual("done", steps["service_boot"]["status"])
        self.assertEqual("skipped", steps["card_ready"]["status"])
        self.assertEqual("skipped", steps["manual_trigger"]["status"])
        self.assertEqual("skipped", steps["manual_confirm"]["status"])

    def test_auth_done_with_trigger_marks_manual_steps_done(self):
        steps = merge_startup_steps({}, {"auth": {"status": "done", "detail": "2FA finished"}}, True)

        self.assertEqual("done", steps["service_boot"]["status"])
        self.assertEqual("done", steps["card_ready"]["status"])
        self.assertEqual("done", steps["manual_trigger"]["status"])
        self.assertEqual("done", steps["manual_confirm"]["status"])

    def test_complete_from_empty_state_skips_unrun_manual_2fa_steps(self):
        pb = _FakePB()
        exports = register_startup_routes(_FakeApp(), deps=_build_deps(pb))

        request_stub = types.SimpleNamespace(
            get_json=lambda silent=True: {
                "environment": "paper",
                "action": "complete",
                "title": "IBKR Runtime 启动完成",
                "summary": "Runtime 已可用。",
            }
        )
        with mock.patch.object(progress_routes, "request", request_stub):
            with mock.patch.object(progress_routes, "jsonify", side_effect=lambda payload: payload):
                payload = exports["custom_ibkr_startup_progress"]()

        self.assertTrue(payload["ok"])
        saved = pb.records[("ibkr_runtime_startup", "paper", "global")]["data"]
        self.assertEqual("completed", saved["status"])
        self.assertFalse(saved["active"])
        statuses = {key: step["status"] for key, step in saved["steps"].items()}
        self.assertEqual("done", statuses["service_boot"])
        self.assertEqual("skipped", statuses["card_ready"])
        self.assertEqual("skipped", statuses["manual_trigger"])
        self.assertEqual("skipped", statuses["manual_confirm"])
        self.assertEqual("done", statuses["runtime_resume"])
        self.assertEqual("done", statuses["health_check"])
        self.assertNotIn("pending", statuses.values())

    def test_complete_active_triggered_cycle_keeps_manual_2fa_steps_done(self):
        initial_state = normalize_startup_state(
            {
                "cycle_id": "paper-cycle",
                "startup_seq": 1,
                "startup_label": "PAPER-20260518-093000-001",
                "active": True,
                "status": "active",
                "trigger_login": True,
                "steps": merge_startup_steps(
                    default_startup_steps(),
                    {
                        "service_boot": {"status": "done"},
                        "card_ready": {"status": "done"},
                        "manual_trigger": {"status": "done"},
                        "manual_confirm": {"status": "waiting"},
                    },
                    True,
                ),
            },
            "paper",
            normalize_environment=_normalize_environment,
            startup_chat_id_fn=lambda env: f"chat-{env}",
        )
        pb = _FakePB({("ibkr_runtime_startup", "paper", "global"): {"data": initial_state}})
        exports = register_startup_routes(_FakeApp(), deps=_build_deps(pb))

        request_stub = types.SimpleNamespace(
            get_json=lambda silent=True: {
                "environment": "paper",
                "action": "complete",
                "trigger_login": True,
                "title": "IBKR Runtime 启动完成",
                "summary": "Runtime 已可用。",
            }
        )
        with mock.patch.object(progress_routes, "request", request_stub):
            with mock.patch.object(progress_routes, "jsonify", side_effect=lambda payload: payload):
                payload = exports["custom_ibkr_startup_progress"]()

        self.assertTrue(payload["ok"])
        saved = pb.records[("ibkr_runtime_startup", "paper", "global")]["data"]
        statuses = {key: step["status"] for key, step in saved["steps"].items()}
        self.assertEqual("done", statuses["service_boot"])
        self.assertEqual("done", statuses["card_ready"])
        self.assertEqual("done", statuses["manual_trigger"])
        self.assertEqual("done", statuses["manual_confirm"])
        self.assertEqual("done", statuses["runtime_resume"])
        self.assertEqual("done", statuses["health_check"])

    def test_two_factor_success_without_manual_flow_marks_manual_steps_skipped(self):
        initial_state = normalize_startup_state(
            {
                "cycle_id": "paper-cycle",
                "startup_seq": 1,
                "startup_label": "PAPER-20260518-093000-001",
                "active": True,
                "status": "active",
                "trigger_login": False,
                "steps": default_startup_steps(),
            },
            "paper",
            normalize_environment=_normalize_environment,
            startup_chat_id_fn=lambda env: f"chat-{env}",
        )
        pb = _FakePB({("ibkr_runtime_startup", "paper", "global"): {"data": initial_state}})

        result = sync_startup_auth_progress(
            pb,
            "paper",
            "success",
            {"status": "success", "last_result": "复用现有认证会话。"},
            normalize_environment=_normalize_environment,
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=lambda state, environment: {"success": True, "message_id": "startup-msg"},
        )

        self.assertTrue(result["ok"])
        saved = pb.records[("ibkr_runtime_startup", "paper", "global")]["data"]
        statuses = {key: step["status"] for key, step in saved["steps"].items()}
        self.assertEqual("done", statuses["service_boot"])
        self.assertEqual("skipped", statuses["card_ready"])
        self.assertEqual("skipped", statuses["manual_trigger"])
        self.assertEqual("skipped", statuses["manual_confirm"])
        self.assertEqual("running", statuses["runtime_resume"])

    def test_two_factor_success_after_manual_flow_keeps_manual_steps_done(self):
        initial_state = normalize_startup_state(
            {
                "cycle_id": "paper-cycle",
                "startup_seq": 1,
                "startup_label": "PAPER-20260518-093000-001",
                "active": True,
                "status": "active",
                "trigger_login": False,
                "steps": merge_startup_steps(
                    default_startup_steps(),
                    {
                        "manual_trigger": {"status": "done"},
                        "manual_confirm": {"status": "waiting"},
                    },
                    False,
                ),
            },
            "paper",
            normalize_environment=_normalize_environment,
            startup_chat_id_fn=lambda env: f"chat-{env}",
        )
        pb = _FakePB({("ibkr_runtime_startup", "paper", "global"): {"data": initial_state}})

        result = sync_startup_auth_progress(
            pb,
            "paper",
            "success",
            {"status": "success", "last_result": "2FA 已完成。"},
            normalize_environment=_normalize_environment,
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=lambda state, environment: {"success": True, "message_id": "startup-msg"},
        )

        self.assertTrue(result["ok"])
        saved = pb.records[("ibkr_runtime_startup", "paper", "global")]["data"]
        statuses = {key: step["status"] for key, step in saved["steps"].items()}
        self.assertEqual("done", statuses["card_ready"])
        self.assertEqual("done", statuses["manual_trigger"])
        self.assertEqual("done", statuses["manual_confirm"])


if __name__ == "__main__":
    unittest.main()
