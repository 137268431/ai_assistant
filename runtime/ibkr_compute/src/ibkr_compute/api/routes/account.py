from __future__ import annotations

from flask import jsonify

from ibkr_compute.api.account.views import (
    _build_ibkr_account_snapshot,
    _build_ibkr_cancel_all_orders_response,
    _build_ibkr_cancel_order_response,
    _build_ibkr_close_position_response,
    _build_ibkr_modify_order_response,
    _build_ibkr_order_history,
    _build_ibkr_place_order_response,
)
from ibkr_compute.api.route_request import get_query_arg_int
from ibkr_compute.api.route_response import build_json_request_response, json_response
from ibkr_compute.api.route_runtime import require_ibkr_service


def _resolve_account_service():
    _, service, unavailable = require_ibkr_service()
    if unavailable:
        return None, unavailable
    return service, None


def _build_account_action_response(builder):
    service, unavailable = _resolve_account_service()
    if unavailable:
        return unavailable
    return build_json_request_response(lambda payload: builder(service, payload))


def register_account_routes(app):
    @app.route("/ibkr/account", methods=["GET"])
    def ibkr_account():
        service, unavailable = _resolve_account_service()
        if unavailable:
            return unavailable
        try:
            return jsonify(_build_ibkr_account_snapshot(service))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/ibkr/positions", methods=["GET"])
    def ibkr_positions():
        service, unavailable = _resolve_account_service()
        if unavailable:
            return unavailable
        snapshot = _build_ibkr_account_snapshot(service)
        return jsonify(
            {
                "ok": True,
                "environment": snapshot.get("environment"),
                "account_id": snapshot.get("account_id"),
                "positions": snapshot.get("positions", []),
                "count": snapshot.get("counts", {}).get("positions", 0),
                "fetched_at": snapshot.get("fetched_at"),
            }
        )

    @app.route("/ibkr/orders/live", methods=["GET"])
    def ibkr_live_orders():
        service, unavailable = _resolve_account_service()
        if unavailable:
            return unavailable
        snapshot = _build_ibkr_account_snapshot(service)
        return jsonify(
            {
                "ok": True,
                "environment": snapshot.get("environment"),
                "account_id": snapshot.get("account_id"),
                "orders": snapshot.get("orders", []),
                "count": snapshot.get("counts", {}).get("orders", 0),
                "open_count": snapshot.get("counts", {}).get("open_orders", 0),
                "fetched_at": snapshot.get("fetched_at"),
            }
        )

    @app.route("/ibkr/orders/history", methods=["GET"])
    def ibkr_order_history():
        service, unavailable = _resolve_account_service()
        if unavailable:
            return unavailable

        requested_days = get_query_arg_int("days", 1, minimum=1)
        payload = _build_ibkr_order_history(service, requested_days=requested_days)
        if payload.get("ok"):
            return jsonify(payload)
        error_text = str(payload.get("error") or (payload.get("errors") or {}).get("broker") or "").strip().lower()
        status_code = 409 if "not authenticated" in error_text else 502
        return json_response(payload, status_code)

    @app.route("/ibkr/orders/cancel", methods=["POST"])
    def ibkr_cancel_order():
        return _build_account_action_response(_build_ibkr_cancel_order_response)

    @app.route("/ibkr/orders/cancel_all", methods=["POST"])
    def ibkr_cancel_all_orders():
        return _build_account_action_response(_build_ibkr_cancel_all_orders_response)

    @app.route("/ibkr/orders/modify", methods=["POST"])
    def ibkr_modify_order():
        return _build_account_action_response(_build_ibkr_modify_order_response)

    @app.route("/ibkr/orders/place", methods=["POST"])
    def ibkr_place_order():
        return _build_account_action_response(_build_ibkr_place_order_response)

    @app.route("/ibkr/positions/close", methods=["POST"])
    def ibkr_close_position():
        return _build_account_action_response(_build_ibkr_close_position_response)
