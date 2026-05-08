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
from ibkr_api.system.jobs.data_gap import build_data_gap_guard_response
from ibkr_api.system.jobs.monitor_alert import build_system_monitor_alert_guard_response
from ibkr_api.system.jobs.status_heartbeat import build_system_heartbeat_response, build_system_status_reminder_response
from ibkr_scheduler.cron_registry import build_cron_payload
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

__all__ = [name for name in globals() if not name.startswith("__")]
