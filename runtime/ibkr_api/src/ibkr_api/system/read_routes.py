from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request


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
    build_service_topology = deps["build_service_topology"]

    @app.route("/api/custom/system/cronz", methods=["GET"])
    def custom_system_cronz() -> Response:
        environment = normalize_environment(request.args.get("environment"), "live")
        config.refresh()
        scheduler_payload = scheduler_status(environment)
        scheduler_jobs = scheduler_payload.get("jobs") if isinstance(scheduler_payload.get("jobs"), dict) else {}
        items = build_cron_payload(config, environment, scheduler_jobs)
        return jsonify(
            {
                "ok": True,
                "items": items,
                "scheduler": augment_scheduler_summary(build_scheduler_summary(environment, scheduler_payload), items),
                "source": "ibkr-api",
                "service_topology": build_service_topology(),
            }
        )

    exports["custom_system_cronz"] = custom_system_cronz

    @app.route("/api/custom/system/healthz", methods=["GET"])
    def custom_system_healthz() -> Response:
        environment = normalize_environment(request.args.get("environment"), "live")
        payload = build_system_monitor_payload(environment)
        return jsonify(
            {
                "ok": bool(payload.get("ok", False)),
                "status": str(payload.get("status") or "offline"),
                "environment": environment,
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
        environment = normalize_environment(request.args.get("environment"), "live")
        config.refresh()
        scheduler_payload = scheduler_status(environment)
        scheduler_jobs = scheduler_payload.get("jobs") if isinstance(scheduler_payload.get("jobs"), dict) else {}
        items = build_cron_payload(config, environment, scheduler_jobs)
        summary = augment_scheduler_summary(build_scheduler_summary(environment, scheduler_payload), items)
        return jsonify(
            {
                "ok": bool(scheduler_payload.get("ok", False)),
                "status": str(summary.get("status") or "offline"),
                "environment": environment,
                "scheduler": summary,
                "items": items,
                "source": "ibkr-api",
                "service_topology": build_service_topology(),
            }
        )

    exports["custom_system_schedulerz"] = custom_system_schedulerz

    @app.route("/api/custom/system/summaryz", methods=["GET"])
    def custom_system_summaryz() -> Response:
        environment = normalize_environment(request.args.get("environment"), "live")
        lite_mode = parse_boolean(request.args.get("lite"), False)
        return jsonify(build_system_summary_payload(environment, lite_mode=lite_mode))

    exports["custom_system_summaryz"] = custom_system_summaryz

    @app.route("/api/custom/system/monitorz", methods=["GET"])
    def custom_system_monitorz() -> Response:
        environment = normalize_environment(request.args.get("environment"), "live")
        return jsonify(build_system_monitor_payload(environment))

    exports["custom_system_monitorz"] = custom_system_monitorz
    return exports


__all__ = ["register_system_read_routes"]
