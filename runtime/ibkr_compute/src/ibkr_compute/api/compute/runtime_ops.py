from __future__ import annotations

import time
import traceback

from flask import jsonify

from ibkr_compute.api.compute.request import build_compute_disabled_payload, get_requested_environments
from ibkr_compute.api.runtime_status_client import get_remote_runtime_status, is_runtime_status_payload
from ibkr_compute.api.shared.service_status import get_service_status_snapshot
from ibkr_compute.api.service_topology import is_runtime_remote_mode
from ibkr_compute.workflows.daily_scanner import DailyScanner


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


def build_scan_response(payload=None):
    api_app = _api_app()
    api_app.cfg.refresh()
    payload = payload if isinstance(payload, dict) else None
    requested_environments = get_requested_environments(payload)
    enabled_environments = [env for env in requested_environments if api_app.is_environment_compute_enabled(env)]
    if not enabled_environments:
        return jsonify(build_compute_disabled_payload(requested_environments))

    date_str = api_app.current_market_date()
    scanner = DailyScanner(pb_client=api_app.pb, engines=api_app.engines)
    result = scanner.run_scan(date_str, environments=enabled_environments)
    api_app.last_scan_time = time.time()

    return jsonify({
        "ok": True,
        "date": date_str,
        "requested_environments": requested_environments,
        "environments": enabled_environments,
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
