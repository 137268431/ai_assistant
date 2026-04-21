from __future__ import annotations

import copy
import logging
import os
import threading
import time
import traceback
from datetime import datetime, timezone

from ibkr_compute.api.service_topology import get_runtime_mode, get_service_profile


LOGGER = logging.getLogger("ibkr_compute.api")
_STARTUP_PRELOAD_LOCK = threading.Lock()
_STARTUP_PRELOAD_THREAD: threading.Thread | None = None


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
        "env_total": 0,
        "env_completed": 0,
        "symbol_total": 0,
        "symbol_completed": 0,
        "ready_count": 0,
        "started_at": 0.0,
        "finished_at": 0.0,
        "reason": "",
        "error": "",
        "results": {},
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


def _env_flag(name: str, default: bool) -> bool:
    raw_value = str(os.environ.get(name, "") or "").strip().lower()
    if not raw_value:
        return bool(default)
    return raw_value not in {"0", "false", "no", "off"}


def is_compute_startup_preload_enabled() -> bool:
    return _env_flag("IBKR_COMPUTE_STARTUP_PRELOAD_ENABLED", True)


def should_schedule_compute_startup_preload() -> bool:
    if not is_compute_startup_preload_enabled():
        return False
    if get_service_profile() != "compute":
        return False
    return get_runtime_mode() == "remote"


def resolve_compute_startup_preload_environments(api_app=None) -> list[str]:
    api_app = api_app or _api_app()
    configured = str(os.environ.get("IBKR_COMPUTE_STARTUP_PRELOAD_ENVS", "") or "").strip()
    if configured:
        candidates = api_app.normalize_symbol_csv(configured)
    else:
        candidates = [str(environment or "").strip().lower() for environment in (api_app.DEFAULT_COMPUTE_ENVIRONMENTS or [])]

    environments = []
    supported = {
        str(environment or "").strip().lower()
        for environment in (api_app.SUPPORTED_COMPUTE_ENVIRONMENTS or [])
    }
    for environment in candidates:
        normalized = str(environment or "").strip().lower()
        if not normalized or normalized not in supported or normalized in environments:
            continue
        environments.append(normalized)
    return environments


def _collect_preload_interval_targets(api_app, environment: str) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    cursor_map = api_app.collect_environment_cursor_map(environment) or {}
    for raw_key, raw_value in cursor_map.items():
        symbol, interval = api_app.parse_compute_cursor_key(raw_key)
        target_ms = int(raw_value or 0)
        if not symbol or not interval or target_ms <= 0:
            continue
        grouped.setdefault(interval, {})[symbol] = target_ms

    ordered_groups = {}
    for interval in list(api_app.INTERVALS or []):
        targets = grouped.pop(interval, {})
        if targets:
            ordered_groups[interval] = {
                symbol: targets[symbol]
                for symbol in sorted(targets)
            }
    for interval in sorted(grouped):
        targets = grouped[interval]
        if targets:
            ordered_groups[interval] = {
                symbol: targets[symbol]
                for symbol in sorted(targets)
            }
    return ordered_groups


def get_compute_startup_preload_state(api_app=None) -> dict:
    api_app = api_app or _api_app()
    snapshot = _read_startup_preload_state(api_app)
    if not snapshot:
        snapshot = _new_startup_preload_state()

    snapshot["enabled"] = is_compute_startup_preload_enabled()
    snapshot["should_schedule"] = should_schedule_compute_startup_preload()
    snapshot["service_profile"] = get_service_profile()
    snapshot["runtime_mode"] = get_runtime_mode()
    if not snapshot.get("environments"):
        snapshot["environments"] = resolve_compute_startup_preload_environments(api_app)
    snapshot["env_total"] = max(
        int(snapshot.get("env_total") or 0),
        len(snapshot.get("environments") or []),
    )

    started_at = float(snapshot.get("started_at") or 0.0)
    finished_at = float(snapshot.get("finished_at") or 0.0)
    elapsed_s = 0.0
    if started_at > 0.0:
        end_time = finished_at if finished_at > 0.0 else time.time()
        elapsed_s = round(max(0.0, end_time - started_at), 3)

    status = str(snapshot.get("status") or "").strip().lower()
    if (not snapshot["enabled"] or not snapshot["should_schedule"]) and status in {"", "idle"}:
        snapshot["status"] = "disabled"
    elif not status:
        if snapshot.get("running"):
            snapshot["status"] = "running"
        elif snapshot.get("error"):
            snapshot["status"] = "failed"
        elif finished_at > 0.0:
            snapshot["status"] = "completed"
        elif snapshot.get("scheduled"):
            snapshot["status"] = "scheduled"
        else:
            snapshot["status"] = "idle"
    else:
        snapshot["status"] = status

    if (
        (not snapshot.get("reason"))
        and snapshot["status"] == "disabled"
        and (not snapshot["enabled"] or not snapshot["should_schedule"])
    ):
        snapshot["reason"] = "schedule_guard_blocked"

    snapshot["started_at"] = _to_iso8601(started_at)
    snapshot["finished_at"] = _to_iso8601(finished_at)
    snapshot["elapsed_s"] = elapsed_s
    return snapshot


def run_compute_startup_preload(api_app=None) -> dict:
    api_app = api_app or _api_app()
    environments = resolve_compute_startup_preload_environments(api_app)
    previous_state = _read_startup_preload_state(api_app)
    summary = {
        "ok": True,
        "enabled": is_compute_startup_preload_enabled(),
        "should_schedule": should_schedule_compute_startup_preload(),
        "scheduled": bool(previous_state.get("scheduled")),
        "status": "running",
        "running": True,
        "service_profile": get_service_profile(),
        "runtime_mode": get_runtime_mode(),
        "environments": environments,
        "env_total": len(environments),
        "env_completed": 0,
        "symbol_total": 0,
        "symbol_completed": 0,
        "ready_count": 0,
        "started_at": time.time(),
        "finished_at": 0.0,
        "reason": "",
        "error": "",
        "results": {},
    }
    _publish_startup_preload_state(api_app, summary)

    if not environments:
        summary["status"] = "skipped"
        summary["running"] = False
        summary["finished_at"] = time.time()
        summary["reason"] = "no_preload_environments"
        _publish_startup_preload_state(api_app, summary)
        return summary

    LOGGER.info(
        "Compute startup preload starting: envs=%s service_profile=%s runtime_mode=%s",
        ",".join(environments),
        summary["service_profile"],
        summary["runtime_mode"],
    )

    try:
        api_app.cfg.refresh()
    except Exception as exc:
        LOGGER.warning("Compute startup preload config refresh failed: %s", exc)

    try:
        api_app.refresh_symbol_metadata(force=True)
    except Exception as exc:
        LOGGER.warning("Compute startup preload symbol metadata refresh failed: %s", exc)

    try:
        api_app.refresh_daily_close_cache(environments, force=True)
    except Exception as exc:
        LOGGER.warning("Compute startup preload daily close refresh failed: %s", exc)

    try:
        for environment in environments:
            cursor_applied = int(api_app.load_persisted_compute_cursors(environment) or 0)
            interval_targets = _collect_preload_interval_targets(api_app, environment)
            cursor_count = len(api_app.collect_environment_cursor_map(environment) or {})
            env_result = {
                "status": "running",
                "cursor_applied": cursor_applied,
                "cursor_count": cursor_count,
                "interval_total": len(interval_targets),
                "interval_completed": 0,
                "symbol_total": sum(len(targets or {}) for targets in interval_targets.values()),
                "symbol_completed": 0,
                "ready_count": 0,
                "intervals": {
                    interval: {
                        "status": "pending",
                        "symbol_count": len(targets or {}),
                        "symbol_completed": 0,
                        "ready_count": 0,
                    }
                    for interval, targets in interval_targets.items()
                },
            }
            summary["symbol_total"] += env_result["symbol_total"]
            summary["results"][environment] = env_result
            LOGGER.info(
                "Compute startup preload environment: env=%s cursor_applied=%d cursor_count=%d intervals=%d",
                environment,
                cursor_applied,
                cursor_count,
                len(interval_targets),
            )
            _publish_startup_preload_state(api_app, summary)

            for interval, targets in interval_targets.items():
                interval_state = env_result["intervals"].setdefault(
                    interval,
                    {
                        "status": "pending",
                        "symbol_count": len(targets or {}),
                        "symbol_completed": 0,
                        "ready_count": 0,
                    },
                )
                interval_state["status"] = "running" if targets else "completed"
                _publish_startup_preload_state(api_app, summary)
                for symbol, target_ms in targets.items():
                    api_app.bootstrap_engine_state(
                        environment,
                        symbol,
                        interval,
                        target_ms,
                        inclusive=True,
                    )
                    engine = api_app.engines.get((environment, symbol, interval))
                    if engine and engine.is_ready():
                        interval_state["ready_count"] += 1
                        env_result["ready_count"] += 1
                        summary["ready_count"] += 1
                    interval_state["symbol_completed"] += 1
                    env_result["symbol_completed"] += 1
                    summary["symbol_completed"] += 1
                    _publish_startup_preload_state(api_app, summary)
                interval_state["status"] = "completed"
                env_result["interval_completed"] += 1
                _publish_startup_preload_state(api_app, summary)

            env_result["status"] = "completed"
            summary["env_completed"] += 1
            _publish_startup_preload_state(api_app, summary)
    except Exception as exc:
        traceback.print_exc()
        summary["ok"] = False
        summary["status"] = "failed"
        summary["running"] = False
        summary["error"] = str(exc)
        summary["finished_at"] = time.time()
        _publish_startup_preload_state(api_app, summary)
        return summary

    summary["status"] = "completed"
    summary["running"] = False
    summary["finished_at"] = time.time()
    _publish_startup_preload_state(api_app, summary)
    return summary


def _run_compute_startup_preload_thread(api_app=None):
    result = run_compute_startup_preload(api_app)
    elapsed_s = round(max(0.0, float(result.get("finished_at", 0.0) or 0.0) - float(result.get("started_at", 0.0) or 0.0)), 3)
    if result.get("ok", False):
        LOGGER.info(
            "Compute startup preload finished: envs=%s elapsed_s=%s",
            ",".join(result.get("environments") or []),
            elapsed_s,
        )
        return
    LOGGER.warning(
        "Compute startup preload failed: envs=%s elapsed_s=%s error=%s",
        ",".join(result.get("environments") or []),
        elapsed_s,
        result.get("error", ""),
    )


def schedule_compute_startup_preload() -> bool:
    global _STARTUP_PRELOAD_THREAD

    if not should_schedule_compute_startup_preload():
        return False

    with _STARTUP_PRELOAD_LOCK:
        thread = _STARTUP_PRELOAD_THREAD
        if thread is not None and thread.is_alive():
            return False
        api_app = _api_app()
        environments = resolve_compute_startup_preload_environments(api_app)
        _publish_startup_preload_state(
            api_app,
            {
                "ok": True,
                "enabled": is_compute_startup_preload_enabled(),
                "should_schedule": True,
                "scheduled": True,
                "status": "scheduled",
                "running": False,
                "service_profile": get_service_profile(),
                "runtime_mode": get_runtime_mode(),
                "environments": environments,
                "env_total": len(environments),
                "env_completed": 0,
                "symbol_total": 0,
                "symbol_completed": 0,
                "ready_count": 0,
                "started_at": 0.0,
                "finished_at": 0.0,
                "reason": "",
                "error": "",
                "results": {},
            },
        )
        _STARTUP_PRELOAD_THREAD = threading.Thread(
            target=_run_compute_startup_preload_thread,
            args=(api_app,),
            daemon=True,
            name="compute-startup-preload",
        )
        _STARTUP_PRELOAD_THREAD.start()
        return True


__all__ = [
    "get_compute_startup_preload_state",
    "is_compute_startup_preload_enabled",
    "resolve_compute_startup_preload_environments",
    "run_compute_startup_preload",
    "schedule_compute_startup_preload",
    "should_schedule_compute_startup_preload",
]
