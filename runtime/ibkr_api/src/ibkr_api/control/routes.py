from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.control.actions import (
    build_emergency_stop_response,
    build_reauth_response,
    build_recover_response,
    build_service_action_response,
)
from ibkr_api.control.broker_mode_switch import (
    build_broker_mode_switch_preview_response,
    build_broker_mode_switch_response,
)


def register_control_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    escape_filter_string = deps["escape_filter_string"]
    request_json_request = deps["request_json_request"]
    runtime_base_url = deps["runtime_base_url"]
    inspect_runtime_environment = deps["inspect_runtime_environment"]
    build_runtime_environment_mismatch_payload = deps["build_runtime_environment_mismatch_payload"]
    emit_system_event = deps.get("emit_system_event")
    as_dict = deps["as_dict"]
    fetch_runtime_status = deps["fetch_runtime_status"]
    get_state_payload = deps.get("get_state_payload")
    ibkr_2fa_state_key = deps.get("ibkr_2fa_state_key", "ibkr_2fa")
    ibkr_2fa_state_date = deps.get("ibkr_2fa_state_date", "global")
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/broker-mode/switch/preview", methods=["GET"])
    def custom_ibkr_broker_mode_switch_preview() -> Response:
        payload, status_code = build_broker_mode_switch_preview_response(
            pb,
            payload=request.args.to_dict(flat=True),
            normalize_environment=normalize_environment,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            fetch_runtime_status=fetch_runtime_status,
            as_dict=as_dict,
            get_state_payload=get_state_payload,
            ibkr_2fa_state_key=ibkr_2fa_state_key,
            ibkr_2fa_state_date=ibkr_2fa_state_date,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_broker_mode_switch_preview"] = custom_ibkr_broker_mode_switch_preview

    @app.route("/api/custom/ibkr/broker-mode/switch", methods=["POST"])
    def custom_ibkr_broker_mode_switch() -> Response:
        payload, status_code = build_broker_mode_switch_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            fetch_runtime_status=fetch_runtime_status,
            as_dict=as_dict,
            get_state_payload=get_state_payload,
            ibkr_2fa_state_key=ibkr_2fa_state_key,
            ibkr_2fa_state_date=ibkr_2fa_state_date,
            emit_system_event=emit_system_event,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_broker_mode_switch"] = custom_ibkr_broker_mode_switch

    @app.route("/api/custom/ibkr/emergency-stop", methods=["POST"])
    def custom_ibkr_emergency_stop() -> Response:
        payload, status_code = build_emergency_stop_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            inspect_runtime_environment=inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=build_runtime_environment_mismatch_payload,
            emit_system_event=emit_system_event,
            as_dict=as_dict,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_emergency_stop"] = custom_ibkr_emergency_stop

    @app.route("/api/custom/ibkr/recover", methods=["POST"])
    def custom_ibkr_recover() -> Response:
        payload, status_code = build_recover_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            emit_system_event=emit_system_event,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_recover"] = custom_ibkr_recover

    @app.route("/api/custom/ibkr/services/action", methods=["POST"])
    def custom_ibkr_service_action() -> Response:
        payload, status_code = build_service_action_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            inspect_runtime_environment=inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=build_runtime_environment_mismatch_payload,
            emit_system_event=emit_system_event,
            as_dict=as_dict,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_service_action"] = custom_ibkr_service_action

    @app.route("/api/custom/ibkr/reauth", methods=["POST"])
    def custom_ibkr_reauth() -> Response:
        payload, status_code = build_reauth_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            inspect_runtime_environment=inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=build_runtime_environment_mismatch_payload,
            as_dict=as_dict,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reauth"] = custom_ibkr_reauth

    return exports


__all__ = ["register_control_routes"]
