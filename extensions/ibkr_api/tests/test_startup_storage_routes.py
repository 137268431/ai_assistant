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

    class _FakeFlask:
        def __init__(self, name):
            self.name = name

        def route(self, _path, methods=None):
            def decorator(func):
                return func

            return decorator

    flask_stub.Flask = _FakeFlask
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = types.SimpleNamespace(args={}, headers={}, method="GET", get_json=lambda silent=True: {})
    sys.modules["flask"] = flask_stub

from ibkr_api.startup.routes import register_startup_routes
from ibkr_api.storage.routes import register_storage_routes
import ibkr_api.startup.status_routes as startup_status_routes
import ibkr_api.storage.bar_routes as storage_bar_routes
import ibkr_api.storage.ping_routes as storage_ping_routes


class _FakeApp:
    def __init__(self):
        self.routes = {}

    def route(self, path, methods=None):
        def decorator(func):
            self.routes[(path, tuple(methods or []))] = func
            return func

        return decorator


class _FakePB:
    def __init__(self):
        self.records = {}
        self.created = []
        self.updated = []
        self.bar_upserts = []

    def get_state(self, state_key, environment, date="global"):
        return self.records.get((state_key, environment, date), {"environment": environment, "date": date, "data": {}})

    def upsert_state(self, state_key, environment, data, date="global"):
        record = {"state_key": state_key, "environment": environment, "date": date, "data": dict(data)}
        self.records[(state_key, environment, date)] = record
        return record

    def get_first_record(self, collection, filter=None, sort=None):
        key = (collection, str(filter or ""))
        return self.records.get(key)

    def update_record(self, collection, record_id, patch):
        row = {"id": record_id, **dict(patch)}
        self.updated.append((collection, record_id, dict(patch)))
        return row

    def create_record(self, collection, data):
        row = {"id": f"{collection}-1", **dict(data)}
        self.created.append((collection, dict(data)))
        return row

    def upsert_bars(self, rows):
        self.bar_upserts.append(list(rows))
        return {"ok": True, "created": len(rows), "updated": 0, "skipped": 0}


class StartupStorageRoutesTest(unittest.TestCase):
    def test_register_startup_routes_keeps_expected_exports_and_status_shape(self):
        app = _FakeApp()
        pb = _FakePB()
        pb.records[("ibkr_startup", "live", "global")] = {
            "environment": "live",
            "date": "global",
            "data": {
                "startup_label": "LIVE-20260424-070000-001",
                "status": "running",
                "current_step": "manual_trigger",
                "operator_action": "open feishu",
                "steps": {"manual_trigger": {"status": "waiting"}},
            },
        }

        deps = {
            "pb": pb,
            "normalize_environment": lambda value, fallback: str(value or fallback).strip().lower() or fallback,
            "time_strings": lambda: {"us": "2026-04-24 07:00:00"},
            "get_state_payload": lambda state_key, environment, date="global": pb.get_state(state_key, environment, date=date),
            "normalize_startup_state": lambda value, environment: dict(value or {}),
            "build_startup_cycle_id": lambda environment: f"{environment}-cycle",
            "build_startup_label": lambda environment, startup_seq, started_at: f"{environment}-{startup_seq}",
            "startup_chat_id": lambda environment: f"chat-{environment}",
            "default_startup_steps": lambda: {},
            "normalize_startup_fields": lambda value: dict(value or {}),
            "merge_startup_steps": lambda current, patch, trigger_login: dict(current or patch or {}),
            "deliver_startup_progress_card": lambda state, environment: {"success": True, "message_id": "msg-1"},
            "resolve_startup_step_label": lambda state: str((state or {}).get("current_step") or ""),
            "write_system_event_record": lambda *args, **kwargs: True,
            "ibkr_startup_state_key": "ibkr_startup",
            "ibkr_startup_state_date": "global",
            "as_dict": lambda value: dict(value or {}),
        }

        exports = register_startup_routes(app, deps=deps)

        self.assertEqual(
            set(exports),
            {"custom_ibkr_startup_progress", "custom_ibkr_startup_status"},
        )
        self.assertIn(("/api/custom/ibkr/startup/progress", ("POST",)), app.routes)
        self.assertIn(("/api/custom/ibkr/startup/status", ("GET",)), app.routes)

        with mock.patch.object(startup_status_routes, "request", types.SimpleNamespace(args={"environment": "live"})):
            with mock.patch.object(startup_status_routes, "jsonify", side_effect=lambda payload: payload):
                payload = exports["custom_ibkr_startup_status"]()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["startup_label"], "LIVE-20260424-070000-001")
        self.assertEqual(payload["state"]["current_step"], "manual_trigger")
        self.assertEqual(payload["state"]["operator_action"], "open feishu")

    def test_register_storage_routes_keeps_expected_exports_and_ping_write_uses_create_path(self):
        app = _FakeApp()
        pb = _FakePB()
        deps = {
            "pb": pb,
            "normalize_environment": lambda value, fallback: str(value or fallback).strip().lower() or fallback,
            "parse_boolean": lambda value, fallback: str(value).strip().lower() not in {"0", "false", "no", "off"},
            "config_value": lambda key, default, environment: default,
        }

        exports = register_storage_routes(app, deps=deps)

        self.assertEqual(
            set(exports),
            {
                "custom_ibkr_ping_write",
                "custom_ibkr_bars",
                "custom_ibkr_indicator",
                "custom_ibkr_indicators",
                "custom_ibkr_scan",
                "custom_ibkr_data_quality_upsert",
                "custom_ibkr_data_quality_daily_upsert",
                "custom_ibkr_data_quality_truth_upsert",
            },
        )
        self.assertIn(("/api/custom/ibkr/ping_write", ("GET",)), app.routes)
        self.assertIn(("/api/custom/ibkr/bars", ("POST",)), app.routes)
        self.assertIn(("/api/custom/ibkr/data_quality/truth_upsert", ("POST",)), app.routes)

        with mock.patch.object(storage_ping_routes.time, "time", return_value=1713927600.0):
            with mock.patch.object(storage_ping_routes, "jsonify", side_effect=lambda payload: payload):
                payload = exports["custom_ibkr_ping_write"]()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["id"], "ibkr_signals-1")
        self.assertEqual(len(pb.created), 1)
        self.assertEqual(pb.created[0][0], "ibkr_signals")

    def test_bars_route_preserves_publish_toggle_behavior(self):
        app = _FakeApp()
        pb = _FakePB()
        deps = {
            "pb": pb,
            "normalize_environment": lambda value, fallback: str(value or fallback).strip().lower() or fallback,
            "parse_boolean": lambda value, fallback: str(value).strip().lower() not in {"0", "false", "no", "off"},
            "config_value": lambda key, default, environment: "false",
        }
        exports = register_storage_routes(app, deps=deps)

        request_stub = types.SimpleNamespace(
            get_json=lambda silent=True: {
                "environment": "live",
                "bars": [{"symbol": "AAPL", "interval": "5m", "bar_time_ms": 1713927600000}],
            }
        )
        with mock.patch.object(storage_bar_routes, "request", request_stub):
            with mock.patch.object(storage_bar_routes, "jsonify", side_effect=lambda payload: payload):
                payload = exports["custom_ibkr_bars"]()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "ibkr_bar_publish_enabled=false")
        self.assertEqual(pb.bar_upserts, [])


if __name__ == "__main__":
    unittest.main()
