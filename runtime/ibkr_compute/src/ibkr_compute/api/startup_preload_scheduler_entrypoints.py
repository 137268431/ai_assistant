from __future__ import annotations

import threading
import time
import traceback

from .startup_preload_config_resolvers import *
from .startup_preload_direct_backfill import *
from .startup_preload_indicator_preload import *
from .startup_preload_state_store import *


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
    if not isinstance(snapshot.get("direct_backfill"), dict):
        first_environment = (snapshot.get("environments") or ["live"])[0]
        snapshot["direct_backfill"] = _new_direct_backfill_state(api_app, first_environment)
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
    direct_environment = environments[0] if environments else "live"
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
        "direct_backfill": _new_direct_backfill_state(api_app, direct_environment),
    }
    _publish_startup_preload_state(api_app, summary)

    if not environments or not preload_intervals:
        summary["status"] = "skipped"
        summary["running"] = False
        summary["finished_at"] = time.time()
        summary["reason"] = "no_preload_environments" if not environments else "no_preload_intervals"
        direct_state = summary.setdefault("direct_backfill", {})
        direct_state["status"] = "skipped"
        direct_state["running"] = False
        direct_state["reason"] = summary["reason"]
        direct_state["finished_at"] = summary["finished_at"]
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

    if _resolve_compute_startup_preload_refresh_enabled(api_app, environments[0] if environments else "live", kind="metadata"):
        try:
            api_app.refresh_symbol_metadata(force=True)
        except Exception as exc:
            LOGGER.warning("Compute startup preload symbol metadata refresh failed: %s", exc)

    if _resolve_compute_startup_preload_refresh_enabled(api_app, environments[0] if environments else "live", kind="daily_close"):
        try:
            api_app.refresh_daily_close_cache(environments, force=True)
        except Exception as exc:
            LOGGER.warning("Compute startup preload daily close refresh failed: %s", exc)

    try:
        for environment in environments:
            cursor_applied = int(api_app.load_persisted_compute_cursors(environment) or 0)
            all_interval_targets = _collect_preload_interval_targets(api_app, environment, preload_intervals)
            data_symbols = _resolve_startup_direct_core_symbols(api_app, environment)
            direct_symbols = _resolve_startup_direct_backfill_symbols(api_app, environment)
            interval_targets = _filter_preload_interval_targets(all_interval_targets, data_symbols)
            cursor_count = len(api_app.collect_environment_cursor_map(environment) or {})
            full_cursor_symbol_total = sum(len(targets or {}) for targets in all_interval_targets.values())
            env_result = {
                "status": "running",
                "preload_scope": "data_universe",
                "core_symbols": list(direct_symbols),
                "core_symbol_count": len(direct_symbols),
                "data_symbols": list(data_symbols),
                "data_symbol_count": len(data_symbols),
                "cursor_applied": cursor_applied,
                "cursor_count": cursor_count,
                "full_cursor_symbol_total": full_cursor_symbol_total,
                "full_cursor_interval_total": len(all_interval_targets),
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

            _run_startup_direct_backfill_environment(
                api_app,
                summary,
                environment,
                direct_symbols,
            )

            fallback_symbols = [
                symbol
                for symbol in data_symbols
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
                use_materialize = _resolve_compute_startup_preload_use_materialize(api_app, environment)
                if use_materialize:
                    target_items = list((targets or {}).items())
                    chunk_size = _resolve_compute_startup_preload_chunk_size(api_app, environment)
                    for index in range(0, len(target_items), chunk_size):
                        chunk_targets = {
                            symbol: target_ms
                            for symbol, target_ms in target_items[index:index + chunk_size]
                        }
                        chunk_results = _preload_interval_indicator_state(
                            api_app,
                            environment,
                            list(chunk_targets.keys()),
                            interval,
                            chunk_targets,
                        )
                        for symbol in chunk_targets:
                            preload_result = dict((chunk_results or {}).get(symbol) or {})
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
                else:
                    for symbol, target_ms in (targets or {}).items():
                        preload_result = _preload_symbol_indicator_state(
                            api_app,
                            environment,
                            symbol,
                            interval,
                            target_ms,
                            use_materialize=False,
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
                chunk_size = _resolve_compute_startup_preload_chunk_size(api_app, environment)
                for index in range(0, len(fallback_symbols), chunk_size):
                    chunk_symbols = fallback_symbols[index:index + chunk_size]
                    fallback_results = _preload_interval_indicator_state(
                        api_app,
                        environment,
                        chunk_symbols,
                        "5m",
                        {},
                    )
                    for symbol in chunk_symbols:
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
                "indicator_seeded": 0,
                "started_at": 0.0,
                "finished_at": 0.0,
                "reason": "",
                "error": "",
                "results": {},
                "direct_backfill": _new_direct_backfill_state(
                    api_app,
                    environments[0] if environments else "live",
                ),
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


__all__ = [name for name in globals() if not name.startswith("__")]
