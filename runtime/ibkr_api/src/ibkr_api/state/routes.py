from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.state.orders import build_order_state_get_response, build_order_state_upsert_response
from ibkr_api.state.signals import build_signal_state_get_response, build_signal_state_upsert_response


def register_state_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    normalize_environment = deps["normalize_environment"]
    get_state_payload = deps["get_state_payload"]
    upsert_state = deps["upsert_state"]
    as_dict = deps["as_dict"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/state/signals", methods=["GET"])
    def custom_ibkr_state_signals_get() -> Response:
        payload, status_code = build_signal_state_get_response(
            date_str=request.args.get("date") or "",
            environment=request.args.get("environment"),
            normalize_environment=normalize_environment,
            get_state_payload=get_state_payload,
            as_dict=as_dict,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_state_signals_get"] = custom_ibkr_state_signals_get

    @app.route("/api/custom/ibkr/state/signals", methods=["POST"])
    def custom_ibkr_state_signals_post() -> Response:
        payload, status_code = build_signal_state_upsert_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            upsert_state=upsert_state,
            as_dict=as_dict,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_state_signals_post"] = custom_ibkr_state_signals_post

    @app.route("/api/custom/ibkr/state/orders", methods=["GET"])
    def custom_ibkr_state_orders_get() -> Response:
        payload, status_code = build_order_state_get_response(
            date_str=request.args.get("date") or "",
            environment=request.args.get("environment"),
            normalize_environment=normalize_environment,
            get_state_payload=get_state_payload,
            as_dict=as_dict,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_state_orders_get"] = custom_ibkr_state_orders_get

    @app.route("/api/custom/ibkr/state/orders", methods=["POST"])
    def custom_ibkr_state_orders_post() -> Response:
        payload, status_code = build_order_state_upsert_response(
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            upsert_state=upsert_state,
            as_dict=as_dict,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_state_orders_post"] = custom_ibkr_state_orders_post

    return exports


__all__ = ["register_state_routes"]
