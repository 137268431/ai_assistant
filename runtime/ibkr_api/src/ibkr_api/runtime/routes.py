from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request


def register_runtime_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    build_service_topology = deps["build_service_topology"]
    normalize_environment = deps["normalize_environment"]
    parse_boolean = deps["parse_boolean"]
    pick_effective_config_rows = deps["pick_effective_config_rows"]
    serialize_config_rows = deps["serialize_config_rows"]
    merge_service_topology = deps["merge_service_topology"]
    fetch_compute_health = deps["fetch_compute_health"]
    fetch_compute_status = deps["fetch_compute_status"]
    fetch_runtime_health = deps["fetch_runtime_health"]
    fetch_runtime_status = deps["fetch_runtime_status"]
    as_dict = deps["as_dict"]
    load_daily_scan_state = deps["load_daily_scan_state"]
    count_active_today_targets = deps["count_active_today_targets"]
    build_statusz_compute_payload = deps["build_statusz_compute_payload"]
    build_statusz_live_readiness = deps["build_statusz_live_readiness"]
    build_statusz_runtime_payload = deps["build_statusz_runtime_payload"]
    get_state_payload = deps["get_state_payload"]
    normalize_two_factor_state_with_runtime = deps["normalize_two_factor_state_with_runtime"]
    ibkr_2fa_state_key = deps["ibkr_2fa_state_key"]
    ibkr_2fa_state_date = deps["ibkr_2fa_state_date"]
    compute_base_url = deps["compute_base_url"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/runtime/config", methods=["GET"])
    def custom_ibkr_runtime_config() -> Response:
        environment = normalize_environment(request.args.get("environment"), "live")
        scope = str(request.args.get("scope") or "").strip().lower() or "effective"
        rows = pb.get_runtime_config(scope="all", environment=environment)
        if scope != "all":
            rows = pick_effective_config_rows(rows, environment)
        return jsonify(
            {
                "ok": True,
                "environment": environment,
                "scope": "all" if scope == "all" else "effective",
                "items": serialize_config_rows(rows),
                "source": "ibkr-api",
                "service_topology": build_service_topology(),
            }
        )
    exports["custom_ibkr_runtime_config"] = custom_ibkr_runtime_config

    @app.route("/api/custom/ibkr/healthz", methods=["GET"])
    def custom_ibkr_healthz() -> Response:
        environment = normalize_environment(request.args.get("environment"), "live")
        compute_result = fetch_compute_health(environment)
        runtime_result = fetch_runtime_health(environment)
        compute_payload = as_dict(compute_result.get("payload"))
        runtime_payload = as_dict(runtime_result.get("payload"))
        service_topology = merge_service_topology(compute_payload, runtime_payload)
        runtime_expected = str(service_topology.get("runtime_mode") or "").strip().lower() == "remote"
        ok = bool(compute_result.get("ok")) and (not runtime_expected or bool(runtime_result.get("ok")))
        degraded = bool(compute_result.get("ok")) or bool(runtime_result.get("ok")) or bool(compute_payload) or bool(runtime_payload)
        errors = {
            key: value
            for key, value in {
                "compute": str(compute_result.get("error") or ""),
                "runtime": str(runtime_result.get("error") or ""),
            }.items()
            if value
        }
        return jsonify(
            {
                "ok": ok,
                "status": "running" if ok else ("degraded" if degraded else "offline"),
                "environment": environment,
                "requested_environment": environment,
                "actual_runtime_environment": normalize_environment(runtime_payload.get("environment") or environment, environment),
                "compute": compute_payload,
                "runtime": runtime_payload,
                "service_topology": service_topology,
                "source": "ibkr-api",
                "proxy_upstream_compute": f"{compute_base_url}/health",
                "proxy_upstream_runtime": runtime_result.get("upstream") or "",
                "error": "; ".join(f"{key}: {value}" for key, value in errors.items()),
                "errors": errors,
            }
        )
    exports["custom_ibkr_healthz"] = custom_ibkr_healthz

    @app.route("/api/custom/ibkr/statusz", methods=["GET"])
    def custom_ibkr_statusz() -> Response:
        environment = normalize_environment(request.args.get("environment"), "live")
        include_engines = parse_boolean(request.args.get("full"), False) or not parse_boolean(request.args.get("lite"), True)
        include_warmup_details = (
            parse_boolean(request.args.get("warmup"), False)
            or parse_boolean(request.args.get("warmup_full"), False)
            or include_engines
        )

        compute_result = fetch_compute_status(environment)
        runtime_result = fetch_runtime_status(environment)
        compute_payload = as_dict(compute_result.get("payload"))
        runtime_payload = as_dict(runtime_result.get("payload"))
        persisted_daily_scan = load_daily_scan_state(environment)
        fallback_active_target_date = str(persisted_daily_scan.get("market_date") or "").strip()
        fallback_active_target_count = count_active_today_targets(environment, fallback_active_target_date) if fallback_active_target_date else 0

        compute_data = build_statusz_compute_payload(compute_payload, include_engines)
        live_readiness = build_statusz_live_readiness(compute_payload, runtime_payload)
        runtime_data = build_statusz_runtime_payload(
            runtime_payload,
            include_warmup_details,
            live_readiness=live_readiness,
            fallback_state={
                "daily_scan": persisted_daily_scan,
                "active_target_date": fallback_active_target_date,
                "active_target_count": fallback_active_target_count,
            },
        )
        service_topology = merge_service_topology(compute_data, runtime_data)
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
        ok = not errors and compute_data.get("ok") is not False and (runtime_data.get("ok") is not False or not runtime_data)
        degraded = bool(compute_result.get("ok")) or bool(runtime_result.get("ok")) or bool(compute_data) or bool(runtime_payload)
        response = dict(compute_data)
        if runtime_data.get("ok") is not False:
            response.update(runtime_data)
        response.update(
            {
                "compute": compute_data,
                "runtime": runtime_data,
                "service_topology": service_topology,
                "warmup_details_included": bool(include_warmup_details),
                "requested_environment": environment,
                "actual_runtime_environment": actual_runtime_environment,
                "runtime_environment_mismatch": actual_runtime_environment != environment,
                "ok": ok,
                "status": "running" if ok else ("degraded" if degraded else "offline"),
                "source": "ibkr-api",
                "proxy_upstream_compute": f"{compute_base_url}/status",
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
        environment = normalize_environment(request.args.get("environment"), "live")
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
            actual_runtime_environment = normalize_environment(runtime_payload.get("environment") or environment, environment)
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
                "environment": payload.get("environment") or environment,
                "date": payload.get("date") or ibkr_2fa_state_date,
                "state": state,
                "source": "ibkr-api",
            }
        )
    exports["custom_ibkr_two_factor_status"] = custom_ibkr_two_factor_status

    return exports


__all__ = ["register_runtime_routes"]
