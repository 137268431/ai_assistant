from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.two_factor.runtime_actions import (
    build_two_factor_panic_reset_response,
    build_two_factor_probe_response,
    build_two_factor_takeover_response,
)
from ibkr_api.two_factor.request import build_two_factor_request_response
from ibkr_api.two_factor.respond import build_two_factor_respond_response
from ibkr_api.two_factor.result import build_two_factor_result_response


def register_two_factor_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    as_dict = deps["as_dict"]
    request_json_request = deps["request_json_request"]
    runtime_base_url = deps["runtime_base_url"]
    fetch_runtime_status = deps["fetch_runtime_status"]
    inspect_runtime_environment = deps["inspect_runtime_environment"]
    build_runtime_environment_mismatch_payload = deps["build_runtime_environment_mismatch_payload"]
    console_base_url = deps["console_base_url"]
    config_value = deps["config_value"]
    send_interactive = deps["feishu_send_interactive"]
    update_interactive = deps["feishu_update_interactive"]
    emit_system_event = deps.get("emit_system_event")
    merge_startup_steps = deps["merge_startup_steps"]
    deliver_startup_progress_card = deps["deliver_startup_progress_card"]
    exports: dict[str, Any] = {}

    def _console_base_url() -> str:
        value = console_base_url() if callable(console_base_url) else console_base_url
        return str(value or "").rstrip("/")

    @app.route("/api/custom/ibkr/2fa/request", methods=["POST"])
    def custom_ibkr_two_factor_request() -> Response:
        payload, status_code = build_two_factor_request_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            fetch_runtime_status=fetch_runtime_status,
            inspect_runtime_environment=inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=build_runtime_environment_mismatch_payload,
            console_base_url=_console_base_url(),
            config_value=config_value,
            send_interactive=send_interactive,
            update_interactive=update_interactive,
            emit_system_event=emit_system_event,
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=deliver_startup_progress_card,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_two_factor_request"] = custom_ibkr_two_factor_request

    @app.route("/api/custom/ibkr/2fa/result", methods=["POST"])
    def custom_ibkr_two_factor_result() -> Response:
        payload, status_code = build_two_factor_result_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            console_base_url=_console_base_url(),
            config_value=config_value,
            send_interactive=send_interactive,
            update_interactive=update_interactive,
            emit_system_event=emit_system_event,
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=deliver_startup_progress_card,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_two_factor_result"] = custom_ibkr_two_factor_result

    @app.route("/api/custom/ibkr/2fa/respond", methods=["POST"])
    def custom_ibkr_two_factor_respond() -> Response:
        payload, status_code = build_two_factor_respond_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            inspect_runtime_environment=inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=build_runtime_environment_mismatch_payload,
            console_base_url=_console_base_url(),
            config_value=config_value,
            send_interactive=send_interactive,
            update_interactive=update_interactive,
            merge_startup_steps=merge_startup_steps,
            deliver_startup_progress_card=deliver_startup_progress_card,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_two_factor_respond"] = custom_ibkr_two_factor_respond

    @app.route("/api/custom/ibkr/2fa/takeover", methods=["POST"])
    def custom_ibkr_two_factor_takeover() -> Response:
        payload, status_code = build_two_factor_takeover_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            fetch_runtime_status=fetch_runtime_status,
            inspect_runtime_environment=inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=build_runtime_environment_mismatch_payload,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_two_factor_takeover"] = custom_ibkr_two_factor_takeover

    @app.route("/api/custom/ibkr/2fa/probe", methods=["POST"])
    def custom_ibkr_two_factor_probe() -> Response:
        payload, status_code = build_two_factor_probe_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            fetch_runtime_status=fetch_runtime_status,
            inspect_runtime_environment=inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=build_runtime_environment_mismatch_payload,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_two_factor_probe"] = custom_ibkr_two_factor_probe

    @app.route("/api/custom/ibkr/2fa/panic-reset", methods=["POST"])
    def custom_ibkr_two_factor_panic_reset() -> Response:
        payload, status_code = build_two_factor_panic_reset_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            as_dict=as_dict,
            request_json_request=request_json_request,
            runtime_base_url=runtime_base_url,
            fetch_runtime_status=fetch_runtime_status,
            inspect_runtime_environment=inspect_runtime_environment,
            build_runtime_environment_mismatch_payload=build_runtime_environment_mismatch_payload,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_two_factor_panic_reset"] = custom_ibkr_two_factor_panic_reset

    return exports


__all__ = ["register_two_factor_routes"]
