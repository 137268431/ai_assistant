from __future__ import annotations

from typing import Any


def build_trading_route_deps(
    *,
    globals_dict: dict[str, Any],
    pb: Any,
    runtime_base_url: str,
) -> dict[str, Any]:
    return {
        "pb": pb,
        "normalize_environment": globals_dict["_normalize_environment"],
        "escape_filter_string": globals_dict["_escape_filter_string"],
        "as_dict": globals_dict["_as_dict"],
        "console_base_url": globals_dict["_console_base_url"],
        "signal_chat_id": globals_dict["_signal_chat_id"],
        "order_chat_id": globals_dict["_order_chat_id"],
        "notify_order_status": globals_dict["_notify_order_status"],
        "feishu_send_interactive": globals_dict["_feishu_send_interactive"],
        "feishu_update_interactive": globals_dict["_feishu_update_interactive"],
        "cancel_broker_order": lambda environment, order_id, payload=None: globals_dict["_cancel_broker_order_via_runtime"](
            environment,
            order_id,
            payload,
        ),
        "build_signal_ingest_response": lambda *args, **kwargs: globals_dict["build_signal_ingest_response"](*args, **kwargs),
        "build_signals_ingest_response": lambda *args, **kwargs: globals_dict["build_signals_ingest_response"](*args, **kwargs),
        "build_signals_pending_response": lambda *args, **kwargs: globals_dict["build_signals_pending_response"](*args, **kwargs),
        "build_signals_ack_response": lambda *args, **kwargs: globals_dict["build_signals_ack_response"](*args, **kwargs),
        "build_order_upsert_response": lambda *args, **kwargs: globals_dict["build_order_upsert_response"](*args, **kwargs),
        "build_signal_confirm_webhook_response": lambda *args, **kwargs: globals_dict["build_signal_confirm_webhook_response"](
            *args,
            **kwargs,
        ),
        "build_signal_cancel_webhook_response": lambda *args, **kwargs: globals_dict["build_signal_cancel_webhook_response"](
            *args,
            **kwargs,
        ),
        "config_value": lambda key, default, environment: globals_dict["_config_value"](key, default, environment),
        "parse_boolean": globals_dict["_parse_boolean"],
        "get_state_payload": lambda state_key, environment, date="global": globals_dict["_get_state_payload"](
            state_key,
            environment,
            date=date,
        ),
        "upsert_state": lambda state_key, environment, data, date="global": pb.upsert_state(state_key, environment, data, date=date),
        "build_orders_reconcile_response": lambda *args, **kwargs: globals_dict["build_orders_reconcile_response"](*args, **kwargs),
        "build_order_cancel_sync_response": lambda *args, **kwargs: globals_dict["build_order_cancel_sync_response"](*args, **kwargs),
        "build_order_cancel_group_response": lambda *args, **kwargs: globals_dict["build_order_cancel_group_response"](*args, **kwargs),
        "build_order_close_group_response": lambda *args, **kwargs: globals_dict["build_order_close_group_response"](*args, **kwargs),
        "build_order_cancel_webhook_response": lambda *args, **kwargs: globals_dict["build_order_cancel_webhook_response"](
            *args,
            **kwargs,
        ),
        "build_order_close_webhook_response": lambda *args, **kwargs: globals_dict["build_order_close_webhook_response"](
            *args,
            **kwargs,
        ),
        "build_reverse_list_response": lambda *args, **kwargs: globals_dict["build_reverse_list_response"](*args, **kwargs),
        "build_reverse_calculate_response": lambda *args, **kwargs: globals_dict["build_reverse_calculate_response"](
            *args,
            **kwargs,
        ),
        "build_reverse_pending_response": lambda *args, **kwargs: globals_dict["build_reverse_pending_response"](*args, **kwargs),
        "build_reverse_dispatch_response": lambda *args, **kwargs: globals_dict["build_reverse_dispatch_response"](
            *args,
            **kwargs,
        ),
        "build_reverse_ack_response": lambda *args, **kwargs: globals_dict["build_reverse_ack_response"](*args, **kwargs),
        "upsert_tv_indicator": lambda payload: globals_dict["_upsert_tv_indicator"](payload),
        "upsert_tv_signal": lambda payload: globals_dict["_upsert_tv_signal"](payload),
        "request_json_request": lambda method, base_url, path, params=None, json_body=None, timeout=5.0: globals_dict[
            "_request_json_request"
        ](
            method,
            base_url,
            path,
            params=params,
            json_body=json_body,
            timeout=timeout,
        ),
        "runtime_base_url": runtime_base_url,
        "inspect_runtime_environment": lambda environment: globals_dict["inspect_requested_runtime_environment"](
            environment,
            normalize_environment=globals_dict["_normalize_environment"],
            fetch_runtime_status=globals_dict["_fetch_runtime_status"],
            as_dict=globals_dict["_as_dict"],
        ),
        "build_runtime_environment_mismatch_payload": globals_dict["build_runtime_environment_mismatch_payload"],
        "emit_system_event": globals_dict["_emit_system_event"],
        "fetch_runtime_status": lambda environment: globals_dict["_fetch_runtime_status"](environment),
        "merge_startup_steps": globals_dict["_merge_startup_steps"],
        "deliver_startup_progress_card": lambda state, environment: globals_dict["_deliver_startup_progress_card"](
            state,
            environment,
        ),
        "handle_feishu_callback": globals_dict["_handle_feishu_callback_support"],
        "dispatch_feishu_2fa_callback": lambda action, environment: globals_dict["_dispatch_feishu_2fa_callback"](
            action,
            environment,
        ),
        "dispatch_feishu_order_callback": lambda action, order_id, environment, data_environment="": globals_dict[
            "_dispatch_feishu_order_callback"
        ](action, order_id, environment, data_environment),
        "dispatch_feishu_signal_callback": lambda action, signal_id, environment, data_environment="": globals_dict[
            "_dispatch_feishu_signal_callback"
        ](action, signal_id, environment, data_environment),
        "callback_toast": globals_dict["_callback_toast"],
        "callback_response": globals_dict["_feishu_callback_response"],
    }


__all__ = ["build_trading_route_deps"]
