from __future__ import annotations

import copy
import logging
import threading
from datetime import datetime, timezone


LOGGER = logging.getLogger("ibkr_compute.api")
_STARTUP_PRELOAD_LOCK = threading.Lock()
_STARTUP_PRELOAD_THREAD: threading.Thread | None = None
DIRECT_BACKFILL_DEFAULT_INTERVALS = ("5m", "15m", "30m", "1h", "4h", "1d")
DIRECT_BACKFILL_DEFAULT_PERIODS = {
    "5m": "4d",
    "15m": "10d",
    "30m": "20d",
    "1h": "40d",
    "4h": "120d",
    "1d": "2y",
}
DIRECT_BACKFILL_DEFAULT_REQUIRED_BARS = 260
DIRECT_BACKFILL_DEFAULT_CONID_SCAN_INTERVALS = ("5m", "15m", "30m", "1h", "4h", "1d")
DIRECT_BACKFILL_DEFAULT_CONID_SCAN_PAGES = 3


def _api_app():
    from . import app as api_app

    return api_app


def _new_startup_preload_state() -> dict:
    return {
        "enabled": False,
        "should_schedule": False,
        "scheduled": False,
        "status": "idle",
        "running": False,
        "service_profile": "",
        "runtime_mode": "",
        "environments": [],
        "intervals": [],
        "env_total": 0,
        "env_completed": 0,
        "symbol_total": 0,
        "symbol_completed": 0,
        "ready_count": 0,
        "indicator_seeded": 0,
        "started_at": 0.0,
        "finished_at": 0.0,
        "reason": "",
        "error": "",
        "results": {},
        "direct_backfill": {
            "enabled": False,
            "status": "idle",
            "running": False,
            "intervals": [],
            "required_bars": DIRECT_BACKFILL_DEFAULT_REQUIRED_BARS,
            "symbol_total": 0,
            "symbol_completed": 0,
            "planned_total": 0,
            "written": 0,
            "request_count": 0,
            "ready_count": 0,
            "started_at": 0.0,
            "finished_at": 0.0,
            "reason": "",
            "error": "",
            "results": {},
        },
    }


def _clone_state(payload: dict | None) -> dict:
    return copy.deepcopy(payload) if isinstance(payload, dict) else {}


def _ensure_startup_preload_state(api_app) -> tuple[threading.Lock, dict]:
    lock = getattr(api_app, "_compute_startup_preload_state_lock", None)
    if lock is None:
        lock = threading.Lock()
        setattr(api_app, "_compute_startup_preload_state_lock", lock)

    state = getattr(api_app, "_compute_startup_preload_state", None)
    if not isinstance(state, dict):
        state = _new_startup_preload_state()
        setattr(api_app, "_compute_startup_preload_state", state)
    return lock, state


def _publish_startup_preload_state(api_app, payload: dict) -> dict:
    lock, state = _ensure_startup_preload_state(api_app)
    snapshot = _clone_state(payload)
    with lock:
        state.clear()
        state.update(snapshot)
    return snapshot


def _read_startup_preload_state(api_app) -> dict:
    lock, state = _ensure_startup_preload_state(api_app)
    with lock:
        return _clone_state(state)


def _to_iso8601(timestamp_value) -> str | None:
    numeric = float(timestamp_value or 0.0)
    if numeric <= 0.0:
        return None
    return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()


__all__ = [name for name in globals() if not name.startswith("__")]
