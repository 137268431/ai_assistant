from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.modes import request_broker_mode
from ibkr_api.orders.cancel_sync import build_order_cancel_sync_response
from ibkr_api.orders.group_common import load_order_action_context
from ibkr_api.orders.values import to_text
from ibkr_api.signals.notifications import sync_signal_status_notification
from ibkr_api.signals.values import load_signal_record


def _clear_order_sensitive_read_caches() -> None:
    for import_path, function_name in (
        ("ibkr_api.account.routes", "_clear_account_route_cache"),
        ("ibkr_api.analytics.routes", "_clear_analytics_route_cache"),
        ("ibkr_api.home.routes", "_clear_home_route_cache"),
        ("ibkr_api.reverse.routes", "_clear_reverse_route_cache"),
        ("ibkr_api.signals.routes", "_clear_signal_sensitive_read_caches"),
        ("ibkr_api.universe.routes", "_clear_universe_route_cache"),
    ):
        try:
            module = __import__(import_path, fromlist=[function_name])
            clear_fn = getattr(module, function_name, None)
            if callable(clear_fn):
                if import_path in {"ibkr_api.account.routes", "ibkr_api.signals.routes"}:
                    clear_fn(preserve_orders_fast=True)
                else:
                    clear_fn()
        except Exception:
            pass


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
    notify_order_status = deps.get("notify_order_status")
    notify_order_callback_ledger = deps.get("notify_order_callback_ledger")
    signal_chat_id = deps.get("signal_chat_id")
    feishu_send_interactive = deps.get("feishu_send_interactive")
    feishu_update_interactive = deps.get("feishu_update_interactive")
    console_base_url = deps.get("console_base_url")
    exports: dict[str, Any] = {}

    def _notify_group_action(action: str, action_payload: dict[str, Any], result_payload: dict[str, Any], status_code: int) -> dict[str, Any]:
        if int(status_code or 0) >= 400 or bool((result_payload or {}).get("warning")) or not callable(notify_order_status):
            return {}
        environment = to_text((result_payload or {}).get("environment")) or request_broker_mode(action_payload)
        target_id = to_text(
            (result_payload or {}).get("target_id")
            or (result_payload or {}).get("trade_group_id")
            or (action_payload or {}).get("id")
            or (action_payload or {}).get("order_id")
        )
        if not target_id:
            return {}
        try:
            context = load_order_action_context(
                pb,
                payload={"id": target_id, "broker_mode": environment},
                environment=environment,
                escape_filter_string=escape_filter_string,
            )
            primary_row = context.get("primary_row") or context.get("action_row")
        except Exception:
            primary_row = None
            context = {}
        if not isinstance(primary_row, dict) or not primary_row.get("id"):
            return {}
        message = "交易组已平仓" if action == "closed" else "交易组已取消"
        try:
            return dict(
                notify_order_status(
                    action,
                    primary_row,
                    {
                        "message": message,
                        "message_id": to_text((primary_row.get("extra") or {}).get("feishu_order_message_id"))
                        if isinstance(primary_row.get("extra"), dict)
                        else "",
                        "related_rows": context.get("related_rows") or [],
                    },
                )
                or {}
            )
        except Exception as exc:
            return {"success": False, "error": str(exc), "skipped": True}

    def _notify_signal_group_cancel(result_payload: dict[str, Any], status_code: int) -> dict[str, Any]:
        signal_result = (result_payload or {}).get("signal_result")
        if int(status_code or 0) >= 400:
            return {}
        if not isinstance(signal_result, dict) or signal_result.get("status") != "cancelled":
            return {}
        signal_id = to_text(signal_result.get("signal_id"))
        data_environment = to_text(signal_result.get("data_environment")) or "live"
        broker_mode = to_text(signal_result.get("broker_mode")) or to_text((result_payload or {}).get("environment")) or "live"
        if not signal_id:
            return {}
        try:
            signal_row = load_signal_record(pb, signal_id, data_environment, escape_filter=escape_filter_string)
        except Exception:
            signal_row = None
        if not isinstance(signal_row, dict) or not signal_row.get("id"):
            return {}
        try:
            notify_result = sync_signal_status_notification(
                {**signal_row, "status": "cancelled", "broker_mode": broker_mode, "data_environment": data_environment},
                action="cancelled",
                message="订单已取消，关联信号已同步取消",
                send_interactive=feishu_send_interactive,
                signal_chat_id=to_text(signal_chat_id(broker_mode)) if callable(signal_chat_id) else "",
                update_interactive=feishu_update_interactive,
                console_base_url=to_text(console_base_url()) if callable(console_base_url) else "",
            )
        except Exception as exc:
            return {"success": False, "error": str(exc), "skipped": True}
        extra_patch = notify_result.get("extra_patch") if isinstance(notify_result, dict) else None
        if isinstance(extra_patch, dict) and extra_patch:
            try:
                pb.update_record("ibkr_signals", to_text(signal_row.get("id")), {"extra": extra_patch})
            except Exception as exc:
                notify_result = {**dict(notify_result or {}), "patch_error": str(exc)}
        return dict(notify_result or {})

    @app.route("/api/custom/ibkr/orders/upsert", methods=["POST"])
    def custom_ibkr_orders_upsert() -> Response:
        payload, status_code = build_order_upsert_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            notify_order_status=notify_order_status,
            notify_order_callback_ledger=notify_order_callback_ledger,
        )
        if status_code < 400:
            _clear_order_sensitive_read_caches()
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
        if status_code < 400:
            _clear_order_sensitive_read_caches()
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_orders_reconcile"] = custom_ibkr_orders_reconcile

    @app.route("/api/custom/ibkr/orders/cancel_group", methods=["POST"])
    def custom_ibkr_orders_cancel_group() -> Response:
        request_payload = request.get_json(silent=True) or {}
        payload, status_code = build_order_cancel_group_response(
            pb,
            payload=request_payload,
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=cancel_broker_order,
        )
        notification = _notify_group_action("canceled", request_payload, payload, status_code)
        if notification:
            payload["notification"] = notification
        signal_notification = _notify_signal_group_cancel(payload, status_code)
        if signal_notification:
            payload["signal_notification"] = signal_notification
        if status_code < 400:
            _clear_order_sensitive_read_caches()
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_orders_cancel_group"] = custom_ibkr_orders_cancel_group

    @app.route("/api/custom/ibkr/orders/cancel_sync", methods=["POST"])
    def custom_ibkr_orders_cancel_sync() -> Response:
        request_payload = request.get_json(silent=True) or {}
        payload, status_code = build_order_cancel_sync_response(
            pb,
            payload=request_payload,
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=cancel_broker_order,
        )
        notification = _notify_group_action("canceled", request_payload, payload, status_code)
        if notification:
            payload["notification"] = notification
        signal_notification = _notify_signal_group_cancel(payload, status_code)
        if signal_notification:
            payload["signal_notification"] = signal_notification
        if status_code < 400:
            _clear_order_sensitive_read_caches()
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_orders_cancel_sync"] = custom_ibkr_orders_cancel_sync

    @app.route("/api/custom/ibkr/orders/close_group", methods=["POST"])
    def custom_ibkr_orders_close_group() -> Response:
        request_payload = request.get_json(silent=True) or {}
        payload, status_code = build_order_close_group_response(
            pb,
            payload=request_payload,
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
        )
        notification = _notify_group_action("closed", request_payload, payload, status_code)
        if notification:
            payload["notification"] = notification
        if status_code < 400:
            _clear_order_sensitive_read_caches()
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_orders_close_group"] = custom_ibkr_orders_close_group

    @app.route("/webhook/order/cancel", methods=["GET"])
    def webhook_order_cancel() -> Response:
        broker_mode = request.args.get("broker_mode") or request.args.get("environment") or ""
        action_payload = {
            "id": request.args.get("id") or "",
            "broker_mode": broker_mode,
        }
        payload, status_code = build_order_cancel_webhook_response(
            pb,
            payload=action_payload,
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            cancel_broker_order=cancel_broker_order,
        )
        _notify_group_action("canceled", action_payload, payload, status_code)
        _notify_signal_group_cancel(payload, status_code)
        if status_code < 400:
            _clear_order_sensitive_read_caches()
        return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}
    exports["webhook_order_cancel"] = webhook_order_cancel

    @app.route("/webhook/order/close", methods=["GET"])
    def webhook_order_close() -> Response:
        broker_mode = request.args.get("broker_mode") or request.args.get("environment") or ""
        action_payload = {
            "id": request.args.get("id") or "",
            "broker_mode": broker_mode,
        }
        payload, status_code = build_order_close_webhook_response(
            pb,
            payload=action_payload,
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
        )
        _notify_group_action("closed", action_payload, payload, status_code)
        if status_code < 400:
            _clear_order_sensitive_read_caches()
        return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}
    exports["webhook_order_close"] = webhook_order_close

    return exports


__all__ = ["register_order_routes"]
