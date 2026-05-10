from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.system.jobs import (
    build_auth_edge_guard_response,
    build_auth_pending_guard_response,
    build_data_gap_guard_response,
    build_early_expansion_topup_response,
    build_fundamentals_refresh_job_response,
    build_intraday_window_admission_response,
    build_order_expiry_response,
    build_system_daily_report_response,
    build_system_heartbeat_response,
    build_system_monitor_alert_guard_response,
    build_system_scan_summary_response,
    build_system_status_reminder_response,
    build_two_factor_hourly_check_response,
    build_weekly_reauth_followup_response,
    build_weekly_reauth_reminder_response,
)
from ibkr_api.system.jobs.open_report import build_system_open_report_response, load_market_snapshots_from_pb


SystemDeps = dict[str, Any]



def register_system_job_routes(app, *, deps: SystemDeps, exports: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    escape_filter_string = deps["escape_filter_string"]
    config_value = deps["config_value"]
    signal_chat_id = deps["signal_chat_id"]
    console_base_url = deps["console_base_url"]
    feishu_send_interactive = deps["feishu_send_interactive"]
    feishu_update_interactive = deps["feishu_update_interactive"]
    emit_system_event = deps["emit_system_event"]
    build_system_monitor_payload = deps["build_system_monitor_payload"]
    build_system_summary_payload = deps["build_system_summary_payload"]
    request_json_request = deps["request_json_request"]
    compute_base_url = deps["compute_base_url"]
    time_strings = deps["time_strings"]
    get_state_payload = deps["get_state_payload"]
    normalize_two_factor_state_with_runtime = deps["normalize_two_factor_state_with_runtime"]
    fetch_runtime_status = deps["fetch_runtime_status"]
    request_two_factor_approval = deps["request_two_factor_approval"]
    cancel_broker_order = deps["cancel_broker_order"]
    build_signal_expiry_response = deps["build_signal_expiry_response"]
    build_order_detail_integrity_response = deps["build_order_detail_integrity_response"]
    build_today_targets_response = deps["build_today_targets_response"]
    build_active_window_progress_response = deps["build_active_window_progress_response"]
    write_system_event_record = deps["write_system_event_record"]
    startup_chat_id = deps["startup_chat_id"]

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
            send_interactive=feishu_send_interactive,
            update_interactive=feishu_update_interactive,
            signal_chat_id_fn=signal_chat_id,
            console_base_url=console_base_url(),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_job_order_expiry"] = custom_system_job_order_expiry

    @app.route("/api/custom/system/jobs/auth_edge_guard", methods=["POST"])
    def custom_system_job_auth_edge_guard() -> Response:
        payload, status_code = build_auth_edge_guard_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            get_state_payload=lambda state_key, environment, date=None: get_state_payload(
                state_key,
                environment,
                date=date or ("global" if state_key == "ibkr_2fa" else time_strings()["date"]),
            ),
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
            get_state_payload=lambda state_key, environment, date=None: get_state_payload(
                state_key,
                environment,
                date=date or ("global" if state_key == "ibkr_2fa" else time_strings()["date"]),
            ),
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
        payload, status_code = build_system_open_report_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
            build_today_targets_response=lambda payload: build_today_targets_response(payload=payload),
            build_system_summary_payload=lambda environment, lite_mode=False: build_system_summary_payload(environment, lite_mode=lite_mode),
            build_system_monitor_payload=build_system_monitor_payload,
            feishu_send_interactive=feishu_send_interactive,
            write_system_event_record=write_system_event_record,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date=time_strings()["date"]),
            upsert_state=lambda key, environment, data, date: pb.upsert_state(key, environment, data, date=date),
            config_value=config_value,
            console_base_url=console_base_url,
            startup_chat_id=startup_chat_id,
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: load_market_snapshots_from_pb(
                pb,
                environment,
                symbols,
                market_date,
                computed_at_ms,
            ),
        )
        payload["job_id"] = "system_market_open_reminder"
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_job_market_open_reminder"] = custom_system_job_market_open_reminder

    @app.route("/api/custom/system/jobs/heartbeat", methods=["POST"])
    def custom_system_job_heartbeat() -> Response:
        payload, status_code = build_system_heartbeat_response(
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

    exports["custom_system_job_heartbeat"] = custom_system_job_heartbeat

    @app.route("/api/custom/system/jobs/status_reminder", methods=["POST"])
    def custom_system_job_status_reminder() -> Response:
        payload, status_code = build_system_status_reminder_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
            build_system_summary_payload=lambda environment, lite_mode=False: build_system_summary_payload(environment, lite_mode=lite_mode),
            build_system_monitor_payload=build_system_monitor_payload,
            emit_system_event=emit_system_event,
            build_today_targets_response=lambda payload: build_today_targets_response(payload=payload),
            build_active_window_progress_response=lambda payload: build_active_window_progress_response(payload=payload),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_job_status_reminder"] = custom_system_job_status_reminder

    @app.route("/api/custom/system/jobs/scan_summary", methods=["POST"])
    def custom_system_job_scan_summary() -> Response:
        payload, status_code = build_system_scan_summary_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
            build_today_targets_response=lambda payload: build_today_targets_response(payload=payload),
            build_system_summary_payload=lambda environment, lite_mode=False: build_system_summary_payload(environment, lite_mode=lite_mode),
            build_system_monitor_payload=build_system_monitor_payload,
            feishu_send_interactive=feishu_send_interactive,
            write_system_event_record=write_system_event_record,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date=time_strings()["date"]),
            upsert_state=lambda key, environment, data, date: pb.upsert_state(key, environment, data, date=date),
            config_value=config_value,
            console_base_url=console_base_url,
            signal_chat_id=signal_chat_id,
            startup_chat_id=startup_chat_id,
            load_market_snapshots=lambda environment, symbols, market_date, computed_at_ms: load_market_snapshots_from_pb(
                pb,
                environment,
                symbols,
                market_date,
                computed_at_ms,
            ),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_job_scan_summary"] = custom_system_job_scan_summary

    @app.route("/api/custom/system/jobs/early_expansion_topup", methods=["POST"])
    def custom_system_job_early_expansion_topup() -> Response:
        payload, status_code = build_early_expansion_topup_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
            feishu_send_interactive=feishu_send_interactive,
            write_system_event_record=write_system_event_record,
            config_value=config_value,
            console_base_url=console_base_url,
            startup_chat_id=startup_chat_id,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_job_early_expansion_topup"] = custom_system_job_early_expansion_topup

    @app.route("/api/custom/system/jobs/fundamentals_refresh", methods=["POST"])
    def custom_system_job_fundamentals_refresh() -> Response:
        payload, status_code = build_fundamentals_refresh_job_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            config_value=config_value,
            write_system_event_record=write_system_event_record,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_job_fundamentals_refresh"] = custom_system_job_fundamentals_refresh

    @app.route("/api/custom/system/jobs/intraday_window_admission", methods=["POST"])
    def custom_system_job_intraday_window_admission() -> Response:
        payload, status_code = build_intraday_window_admission_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            time_strings=time_strings,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
            config_value=config_value,
            write_system_event_record=write_system_event_record,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_job_intraday_window_admission"] = custom_system_job_intraday_window_admission

    @app.route("/api/custom/system/jobs/monitor_alert_guard", methods=["POST"])
    def custom_system_job_monitor_alert_guard() -> Response:
        payload, status_code = build_system_monitor_alert_guard_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
            build_system_monitor_payload=build_system_monitor_payload,
            emit_system_event=emit_system_event,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date=time_strings()["date"]),
            upsert_state=lambda key, environment, data, date: pb.upsert_state(key, environment, data, date=date),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_job_monitor_alert_guard"] = custom_system_job_monitor_alert_guard

    @app.route("/api/custom/system/jobs/daily_report", methods=["POST"])
    def custom_system_job_daily_report() -> Response:
        payload, status_code = build_system_daily_report_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            time_strings=time_strings,
            build_system_summary_payload=lambda environment, lite_mode=False: build_system_summary_payload(environment, lite_mode=lite_mode),
            build_system_monitor_payload=build_system_monitor_payload,
            feishu_send_interactive=feishu_send_interactive,
            write_system_event_record=write_system_event_record,
            get_state_payload=lambda state_key, environment: get_state_payload(state_key, environment, date=time_strings()["date"]),
            upsert_state=lambda key, environment, data, date: pb.upsert_state(key, environment, data, date=date),
            config_value=config_value,
            console_base_url=console_base_url,
            startup_chat_id=startup_chat_id,
            build_today_targets_response=lambda payload: build_today_targets_response(payload=payload),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_system_job_daily_report"] = custom_system_job_daily_report
    return exports


__all__ = ["register_system_job_routes"]
