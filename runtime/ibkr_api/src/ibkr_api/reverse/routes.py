from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request


def register_reverse_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    escape_filter_string = deps["escape_filter_string"]
    build_reverse_list_response = deps["build_reverse_list_response"]
    build_reverse_calculate_response = deps["build_reverse_calculate_response"]
    build_reverse_pending_response = deps["build_reverse_pending_response"]
    build_reverse_dispatch_response = deps["build_reverse_dispatch_response"]
    build_reverse_ack_response = deps["build_reverse_ack_response"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/reverse/list", methods=["GET"])
    def custom_ibkr_reverse_list() -> Response:
        payload, status_code = build_reverse_list_response(
            pb,
            environment=request.args.get("environment"),
            date_str=request.args.get("date") or "",
            symbol=request.args.get("symbol") or "",
            statuses=request.args.get("status") or "",
            limit=request.args.get("limit"),
            normalize_environment=normalize_environment,
            escape_filter=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reverse_list"] = custom_ibkr_reverse_list

    @app.route("/api/custom/ibkr/reverse/calculate", methods=["POST"])
    def custom_ibkr_reverse_calculate() -> Response:
        payload, status_code = build_reverse_calculate_response(
            pb,
            payload=request.get_json(silent=True) or {},
            escape_filter=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reverse_calculate"] = custom_ibkr_reverse_calculate

    @app.route("/api/custom/ibkr/reverse/pending", methods=["GET"])
    def custom_ibkr_reverse_pending() -> Response:
        payload, status_code = build_reverse_pending_response(
            pb,
            environment=request.args.get("environment"),
            limit=request.args.get("limit"),
            normalize_environment=normalize_environment,
            escape_filter=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reverse_pending"] = custom_ibkr_reverse_pending

    @app.route("/api/custom/ibkr/reverse/dispatch", methods=["POST"])
    def custom_ibkr_reverse_dispatch() -> Response:
        payload, status_code = build_reverse_dispatch_response(
            pb,
            payload=request.get_json(silent=True) or {},
            escape_filter=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reverse_dispatch"] = custom_ibkr_reverse_dispatch

    @app.route("/api/custom/ibkr/reverse/ack", methods=["POST"])
    def custom_ibkr_reverse_ack() -> Response:
        payload, status_code = build_reverse_ack_response(
            pb,
            payload=request.get_json(silent=True) or {},
            escape_filter=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reverse_ack"] = custom_ibkr_reverse_ack

    return exports


__all__ = ["register_reverse_routes"]
