from __future__ import annotations

import threading
import time
import traceback
import uuid
from datetime import datetime

from flask import jsonify, request

from ibkr_compute.api.compute.request import build_compute_disabled_payload, get_requested_environments
from ibkr_compute.api.runtime_status_client import get_remote_runtime_status, is_runtime_status_payload
from ibkr_compute.api.shared.service_status import get_service_status_snapshot
from ibkr_compute.api.service_topology import is_runtime_remote_mode
from ibkr_compute.core.payload_compact import compact_json_payload
from ibkr_compute.core.time_utils import ET
from ibkr_compute.core.broker_mode import resolve_market_data_mode
from ibkr_compute.workflows.daily_scanner import DEFAULT_SCAN_TIME_ET, DAILY_SCAN_MODE_SEED, DailyScanner


SCAN_ATTEMPT_STATE_KEY = "ibkr_daily_scan_attempt_state"
DAILY_SCAN_STATE_KEY = "ibkr_daily_scan_state"
DAILY_SCAN_STATE_DATE = "global"


def _api_app():
    from .. import app as api_app

    return api_app


def reset_compute_runtime_state():
    api_app = _api_app()
    for engine in api_app.engines.values():
        engine.reset()
    for signal_generator in api_app.signal_gens.values():
        signal_generator.daily_reset()
    api_app.last_processed_ms.clear()
    api_app.last_interval_fetch_ms.clear()
    api_app.daily_close_cache = {}
    api_app.daily_close_cache_date = ""
    api_app.rollup_bootstrap_checked.clear()
    api_app.engine_bootstrap_checked.clear()
    if hasattr(api_app, "signal_bootstrap_checked"):
        api_app.signal_bootstrap_checked.clear()
    api_app.persistent_cursor_envs_loaded.clear()


def _payload_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "force"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _parse_hhmm(value, default: tuple[int, int] | None = None) -> tuple[int, int] | None:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except Exception:
        return default
    return default


def _scan_start_for_environment(api_app, environment: str) -> tuple[tuple[int, int], str]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    configured_time = str(
        api_app.cfg.get_for_environment(
            "ibkr_daily_scan_time_et",
            runtime_environment,
            DEFAULT_SCAN_TIME_ET,
        )
        or DEFAULT_SCAN_TIME_ET
    ).strip()
    parsed = _parse_hhmm(configured_time)
    if parsed:
        return parsed, configured_time

    raw_schedule = str(
        api_app.cfg.get_for_environment(
            "ibkr_scan_schedule",
            runtime_environment,
            f"{DEFAULT_SCAN_TIME_ET}-10:00",
        )
        or ""
    ).strip()
    fallback_text = raw_schedule.split("-", 1)[0].strip() or DEFAULT_SCAN_TIME_ET
    fallback = _parse_hhmm(fallback_text, _parse_hhmm(DEFAULT_SCAN_TIME_ET, (9, 20)))
    return fallback or (9, 20), fallback_text


def _scan_window_state(api_app, environment: str, now_et: datetime | None = None) -> dict:
    start, start_label = _scan_start_for_environment(api_app, environment)
    current_et = now_et.astimezone(ET) if isinstance(now_et, datetime) else datetime.now(ET)
    current_hhmm = (current_et.hour, current_et.minute)
    open_now = current_hhmm >= start
    return {
        "environment": str(environment or "live").strip().lower() or "live",
        "open": open_now,
        "scan_time_et": f"{start[0]:02d}:{start[1]:02d}",
        "configured_scan_time_et": start_label,
        "current_time_et": current_et.strftime("%H:%M"),
        "current_datetime_et": current_et.isoformat(),
    }


def _scan_attempt_lock(api_app):
    lock = getattr(api_app, "_scan_attempt_state_lock", None)
    if lock is None:
        lock = threading.RLock()
        setattr(api_app, "_scan_attempt_state_lock", lock)
    return lock


def _scan_attempt_state(api_app) -> dict:
    state = getattr(api_app, "_scan_attempt_state", None)
    if not isinstance(state, dict):
        state = {"by_key": {}, "by_run_id": {}}
        setattr(api_app, "_scan_attempt_state", state)
    state.setdefault("by_key", {})
    state.setdefault("by_run_id", {})
    return state


def _normalize_scan_mode(value) -> str:
    text = str(value or "").strip().lower()
    if text in {"topup", "incremental", "early_expansion_topup"}:
        return "topup"
    return DAILY_SCAN_MODE_SEED


def _scan_attempt_key(environment: str, date_str: str, mode: str = DAILY_SCAN_MODE_SEED) -> str:
    env = str(environment or "live").strip().lower() or "live"
    date_text = str(date_str or "").strip()
    return f"{env}:{date_text}:{_normalize_scan_mode(mode)}"


def _compact_scan_attempt_result(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    return compact_json_payload(
        result,
        max_list_items=30,
        max_dict_items=120,
        max_string_length=800,
        max_depth=7,
    )


def _scan_result_counts(result: dict | None) -> dict:
    payload = result if isinstance(result, dict) else {}
    return {
        "scanned": int(payload.get("scanned", 0) or 0),
        "eligible": int(payload.get("eligible", 0) or 0),
        "active": int(payload.get("active", 0) or 0),
        "candidates": int(payload.get("candidates", 0) or 0),
        "removed": int(payload.get("removed", 0) or 0),
        "errors": int(payload.get("errors", 0) or 0),
    }


def _persist_scan_attempt_state(api_app, state: dict) -> None:
    pb = getattr(api_app, "pb", None)
    if pb is None or not hasattr(pb, "upsert_state"):
        return
    environment = str(state.get("environment") or "live").strip().lower() or "live"
    date_str = str(state.get("date") or state.get("market_date") or "global").strip() or "global"
    try:
        pb.upsert_state(
            SCAN_ATTEMPT_STATE_KEY,
            environment,
            compact_json_payload(state, max_list_items=30, max_dict_items=120, max_string_length=800, max_depth=7),
            date=date_str,
        )
    except Exception:
        # Scan status is best-effort; in-memory state still keeps this process observable.
        pass


def _persist_latest_daily_scan_state(api_app, state: dict) -> None:
    pb = getattr(api_app, "pb", None)
    if pb is None or not hasattr(pb, "upsert_state"):
        return
    environment = str(state.get("environment") or "live").strip().lower() or "live"
    payload = {
        "market_date": str(state.get("market_date") or state.get("date") or ""),
        "status": str(state.get("status") or ""),
        "reason": str(state.get("trigger_source") or state.get("reason") or "manual"),
        "started_at": state.get("started_at") or "",
        "finished_at": state.get("finished_at") or "",
        "last_error": str(state.get("last_error") or ""),
        "result": state.get("result") if isinstance(state.get("result"), dict) else {},
        "run_id": str(state.get("run_id") or ""),
        "attempt_count": 1,
        "retry_count": 0,
        "next_retry_at": "",
        "retry_cutoff_at": "",
        "retry_block_reason": "",
        "failure": state.get("failure") if isinstance(state.get("failure"), dict) else {},
        "diagnostics": state.get("diagnostics") if isinstance(state.get("diagnostics"), dict) else {},
    }
    try:
        pb.upsert_state(
            DAILY_SCAN_STATE_KEY,
            environment,
            compact_json_payload(payload, max_list_items=30, max_dict_items=120, max_string_length=800, max_depth=7),
            date=DAILY_SCAN_STATE_DATE,
        )
    except Exception:
        pass


def _set_scan_attempt_state(api_app, state: dict) -> dict:
    next_state = dict(state or {})
    environment = str(next_state.get("environment") or "live").strip().lower() or "live"
    date_str = str(next_state.get("date") or next_state.get("market_date") or api_app.current_market_date()).strip()
    run_id = str(next_state.get("run_id") or "").strip()
    next_state["environment"] = environment
    next_state["date"] = date_str
    next_state["market_date"] = date_str
    next_state["mode"] = _normalize_scan_mode(next_state.get("mode"))
    if not run_id:
        run_id = f"scan-{environment}-{date_str}-{uuid.uuid4().hex[:12]}"
        next_state["run_id"] = run_id
    key = _scan_attempt_key(environment, date_str, next_state["mode"])
    with _scan_attempt_lock(api_app):
        store = _scan_attempt_state(api_app)
        store["by_key"][key] = dict(next_state)
        store["by_run_id"][run_id] = dict(next_state)
    _persist_scan_attempt_state(api_app, next_state)
    if _scan_terminal_status(str(next_state.get("status") or "")):
        _persist_latest_daily_scan_state(api_app, next_state)
    return dict(next_state)


def _get_scan_attempt_state(api_app, environment: str, date_str: str, run_id: str = "", mode: str = DAILY_SCAN_MODE_SEED) -> dict | None:
    normalized_run_id = str(run_id or "").strip()
    normalized_env = str(environment or "live").strip().lower() or "live"
    normalized_date = str(date_str or api_app.current_market_date()).strip()
    normalized_mode = _normalize_scan_mode(mode)
    with _scan_attempt_lock(api_app):
        store = _scan_attempt_state(api_app)
        if normalized_run_id:
            candidate = store["by_run_id"].get(normalized_run_id)
            if isinstance(candidate, dict):
                return dict(candidate)
        candidate = store["by_key"].get(_scan_attempt_key(normalized_env, normalized_date, normalized_mode))
        if isinstance(candidate, dict):
            return dict(candidate)

    pb = getattr(api_app, "pb", None)
    if pb is not None and hasattr(pb, "get_state"):
        try:
            row = pb.get_state(SCAN_ATTEMPT_STATE_KEY, normalized_env, date=normalized_date)
            data = row.get("data") if isinstance(row, dict) else {}
            if isinstance(data, dict):
                if not normalized_run_id or str(data.get("run_id") or "") == normalized_run_id:
                    return dict(data)
        except Exception:
            pass
    return None


def _scan_terminal_status(status: str) -> bool:
    return str(status or "").strip().lower() in {"completed", "failed", "cancelled"}


def _execute_async_scan(api_app, initial_state: dict, enabled_environments: list[str], force_scan: bool, scan_windows: list[dict]):
    run_id = str(initial_state.get("run_id") or "")
    environment = str(initial_state.get("environment") or (enabled_environments[0] if enabled_environments else "live"))
    date_str = str(initial_state.get("date") or api_app.current_market_date())
    scan_mode = _normalize_scan_mode(initial_state.get("mode"))
    _set_scan_attempt_state(
        api_app,
        {
            **initial_state,
            "status": "running",
            "started_at": datetime.now(ET).isoformat(),
            "finished_at": "",
            "last_error": "",
            "failure": {},
            "diagnostics": {},
        },
    )
    try:
        scanner = DailyScanner(pb_client=api_app.pb, engines=api_app.engines)
        result = scanner.run_scan(date_str, environments=enabled_environments, mode=scan_mode)
        api_app.last_scan_time = time.time()
        scan_ok = bool(result.get("ok", True))
        error_text = "" if scan_ok else str(result.get("error") or "daily_scan_failed")
        final_state = {
            "ok": scan_ok,
            "async": True,
            "run_id": run_id,
            "status": "completed" if scan_ok else "failed",
            "date": date_str,
            "market_date": date_str,
            "environment": environment,
            "requested_environments": enabled_environments,
            "environments": enabled_environments,
            "force": force_scan,
            "mode": scan_mode,
            "scan_windows": scan_windows,
            "started_at": str(initial_state.get("started_at") or ""),
            "finished_at": datetime.now(ET).isoformat(),
            "last_error": error_text,
            "failure": {"code": str(result.get("error") or ""), "retryable": False} if not scan_ok else {},
            "counts": _scan_result_counts(result),
            "result": _compact_scan_attempt_result({"ok": scan_ok, **result}),
        }
        _set_scan_attempt_state(api_app, final_state)
    except Exception as exc:
        _set_scan_attempt_state(
            api_app,
            {
                **initial_state,
                "ok": False,
                "async": True,
                "status": "failed",
                "finished_at": datetime.now(ET).isoformat(),
                "last_error": str(exc),
                "failure": {
                    "code": "scan_exception",
                    "retryable": True,
                    "message": str(exc),
                },
                "result": {"ok": False, "error": str(exc)},
            },
        )


def _build_async_scan_response(api_app, *, payload: dict, enabled_environments: list[str], force_scan: bool, scan_windows: list[dict]):
    date_str = api_app.current_market_date()
    environment = str((enabled_environments[0] if enabled_environments else "live") or "live").strip().lower() or "live"
    scan_mode = _normalize_scan_mode(payload.get("mode"))
    existing = _get_scan_attempt_state(api_app, environment, date_str, mode=scan_mode)
    if existing and not _scan_terminal_status(str(existing.get("status") or "")):
        return jsonify({
            "ok": True,
            "accepted": True,
            "async": True,
            "existing": True,
            "run_id": existing.get("run_id"),
            "status": existing.get("status") or "running",
            "date": date_str,
            "environment": environment,
            "mode": scan_mode,
            "state": existing,
        })

    run_id = str(payload.get("run_id") or "").strip() or f"scan-{environment}-{date_str}-{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(ET).isoformat()
    initial_state = _set_scan_attempt_state(
        api_app,
        {
            "ok": True,
            "accepted": True,
            "async": True,
            "run_id": run_id,
            "status": "accepted",
            "date": date_str,
            "market_date": date_str,
            "environment": environment,
            "mode": scan_mode,
            "requested_environments": enabled_environments,
            "environments": enabled_environments,
            "force": force_scan,
            "scan_windows": scan_windows,
            "trigger_source": str(payload.get("trigger_source") or payload.get("source") or "").strip(),
            "started_at": now_iso,
            "finished_at": "",
            "last_error": "",
            "failure": {},
            "diagnostics": {},
            "result": {},
        },
    )
    thread = threading.Thread(
        target=_execute_async_scan,
        args=(api_app, initial_state, list(enabled_environments), force_scan, list(scan_windows)),
        name=f"daily-scan-{environment}",
        daemon=True,
    )
    thread.start()
    return jsonify({
        "ok": True,
        "accepted": True,
        "async": True,
        "run_id": run_id,
        "status": "accepted",
        "date": date_str,
        "environment": environment,
        "mode": scan_mode,
        "environments": enabled_environments,
        "scan_windows": scan_windows,
    })


def build_scan_status_response():
    api_app = _api_app()
    environment = resolve_market_data_mode(
        request.args.get("market_data_mode")
        or request.args.get("data_environment")
        or request.args.get("environment")
        or "live"
    )
    date_str = str(request.args.get("date") or request.args.get("market_date") or api_app.current_market_date()).strip()
    run_id = str(request.args.get("run_id") or "").strip()
    scan_mode = _normalize_scan_mode(request.args.get("mode"))
    state = _get_scan_attempt_state(api_app, environment, date_str, run_id=run_id, mode=scan_mode)
    if not state:
        return jsonify({
            "ok": False,
            "status": "not_found",
            "error": "scan_attempt_not_found",
            "environment": environment,
            "date": date_str,
            "run_id": run_id,
        }), 404
    return jsonify({"ok": True, **state})


def build_scan_response(payload=None):
    api_app = _api_app()
    api_app.cfg.refresh()
    payload = payload if isinstance(payload, dict) else {}
    requested_environments = get_requested_environments(payload)
    enabled_environments = [env for env in requested_environments if api_app.is_environment_compute_enabled(env)]
    if not enabled_environments:
        return jsonify(build_compute_disabled_payload(requested_environments))

    force_scan = _payload_bool(payload.get("force"), False)
    scan_mode = _normalize_scan_mode(payload.get("mode"))
    scan_windows = [_scan_window_state(api_app, env) for env in enabled_environments]
    blocked_windows = [window for window in scan_windows if not window["open"]]
    if blocked_windows and not force_scan:
        return jsonify({
            "ok": True,
            "skipped": True,
            "reason": "scan_window_not_open",
            "requested_environments": requested_environments,
            "environments": enabled_environments,
            "scan_windows": scan_windows,
            "requires_force": True,
        })

    if _payload_bool(payload.get("async"), False):
        return _build_async_scan_response(
            api_app,
            payload=payload,
            enabled_environments=enabled_environments,
            force_scan=force_scan,
            scan_windows=scan_windows,
        )

    date_str = api_app.current_market_date()
    scanner = DailyScanner(pb_client=api_app.pb, engines=api_app.engines)
    result = scanner.run_scan(date_str, environments=enabled_environments, mode=scan_mode)
    api_app.last_scan_time = time.time()
    scan_ok = bool(result.get("ok", True))
    response_payload = {
        "ok": True,
        "date": date_str,
        "market_date": date_str,
        "environment": enabled_environments[0] if enabled_environments else "live",
        "requested_environments": requested_environments,
        "environments": enabled_environments,
        "force": force_scan,
        "mode": scan_mode,
        "scan_windows": scan_windows,
        "status": "completed" if scan_ok else "failed",
        "finished_at": datetime.now(ET).isoformat(),
        "last_error": "" if scan_ok else str(result.get("error") or result.get("last_error") or "daily_scan_failed"),
        "result": result,
        **result,
    }
    _persist_latest_daily_scan_state(api_app, response_payload)

    return jsonify(response_payload)


def build_recompute_response():
    api_app = _api_app()
    engines_reset = len(api_app.engines)
    reset_compute_runtime_state()

    compute_payload = {}
    with api_app.app.test_request_context(
        "/compute",
        method="POST",
        json={
            "source": "recompute",
            "force_rollup": True,
            "rollup_intervals": ["15m", "30m", "1h", "4h"],
        },
    ):
        response = api_app.compute()
        try:
            compute_payload = response.get_json() or {}
        except Exception:
            compute_payload = {}

    return jsonify({
        "ok": True,
        "action": "recompute",
        "engines_reset": engines_reset,
        "compute": compute_payload,
    })


def _run_internal_compute(payload: dict) -> dict:
    api_app = _api_app()
    with api_app.app.test_request_context("/compute", method="POST", json=payload or {}):
        response = api_app.compute()
        try:
            return response.get_json() or {}
        except Exception:
            return {}


def _run_internal_scan(payload: dict) -> dict:
    api_app = _api_app()
    with api_app.app.test_request_context("/scan", method="POST", json=payload or {}):
        response = api_app.scan()
        try:
            return response.get_json() or {}
        except Exception:
            return {}


def _get_runtime_status_snapshot(environment: str) -> dict:
    api_app = _api_app()
    if is_runtime_remote_mode():
        remote_payload = get_remote_runtime_status()
        if is_runtime_status_payload(remote_payload):
            return remote_payload
        return {"environment": environment}

    service = api_app.get_ibkr_service()
    if not service or not hasattr(service, "status"):
        return {"environment": environment}
    try:
        return get_service_status_snapshot(service, {"environment": environment})
    except Exception:
        traceback.print_exc()
        return {"environment": environment}
