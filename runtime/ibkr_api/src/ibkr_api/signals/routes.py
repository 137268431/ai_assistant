from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.modes import request_broker_mode, request_market_data_mode


def register_signal_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    escape_filter_string = deps["escape_filter_string"]
    as_dict = deps["as_dict"]
    console_base_url = deps["console_base_url"]
    signal_chat_id = deps["signal_chat_id"]
    feishu_send_interactive = deps["feishu_send_interactive"]
    feishu_update_interactive = deps["feishu_update_interactive"]
    cancel_broker_order = deps["cancel_broker_order"]
    build_signal_ingest_response = deps["build_signal_ingest_response"]
    build_signals_ingest_response = deps["build_signals_ingest_response"]
    build_signals_pending_response = deps["build_signals_pending_response"]
    build_signals_ack_response = deps["build_signals_ack_response"]
    build_order_upsert_response = deps["build_order_upsert_response"]
    build_signal_confirm_webhook_response = deps["build_signal_confirm_webhook_response"]
    build_signal_cancel_webhook_response = deps["build_signal_cancel_webhook_response"]
    config_value = deps["config_value"]
    notify_order_status = deps.get("notify_order_status")
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/signal", methods=["POST"])
    def custom_ibkr_signal() -> Response:
        payload, status_code = build_signal_ingest_response(
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
    exports["custom_ibkr_signal"] = custom_ibkr_signal

    @app.route("/api/custom/ibkr/signals", methods=["POST"])
    def custom_ibkr_signals() -> Response:
        payload, status_code = build_signals_ingest_response(
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
    exports["custom_ibkr_signals"] = custom_ibkr_signals

    @app.route("/api/custom/ibkr/signals/pending", methods=["GET"])
    def custom_ibkr_signals_pending() -> Response:
        mode_payload = {
            "broker_mode": request.args.get("broker_mode") or request.args.get("environment"),
            "market_data_mode": request.args.get("market_data_mode"),
            "data_environment": request.args.get("data_environment"),
        }
        payload, status_code = build_signals_pending_response(
            pb,
            environment=request_broker_mode(mode_payload),
            data_environment=request_market_data_mode(mode_payload),
            date_str=request.args.get("date") or "",
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            as_dict=as_dict,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_signals_pending"] = custom_ibkr_signals_pending

    @app.route("/api/custom/ibkr/signals/ack", methods=["POST"])
    def custom_ibkr_signals_ack() -> Response:
        payload, status_code = build_signals_ack_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            order_upsert_builder=build_order_upsert_response,
            send_interactive=feishu_send_interactive,
            update_interactive=feishu_update_interactive,
            signal_chat_id_fn=signal_chat_id,
            console_base_url=console_base_url(),
            notify_order_status=notify_order_status,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_signals_ack"] = custom_ibkr_signals_ack

    @app.route("/webhook/signal/confirm", methods=["GET"])
    def webhook_signal_confirm() -> Response:
        payload, status_code = build_signal_confirm_webhook_response(
            pb,
            payload={
                "id": request.args.get("id") or "",
                "environment": request.args.get("environment") or "",
                "broker_mode": request.args.get("broker_mode") or "",
                "market_data_mode": request.args.get("market_data_mode") or "",
                "data_environment": request.args.get("data_environment") or "",
            },
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            update_signal_card=feishu_update_interactive,
            console_base_url=console_base_url(),
            config_value=config_value,
        )
        return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}
    exports["webhook_signal_confirm"] = webhook_signal_confirm

    @app.route("/webhook/signal/cancel", methods=["GET"])
    def webhook_signal_cancel() -> Response:
        payload, status_code = build_signal_cancel_webhook_response(
            pb,
            payload={
                "id": request.args.get("id") or "",
                "environment": request.args.get("environment") or "",
                "broker_mode": request.args.get("broker_mode") or "",
                "market_data_mode": request.args.get("market_data_mode") or "",
                "data_environment": request.args.get("data_environment") or "",
            },
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=cancel_broker_order,
            notify_order_status=notify_order_status,
            update_signal_card=feishu_update_interactive,
            console_base_url=console_base_url(),
        )
        return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}
    exports["webhook_signal_cancel"] = webhook_signal_cancel

    return exports


__all__ = ["register_signal_routes"]
