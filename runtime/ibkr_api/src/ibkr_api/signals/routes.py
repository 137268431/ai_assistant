from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.app_core.route_cache import RouteSWRCache, cache_seconds, canonical_cache_key, request_cache_bypass
from ibkr_api.modes import request_broker_mode, request_market_data_mode


_SIGNAL_ROUTE_CACHE = RouteSWRCache("signals")


def _clear_signal_sensitive_read_caches() -> None:
    _SIGNAL_ROUTE_CACHE.clear()
    for import_path, function_name in (
        ("ibkr_api.account.routes", "_clear_account_route_cache"),
        ("ibkr_api.reverse.routes", "_clear_reverse_route_cache"),
        ("ibkr_api.universe.routes", "_clear_universe_route_cache"),
    ):
        try:
            module = __import__(import_path, fromlist=[function_name])
            clear_fn = getattr(module, function_name, None)
            if callable(clear_fn):
                clear_fn()
        except Exception:
            pass


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
    notify_order_callback_ledger = deps.get("notify_order_callback_ledger")
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
        if status_code < 400:
            _clear_signal_sensitive_read_caches()
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
        if status_code < 400:
            _clear_signal_sensitive_read_caches()
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_signals"] = custom_ibkr_signals

    @app.route("/api/custom/ibkr/signals/pending", methods=["GET"])
    def custom_ibkr_signals_pending() -> Response:
        query_payload = request.args.to_dict(flat=True)
        mode_payload = {
            "broker_mode": query_payload.get("broker_mode") or query_payload.get("environment"),
            "market_data_mode": query_payload.get("market_data_mode"),
            "data_environment": query_payload.get("data_environment"),
        }
        payload, status_code = _SIGNAL_ROUTE_CACHE.get(
            canonical_cache_key("signals_pending", query_payload),
            builder=lambda: build_signals_pending_response(
                pb,
                environment=request_broker_mode(mode_payload),
                data_environment=request_market_data_mode(mode_payload),
                date_str=query_payload.get("date") or "",
                normalize_environment=normalize_environment,
                escape_filter_string=escape_filter_string,
                as_dict=as_dict,
            ),
            ttl_seconds=cache_seconds("IBKR_ROUTE_CACHE_SIGNALS_PENDING_TTL_SEC", 30.0),
            stale_seconds=cache_seconds("IBKR_ROUTE_CACHE_SIGNALS_PENDING_STALE_SEC", 120.0),
            force=request_cache_bypass(query_payload),
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
            notify_order_callback_ledger=notify_order_callback_ledger,
        )
        if status_code < 400:
            _clear_signal_sensitive_read_caches()
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
        if status_code < 400:
            _clear_signal_sensitive_read_caches()
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
        if status_code < 400:
            _clear_signal_sensitive_read_caches()
        return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}
    exports["webhook_signal_cancel"] = webhook_signal_cancel

    return exports


__all__ = ["register_signal_routes", "_clear_signal_sensitive_read_caches"]
