from __future__ import annotations

from typing import Any

from ibkr_api.callbacks.routes import register_callback_routes
from ibkr_api.control.routes import register_control_routes
from ibkr_api.orders.routes import register_order_routes
from ibkr_api.reverse.routes import register_reverse_routes
from ibkr_api.signals.routes import register_signal_routes
from ibkr_api.state.routes import register_state_routes
from ibkr_api.tradingview.routes import register_tradingview_routes
from ibkr_api.two_factor.routes import register_two_factor_routes


def register_trading_routes(app, deps: dict[str, Any]) -> dict[str, Any]:
    exports: dict[str, Any] = {}

    exports.update(
        register_signal_routes(
            app,
            deps={
                "pb": deps["pb"],
                "normalize_environment": deps["normalize_environment"],
                "escape_filter_string": deps["escape_filter_string"],
                "as_dict": deps["as_dict"],
                "console_base_url": deps["console_base_url"],
                "signal_chat_id": deps["signal_chat_id"],
                "notify_order_status": deps["notify_order_status"],
                "feishu_send_interactive": deps["feishu_send_interactive"],
                "feishu_update_interactive": deps["feishu_update_interactive"],
                "cancel_broker_order": deps["cancel_broker_order"],
                "build_signal_ingest_response": deps["build_signal_ingest_response"],
                "build_signals_ingest_response": deps["build_signals_ingest_response"],
                "build_signals_pending_response": deps["build_signals_pending_response"],
                "build_signals_ack_response": deps["build_signals_ack_response"],
                "build_order_upsert_response": deps["build_order_upsert_response"],
                "build_signal_confirm_webhook_response": deps["build_signal_confirm_webhook_response"],
                "build_signal_cancel_webhook_response": deps["build_signal_cancel_webhook_response"],
                "config_value": deps["config_value"],
            },
        )
    )

    exports.update(
        register_state_routes(
            app,
            deps={
                "normalize_environment": deps["normalize_environment"],
                "get_state_payload": deps["get_state_payload"],
                "upsert_state": deps["upsert_state"],
                "as_dict": deps["as_dict"],
            },
        )
    )

    exports.update(
        register_order_routes(
            app,
            deps={
                "pb": deps["pb"],
                "normalize_environment": deps["normalize_environment"],
                "escape_filter_string": deps["escape_filter_string"],
                "cancel_broker_order": deps["cancel_broker_order"],
                "build_order_upsert_response": deps["build_order_upsert_response"],
                "build_orders_reconcile_response": deps["build_orders_reconcile_response"],
                "build_order_cancel_sync_response": deps["build_order_cancel_sync_response"],
                "build_order_cancel_group_response": deps["build_order_cancel_group_response"],
                "build_order_close_group_response": deps["build_order_close_group_response"],
                "build_order_cancel_webhook_response": deps["build_order_cancel_webhook_response"],
                "build_order_close_webhook_response": deps["build_order_close_webhook_response"],
                "notify_order_status": deps["notify_order_status"],
            },
        )
    )

    exports.update(
        register_reverse_routes(
            app,
            deps={
                "pb": deps["pb"],
                "normalize_environment": deps["normalize_environment"],
                "escape_filter_string": deps["escape_filter_string"],
                "build_reverse_list_response": deps["build_reverse_list_response"],
                "build_reverse_calculate_response": deps["build_reverse_calculate_response"],
                "build_reverse_pending_response": deps["build_reverse_pending_response"],
                "build_reverse_dispatch_response": deps["build_reverse_dispatch_response"],
                "build_reverse_ack_response": deps["build_reverse_ack_response"],
            },
        )
    )

    exports.update(
        register_tradingview_routes(
            app,
            deps={
                "upsert_tv_indicator": deps["upsert_tv_indicator"],
                "upsert_tv_signal": deps["upsert_tv_signal"],
                "config_value": deps["config_value"],
                "normalize_environment": deps["normalize_environment"],
                "parse_boolean": deps["parse_boolean"],
            },
        )
    )

    exports.update(
        register_control_routes(
            app,
            deps={
                "pb": deps["pb"],
                "normalize_environment": deps["normalize_environment"],
                "escape_filter_string": deps["escape_filter_string"],
                "request_json_request": deps["request_json_request"],
                "runtime_base_url": deps["runtime_base_url"],
                "inspect_runtime_environment": deps["inspect_runtime_environment"],
                "build_runtime_environment_mismatch_payload": deps["build_runtime_environment_mismatch_payload"],
                "emit_system_event": deps["emit_system_event"],
                "as_dict": deps["as_dict"],
                "fetch_runtime_status": deps["fetch_runtime_status"],
                "get_state_payload": deps["get_state_payload"],
                "ibkr_2fa_state_key": deps["ibkr_2fa_state_key"],
                "ibkr_2fa_state_date": deps["ibkr_2fa_state_date"],
            },
        )
    )

    exports.update(
        register_two_factor_routes(
            app,
            deps={
                "pb": deps["pb"],
                "normalize_environment": deps["normalize_environment"],
                "as_dict": deps["as_dict"],
                "request_json_request": deps["request_json_request"],
                "runtime_base_url": deps["runtime_base_url"],
                "fetch_runtime_status": deps["fetch_runtime_status"],
                "inspect_runtime_environment": deps["inspect_runtime_environment"],
                "build_runtime_environment_mismatch_payload": deps["build_runtime_environment_mismatch_payload"],
                "console_base_url": deps["console_base_url"],
                "config_value": deps["config_value"],
                "feishu_send_interactive": deps["feishu_send_interactive"],
                "feishu_update_interactive": deps["feishu_update_interactive"],
                "emit_system_event": deps["emit_system_event"],
                "merge_startup_steps": deps["merge_startup_steps"],
                "deliver_startup_progress_card": deps["deliver_startup_progress_card"],
            },
        )
    )

    exports.update(
        register_callback_routes(
            app,
            deps={
                "handle_feishu_callback": deps["handle_feishu_callback"],
                "as_dict": deps["as_dict"],
                "normalize_environment": deps["normalize_environment"],
                "dispatch_feishu_2fa_callback": deps["dispatch_feishu_2fa_callback"],
                "dispatch_feishu_order_callback": deps["dispatch_feishu_order_callback"],
                "dispatch_feishu_signal_callback": deps["dispatch_feishu_signal_callback"],
                "callback_toast": deps["callback_toast"],
                "callback_response": deps["callback_response"],
            },
        )
    )

    return exports
