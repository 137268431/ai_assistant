from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.system.health_report import build_health_report_response
from ibkr_api.system.jobs import (
    build_auth_edge_guard_response,
    build_auth_pending_guard_response,
    build_data_gap_guard_response,
    build_order_expiry_response,
    build_system_daily_report_response,
    build_system_market_open_reminder_response,
    build_two_factor_hourly_check_response,
    build_weekly_reauth_followup_response,
    build_weekly_reauth_reminder_response,
)
from ibkr_api.system.notify import build_notify_response


def register_system_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    config = deps["config"]
    normalize_environment = deps["normalize_environment"]
    parse_boolean = deps["parse_boolean"]
    escape_filter_string = deps["escape_filter_string"]
    config_value = deps["config_value"]
    signal_chat_id = deps["signal_chat_id"]
    console_base_url = deps["console_base_url"]
    feishu_send_interactive = deps["feishu_send_interactive"]
    feishu_update_interactive = deps["feishu_update_interactive"]
    emit_system_event = deps["emit_system_event"]
    label_title_with_environment = deps["label_title_with_environment"]
    add_environment_to_detail = deps["add_environment_to_detail"]
    write_system_event_record = deps["write_system_event_record"]
    deliver_system_event_notification = deps["deliver_system_event_notification"]
    build_cron_payload = deps["build_cron_payload"]
    scheduler_status = deps["scheduler_status"]
    build_scheduler_summary = deps["build_scheduler_summary"]
    augment_scheduler_summary = deps["augment_scheduler_summary"]
    build_system_monitor_payload = deps["build_system_monitor_payload"]
    build_system_summary_payload = deps["build_system_summary_payload"]
    build_service_topology = deps["build_service_topology"]
    time_strings = deps["time_strings"]
    get_state_payload = deps["get_state_payload"]
    normalize_two_factor_state_with_runtime = deps["normalize_two_factor_state_with_runtime"]
    fetch_runtime_status = deps["fetch_runtime_status"]
    request_two_factor_approval = deps["request_two_factor_approval"]
    cancel_broker_order = deps["cancel_broker_order"]
    build_signal_expiry_response = deps["build_signal_expiry_response"]
    build_order_detail_integrity_response = deps["build_order_detail_integrity_response"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/health-report", methods=["POST"])
    def custom_ibkr_health_report() -> Response:
        payload, status_code = build_health_report_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            label_title_with_environment=label_title_with_environment,
            add_environment_to_detail=add_environment_to_detail,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_health_report"] = custom_ibkr_health_report

    @app.route("/api/custom/ibkr/notify", methods=["POST"])
    def custom_ibkr_notify() -> Response:
        payload, status_code = build_notify_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            emit_system_event=emit_system_event,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_notify"] = custom_ibkr_notify

    @app.route("/api/custom/system/event", methods=["POST"])
    def custom_system_event() -> Response:
        payload = request.get_json(silent=True) or {}
        environment = normalize_environment(payload.get("environment"), "live")
        raw_title = str(payload.get("title") or "").strip()
        raw_detail = payload.get("detail") if payload.get("detail") is not None else {}
        event_type = str(payload.get("event_type") or "status_change").strip() or "status_change"
        level = str(payload.get("level") or "info").strip().lower() or "info"
        source = str(payload.get("source") or "ibkr-api").strip() or "ibkr-api"
        message_id = str(payload.get("message_id") or "").strip()
        if not raw_title:
            return jsonify({"ok": False, "environment": environment, "error": "title required", "source": "ibkr-api"}), 400
        config.refresh()
        delivery = deliver_system_event_notification(
            event_type,
            level,
            source,
            raw_title,
            raw_detail,
            environment,
            message_id=message_id,
        )
        notified = bool(delivery.get("success")) and not bool(delivery.get("suppressed"))
        persisted = bool(write_system_event_record(event_type, level, source, raw_title, raw_detail, environment, notified))
        return jsonify(
            {
                "ok": True,
                "environment": environment,
                "notified": notified,
                "persisted": persisted,
                "message_id": str(delivery.get("message_id") or message_id),
                "updated": bool(delivery.get("updated")),
                "skipped": bool(delivery.get("skipped")),
                "suppressed": bool(delivery.get("suppressed")),
                "error": str(delivery.get("error") or ""),
                "source": "ibkr-api",
            }
        )
    exports["custom_system_event"] = custom_system_event

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

    @app.route("/api/custom/system/jobs/signal_expiry", methods=["POST"])
    def custom_system_job_signal_expiry() -> Response:
        payload, status_code = build_signal_expiry_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            config_value=config_value,
            send_interactive=feishu_send_interactive,
            update_interactive=feishu_update_interactive,
            signal_chat_id_fn=signal_chat_id,
            console_base_url=console_base_url(),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_signal_expiry"] = custom_system_job_signal_expiry

    @app.route("/api/custom/system/jobs/order_detail_integrity", methods=["POST"])
    def custom_system_job_order_detail_integrity() -> Response:
        payload, status_code = build_order_detail_integrity_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_order_detail_integrity"] = custom_system_job_order_detail_integrity

    @app.route("/api/custom/system/jobs/order_expiry", methods=["POST"])
    def custom_system_job_order_expiry() -> Response:
        payload, status_code = build_order_expiry_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            config_value=config_value,
            cancel_broker_order=cancel_broker_order,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_order_expiry"] = custom_system_job_order_expiry

    @app.route("/api/custom/system/jobs/auth_edge_guard", methods=["POST"])
    def custom_system_job_auth_edge_guard() -> Response:
        payload, status_code = build_auth_edge_guard_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date="global" if state_key == "ibkr_2fa" else time_strings()["date"]),
            normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
            fetch_runtime_status=fetch_runtime_status,
            time_strings=time_strings,
            upsert_state=lambda key, environment, data, date: pb.upsert_state(key, environment, data, date=date),
            emit_system_event=emit_system_event,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_auth_edge_guard"] = custom_system_job_auth_edge_guard

    @app.route("/api/custom/system/jobs/auth_pending_guard", methods=["POST"])
    def custom_system_job_auth_pending_guard() -> Response:
        payload, status_code = build_auth_pending_guard_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date="global" if state_key == "ibkr_2fa" else time_strings()["date"]),
            normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
            fetch_runtime_status=fetch_runtime_status,
            time_strings=time_strings,
            upsert_state=lambda key, environment, data, date: pb.upsert_state(key, environment, data, date=date),
            emit_system_event=emit_system_event,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_auth_pending_guard"] = custom_system_job_auth_pending_guard

    @app.route("/api/custom/system/jobs/data_gap_guard", methods=["POST"])
    def custom_system_job_data_gap_guard() -> Response:
        payload, status_code = build_data_gap_guard_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
            emit_system_event=emit_system_event,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_data_gap_guard"] = custom_system_job_data_gap_guard

    @app.route("/api/custom/system/jobs/2fa_hourly_check", methods=["POST"])
    def custom_system_job_two_factor_hourly_check() -> Response:
        payload, status_code = build_two_factor_hourly_check_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date="global"),
            normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
            fetch_runtime_status=fetch_runtime_status,
            request_two_factor_approval=request_two_factor_approval,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_two_factor_hourly_check"] = custom_system_job_two_factor_hourly_check

    @app.route("/api/custom/system/jobs/weekly_reauth_reminder", methods=["POST"])
    def custom_system_job_weekly_reauth_reminder() -> Response:
        payload, status_code = build_weekly_reauth_reminder_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date="global"),
            normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
            fetch_runtime_status=fetch_runtime_status,
            request_two_factor_approval=request_two_factor_approval,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_weekly_reauth_reminder"] = custom_system_job_weekly_reauth_reminder

    @app.route("/api/custom/system/jobs/weekly_reauth_followup", methods=["POST"])
    def custom_system_job_weekly_reauth_followup() -> Response:
        payload, status_code = build_weekly_reauth_followup_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date="global"),
            normalize_two_factor_state_with_runtime=normalize_two_factor_state_with_runtime,
            fetch_runtime_status=fetch_runtime_status,
            request_two_factor_approval=request_two_factor_approval,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_weekly_reauth_followup"] = custom_system_job_weekly_reauth_followup

    @app.route("/api/custom/system/jobs/market_open_reminder", methods=["POST"])
    def custom_system_job_market_open_reminder() -> Response:
        payload, status_code = build_system_market_open_reminder_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
            build_system_summary_payload=lambda environment, lite_mode=False: build_system_summary_payload(environment, lite_mode=lite_mode),
            build_system_monitor_payload=build_system_monitor_payload,
            emit_system_event=emit_system_event,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date=time_strings()["date"]),
            upsert_state=lambda key, environment, data, date: pb.upsert_state(key, environment, data, date=date),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_market_open_reminder"] = custom_system_job_market_open_reminder

    @app.route("/api/custom/system/jobs/daily_report", methods=["POST"])
    def custom_system_job_daily_report() -> Response:
        payload, status_code = build_system_daily_report_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
            build_system_summary_payload=lambda environment, lite_mode=False: build_system_summary_payload(environment, lite_mode=lite_mode),
            build_system_monitor_payload=build_system_monitor_payload,
            emit_system_event=emit_system_event,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date=time_strings()["date"]),
            upsert_state=lambda key, environment, data, date: pb.upsert_state(key, environment, data, date=date),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_system_job_daily_report"] = custom_system_job_daily_report
    return exports
