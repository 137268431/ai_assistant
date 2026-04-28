from __future__ import annotations

import time
import traceback
from datetime import datetime

from flask import jsonify

from ibkr_compute.api.compute.request import build_compute_disabled_payload, get_requested_environments
from ibkr_compute.api.runtime_status_client import get_remote_runtime_status, is_runtime_status_payload
from ibkr_compute.api.shared.service_status import get_service_status_snapshot
from ibkr_compute.api.service_topology import is_runtime_remote_mode
from ibkr_compute.core.time_utils import ET
from ibkr_compute.workflows.daily_scanner import DEFAULT_SCAN_TIME_ET, DailyScanner


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


def build_scan_response(payload=None):
    api_app = _api_app()
    api_app.cfg.refresh()
    payload = payload if isinstance(payload, dict) else {}
    requested_environments = get_requested_environments(payload)
    enabled_environments = [env for env in requested_environments if api_app.is_environment_compute_enabled(env)]
    if not enabled_environments:
        return jsonify(build_compute_disabled_payload(requested_environments))

    force_scan = _payload_bool(payload.get("force"), False)
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

    date_str = api_app.current_market_date()
    scanner = DailyScanner(pb_client=api_app.pb, engines=api_app.engines)
    result = scanner.run_scan(date_str, environments=enabled_environments)
    api_app.last_scan_time = time.time()

    return jsonify({
        "ok": True,
        "date": date_str,
        "requested_environments": requested_environments,
        "environments": enabled_environments,
        "force": force_scan,
        "scan_windows": scan_windows,
        **result,
    })


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
