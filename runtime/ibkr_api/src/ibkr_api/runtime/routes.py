from __future__ import annotations

import copy
import os
import threading
import time
from typing import Any

from flask import Response, jsonify, request

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.runtime.strategy_capacity import normalize_strategy_capacity_snapshot, unavailable_strategy_capacity
from ibkr_api.system.service_state import canonicalize_topology


_RUNTIME_CONFIG_CACHE_LOCK = threading.RLock()
_RUNTIME_CONFIG_CACHE: dict[tuple[str, str, str], dict[str, Any]] = {}


def _runtime_config_cache_ttl_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("IBKR_RUNTIME_CONFIG_CACHE_TTL_SEC", "5") or "5"))
    except (TypeError, ValueError):
        return 5.0


def _runtime_config_cache_get(key: tuple[str, str, str]) -> dict[str, Any] | None:
    ttl = _runtime_config_cache_ttl_seconds()
    if ttl <= 0:
        return None
    now = time.time()
    with _RUNTIME_CONFIG_CACHE_LOCK:
        entry = _RUNTIME_CONFIG_CACHE.get(key)
        if not entry:
            return None
        if float(entry.get("expires_at") or 0.0) <= now:
            _RUNTIME_CONFIG_CACHE.pop(key, None)
            return None
        payload = copy.deepcopy(entry.get("payload") if isinstance(entry.get("payload"), dict) else {})
    if payload:
        payload["_cache"] = {
            "state": "hit",
            "ttl_s": ttl,
            "age_s": round(max(0.0, now - float(entry.get("stored_at") or now)), 3),
        }
    return payload if payload else None


def _runtime_config_cache_store(key: tuple[str, str, str], payload: dict[str, Any]) -> dict[str, Any]:
    ttl = _runtime_config_cache_ttl_seconds()
    if ttl <= 0:
        return payload
    now = time.time()
    cached_payload = copy.deepcopy(payload or {})
    with _RUNTIME_CONFIG_CACHE_LOCK:
        _RUNTIME_CONFIG_CACHE[key] = {
            "payload": cached_payload,
            "stored_at": now,
            "expires_at": now + ttl,
        }
    payload["_cache"] = {"state": "miss", "ttl_s": ttl, "age_s": 0.0}
    return payload


def _clear_runtime_config_cache() -> None:
    with _RUNTIME_CONFIG_CACHE_LOCK:
        _RUNTIME_CONFIG_CACHE.clear()


def register_runtime_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    build_service_topology = deps["build_service_topology"]
    normalize_environment = deps["normalize_environment"]
    parse_boolean = deps["parse_boolean"]
    pick_effective_config_rows = deps["pick_effective_config_rows"]
    serialize_config_rows = deps["serialize_config_rows"]
    merge_service_topology = deps["merge_service_topology"]
    fetch_compute_health = deps["fetch_compute_health"]
    fetch_backtest_health = deps["fetch_backtest_health"]
    fetch_backtest_status = deps["fetch_backtest_status"]
    fetch_compute_status = deps["fetch_compute_status"]
    fetch_runtime_health = deps["fetch_runtime_health"]
    fetch_runtime_status = deps["fetch_runtime_status"]
    as_dict = deps["as_dict"]
    load_daily_scan_state = deps["load_daily_scan_state"]
    count_active_today_targets = deps["count_active_today_targets"]
    build_today_targets_response = deps.get("build_today_targets_response")
    build_statusz_compute_payload = deps["build_statusz_compute_payload"]
    build_statusz_live_readiness = deps["build_statusz_live_readiness"]
    build_statusz_runtime_payload = deps["build_statusz_runtime_payload"]
    collect_storage_health = deps.get("collect_storage_health")
    get_state_payload = deps["get_state_payload"]
    normalize_two_factor_state_with_runtime = deps["normalize_two_factor_state_with_runtime"]
    ibkr_2fa_state_key = deps["ibkr_2fa_state_key"]
    ibkr_2fa_state_date = deps["ibkr_2fa_state_date"]
    compute_base_url = deps["compute_base_url"]
    backtest_base_url = deps["backtest_base_url"]
    exports: dict[str, Any] = {}

    def requested_mode_payload() -> dict[str, Any]:
        return {
            "broker_mode": request.args.get("broker_mode"),
            "market_data_mode": request.args.get("market_data_mode") or request.args.get("data_environment") or request.args.get("environment"),
            "data_environment": request.args.get("data_environment"),
        }

    def safe_storage_health(environment: str) -> dict[str, Any]:
        if collect_storage_health is None:
            return {}
        try:
            return collect_storage_health(environment, None)
        except Exception as exc:
            return {
                "ok": False,
                "status": "unavailable",
                "environment": environment,
                "source": "ibkr-api",
                "error": str(exc),
            }

    def fallback_today_target_state(*, broker_mode: str, data_environment: str, market_date: str) -> dict[str, Any]:
        fallback: dict[str, Any] = {
            "active_target_date": market_date,
            "active_target_count": count_active_today_targets(data_environment, market_date) if market_date else 0,
        }
        if not callable(build_today_targets_response) or not market_date:
            return fallback
        try:
            payload, status_code = build_today_targets_response(
                payload={
                    "broker_mode": broker_mode,
                    "market_data_mode": data_environment,
                    "data_environment": data_environment,
                    "environment": data_environment,
                    "market_date": market_date,
                    "date": market_date,
                    "per_page": 200,
                    "page": 1,
                }
            )
        except Exception:
            return fallback
        if status_code != 200 or not isinstance(payload, dict):
            return fallback
        summary = as_dict(payload.get("summary"))
        items = [dict(item) for item in payload.get("items") or [] if isinstance(item, dict)]
        execution_symbols = [
            str(item.get("symbol") or "").strip().upper()
            for item in items
            if bool(item.get("execution_eligible") or item.get("is_execution_eligible")) and str(item.get("symbol") or "").strip()
        ]
        observe_symbols = [
            str(item.get("symbol") or "").strip().upper()
            for item in items
            if not bool(item.get("execution_eligible") or item.get("is_execution_eligible")) and str(item.get("symbol") or "").strip()
        ]
        fallback.update(
            {
                "active_target_count": int(summary.get("active_count") or fallback.get("active_target_count") or 0),
                "execution_eligible_target_count": int(summary.get("execution_eligible_count") or len(execution_symbols)),
                "execution_eligible_symbols": execution_symbols,
                "observe_target_count": int(summary.get("observe_only_count") or len(observe_symbols)),
                "observe_target_symbols": observe_symbols[:25],
            }
        )
        return fallback

    @app.route("/api/custom/ibkr/strategy-capacity", methods=["GET"])
    def custom_ibkr_strategy_capacity() -> Response:
        mode_payload = requested_mode_payload()
        environment = request_broker_mode(mode_payload)
        data_environment = request_market_data_mode(mode_payload)
        runtime_result = fetch_runtime_status(environment)
        runtime_payload = as_dict(runtime_result.get("payload"))
        if not runtime_payload:
            capacity = unavailable_strategy_capacity(runtime_result.get("error") or "runtime_status_unavailable")
        else:
            capacity = normalize_strategy_capacity_snapshot(runtime_payload)
        payload = {
            "ok": True,
            "available": bool(capacity.get("available")),
            "broker_mode": environment,
            "environment": environment,
            "data_environment": data_environment,
            "strategy_capacity": capacity,
            "source": "ibkr-api",
        }
        if not payload["available"]:
            payload["error"] = str(capacity.get("error") or runtime_result.get("error") or "strategy_capacity_unavailable")
        return jsonify(payload)
    exports["custom_ibkr_strategy_capacity"] = custom_ibkr_strategy_capacity

    @app.route("/api/custom/ibkr/runtime/config", methods=["GET"])
    def custom_ibkr_runtime_config() -> Response:
        mode_payload = requested_mode_payload()
        requested_environment = request_broker_mode(mode_payload)
        data_environment = request_market_data_mode(mode_payload)
        scope = str(request.args.get("scope") or "").strip().lower() or "effective"
        cache_key = (str(requested_environment or ""), str(data_environment or ""), "all" if scope == "all" else "effective")
        cached_payload = _runtime_config_cache_get(cache_key)
        if cached_payload is not None:
            return jsonify(cached_payload)

        environment = requested_environment
        runtime_payload: dict[str, Any] = {}
        try:
            runtime_result = fetch_runtime_health(requested_environment)
            runtime_payload = as_dict(runtime_result.get("payload"))
            if runtime_payload:
                environment = normalize_environment(
                    runtime_payload.get("broker_mode") or runtime_payload.get("environment") or requested_environment,
                    requested_environment,
                )
        except Exception:
            runtime_payload = {}
        rows = pb.get_runtime_config(scope="all", environment=environment)
        if scope != "all":
            rows = pick_effective_config_rows(rows, environment)
        payload = {
            "ok": True,
            "environment": environment,
            "requested_environment": requested_environment,
            "broker_mode": environment,
            "gateway_mode": str(runtime_payload.get("gateway_mode") or ""),
            "data_environment": data_environment,
            "market_data_environment": data_environment,
            "shared_market_data": data_environment == "live",
            "runtime": runtime_payload,
            "scope": "all" if scope == "all" else "effective",
            "items": serialize_config_rows(rows),
            "source": "ibkr-api",
            "service_topology": build_service_topology(),
        }
        return jsonify(_runtime_config_cache_store(cache_key, payload))
    exports["custom_ibkr_runtime_config"] = custom_ibkr_runtime_config
    exports["_clear_runtime_config_cache"] = _clear_runtime_config_cache

    @app.route("/api/custom/ibkr/healthz", methods=["GET"])
    def custom_ibkr_healthz() -> Response:
        mode_payload = requested_mode_payload()
        environment = request_broker_mode(mode_payload)
        data_environment = request_market_data_mode(mode_payload)
        compute_result = fetch_compute_health(data_environment)
        backtest_result = fetch_backtest_health(data_environment)
        runtime_result = fetch_runtime_health(environment)
        compute_payload = as_dict(compute_result.get("payload"))
        backtest_payload = as_dict(backtest_result.get("payload"))
        backtest_service_payload = {
            **backtest_result,
            "payload": backtest_payload,
        }
        runtime_payload = as_dict(runtime_result.get("payload"))
        service_topology = merge_service_topology(compute_payload, runtime_payload, backtest_payload)
        service_topology, service_monitor = canonicalize_topology(
            environment,
            service_topology,
            compute_payload,
            runtime_payload,
            backtest_service_payload,
        )
        runtime_expected = str(service_topology.get("runtime_mode") or "").strip().lower() == "remote"
        ok = bool(compute_result.get("ok")) and (not runtime_expected or bool(runtime_result.get("ok")))
        degraded = (
            bool(compute_result.get("ok"))
            or bool(runtime_result.get("ok"))
            or bool(compute_payload)
            or bool(runtime_payload)
        )
        errors = {
            key: value
            for key, value in {
                "compute": str(compute_result.get("error") or ""),
                "runtime": str(runtime_result.get("error") or ""),
            }.items()
            if value
        }
        storage_health = safe_storage_health(data_environment)
        return jsonify(
            {
                "ok": ok,
                "status": "running" if ok else ("degraded" if degraded else "offline"),
                "environment": environment,
                "broker_mode": environment,
                "data_environment": data_environment,
                "market_data_environment": data_environment,
                "shared_market_data": data_environment == "live",
                "requested_environment": environment,
                "requested_broker_mode": environment,
                "requested_market_data_mode": data_environment,
                "actual_runtime_environment": normalize_environment(runtime_payload.get("broker_mode") or runtime_payload.get("environment") or environment, environment),
                "compute": compute_payload,
                "backtest_service": backtest_service_payload,
                "backtest": as_dict(backtest_payload.get("backtest")),
                "runtime": runtime_payload,
                "service_topology": service_topology,
                "service_monitor": service_monitor,
                "storage_health": storage_health,
                "source": "ibkr-api",
                "proxy_upstream_compute": f"{compute_base_url}/health",
                "proxy_upstream_backtest": backtest_result.get("upstream") or f"{backtest_base_url}/health",
                "backtest_error": str(backtest_result.get("error") or ""),
                "proxy_upstream_runtime": runtime_result.get("upstream") or "",
                "error": "; ".join(f"{key}: {value}" for key, value in errors.items()),
                "errors": errors,
            }
        )
    exports["custom_ibkr_healthz"] = custom_ibkr_healthz

    @app.route("/api/custom/ibkr/statusz", methods=["GET"])
    def custom_ibkr_statusz() -> Response:
        mode_payload = requested_mode_payload()
        environment = request_broker_mode(mode_payload)
        data_environment = request_market_data_mode(mode_payload)
        include_engines = parse_boolean(request.args.get("full"), False) or not parse_boolean(request.args.get("lite"), True)
        include_warmup_details = (
            parse_boolean(request.args.get("warmup"), False)
            or parse_boolean(request.args.get("warmup_full"), False)
            or include_engines
        )

        compute_result = fetch_compute_status(data_environment, include_engines=include_engines)
        backtest_health_result = fetch_backtest_health(data_environment)
        backtest_status_result = fetch_backtest_status(data_environment)
        runtime_result = fetch_runtime_status(environment)
        compute_payload = as_dict(compute_result.get("payload"))
        backtest_health_payload = as_dict(backtest_health_result.get("payload"))
        backtest_status_payload = as_dict(backtest_status_result.get("payload"))
        backtest_service_payload = {
            **backtest_health_result,
            "payload": backtest_health_payload,
        }
        runtime_payload = as_dict(runtime_result.get("payload"))
        persisted_daily_scan = load_daily_scan_state(data_environment)
        fallback_active_target_date = str(persisted_daily_scan.get("market_date") or "").strip()
        fallback_target_state = fallback_today_target_state(
            broker_mode=environment,
            data_environment=data_environment,
            market_date=fallback_active_target_date,
        )

        compute_data = build_statusz_compute_payload(compute_payload, include_engines)
        live_readiness = build_statusz_live_readiness(compute_payload, runtime_payload)
        runtime_data = build_statusz_runtime_payload(
            runtime_payload,
            include_warmup_details,
            live_readiness=live_readiness,
            fallback_state={
                "daily_scan": persisted_daily_scan,
                **fallback_target_state,
            },
        )
        service_topology = merge_service_topology(compute_data, runtime_data, backtest_health_payload)
        service_topology, service_monitor = canonicalize_topology(
            environment,
            service_topology,
            compute_data,
            runtime_data,
            backtest_service_payload,
            backtest_status_payload,
        )
        actual_runtime_environment = normalize_environment(
            runtime_data.get("environment") or compute_data.get("environment") or environment,
            environment,
        )
        errors = {
            key: value
            for key, value in {
                "compute": str(compute_result.get("error") or ""),
                "runtime": str(runtime_result.get("error") or ""),
            }.items()
            if value
        }
        storage_health = safe_storage_health(data_environment)
        ok = (
            not errors
            and compute_data.get("ok") is not False
            and (runtime_data.get("ok") is not False or not runtime_data)
        )
        degraded = (
            bool(compute_result.get("ok"))
            or bool(runtime_result.get("ok"))
            or bool(compute_data)
            or bool(runtime_payload)
        )
        response = dict(compute_data)
        if runtime_data.get("ok") is not False:
            response.update(runtime_data)
        response.update(
            {
                "compute": compute_data,
                "backtest_service": backtest_service_payload,
                "backtest": backtest_status_payload or as_dict(backtest_health_payload.get("backtest")),
                "runtime": runtime_data,
                "service_topology": service_topology,
                "service_monitor": service_monitor,
                "storage_health": storage_health,
                "warmup_details_included": bool(include_warmup_details),
                "requested_environment": environment,
                "requested_broker_mode": environment,
                "requested_market_data_mode": data_environment,
                "broker_mode": runtime_data.get("broker_mode") or actual_runtime_environment,
                "gateway_mode": runtime_data.get("gateway_mode") or "",
                "data_environment": runtime_data.get("data_environment") or data_environment,
                "market_data_environment": runtime_data.get("market_data_environment") or data_environment,
                "shared_market_data": bool(runtime_data.get("shared_market_data") or data_environment == "live"),
                "actual_runtime_environment": actual_runtime_environment,
                "runtime_environment_mismatch": actual_runtime_environment != environment,
                "ok": ok,
                "status": "running" if ok else ("degraded" if degraded else "offline"),
                "source": "ibkr-api",
                "proxy_upstream_compute": f"{compute_base_url}/status",
                "proxy_upstream_backtest": backtest_health_result.get("upstream") or f"{backtest_base_url}/health",
                "proxy_upstream_backtest_status": backtest_status_result.get("upstream") or f"{backtest_base_url}/backtest/status",
                "backtest_error": str(backtest_health_result.get("error") or backtest_status_result.get("error") or ""),
                "proxy_upstream_runtime": runtime_result.get("selected_upstream") or "",
                "proxy_upstream_runtime_proxy": runtime_result.get("proxy_upstream") or "",
                "proxy_upstream_runtime_direct": runtime_result.get("direct_upstream") or "",
            }
        )
        if errors:
            response["errors"] = errors
            response["error"] = "; ".join(f"{key}: {value}" for key, value in errors.items())
        return jsonify(response)
    exports["custom_ibkr_statusz"] = custom_ibkr_statusz

    @app.route("/api/custom/ibkr/2fa/status", methods=["GET"])
    def custom_ibkr_two_factor_status() -> Response:
        environment = request_broker_mode(requested_mode_payload())
        payload = get_state_payload(ibkr_2fa_state_key, environment, date=ibkr_2fa_state_date)
        state = as_dict(payload.get("data"))
        if not str(state.get("status") or "").strip():
            state["status"] = "requested"
        if not str(state.get("recovery_phase") or "").strip():
            state["recovery_phase"] = "idle"

        runtime_result = fetch_runtime_status(environment)
        runtime_payload = as_dict(runtime_result.get("payload"))
        if runtime_payload:
            state = normalize_two_factor_state_with_runtime(state, runtime_payload)
            actual_runtime_environment = normalize_environment(runtime_payload.get("broker_mode") or runtime_payload.get("environment") or environment, environment)
            state["requested_environment"] = environment
            state["actual_runtime_environment"] = actual_runtime_environment
            state["runtime_environment_mismatch"] = actual_runtime_environment != environment
            if state["runtime_environment_mismatch"]:
                state["message"] = (
                    f"当前 {environment.upper()} 页面没有独立 runtime；实际运行中的是 "
                    f"{actual_runtime_environment.upper()}，2FA 动作已阻止。"
                )
                state["last_result"] = f"当前显示的是 {actual_runtime_environment.upper()} 运行态。"
        if runtime_result.get("error"):
            state["runtime_status_error"] = str(runtime_result.get("error") or "")

        return jsonify(
            {
                "ok": True,
                "environment": environment,
                "broker_mode": environment,
                "date": payload.get("date") or ibkr_2fa_state_date,
                "state": state,
                "source": "ibkr-api",
            }
        )
    exports["custom_ibkr_two_factor_status"] = custom_ibkr_two_factor_status

    return exports


__all__ = ["register_runtime_routes"]
