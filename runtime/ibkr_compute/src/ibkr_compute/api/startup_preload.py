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
    supported_order = [
        str(environment or "").strip().lower()
        for environment in (api_app.SUPPORTED_COMPUTE_ENVIRONMENTS or [])
        if str(environment or "").strip()
    ]
    supported = set(supported_order)
    configured = str(os.environ.get("IBKR_COMPUTE_STARTUP_PRELOAD_ENVS", "") or "").strip()
    if configured:
        candidates = api_app.normalize_symbol_csv(configured)
        if any(str(environment or "").strip().lower() in {"*", "all"} for environment in candidates):
            candidates = supported_order
    elif "live" in supported:
        candidates = ["live"]
    else:
        candidates = [str(environment or "").strip().lower() for environment in (api_app.DEFAULT_COMPUTE_ENVIRONMENTS or [])]

    environments = []
    for environment in candidates:
        normalized = str(environment or "").strip().lower()
        if not normalized or normalized not in supported or normalized in environments:
            continue
        environments.append(normalized)
    return environments


def _parse_preload_interval_csv(raw_value: str) -> list[str]:
    return [
        str(item or "").strip().lower()
        for item in str(raw_value or "").replace(";", ",").split(",")
        if str(item or "").strip()
    ]


def resolve_compute_startup_preload_intervals(api_app=None) -> list[str]:
    api_app = api_app or _api_app()
    supported_source = getattr(api_app, "INTERVALS", None) or ["5m"]
    supported = [str(interval or "").strip().lower() for interval in supported_source if str(interval or "").strip()]
    configured = str(os.environ.get("IBKR_COMPUTE_STARTUP_PRELOAD_INTERVALS", "") or "").strip()
    candidates = _parse_preload_interval_csv(configured) if configured else ["5m"]
    if any(item in {"*", "all"} for item in candidates):
        candidates = supported

    intervals = []
    supported_set = set(supported)
    for interval in candidates:
        normalized = str(interval or "").strip().lower()
        if not normalized or normalized not in supported_set or normalized in intervals:
            continue
        intervals.append(normalized)
    return intervals


def _collect_preload_interval_targets(api_app, environment: str, intervals: list[str] | tuple[str, ...] | set[str] | None = None) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    interval_filter = {str(interval or "").strip().lower() for interval in (intervals or []) if str(interval or "").strip()}
    cursor_map = api_app.collect_environment_cursor_map(environment) or {}
    for raw_key, raw_value in cursor_map.items():
        symbol, interval = api_app.parse_compute_cursor_key(raw_key)
        interval = str(interval or "").strip().lower()
        target_ms = int(raw_value or 0)
        if not symbol or not interval or target_ms <= 0:
            continue
        if interval_filter and interval not in interval_filter:
            continue
        grouped.setdefault(interval, {})[symbol] = target_ms

    ordered_groups = {}
    for interval in list(getattr(api_app, "INTERVALS", None) or []):
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


def _resolve_preload_watchlist_symbols(api_app, environment: str) -> list[str]:
    try:
        runtime_environment = str(environment or "live").strip().lower() or "live"
        rows = api_app.pb.get_all_records(
            "watchlist",
            filter=(
                f'environment = "{runtime_environment}" '
                '|| environment = "global" '
                '|| environment = ""'
            ),
            sort="-updated",
            max_pages=30,
        )
        merged = {}
        applied = {}
        priority = {"": 0, "global": 1, runtime_environment: 2}
        for row in rows:
            symbol = str((row or {}).get("symbol", "")).strip().upper()
            if not symbol:
                continue
            row_environment = str((row or {}).get("environment", "") or "").strip().lower()
            rank = priority.get(row_environment, -1)
            if rank < 0:
                continue
            if symbol in applied and applied[symbol] > rank:
                continue
            applied[symbol] = rank
            merged[symbol] = True
        if merged:
            return sorted(merged.keys())
    except Exception:
        LOGGER.exception("Compute startup preload watchlist resolve failed: env=%s", environment)

    metadata = getattr(api_app, "symbol_metadata_cache", {}) or {}
    return sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in metadata.keys()
            if str(symbol or "").strip()
        }
    )


def _preload_symbol_indicator_state(
    api_app,
    environment: str,
    symbol: str,
    interval: str,
    target_ms: int,
) -> dict:
    normalized_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = str(interval or "").strip().lower()

    if hasattr(api_app, "materialize_engines_from_storage"):
        try:
            results = api_app.materialize_engines_from_storage(
                normalized_environment,
                [normalized_symbol],
                normalized_interval,
                hydrate_signal_state=True,
                persist_latest_indicator=False,
            )
            result = dict((results or {}).get(normalized_symbol) or {})
            if result:
                result.setdefault("indicator_seeded", False)
                return result
        except Exception:
            LOGGER.exception(
                "Compute startup preload materialize failed: env=%s interval=%s symbol=%s",
                normalized_environment,
                normalized_interval,
                normalized_symbol,
            )

    processed = api_app.bootstrap_engine_state(
        normalized_environment,
        normalized_symbol,
        normalized_interval,
        int(target_ms or 0),
        inclusive=True,
    )
    engine = api_app.engines.get((normalized_environment, normalized_symbol, normalized_interval))
    return {
        "processed": processed,
        "bar_count": int(getattr(engine, "bar_count", 0) or 0) if engine else 0,
        "last_bar_time_ms": int(getattr(engine, "last_bar_time_ms", 0) or 0) if engine else 0,
        "is_ready": bool(engine and engine.is_ready()),
        "indicator_seeded": False,
        "reason": "bootstrapped" if processed > 0 else "already_materialized",
    }


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
    if not snapshot.get("intervals"):
        snapshot["intervals"] = resolve_compute_startup_preload_intervals(api_app)
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
    preload_intervals = resolve_compute_startup_preload_intervals(api_app)
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
        "intervals": preload_intervals,
        "env_total": len(environments),
        "env_completed": 0,
        "symbol_total": 0,
        "symbol_completed": 0,
        "ready_count": 0,
        "indicator_seeded": 0,
        "started_at": time.time(),
        "finished_at": 0.0,
        "reason": "",
        "error": "",
        "results": {},
    }
    _publish_startup_preload_state(api_app, summary)

    if not environments or not preload_intervals:
        summary["status"] = "skipped"
        summary["running"] = False
        summary["finished_at"] = time.time()
        summary["reason"] = "no_preload_environments" if not environments else "no_preload_intervals"
        _publish_startup_preload_state(api_app, summary)
        return summary

    LOGGER.info(
        "Compute startup preload starting: envs=%s intervals=%s service_profile=%s runtime_mode=%s",
        ",".join(environments),
        ",".join(preload_intervals),
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
            interval_targets = _collect_preload_interval_targets(api_app, environment, preload_intervals)
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
                "indicator_seeded": 0,
                "intervals": {
                    interval: {
                        "status": "pending",
                        "symbol_count": len(targets or {}),
                        "symbol_completed": 0,
                        "ready_count": 0,
                        "indicator_seeded": 0,
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

            fallback_symbols = [
                symbol
                for symbol in _resolve_preload_watchlist_symbols(api_app, environment)
                if symbol not in set((interval_targets.get("5m") or {}).keys())
            ] if "5m" in preload_intervals else []
            if fallback_symbols:
                # Fresh environments can have bars but no cursor state yet; warm 5m engines from storage first.
                interval_state = env_result["intervals"].setdefault(
                    "5m",
                    {
                        "status": "pending",
                        "symbol_count": 0,
                        "symbol_completed": 0,
                        "ready_count": 0,
                        "indicator_seeded": 0,
                    },
                )
                interval_state["symbol_count"] = int(interval_state.get("symbol_count", 0) or 0) + len(fallback_symbols)
                env_result["symbol_total"] += len(fallback_symbols)
                summary["symbol_total"] += len(fallback_symbols)
                env_result["storage_fallback_symbols"] = len(fallback_symbols)

            _publish_startup_preload_state(api_app, summary)

            for interval, targets in interval_targets.items():
                interval_state = env_result["intervals"].setdefault(
                    interval,
                    {
                        "status": "pending",
                        "symbol_count": len(targets or {}),
                        "symbol_completed": 0,
                        "ready_count": 0,
                        "indicator_seeded": 0,
                    },
                )
                interval_state["status"] = "running" if targets else "completed"
                _publish_startup_preload_state(api_app, summary)
                for symbol, target_ms in targets.items():
                    preload_result = _preload_symbol_indicator_state(
                        api_app,
                        environment,
                        symbol,
                        interval,
                        target_ms,
                    )
                    if bool(preload_result.get("is_ready")):
                        interval_state["ready_count"] += 1
                        env_result["ready_count"] += 1
                        summary["ready_count"] += 1
                    if bool(preload_result.get("indicator_seeded")):
                        interval_state["indicator_seeded"] += 1
                        env_result["indicator_seeded"] += 1
                        summary["indicator_seeded"] += 1
                    interval_state["symbol_completed"] += 1
                    env_result["symbol_completed"] += 1
                    summary["symbol_completed"] += 1
                    _publish_startup_preload_state(api_app, summary)
                interval_state["status"] = "completed"
                env_result["interval_completed"] += 1
                _publish_startup_preload_state(api_app, summary)

            if fallback_symbols:
                interval_state = env_result["intervals"].setdefault(
                    "5m",
                    {
                        "status": "pending",
                        "symbol_count": len(fallback_symbols),
                        "symbol_completed": 0,
                        "ready_count": 0,
                        "indicator_seeded": 0,
                    },
                )
                interval_state["status"] = "running"
                _publish_startup_preload_state(api_app, summary)
                indicator_seeded = 0
                for symbol in fallback_symbols:
                    fallback_results = api_app.materialize_engines_from_storage(
                        environment,
                        [symbol],
                        "5m",
                        hydrate_signal_state=True,
                        persist_latest_indicator=False,
                    )
                    result = dict((fallback_results or {}).get(symbol) or {})
                    if bool(result.get("is_ready")):
                        interval_state["ready_count"] += 1
                        env_result["ready_count"] += 1
                        summary["ready_count"] += 1
                    if bool(result.get("indicator_seeded")):
                        indicator_seeded += 1
                        interval_state["indicator_seeded"] += 1
                        env_result["indicator_seeded"] += 1
                        summary["indicator_seeded"] += 1
                    interval_state["symbol_completed"] += 1
                    env_result["symbol_completed"] += 1
                    summary["symbol_completed"] += 1
                    _publish_startup_preload_state(api_app, summary)
                env_result["storage_fallback_indicator_seeded"] = indicator_seeded
                if not interval_targets.get("5m"):
                    env_result["interval_completed"] += 1
                interval_state["status"] = "completed"
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
                "intervals": resolve_compute_startup_preload_intervals(api_app),
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
    "resolve_compute_startup_preload_intervals",
    "run_compute_startup_preload",
    "schedule_compute_startup_preload",
    "should_schedule_compute_startup_preload",
]
