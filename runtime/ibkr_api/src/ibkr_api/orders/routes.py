from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.orders.cancel_sync import build_order_cancel_sync_response


def register_order_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    escape_filter_string = deps["escape_filter_string"]
    cancel_broker_order = deps["cancel_broker_order"]
    build_order_upsert_response = deps["build_order_upsert_response"]
    build_orders_reconcile_response = deps["build_orders_reconcile_response"]
    build_order_cancel_sync_response = deps["build_order_cancel_sync_response"]
    build_order_cancel_group_response = deps["build_order_cancel_group_response"]
    build_order_close_group_response = deps["build_order_close_group_response"]
    build_order_cancel_webhook_response = deps["build_order_cancel_webhook_response"]
    build_order_close_webhook_response = deps["build_order_close_webhook_response"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/orders/upsert", methods=["POST"])
    def custom_ibkr_orders_upsert() -> Response:
        payload, status_code = build_order_upsert_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_orders_upsert"] = custom_ibkr_orders_upsert

    @app.route("/api/custom/ibkr/orders/reconcile", methods=["POST"])
    def custom_ibkr_orders_reconcile() -> Response:
        payload, status_code = build_orders_reconcile_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_orders_reconcile"] = custom_ibkr_orders_reconcile

    @app.route("/api/custom/ibkr/orders/cancel_group", methods=["POST"])
    def custom_ibkr_orders_cancel_group() -> Response:
        payload, status_code = build_order_cancel_group_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=cancel_broker_order,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_orders_cancel_group"] = custom_ibkr_orders_cancel_group

    @app.route("/api/custom/ibkr/orders/cancel_sync", methods=["POST"])
    def custom_ibkr_orders_cancel_sync() -> Response:
        payload, status_code = build_order_cancel_sync_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=cancel_broker_order,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_orders_cancel_sync"] = custom_ibkr_orders_cancel_sync

    @app.route("/api/custom/ibkr/orders/close_group", methods=["POST"])
    def custom_ibkr_orders_close_group() -> Response:
        payload, status_code = build_order_close_group_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_orders_close_group"] = custom_ibkr_orders_close_group

    @app.route("/webhook/order/cancel", methods=["GET"])
    def webhook_order_cancel() -> Response:
        payload, status_code = build_order_cancel_webhook_response(
            pb,
            payload={
                "id": request.args.get("id") or "",
                "environment": request.args.get("environment") or "",
            },
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=cancel_broker_order,
        )
        return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}
    exports["webhook_order_cancel"] = webhook_order_cancel

    @app.route("/webhook/order/close", methods=["GET"])
    def webhook_order_close() -> Response:
        payload, status_code = build_order_close_webhook_response(
            pb,
            payload={
                "id": request.args.get("id") or "",
                "environment": request.args.get("environment") or "",
            },
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
        )
        return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}
    exports["webhook_order_close"] = webhook_order_close

    return exports


__all__ = ["register_order_routes"]
