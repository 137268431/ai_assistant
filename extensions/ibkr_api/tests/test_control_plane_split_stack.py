import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_scheduler" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

os.environ.setdefault("IBKR_SCHEDULER_AUTOSTART", "false")

if "flask" not in sys.modules:
    import types

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
    flask_stub.request = SimpleNamespace(
        args={},
        headers={},
        method="GET",
        get_json=lambda silent=True: {},
        get_data=lambda cache=True: b"",
    )
    sys.modules["flask"] = flask_stub

from ibkr_api import api_app as api_app_mod
from ibkr_api.order_upsert import build_order_upsert_response
from ibkr_api.signal_ack import build_signal_ack_orders
from ibkr_scheduler.scheduler_app import (
    BAR_INGEST_CURSOR_STATE_KEY,
    COMPUTE_DISPATCH_CURSOR_STATE_KEY,
    SchedulerService,
)


class _FakeConfig:
    def refresh(self):
        return None

    def get_for_environment(self, key, environment, default=None):
        return "TRUE" if key.startswith("pb_") else (default or "")


class _FakePB:
    def __init__(self):
        self.states = {}
        self.records = []

    def get_state(self, state_key, environment, date="global"):
        return self.states.get((state_key, environment, date))

    def upsert_state(self, state_key, environment, data, date="global"):
        record = {"data": data}
        self.states[(state_key, environment, date)] = record
        return record

    def create_record(self, collection, data):
        self.records.append((collection, data))
        return data


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.content = b"{}"
        self.headers = {}

    def json(self):
        return self._payload


def _sample_compute_status_payload():
    return {
        "ok": True,
        "environment": "live",
        "runtime_mode": "remote",
        "service_profile": "compute",
        "total_engines": 1,
        "ready_engines": 1,
        "engines": {
            "live/AAPL/5m": {
                "environment": "live",
                "bar_count": 120,
                "is_ready": True,
                "last_bar_time_ms": 1713797100000,
                "last_close": 181.25,
            }
        },
        "service_topology": api_app_mod.build_service_topology(),
    }


def _sample_runtime_status_payload(*, environment="live", authenticated=True):
    return {
        "ok": True,
        "environment": environment,
        "runtime_mode": "remote",
        "service_profile": "runtime",
        "starting": True,
        "startup_complete": False,
        "runtime_phase": "running",
        "service_topology": api_app_mod.build_service_topology(),
        "market_session": {"kind": "regular"},
        "gateway": {
            "running": True,
            "reachable": True,
            "managed_by": "systemd",
            "status_code": 200 if authenticated else 401,
            "pid": 123,
            "uptime_s": 60,
        },
        "session": {
            "authenticated": authenticated,
            "running": True,
            "consecutive_failures": 0 if authenticated else 3,
            "last_check": "2026-04-22T07:00:00-04:00",
            "last_tickle": "2026-04-22T07:00:00-04:00",
        },
        "auth_recovery": {
            "cycle_id": "cycle-1",
            "recovery_phase": "recovered" if authenticated else "resume_waiting_manual",
            "recovery_class": "manual_auth_required" if not authenticated else "",
            "recovery_reason": "auto_restore" if not authenticated else "",
            "interruption_kind": "server_boot_resume" if not authenticated else "",
            "probe_result": "resume_probe_timeout" if not authenticated else "",
            "probe_attempts": 3 if not authenticated else 0,
            "manual_takeover_active": False,
            "auto_restart_scheduled": False,
        },
        "websocket": {
            "connected": authenticated,
            "ready": authenticated,
            "running": True,
            "message_count": 10,
        },
        "realtime_quotes": {
            "total_quotes": 5,
            "stale_quotes": 0,
            "tick_count": 12,
            "update_count": 12,
        },
        "canonical_5m": {
            "enabled": True,
            "driver": "ibkr_history_close",
            "close_delay_sec": 8,
            "pending_symbols": [],
            "written_symbols": ["AAPL"],
        },
        "data_backfill": {"total_backfilled": 10},
        "order_tracker": {"running": True, "tracked_orders": 2, "last_poll": "2026-04-22T07:00:00-04:00"},
        "warmup": {
            "phase": "ready" if authenticated else "pending",
            "trading_gate_open": authenticated,
            "trading_gate_reason": "ready" if authenticated else "warmup_incomplete",
            "required_interval": "5m",
            "symbols_total": 1,
            "trade_symbols_total": 1,
            "monitor_symbols_total": 0,
            "ready_symbols": 1 if authenticated else 0,
            "ready_trade_symbols": 1 if authenticated else 0,
            "ready_monitor_symbols": 0,
            "pending_symbols": [] if authenticated else ["AAPL"],
            "symbols": ["AAPL"],
            "trade_symbols": ["AAPL"],
            "monitor_symbols": [],
            "ready_symbols_list": ["AAPL"] if authenticated else [],
            "integrity_pending_symbols": [],
            "finished_at": "2026-04-22T07:00:00-04:00" if authenticated else "",
        },
        "realtime_compute": {
            "runs": 2,
            "queue_size": 0,
            "thread_alive": True,
            "inflight": False,
            "last_started": "2026-04-22T07:00:00-04:00",
            "last_run": "2026-04-22T07:00:10-04:00",
            "last_result": {"processed": 2, "signals": 1, "errors": 0, "elapsed_s": 0.5},
        },
        "daily_scan": {
            "market_date": "2026-04-22",
            "status": "completed",
            "result": {"symbols": ["AAPL"]},
        },
        "market_universe": {
            "market_date": "2026-04-22",
            "active_target_date": "2026-04-22",
            "active_target_count": 1,
            "active_trade_symbols": ["AAPL"],
            "watchlist_pool_count": 1,
        },
        "runtime_control": {
            "desired_running": True,
            "environment": environment,
            "last_reason": "manual_start",
            "last_source": "runtime_page",
        },
    }


class ControlPlaneSplitStackTest(unittest.TestCase):
    def test_runtime_config_route_returns_effective_environment_values(self):
        rows = [
            {"key": "alpha", "value": "global", "environment": "global", "updated": "2026-04-22 00:00:00"},
            {"key": "alpha", "value": "live", "environment": "live", "updated": "2026-04-22 00:05:00"},
            {"key": "beta", "value": "fallback", "environment": "", "updated": "2026-04-22 00:01:00"},
        ]

        with mock.patch.object(api_app_mod.pb, "get_runtime_config", return_value=rows):
            with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                payload = api_app_mod.custom_ibkr_runtime_config()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "live")
        self.assertEqual(payload["scope"], "effective")
        item_map = {item["key"]: item["value"] for item in payload["items"]}
        self.assertEqual(item_map["alpha"], "live")
        self.assertEqual(item_map["beta"], "fallback")
        self.assertIn("ibkr-api", payload["service_topology"]["services"])
        self.assertIn("ibkr-scheduler", payload["service_topology"]["services"])

    def test_api_status_reports_native_routes(self):
        scheduler_payload = {
            "ok": True,
            "status": "running",
            "environment": "live",
            "loop_interval_seconds": 30,
            "ingest_cursor": {},
            "compute_dispatch_cursor": {},
            "jobs": {"ibkr_compute_runtime": {"status": "ok"}},
        }
        with mock.patch.object(api_app_mod.config, "refresh", return_value=None):
            with mock.patch.object(api_app_mod, "_scheduler_status", return_value=scheduler_payload):
                payload = api_app_mod.status()
        self.assertTrue(payload["ok"])
        self.assertIn("ibkr/2fa/request", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/respond", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/result", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/status", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/takeover", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/probe", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/panic-reset", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/bars", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/indicator", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/indicators", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/scan", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/data_quality/upsert", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/data_quality/truth_upsert", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/healthz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/orders/upsert", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/orders/cancel_group", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/reverse/dispatch", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/runtime/config", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/signal", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/signals", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/signals/pending", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/startup/progress", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/startup/status", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/statusz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/reverse/calculate", payload["compatibility"]["native_custom_routes"])
        self.assertIn("order/cancel", payload["compatibility"]["native_webhook_routes"])
        self.assertIn("signal/confirm", payload["compatibility"]["native_webhook_routes"])
        self.assertIn("signal/cancel", payload["compatibility"]["native_webhook_routes"])
        self.assertIn("order/close", payload["compatibility"]["native_webhook_routes"])
        self.assertNotIn("ibkr/signal", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/signals", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/orders/upsert", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/orders/cancel_group", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/orders/close_group", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/orders/reconcile", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/reverse/ack", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/reverse/calculate", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/reverse/dispatch", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/reverse/list", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/reverse/pending", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/signals/ack", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("order/cancel", payload["compatibility"]["delegated_pocketbase_webhook_routes"])
        self.assertNotIn("order/close", payload["compatibility"]["delegated_pocketbase_webhook_routes"])
        self.assertNotIn("signal/confirm", payload["compatibility"]["delegated_pocketbase_webhook_routes"])
        self.assertNotIn("signal/cancel", payload["compatibility"]["delegated_pocketbase_webhook_routes"])
        self.assertIn("ibkr/account", payload["compatibility"]["direct_proxy_routes"])
        self.assertIn("system/cronz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/event", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/healthz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/schedulerz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/monitorz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/summaryz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr-api", payload["service_topology"]["services"])
        self.assertEqual(payload["compatibility"]["pocketbase_proxy_routes"]["custom"], "/api/custom/*")
        self.assertEqual(payload["compatibility"]["pocketbase_proxy_routes"]["webhook"], "/webhook/*")

    def test_generic_custom_proxy_falls_back_to_pocketbase(self):
        sentinel = {"ok": True, "source": "pb"}
        with mock.patch.object(api_app_mod, "_proxy_custom_to_pb", return_value=sentinel) as proxy_mock:
            payload = api_app_mod.custom_proxy("ibkr/legacy_fallback")
        self.assertIs(payload, sentinel)
        proxy_mock.assert_called_once_with("ibkr/legacy_fallback")

    def test_generic_webhook_proxy_falls_back_to_pocketbase(self):
        sentinel = {"ok": True, "source": "pb-webhook"}
        with mock.patch.object(api_app_mod, "_forward_request", return_value=sentinel) as forward_mock:
            payload = api_app_mod.webhook_proxy("signal/confirm")
        self.assertIs(payload, sentinel)
        forward_mock.assert_called_once_with(api_app_mod.PB_BASE_URL, "/webhook/signal/confirm")

    def test_signals_pending_route_reads_native_pb_records_and_enriches_indicator(self):
        signal_rows = [
            {
                "id": "sig-row-1",
                "signal_id": "sig-1",
                "environment": "live",
                "symbol": "AAPL",
                "direction": "long",
                "signal": "buy",
                "entry": 180.0,
                "stop_loss": 178.0,
                "take_profit": 184.0,
                "limit_price": 180.1,
                "shares": 10,
                "rr": 2.0,
                "reason": "breakout",
                "exchange": "NASDAQ",
                "interval": "5m",
                "chart_tf": "5m",
                "date": "2026-04-22",
                "us_time": "2026-04-22 09:35:00",
                "cn_time": "2026-04-22 21:35:00",
                "bar_time_ms": 1713797700000,
                "extra": {"note": "from-test"},
                "created": "2026-04-22 09:35:01",
            }
        ]
        indicator_row = {
            "id": "ind-1",
            "symbol": "AAPL",
            "exchange": "NASDAQ",
            "interval": "5m",
            "script_tag": "main",
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
            "bar_index": 321,
            "created": "2026-04-22 09:35:01",
            "updated": "2026-04-22 09:35:02",
            "extra": {"close": 181.25, "crsi": 72.1},
        }

        with mock.patch.object(api_app_mod.request, "args", {"environment": "live", "date": "2026-04-22"}):
            with mock.patch.object(api_app_mod.pb, "get_records", return_value=signal_rows) as records_mock:
                with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=indicator_row) as first_mock:
                    payload = api_app_mod.custom_ibkr_signals_pending()

        records_mock.assert_called_once()
        first_mock.assert_called_once()
        self.assertEqual(len(payload["ibkr_signals"]), 1)
        signal = payload["ibkr_signals"][0]
        self.assertEqual(signal["signal_id"], "sig-1")
        self.assertEqual(signal["latest_indicator"]["id"], "ind-1")
        self.assertEqual(signal["extra"]["latest_indicator_id"], "ind-1")
        self.assertEqual(signal["extra"]["close"], 181.25)
        self.assertEqual(signal["extra"]["crsi"], 72.1)

    def test_signal_route_uses_native_builder(self):
        sentinel = {"ok": True, "source": "ibkr-api", "action": "created"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live", "signal_id": "sig-1", "symbol": "AAPL"}):
            with mock.patch.object(api_app_mod, "_proxy_custom_to_pb", side_effect=AssertionError("unexpected pb fallback")) as proxy_mock:
                with mock.patch.object(api_app_mod, "build_signal_ingest_response", return_value=(sentinel, 200)) as builder_mock:
                    payload = api_app_mod.custom_ibkr_signal()
        self.assertEqual(payload["action"], "created")
        proxy_mock.assert_not_called()
        builder_mock.assert_called_once()

    def test_signals_batch_route_uses_native_builder(self):
        sentinel = {"ok": True, "source": "ibkr-api", "created": 2}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live", "items": [{"signal_id": "sig-1", "symbol": "AAPL"}]}):
            with mock.patch.object(api_app_mod, "_proxy_custom_to_pb", side_effect=AssertionError("unexpected pb fallback")) as proxy_mock:
                with mock.patch.object(api_app_mod, "build_signals_ingest_response", return_value=(sentinel, 200)) as builder_mock:
                    payload = api_app_mod.custom_ibkr_signals()
        self.assertEqual(payload["created"], 2)
        proxy_mock.assert_not_called()
        builder_mock.assert_called_once()

    def test_system_signal_expiry_job_route_uses_native_builder(self):
        sentinel = {"ok": True, "expired_count": 1, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live"}):
            with mock.patch.object(api_app_mod, "build_signal_expiry_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_system_job_signal_expiry()
        self.assertEqual(payload["expired_count"], 1)
        builder_mock.assert_called_once()

    def test_system_order_detail_integrity_job_route_uses_native_builder(self):
        sentinel = {"success": True, "summary": {"repaired": 1}, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live"}):
            with mock.patch.object(api_app_mod, "build_order_detail_integrity_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_system_job_order_detail_integrity()
        self.assertEqual(payload["summary"]["repaired"], 1)
        builder_mock.assert_called_once()

    def test_orders_cancel_group_route_uses_native_builder(self):
        sentinel = {"ok": True, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"id": "sig-1_entry", "environment": "live"}):
            with mock.patch.object(api_app_mod, "build_order_cancel_group_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_orders_cancel_group()
        self.assertEqual(payload["source"], "ibkr-api")
        builder_mock.assert_called_once()

    def test_orders_close_group_route_uses_native_builder(self):
        sentinel = {"ok": True, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"id": "sig-1_entry", "environment": "live"}):
            with mock.patch.object(api_app_mod, "build_order_close_group_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_orders_close_group()
        self.assertEqual(payload["source"], "ibkr-api")
        builder_mock.assert_called_once()

    def test_reverse_calculate_route_uses_native_builder(self):
        sentinel = {"success": True, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"symbol": "AAPL", "direction": "long"}):
            with mock.patch.object(api_app_mod, "build_reverse_calculate_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_reverse_calculate()
        self.assertTrue(payload["success"])
        builder_mock.assert_called_once()

    def test_orders_upsert_route_writes_order_and_detail_natively(self):
        request_payload = {
            "environment": "live",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "symbol": "AAPL",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "role": "entry",
            "signal_id": "sig-1",
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
            "extra": {"reason": "entry submitted"},
        }
        created = []

        def fake_create_record(collection, data):
            row = dict(data)
            row["id"] = f"{collection}-1"
            created.append((collection, row))
            return row

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=None):
                with mock.patch.object(api_app_mod.pb, "get_records", return_value=[]):
                    with mock.patch.object(api_app_mod.pb, "create_record", side_effect=fake_create_record):
                        payload = api_app_mod.custom_ibkr_orders_upsert()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["order"]["unique_id"], "sig-1_entry")
        self.assertEqual(payload["order"]["status"], "Submitted")
        self.assertEqual(created[0][0], "orders")
        self.assertEqual(created[1][0], "ibkr_order_details")
        self.assertEqual(created[1][1]["order_id"], "sig-1_entry")
        self.assertEqual(created[1][1]["extra"]["sequence"], 1)
        self.assertEqual(created[1][1]["extra"]["source"], "orders/upsert")

    def test_orders_reconcile_route_repairs_missing_detail_natively(self):
        request_payload = {
            "environment": "live",
            "trade_group_id": "sig-1_entry",
        }
        order_rows = [
            {
                "id": "order-1",
                "unique_id": "sig-1_entry",
                "order_type": "Entry",
                "order_id": "",
                "broker_order_id": "",
                "symbol": "AAPL",
                "environment": "live",
                "direction": "long",
                "quantity": 10,
                "limit_price": 180.1,
                "status": "Submitted",
                "filled_qty": 0,
                "fill_price": 0,
                "signal_id": "sig-1",
                "trade_group_id": "sig-1_entry",
                "entry_order_unique_id": "sig-1_entry",
                "parent_order_unique_id": "",
                "sibling_order_unique_id": "",
                "role": "entry",
                "relation_status": "active",
                "position_side": "long",
                "order_time": "2026-04-22 09:35:00",
                "fill_time": "",
                "bar_time_ms": 1713797700000,
                "us_time": "2026-04-22 09:35:00",
                "cn_time": "2026-04-22 21:35:00",
                "extra": {"reason": "entry submitted"},
            }
        ]
        created = []

        def fake_get_records(collection, filter=None, sort=None, per_page=200, page=1):
            if collection == "orders":
                return order_rows
            if collection == "ibkr_order_details":
                return []
            return []

        def fake_create_record(collection, data):
            row = dict(data)
            row["id"] = f"{collection}-1"
            created.append((collection, row))
            return row

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "get_records", side_effect=fake_get_records):
                with mock.patch.object(api_app_mod.pb, "create_record", side_effect=fake_create_record):
                    payload = api_app_mod.custom_ibkr_orders_reconcile()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["summary"]["repaired"], 1)
        self.assertEqual(created[0][0], "ibkr_order_details")
        self.assertEqual(created[0][1]["order_id"], "sig-1_entry")
        self.assertEqual(created[0][1]["extra"]["source"], "orders/reconcile")
        self.assertEqual(created[0][1]["extra"]["repair_mode"], "missing_ibkr_order_details")

    def test_signals_ack_route_updates_signal_and_uses_native_order_upserts(self):
        signal_row = {
            "id": "sig-row-1",
            "signal_id": "sig-1",
            "symbol": "AAPL",
            "direction": "long",
            "shares": 10,
            "entry": 180.0,
            "stop_loss": 178.0,
            "take_profit": 184.0,
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
        }
        request_payload = {
            "environment": "live",
            "signal_id": "sig-1",
            "status": "executed",
            "note": "broker_ack",
            "order": {
                "unique_id": "sig-1_entry",
                "order_type": "Entry",
                "status": "Submitted",
                "direction": "long",
                "quantity": 10,
                "limit_price": 180.1,
            },
        }
        upsert_result = (
            {
                "success": True,
                "order": {
                    "id": "order-1",
                    "unique_id": "sig-1_entry",
                    "status": "Submitted",
                },
            },
            200,
        )

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "get_first_record", return_value=signal_row):
                with mock.patch.object(api_app_mod.pb, "update_record", return_value={"id": "sig-row-1"}) as update_mock:
                    with mock.patch.object(api_app_mod, "build_order_upsert_response", return_value=upsert_result) as req_mock:
                        payload = api_app_mod.custom_ibkr_signals_ack()

        update_mock.assert_called_once_with(
            "ibkr_signals",
            "sig-row-1",
            {"status": "executed", "note": "broker_ack"},
        )
        self.assertEqual(req_mock.call_count, 3)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["signal_id"], "sig-1")
        self.assertEqual(payload["status"], "Submitted")
        self.assertEqual(payload["order_results"][0]["unique_id"], "sig-1_entry")

    def test_signals_ack_route_requires_signal_id(self):
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live"}):
            payload, status_code = api_app_mod.custom_ibkr_signals_ack()
        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"], "Missing signal_id")

    def test_reverse_dispatch_route_uses_native_builder(self):
        sentinel = {"ok": True, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"reverse_id": "rev-1", "action": "execute"}):
            with mock.patch.object(api_app_mod, "build_reverse_dispatch_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_reverse_dispatch()
        self.assertEqual(payload["source"], "ibkr-api")
        builder_mock.assert_called_once()

    def test_reverse_list_route_uses_native_builder(self):
        sentinel = {"ibkr_signals": [{"id": "rev-1"}]}
        with mock.patch.object(api_app_mod.request, "args", {"environment": "live", "date": "2026-04-22", "symbol": "AAPL", "status": "pending", "limit": "20"}):
            with mock.patch.object(api_app_mod, "build_reverse_list_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_reverse_list()
        self.assertEqual(payload["ibkr_signals"][0]["id"], "rev-1")
        builder_mock.assert_called_once()

    def test_reverse_pending_route_uses_native_builder(self):
        sentinel = {"ibkr_signals": [{"id": "rev-1"}]}
        with mock.patch.object(api_app_mod.request, "args", {"environment": "live", "limit": "20"}):
            with mock.patch.object(api_app_mod, "build_reverse_pending_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_reverse_pending()
        self.assertEqual(payload["ibkr_signals"][0]["id"], "rev-1")
        builder_mock.assert_called_once()

    def test_reverse_ack_route_uses_native_builder(self):
        sentinel = {"success": True, "signal": {"id": "rev-1"}}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"reverse_id": "rev-1", "status": "confirmed"}):
            with mock.patch.object(api_app_mod, "build_reverse_ack_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_ibkr_reverse_ack()
        self.assertTrue(payload["success"])
        builder_mock.assert_called_once()

    def test_order_cancel_webhook_uses_native_builder(self):
        with mock.patch.object(api_app_mod.request, "args", {"id": "sig-1_entry", "environment": "live"}):
            with mock.patch.object(
                api_app_mod,
                "build_order_cancel_webhook_response",
                return_value=({"body": "<html>cancel</html>", "content_type": "text/html; charset=utf-8"}, 200),
            ) as builder_mock:
                response = api_app_mod.webhook_order_cancel()
        self.assertEqual(response[0], "<html>cancel</html>")
        self.assertEqual(response[1], 200)
        self.assertEqual(response[2]["Content-Type"], "text/html; charset=utf-8")
        builder_mock.assert_called_once()

    def test_order_close_webhook_uses_native_builder(self):
        with mock.patch.object(api_app_mod.request, "args", {"id": "sig-1_entry", "environment": "live"}):
            with mock.patch.object(
                api_app_mod,
                "build_order_close_webhook_response",
                return_value=({"body": "<html>close</html>", "content_type": "text/html; charset=utf-8"}, 200),
            ) as builder_mock:
                response = api_app_mod.webhook_order_close()
        self.assertEqual(response[0], "<html>close</html>")
        self.assertEqual(response[1], 200)
        self.assertEqual(response[2]["Content-Type"], "text/html; charset=utf-8")
        builder_mock.assert_called_once()

    def test_signal_confirm_webhook_uses_native_builder(self):
        with mock.patch.object(api_app_mod.request, "args", {"id": "sig-1", "environment": "live"}):
            with mock.patch.object(
                api_app_mod,
                "build_signal_confirm_webhook_response",
                return_value=({"body": "<html>confirm</html>", "content_type": "text/html; charset=utf-8"}, 200),
            ) as builder_mock:
                response = api_app_mod.webhook_signal_confirm()
        self.assertEqual(response[0], "<html>confirm</html>")
        self.assertEqual(response[1], 200)
        self.assertEqual(response[2]["Content-Type"], "text/html; charset=utf-8")
        builder_mock.assert_called_once()

    def test_signal_cancel_webhook_uses_native_builder(self):
        with mock.patch.object(api_app_mod.request, "args", {"id": "sig-1", "environment": "live"}):
            with mock.patch.object(
                api_app_mod,
                "build_signal_cancel_webhook_response",
                return_value=({"body": "<html>cancel</html>", "content_type": "text/html; charset=utf-8"}, 200),
            ) as builder_mock:
                response = api_app_mod.webhook_signal_cancel()
        self.assertEqual(response[0], "<html>cancel</html>")
        self.assertEqual(response[1], 200)
        self.assertEqual(response[2]["Content-Type"], "text/html; charset=utf-8")
        builder_mock.assert_called_once()

    def test_feishu_callback_response_sets_update_card_token_header(self):
        class _JsonResponse:
            def __init__(self, payload):
                self.payload = payload
                self.headers = {}

        with mock.patch.object(api_app_mod, "jsonify", side_effect=lambda payload: _JsonResponse(payload)):
            response, status_code = api_app_mod._feishu_callback_response(
                {"ok": True},
                update_token="token-123",
                status_code=202,
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(response.payload["ok"], True)
        self.assertEqual(response.headers["update_card_token"], "token-123")

    def test_webhook_tv_indicator_route_uses_native_indicator_upsert(self):
        sentinel = {"ok": True, "kind": "indicator"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"type": "indicator", "symbol": "AAPL"}):
            with mock.patch.object(api_app_mod, "_upsert_tv_indicator", return_value=sentinel) as upsert_mock:
                payload = api_app_mod.webhook_tv()
        self.assertIs(payload, sentinel)
        upsert_mock.assert_called_once_with({"type": "indicator", "symbol": "AAPL"})

    def test_webhook_tv_signal_route_uses_native_signal_upsert(self):
        sentinel = {"ok": True, "kind": "signal"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"symbol": "AAPL"}):
            with mock.patch.object(api_app_mod, "_upsert_tv_signal", return_value=sentinel) as upsert_mock:
                payload = api_app_mod.webhook_tv()
        self.assertIs(payload, sentinel)
        upsert_mock.assert_called_once_with({"symbol": "AAPL"})

    def test_webhook_feishu_callback_url_verification_returns_challenge(self):
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"type": "url_verification", "challenge": "abc"}):
            payload = api_app_mod.webhook_feishu_callback()
        self.assertEqual(payload["challenge"], "abc")

    def test_webhook_feishu_callback_dispatches_signal_actions(self):
        callback_result = {"toast": {"type": "success"}}
        with mock.patch.object(
            api_app_mod.request,
            "get_json",
            return_value={
                "event": {
                    "token": "card-token",
                    "action": {
                        "value": {
                            "action": "confirm",
                            "signal_id": "sig-1",
                            "environment": "live",
                        }
                    },
                }
            },
        ):
            with mock.patch.object(api_app_mod, "_dispatch_feishu_signal_callback", return_value=(callback_result, 200)) as dispatch_mock:
                payload = api_app_mod.webhook_feishu_callback()
        self.assertEqual(payload["toast"]["type"], "success")
        dispatch_mock.assert_called_once_with("confirm", "sig-1", "live")

    def test_build_signal_ack_orders_creates_entry_and_children(self):
        signal_row = {
            "signal_id": "sig-1",
            "symbol": "AAPL",
            "direction": "long",
            "shares": 10,
            "entry": 180.0,
            "stop_loss": 178.0,
            "take_profit": 184.0,
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
        }
        payload = {
            "signal_id": "sig-1",
            "order": {
                "unique_id": "sig-1_entry",
                "order_type": "Entry",
                "direction": "long",
                "status": "Submitted",
                "limit_price": 180.1,
            },
        }
        orders = build_signal_ack_orders(signal_row, payload, "live")
        self.assertEqual(len(orders), 3)
        self.assertEqual(orders[0]["role"], "entry")
        self.assertEqual(orders[1]["order_type"], "TakeProfit")
        self.assertEqual(orders[2]["order_type"], "StopLoss")
        self.assertEqual(orders[1]["parent_order_unique_id"], "sig-1_entry")
        self.assertEqual(orders[2]["sibling_order_unique_id"], "sig-1_take_profit")

    def test_build_order_upsert_response_idempotent_skips_detail_append(self):
        existing_row = {
            "id": "order-1",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "order_id": "",
            "broker_order_id": "",
            "symbol": "AAPL",
            "environment": "live",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "filled_qty": 0,
            "fill_price": 0,
            "signal_id": "sig-1",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "parent_order_unique_id": "",
            "sibling_order_unique_id": "",
            "role": "entry",
            "relation_status": "active",
            "position_side": "long",
            "order_time": "2026-04-22 09:35:00",
            "fill_time": "",
            "bar_time_ms": 1713797700000,
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "extra": {
                "environment": "live",
                "order_id": "",
                "broker_order_id": "",
                "order_time": "2026-04-22 09:35:00",
                "us_time": "2026-04-22 09:35:00",
                "cn_time": "2026-04-22 21:35:00",
                "bar_time_ms": 1713797700000,
                "trade_group_id": "sig-1_entry",
                "entry_order_unique_id": "sig-1_entry",
                "parent_order_unique_id": "",
                "sibling_order_unique_id": "",
                "role": "entry",
                "relation_status": "active",
                "position_side": "long",
                "previous_status": "",
                "current_status": "Submitted",
                "status_transition_text": "待成交",
                "status_updated_us_time": "2026-04-22 09:35:00",
                "status_updated_cn_time": "2026-04-22 21:35:00",
                "status_updated_bar_time_ms": 1713797700000,
                "last_status_source": "orders/upsert",
                "last_status_reason": "entry submitted",
                "created_us_time": "2026-04-22 09:35:00",
                "created_cn_time": "2026-04-22 21:35:00",
                "created_bar_time_ms": 1713797700000,
                "reason": "entry submitted",
            },
        }
        request_payload = {
            "environment": "live",
            "unique_id": "sig-1_entry",
            "order_type": "Entry",
            "symbol": "AAPL",
            "direction": "long",
            "quantity": 10,
            "limit_price": 180.1,
            "status": "Submitted",
            "trade_group_id": "sig-1_entry",
            "entry_order_unique_id": "sig-1_entry",
            "role": "entry",
            "signal_id": "sig-1",
            "us_time": "2026-04-22 09:35:00",
            "cn_time": "2026-04-22 21:35:00",
            "bar_time_ms": 1713797700000,
            "extra": {"reason": "entry submitted"},
        }
        fake_pb = _FakePB()
        fake_pb.get_first_record = mock.Mock(return_value=existing_row)
        fake_pb.get_records = mock.Mock(return_value=[])
        fake_pb.update_record = mock.Mock()
        fake_pb.create_record = mock.Mock()

        payload, status_code = build_order_upsert_response(
            fake_pb,
            payload=request_payload,
            normalize_environment=api_app_mod._normalize_environment,
            escape_filter_string=api_app_mod._escape_filter_string,
        )
        self.assertEqual(status_code, 200)
        self.assertTrue(payload["idempotent"])
        fake_pb.update_record.assert_not_called()
        fake_pb.create_record.assert_not_called()

    def test_schedulerz_route_returns_scheduler_summary(self):
        scheduler_payload = {
            "ok": True,
            "status": "running",
            "environment": "live",
            "loop_interval_seconds": 30,
            "ingest_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713797100000}}},
            "compute_dispatch_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000, "last_dispatched_at_ms": 1713797160000}}},
            "jobs": {
                "ibkr_compute_runtime": {
                    "status": "ok",
                    "last_success_at_ms": 1713797160000,
                }
            },
        }
        with mock.patch.object(api_app_mod.config, "refresh", return_value=None):
            with mock.patch.object(api_app_mod, "_scheduler_status", return_value=scheduler_payload):
                with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                    payload = api_app_mod.custom_system_schedulerz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["scheduler"]["job_count"], 1)
        self.assertEqual(payload["scheduler"]["dispatch_lag_min"], 5.0)
        self.assertIn("ibkr-scheduler", payload["service_topology"]["services"])

    def test_monitorz_route_merges_split_stack_service_monitor(self):
        base_monitor_payload = {
            "ok": True,
            "status": "ok",
            "environment": "live",
            "runtime": {
                "runtime_phase": "running",
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
                "gateway": {"running": True, "reachable": True, "managed_by": "ibkr-runtime", "pid": 123},
            },
            "compute": {
                "ready_engines": 4,
                "total_engines": 5,
                "compute_count": 12,
                "tracked_cursors": 3,
            },
            "flags": [],
            "pocketbase": {"disk": {"status": "ready", "data_path": "/opt/pocketbase/pb_data"}},
            "service_topology": api_app_mod.build_service_topology(),
        }
        scheduler_payload = {
            "ok": True,
            "status": "running",
            "environment": "live",
            "loop_interval_seconds": 30,
            "ingest_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713797100000}}},
            "compute_dispatch_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000, "last_dispatched_at_ms": 1713797160000}}},
            "jobs": {"ibkr_compute_runtime": {"status": "ok"}},
        }
        request_results = [
            {"ok": True, "status_code": 200, "payload": base_monitor_payload, "target_url": "http://compute/ibkr/monitor", "error": ""},
            {"ok": True, "status_code": 200, "payload": {"code": 200, "message": "OK"}, "target_url": "http://pb/api/health", "error": ""},
        ]

        with mock.patch.object(api_app_mod.config, "refresh", return_value=None):
            with mock.patch.object(api_app_mod, "_request_json", side_effect=request_results):
                with mock.patch.object(api_app_mod, "_scheduler_status", return_value=scheduler_payload):
                    with mock.patch.object(api_app_mod.pb, "get_runtime_config", return_value=[]):
                        with mock.patch.object(api_app_mod.pb, "get_records", return_value=[]):
                            with mock.patch.object(api_app_mod.pb, "get_all_records", return_value=[]):
                                with mock.patch.object(api_app_mod, "_probe_console_status", return_value={"ok": True, "status_code": 200, "target_url": "http://console/index.html", "error": ""}):
                                    with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                                        payload = api_app_mod.custom_system_monitorz()

        self.assertTrue(payload["ok"])
        self.assertIn("service_monitor", payload)
        self.assertIn("ibkr-console", payload["service_monitor"]["services"])
        self.assertEqual(payload["service_monitor"]["services"]["ibkr-scheduler"]["status"], "running")
        self.assertEqual(payload["scheduler"]["dispatch_lag_min"], 5.0)
        self.assertEqual(payload["upstream_monitor"]["target_url"], "http://compute/ibkr/monitor")
        self.assertEqual(payload["pocketbase"]["disk"]["data_path"], "/opt/pocketbase/pb_data")

    def test_summaryz_route_returns_native_split_stack_payload(self):
        compute_health = {
            "ok": True,
            "payload": {
                "ok": True,
                "status": "running",
                "compute_count": 9,
                "error_count": 1,
                "uptime_s": 600,
                "last_compute": "2026-04-22T07:05:00-04:00",
                "last_scan": "2026-04-22T07:00:00-04:00",
                "service_topology": api_app_mod.build_service_topology(),
            },
            "error": "",
        }
        compute_status = {
            "ok": True,
            "payload": _sample_compute_status_payload(),
            "error": "",
        }
        runtime_result = {
            "ok": True,
            "payload": _sample_runtime_status_payload(authenticated=True),
            "error": "",
            "selected_upstream": "http://runtime/ibkr/status",
            "proxy_upstream": "http://compute/ibkr/status",
            "direct_upstream": "http://runtime/ibkr/status",
        }
        config_rows = [
            {"key": "ibkr_compute_enabled", "value": "TRUE", "environment": "live"},
            {"key": "ibkr_trading_enabled", "value": "FALSE", "environment": "live"},
            {"key": "pb_scheduler_enabled", "value": "TRUE", "environment": "global"},
        ]
        recent_rows = [
            {
                "id": "evt-1",
                "event_type": "status_change",
                "level": "info",
                "source": "ibkr_scheduler",
                "environment": "live",
                "title": "[LIVE] Scheduler dispatch ok",
                "notified": True,
                "us_time": "2026-04-22 07:10:00",
                "created": "2026-04-22 07:10:01",
            }
        ]
        with mock.patch.object(api_app_mod, "_fetch_compute_health", return_value=compute_health):
            with mock.patch.object(api_app_mod, "_fetch_compute_status", return_value=compute_status):
                with mock.patch.object(api_app_mod, "_fetch_runtime_status", return_value=runtime_result):
                    with mock.patch.object(api_app_mod.pb, "get_runtime_config", return_value=config_rows):
                        with mock.patch.object(api_app_mod.pb, "get_records", return_value=recent_rows):
                            with mock.patch.object(api_app_mod.request, "args", {"environment": "live", "lite": "1"}):
                                payload = api_app_mod.custom_system_summaryz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["environment"], "live")
        self.assertFalse(payload["ibkr_trading_enabled"])
        self.assertEqual(payload["ibkr_compute"]["compute_count"], 9)
        self.assertEqual(payload["ibkr_runtime"]["proxy_upstream"], "http://runtime/ibkr/status")
        self.assertEqual(payload["recent_events"][0]["id"], "evt-1")
        self.assertIn("ibkr-api", payload["service_topology"]["services"])

    def test_system_healthz_route_uses_native_monitor_summary(self):
        monitor_payload = {
            "ok": True,
            "status": "warning",
            "requested_environment": "live",
            "actual_runtime_environment": "paper",
            "runtime_environment_mismatch": True,
            "service_topology": api_app_mod.build_service_topology(),
            "service_monitor": {"services": {"ibkr-api": {"status": "running"}}},
            "scheduler": {"status": "running"},
        }
        with mock.patch.object(api_app_mod, "_build_system_monitor_payload", return_value=monitor_payload):
            with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                payload = api_app_mod.custom_system_healthz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "warning")
        self.assertEqual(payload["actual_runtime_environment"], "paper")
        self.assertTrue(payload["runtime_environment_mismatch"])
        self.assertIn("ibkr-api", payload["service_topology"]["services"])

    def test_startup_status_route_reads_persisted_state(self):
        state_record = {
            "date": "global",
            "data": {
                "startup_label": "LIVE-20260422-070000-001",
                "status": "running",
                "current_step": "manual_trigger",
                "current_blocker": "等待人工开始 2FA",
                "operator_action": "去飞书点击开始验证",
                "steps": {
                    "manual_trigger": {
                        "label": "在飞书手动触发 2FA",
                        "status": "waiting",
                    }
                },
            },
        }
        with mock.patch.object(api_app_mod.pb, "get_state", return_value=state_record):
            with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                payload = api_app_mod.custom_ibkr_startup_status()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["startup_label"], "LIVE-20260422-070000-001")
        self.assertEqual(payload["state"]["current_step"], "manual_trigger")
        self.assertEqual(payload["state"]["operator_action"], "去飞书点击开始验证")

    def test_system_event_route_writes_record_with_native_delivery(self):
        records = []

        def fake_create_record(collection, data):
            records.append((collection, data))
            return data

        with mock.patch.object(api_app_mod.request, "get_json", return_value={
            "environment": "live",
            "title": "Runtime 已恢复",
            "detail": {"状态结论": "系统恢复"},
            "event_type": "status_change",
            "level": "info",
            "source": "ibkr_compute",
        }):
            with mock.patch.object(api_app_mod.config, "refresh", return_value=None):
                with mock.patch.object(api_app_mod, "_deliver_system_event_notification", return_value={"success": True, "message_id": "msg-1"}):
                    with mock.patch.object(api_app_mod.pb, "create_record", side_effect=fake_create_record):
                        payload = api_app_mod.custom_system_event()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["notified"])
        self.assertEqual(payload["message_id"], "msg-1")
        self.assertEqual(records[0][0], "system_events")
        self.assertEqual(records[0][1]["environment"], "live")
        self.assertTrue(str(records[0][1]["title"]).startswith("[LIVE] "))

    def test_startup_progress_route_upserts_state_and_records_event(self):
        state_store = {}
        records = []

        def fake_get_state(state_key, environment, date="global"):
            return state_store.get((state_key, environment, date))

        def fake_upsert_state(state_key, environment, data, date="global"):
            record = {
                "state_key": state_key,
                "environment": environment,
                "date": date,
                "data": dict(data),
            }
            state_store[(state_key, environment, date)] = record
            return record

        def fake_create_record(collection, data):
            records.append((collection, data))
            return data

        with mock.patch.object(api_app_mod.request, "get_json", return_value={
            "environment": "live",
            "action": "begin",
            "title": "IBKR Runtime 启动中",
            "summary": "等待 Gateway 与 2FA",
            "current_step": "auth",
            "current_blocker": "等待手动触发 2FA",
            "operator_action": "去飞书点击开始验证",
            "fields": {"启动原因": "manual_start"},
            "steps": {"auth": {"status": "waiting", "detail": "等待人工按钮"}},
            "record_event": True,
            "event_type": "status_change",
            "event_title": "IBKR Runtime 等待手动 2FA",
            "event_detail": {"状态结论": "等待中"},
            "level": "warning",
            "event_source": "ibkr_compute",
        }):
            with mock.patch.object(api_app_mod.pb, "get_state", side_effect=fake_get_state):
                with mock.patch.object(api_app_mod.pb, "upsert_state", side_effect=fake_upsert_state):
                    with mock.patch.object(api_app_mod.pb, "create_record", side_effect=fake_create_record):
                        with mock.patch.object(api_app_mod, "_deliver_startup_progress_card", return_value={"success": True, "message_id": "startup-msg-1"}):
                            payload = api_app_mod.custom_ibkr_startup_progress()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "live")
        self.assertEqual(payload["message_id"], "startup-msg-1")
        self.assertTrue(payload["startup_label"].startswith("LIVE-"))
        saved = state_store[(api_app_mod.IBKR_STARTUP_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(saved["message_id"], "startup-msg-1")
        self.assertEqual(saved["current_step"], "auth")
        self.assertTrue(any(collection == "system_events" for collection, _ in records))

    def test_two_factor_status_route_merges_runtime_state(self):
        state_record = {
            "date": "global",
            "data": {
                "status": "requested",
                "reason": "manual_reauth",
                "recovery_phase": "idle",
            },
        }
        runtime_result = {
            "ok": True,
            "payload": _sample_runtime_status_payload(authenticated=True),
            "error": "",
            "selected_upstream": "http://runtime/ibkr/status",
            "proxy_upstream": "http://compute/ibkr/status",
            "direct_upstream": "http://runtime/ibkr/status",
        }
        with mock.patch.object(api_app_mod.pb, "get_state", return_value=state_record):
            with mock.patch.object(api_app_mod, "_fetch_runtime_status", return_value=runtime_result):
                with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                    payload = api_app_mod.custom_ibkr_two_factor_status()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"]["status"], "success")
        self.assertTrue(payload["state"]["runtime_authenticated"])
        self.assertTrue(payload["state"]["gateway_reachable"])
        self.assertFalse(payload["state"]["runtime_environment_mismatch"])

    def test_statusz_route_returns_merged_split_stack_payload(self):
        compute_result = {"ok": True, "payload": _sample_compute_status_payload(), "error": ""}
        runtime_result = {
            "ok": True,
            "payload": _sample_runtime_status_payload(authenticated=True),
            "error": "",
            "selected_upstream": "http://runtime/ibkr/status",
            "proxy_upstream": "http://compute/ibkr/status",
            "direct_upstream": "http://runtime/ibkr/status",
        }
        with mock.patch.object(api_app_mod, "_fetch_compute_status", return_value=compute_result):
            with mock.patch.object(api_app_mod, "_fetch_runtime_status", return_value=runtime_result):
                with mock.patch.object(api_app_mod, "_load_daily_scan_state", return_value={"market_date": "2026-04-22"}):
                    with mock.patch.object(api_app_mod, "_count_active_today_targets", return_value=1):
                        with mock.patch.object(api_app_mod.request, "args", {"environment": "live", "lite": "1"}):
                            payload = api_app_mod.custom_ibkr_statusz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["requested_environment"], "live")
        self.assertEqual(payload["actual_runtime_environment"], "live")
        self.assertFalse(payload["warmup_details_included"])
        self.assertEqual(payload["compute"]["statusz_mode"], "lite")
        self.assertEqual(payload["runtime"]["market_universe"]["active_target_count"], 1)
        self.assertIn(payload["live_readiness"]["source"], {"compute_engines", "runtime_warmup_snapshot"})
        self.assertIn("ibkr-runtime", payload["service_topology"]["services"])

    def test_healthz_route_returns_merged_compute_runtime_health(self):
        compute_result = {
            "ok": True,
            "payload": {
                "ok": True,
                "status": "running",
                "service_topology": api_app_mod.build_service_topology(),
            },
            "error": "",
        }
        runtime_result = {
            "ok": True,
            "payload": {
                "ok": True,
                "status": "running",
                "environment": "live",
                "service_topology": api_app_mod.build_service_topology(),
            },
            "error": "",
            "upstream": "http://runtime/health",
        }
        with mock.patch.object(api_app_mod, "_fetch_compute_health", return_value=compute_result):
            with mock.patch.object(api_app_mod, "_fetch_runtime_health", return_value=runtime_result):
                with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                    payload = api_app_mod.custom_ibkr_healthz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["actual_runtime_environment"], "live")
        self.assertIn("ibkr-runtime", payload["service_topology"]["services"])

    def test_scheduler_dispatches_only_new_persisted_bars_and_updates_cursor(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713797100000}}}
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch("ibkr_scheduler.scheduler_app.requests.post", return_value=_FakeResponse({"ok": True, "processed": 3})):
            result = scheduler.run_job("ibkr_compute_runtime", "live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713797100000)
        job_state = pb.states[("ibkr_scheduler_job_state:ibkr_compute_runtime", "live", "global")]["data"]
        self.assertEqual(job_state["status"], "ok")
        self.assertGreater(int(job_state["last_success_at_ms"]), 0)
        self.assertTrue(any(collection == "system_events" for collection, _ in pb.records))


if __name__ == "__main__":
    unittest.main()
