from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.system.scheduler_support import run_scheduler_job


SystemDeps = dict[str, Any]



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
    scheduler_base_url = deps["scheduler_base_url"]

    def _request_modes_from_args() -> tuple[str, str]:
        payload = {
            "broker_mode": request.args.get("broker_mode"),
            "market_data_mode": request.args.get("market_data_mode") or request.args.get("environment"),
        }
        return request_broker_mode(payload), request_market_data_mode(payload)

    @app.route("/api/custom/system/cronz", methods=["GET"])
    def custom_system_cronz() -> Response:
        broker_mode, market_data_mode = _request_modes_from_args()
        config.refresh()
        scheduler_payload = scheduler_status(
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
        scheduler_payload = scheduler_status(
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
        return jsonify(build_system_summary_payload(environment, lite_mode=lite_mode))

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
        return jsonify(build_system_monitor_payload(environment))

    exports["custom_system_monitorz"] = custom_system_monitorz
    return exports


__all__ = ["register_system_read_routes"]
