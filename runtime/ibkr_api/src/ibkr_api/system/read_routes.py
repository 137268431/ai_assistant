from __future__ import annotations

import os
import threading
import time
from typing import Any

from flask import Response, jsonify, request

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.system.jobs import build_daily_event_ledger_response
from ibkr_api.system.jobs.market_calendar import build_market_calendar_response
from ibkr_api.system.scheduler_support import run_scheduler_job


SystemDeps = dict[str, Any]

_CONTROL_PLANE_CACHE_LOCK = threading.RLock()
_CONTROL_PLANE_CACHE: dict[tuple[str, str, bool], dict[str, Any]] = {}
_CONTROL_PLANE_IN_FLIGHT: dict[tuple[str, str, bool], threading.Event] = {}


def _cache_seconds(env_name: str, fallback: float) -> float:
    try:
        return max(0.0, float(os.environ.get(env_name, fallback)))
    except (TypeError, ValueError):
        return fallback


def _control_plane_ttl(endpoint: str, *, lite_mode: bool) -> float:
    if endpoint == "summaryz" and lite_mode:
        return _cache_seconds("IBKR_CONTROL_PLANE_SUMMARY_LITE_TTL_SEC", 15.0)
    if endpoint == "monitorz" and lite_mode:
        return _cache_seconds("IBKR_CONTROL_PLANE_MONITOR_LITE_TTL_SEC", 15.0)
    if endpoint == "monitorz":
        return _cache_seconds("IBKR_CONTROL_PLANE_MONITOR_FULL_TTL_SEC", 45.0)
    return _cache_seconds("IBKR_CONTROL_PLANE_DEFAULT_TTL_SEC", 15.0)


def _control_plane_stale_seconds(endpoint: str, *, lite_mode: bool) -> float:
    if endpoint == "monitorz" and not lite_mode:
        return _cache_seconds("IBKR_CONTROL_PLANE_MONITOR_FULL_STALE_SEC", 120.0)
    return _cache_seconds("IBKR_CONTROL_PLANE_STALE_SEC", 60.0)


def _with_cache_meta(
    payload: Any,
    *,
    entry: dict[str, Any],
    state: str,
    stale: bool = False,
    error: Any = None,
) -> Any:
    if not isinstance(payload, dict):
        return payload
    now = time.monotonic()
    created_at = float(entry.get("created_at") or now)
    ttl_seconds = float(entry.get("ttl_seconds") or 0.0)
    meta = {
        "state": state,
        "age_s": round(max(0.0, now - created_at), 3),
        "ttl_s": round(ttl_seconds, 3),
        "stale": bool(stale),
    }
    if error is not None:
        meta["error"] = str(error)
    return {
        **payload,
        "_cache": meta,
    }


def _cache_entry(payload: dict[str, Any], ttl_seconds: float, stale_seconds: float) -> dict[str, Any]:
    now = time.monotonic()
    return {
        "payload": payload,
        "created_at": now,
        "expires_at": now + ttl_seconds,
        "stale_until": now + ttl_seconds + stale_seconds,
        "ttl_seconds": ttl_seconds,
    }


def _cached_control_plane_payload(
    key: tuple[str, str, bool],
    *,
    builder: Any,
    ttl_seconds: float,
    stale_seconds: float,
) -> dict[str, Any]:
    if str(os.environ.get("IBKR_CONTROL_PLANE_CACHE_ENABLED", "true")).strip().lower() in {"0", "false", "no", "off"}:
        return builder()

    stale_entry: dict[str, Any] | None = None
    created_event: threading.Event | None = None
    while True:
        stale_entry = None
        now = time.monotonic()
        with _CONTROL_PLANE_CACHE_LOCK:
            entry = _CONTROL_PLANE_CACHE.get(key)
            if entry and now <= float(entry.get("expires_at") or 0.0):
                return _with_cache_meta(entry.get("payload"), entry=entry, state="hit")
            if entry and now <= float(entry.get("stale_until") or 0.0):
                stale_entry = entry
            event = _CONTROL_PLANE_IN_FLIGHT.get(key)
            if event is None:
                event = threading.Event()
                _CONTROL_PLANE_IN_FLIGHT[key] = event
                created_event = event
                break

        if stale_entry is not None and not event.wait(timeout=max(0.5, min(5.0, ttl_seconds))):
            return _with_cache_meta(
                stale_entry.get("payload"),
                entry=stale_entry,
                state="stale_wait_timeout",
                stale=True,
            )
        event.wait(timeout=max(0.5, ttl_seconds + stale_seconds))

    try:
        payload = builder()
        entry = _cache_entry(payload, ttl_seconds, stale_seconds)
        with _CONTROL_PLANE_CACHE_LOCK:
            _CONTROL_PLANE_CACHE[key] = entry
        return _with_cache_meta(payload, entry=entry, state="miss")
    except Exception as exc:
        if stale_entry is not None:
            return _with_cache_meta(
                stale_entry.get("payload"),
                entry=stale_entry,
                state="stale_error",
                stale=True,
                error=exc,
            )
        raise
    finally:
        with _CONTROL_PLANE_CACHE_LOCK:
            event = _CONTROL_PLANE_IN_FLIGHT.get(key)
            if event is created_event:
                _CONTROL_PLANE_IN_FLIGHT.pop(key, None)
                event.set()


def _clear_control_plane_cache() -> None:
    with _CONTROL_PLANE_CACHE_LOCK:
        _CONTROL_PLANE_CACHE.clear()
        _CONTROL_PLANE_IN_FLIGHT.clear()



def register_system_read_routes(app, *, deps: SystemDeps, exports: dict[str, Any]) -> dict[str, Any]:
    config = deps["config"]
    normalize_environment = deps["normalize_environment"]
    parse_boolean = deps["parse_boolean"]
    build_cron_payload = deps["build_cron_payload"]
    scheduler_status = deps["scheduler_status"]
    build_scheduler_summary = deps["build_scheduler_summary"]
    augment_scheduler_summary = deps["augment_scheduler_summary"]
    build_system_monitor_payload = deps["build_system_monitor_payload"]
    build_system_summary_payload = deps["build_system_summary_payload"]
    collect_storage_health = deps.get("collect_storage_health")
    build_service_topology = deps["build_service_topology"]
    request_json_request = deps["request_json_request"]
    compute_base_url = deps["compute_base_url"]
    scheduler_base_url = deps["scheduler_base_url"]
    normalize_environment = deps["normalize_environment"]
    time_strings = deps["time_strings"]
    get_state_payload = deps["get_state_payload"]
    config_value = deps["config_value"]

    def _cached_summary_payload(environment: str, *, lite_mode: bool) -> dict[str, Any]:
        ttl_seconds = _control_plane_ttl("summaryz", lite_mode=lite_mode)
        return _cached_control_plane_payload(
            ("summaryz", environment, lite_mode),
            builder=lambda: build_system_summary_payload(environment, lite_mode=lite_mode),
            ttl_seconds=ttl_seconds,
            stale_seconds=_control_plane_stale_seconds("summaryz", lite_mode=lite_mode),
        )

    def _cached_monitor_payload(environment: str) -> dict[str, Any]:
        ttl_seconds = _control_plane_ttl("monitorz", lite_mode=False)
        return _cached_control_plane_payload(
            ("monitorz", environment, False),
            builder=lambda: build_system_monitor_payload(environment),
            ttl_seconds=ttl_seconds,
            stale_seconds=_control_plane_stale_seconds("monitorz", lite_mode=False),
        )

    def _request_modes_from_args() -> tuple[str, str]:
        payload = {
            "broker_mode": request.args.get("broker_mode"),
            "market_data_mode": request.args.get("market_data_mode") or request.args.get("environment"),
        }
        return request_broker_mode(payload), request_market_data_mode(payload)

    def _scheduler_status_lite(environment: str, *, broker_mode: str, market_data_mode: str) -> dict[str, Any]:
        try:
            return scheduler_status(
                environment,
                broker_mode=broker_mode,
                market_data_mode=market_data_mode,
                lite=True,
            )
        except TypeError as exc:
            if "lite" not in str(exc):
                raise
            return scheduler_status(
                environment,
                broker_mode=broker_mode,
                market_data_mode=market_data_mode,
            )

    @app.route("/api/custom/system/cronz", methods=["GET"])
    def custom_system_cronz() -> Response:
        broker_mode, market_data_mode = _request_modes_from_args()
        if parse_boolean(request.args.get("lite"), False):
            return jsonify(
                {
                    "ok": True,
                    "items": [],
                    "scheduler": {
                        "ok": True,
                        "status": "running",
                        "environment": market_data_mode,
                        "broker_mode": broker_mode,
                        "market_data_mode": market_data_mode,
                        "lite": True,
                    },
                    "environment": market_data_mode,
                    "broker_mode": broker_mode,
                    "market_data_mode": market_data_mode,
                    "source": "ibkr-api",
                    "service_topology": build_service_topology(),
                    "lite": True,
                }
            )
        config.refresh()
        scheduler_payload = _scheduler_status_lite(
            market_data_mode,
            broker_mode=broker_mode,
            market_data_mode=market_data_mode,
        )
        scheduler_jobs = scheduler_payload.get("jobs") if isinstance(scheduler_payload.get("jobs"), dict) else {}
        items = scheduler_payload.get("items") if isinstance(scheduler_payload.get("items"), list) else build_cron_payload(config, market_data_mode, scheduler_jobs)
        return jsonify(
            {
                "ok": True,
                "items": items,
                "scheduler": augment_scheduler_summary(build_scheduler_summary(market_data_mode, scheduler_payload), items),
                "environment": market_data_mode,
                "broker_mode": broker_mode,
                "market_data_mode": market_data_mode,
                "source": "ibkr-api",
                "service_topology": build_service_topology(),
            }
        )

    exports["custom_system_cronz"] = custom_system_cronz

    @app.route("/api/custom/system/healthz", methods=["GET"])
    def custom_system_healthz() -> Response:
        environment, market_data_mode = _request_modes_from_args()
        payload = build_system_monitor_payload(environment)
        return jsonify(
            {
                "ok": bool(payload.get("ok", False)),
                "status": str(payload.get("status") or "offline"),
                "environment": environment,
                "broker_mode": environment,
                "data_environment": market_data_mode,
                "market_data_environment": market_data_mode,
                "requested_environment": payload.get("requested_environment") or environment,
                "actual_runtime_environment": payload.get("actual_runtime_environment") or environment,
                "runtime_environment_mismatch": bool(payload.get("runtime_environment_mismatch")),
                "service_topology": payload.get("service_topology") if isinstance(payload.get("service_topology"), dict) else build_service_topology(),
                "service_monitor": payload.get("service_monitor") if isinstance(payload.get("service_monitor"), dict) else {},
                "scheduler": payload.get("scheduler") if isinstance(payload.get("scheduler"), dict) else {},
                "source": "ibkr-api",
            }
        )

    exports["custom_system_healthz"] = custom_system_healthz

    @app.route("/api/custom/system/schedulerz", methods=["GET"])
    def custom_system_schedulerz() -> Response:
        broker_mode, market_data_mode = _request_modes_from_args()
        config.refresh()
        scheduler_payload = _scheduler_status_lite(
            market_data_mode,
            broker_mode=broker_mode,
            market_data_mode=market_data_mode,
        )
        scheduler_jobs = scheduler_payload.get("jobs") if isinstance(scheduler_payload.get("jobs"), dict) else {}
        items = scheduler_payload.get("items") if isinstance(scheduler_payload.get("items"), list) else build_cron_payload(config, market_data_mode, scheduler_jobs)
        summary = augment_scheduler_summary(build_scheduler_summary(market_data_mode, scheduler_payload), items)
        return jsonify(
            {
                "ok": bool(scheduler_payload.get("ok", False)),
                "status": str(summary.get("status") or "offline"),
                "environment": market_data_mode,
                "broker_mode": broker_mode,
                "market_data_mode": market_data_mode,
                "scheduler": summary,
                "items": items,
                "source": "ibkr-api",
                "service_topology": build_service_topology(),
            }
        )

    exports["custom_system_schedulerz"] = custom_system_schedulerz

    @app.route("/api/custom/system/daily_event_ledger", methods=["GET"])
    def custom_system_daily_event_ledger() -> Response:
        args = dict(request.args or {})
        market_date = str(args.get("market_date") or args.get("date") or time_strings()["date"]).strip()
        payload, status_code = build_daily_event_ledger_response(
            payload=args,
            normalize_environment=normalize_environment,
            time_strings=time_strings,
            get_state_payload=lambda state_key, environment, date=None: get_state_payload(
                state_key,
                environment,
                date=date or market_date,
            ),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_daily_event_ledger"] = custom_system_daily_event_ledger

    @app.route("/api/custom/system/market_calendar", methods=["GET"])
    def custom_system_market_calendar() -> Response:
        payload, status_code = build_market_calendar_response(
            payload=dict(request.args or {}),
            time_strings=time_strings,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
            config_value=config_value,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_market_calendar"] = custom_system_market_calendar

    @app.route("/api/custom/system/scheduler/jobs/run", methods=["POST"])
    def custom_system_scheduler_job_run() -> Response:
        payload = request.get_json(silent=True) or {}
        if "environment" in payload:
            return jsonify(
                {
                    "ok": False,
                    "error": "environment_not_supported",
                    "message": "Use broker_mode/market_data_mode or omit mode so scheduler selects by job mode_scope.",
                    "source": "ibkr-api",
                }
            ), 400
        broker_mode = request_broker_mode(payload)
        market_data_mode = request_market_data_mode(payload)
        response_payload, status_code = run_scheduler_job(
            job_id=str(payload.get("job_id") or "").strip(),
            broker_mode=broker_mode,
            market_data_mode=market_data_mode,
            trigger_source=str(payload.get("trigger_source") or "api_manual").strip() or "api_manual",
            request_json_request=request_json_request,
            scheduler_base_url=scheduler_base_url,
        )
        response = jsonify(response_payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_scheduler_job_run"] = custom_system_scheduler_job_run

    @app.route("/api/custom/system/summaryz", methods=["GET"])
    def custom_system_summaryz() -> Response:
        environment, _market_data_mode = _request_modes_from_args()
        lite_mode = parse_boolean(request.args.get("lite"), False)
        return jsonify(_cached_summary_payload(environment, lite_mode=lite_mode))

    exports["custom_system_summaryz"] = custom_system_summaryz

    @app.route("/api/custom/system/storagez", methods=["GET"])
    def custom_system_storagez() -> Response:
        _broker_mode, environment = _request_modes_from_args()
        if collect_storage_health is None:
            return jsonify(
                {
                    "ok": False,
                    "status": "unavailable",
                    "environment": environment,
                    "source": "ibkr-api",
                    "error": "storage_health_unavailable",
                    "service_topology": build_service_topology(),
                }
            )
        config_map = {}
        try:
            config.refresh()
            if hasattr(config, "get_for_environment"):
                config_map["ibkr_history_retention_days"] = config.get_for_environment(
                    "ibkr_history_retention_days",
                    environment,
                    "365",
                )
        except Exception:
            config_map = {}
        try:
            payload = collect_storage_health(environment, config_map)
        except Exception as exc:
            payload = {
                "ok": False,
                "status": "unavailable",
                "environment": environment,
                "source": "ibkr-api",
                "error": str(exc),
            }
        return jsonify(
            {
                **payload,
                "service_topology": build_service_topology(),
            }
        )

    exports["custom_system_storagez"] = custom_system_storagez

    @app.route("/api/custom/system/monitorz", methods=["GET"])
    def custom_system_monitorz() -> Response:
        environment, _market_data_mode = _request_modes_from_args()
        if parse_boolean(request.args.get("lite"), False):
            summary = _cached_summary_payload(environment, lite_mode=True)
            payload = {
                "ok": bool(summary.get("ok", False)),
                "status": str(summary.get("status") or "offline"),
                "environment": summary.get("environment") or environment,
                "broker_mode": environment,
                "data_environment": summary.get("data_environment") or _market_data_mode,
                "market_data_environment": summary.get("data_environment") or _market_data_mode,
                "service_topology": summary.get("service_topology") if isinstance(summary.get("service_topology"), dict) else build_service_topology(),
                "service_monitor": summary.get("service_monitor") if isinstance(summary.get("service_monitor"), dict) else {},
                "source": "ibkr-api",
                "lite": True,
            }
            if isinstance(summary.get("_cache"), dict):
                payload["_cache"] = summary["_cache"]
            return jsonify(payload)
        return jsonify(_cached_monitor_payload(environment))

    exports["custom_system_monitorz"] = custom_system_monitorz
    exports["_clear_control_plane_cache"] = _clear_control_plane_cache
    return exports


__all__ = ["register_system_read_routes", "_clear_control_plane_cache"]
