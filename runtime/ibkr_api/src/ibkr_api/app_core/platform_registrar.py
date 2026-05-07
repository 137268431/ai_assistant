from __future__ import annotations

from typing import Any

from ibkr_api.account.routes import register_account_routes
from ibkr_api.data_quality.routes import register_data_quality_routes
from ibkr_api.runtime.routes import register_runtime_routes
from ibkr_api.startup.routes import register_startup_routes
from ibkr_api.storage.routes import register_storage_routes
from ibkr_api.system.routes import register_system_routes
from ibkr_api.universe.routes import register_universe_routes


def register_platform_routes(app, deps: dict[str, Any]) -> dict[str, Any]:
    exports: dict[str, Any] = {}

    exports.update(
        register_system_routes(
            app,
            deps={
                "pb": deps["pb"],
                "config": deps["config"],
                "normalize_environment": deps["normalize_environment"],
                "parse_boolean": deps["parse_boolean"],
                "escape_filter_string": deps["escape_filter_string"],
                "config_value": deps["config_value"],
                "signal_chat_id": deps["signal_chat_id"],
                "console_base_url": deps["console_base_url"],
                "feishu_send_interactive": deps["feishu_send_interactive"],
                "feishu_update_interactive": deps["feishu_update_interactive"],
                "build_system_summary_payload": deps["build_system_summary_payload"],
                "build_system_monitor_payload": deps["build_system_monitor_payload"],
                "collect_storage_health": deps["collect_storage_health"],
                "compute_base_url": deps["compute_base_url"],
                "request_two_factor_approval": deps["request_two_factor_approval"],
                "emit_system_event": deps["emit_system_event"],
                "label_title_with_environment": deps["label_title_with_environment"],
                "add_environment_to_detail": deps["add_environment_to_detail"],
                "write_system_event_record": deps["write_system_event_record"],
                "deliver_system_event_notification": deps["deliver_system_event_notification"],
                "build_cron_payload": deps["build_cron_payload"],
                "scheduler_status": deps["scheduler_status"],
                "build_scheduler_summary": deps["build_scheduler_summary"],
                "augment_scheduler_summary": deps["augment_scheduler_summary"],
                "build_service_topology": deps["build_service_topology"],
                "request_json_request": deps["request_json_request"],
                "scheduler_base_url": deps["scheduler_base_url"],
                "time_strings": deps["time_strings"],
                "get_state_payload": deps["get_state_payload"],
                "normalize_two_factor_state_with_runtime": deps["normalize_two_factor_state_with_runtime"],
                "fetch_runtime_status": deps["fetch_runtime_status"],
                "build_signal_expiry_response": deps["build_signal_expiry_response"],
                "build_order_detail_integrity_response": deps["build_order_detail_integrity_response"],
                "build_today_targets_response": deps["build_today_targets_response"],
                "build_active_window_progress_response": deps["build_active_window_progress_response"],
                "system_status_chat_id": deps["system_status_chat_id"],
                "startup_chat_id": deps["startup_chat_id"],
                "cancel_broker_order": deps["cancel_broker_order"],
            },
        )
    )

    exports.update(
        register_runtime_routes(
            app,
            deps={
                "pb": deps["pb"],
                "build_service_topology": deps["build_service_topology"],
                "normalize_environment": deps["normalize_environment"],
                "parse_boolean": deps["parse_boolean"],
                "pick_effective_config_rows": deps["pick_effective_config_rows"],
                "serialize_config_rows": deps["serialize_config_rows"],
                "merge_service_topology": deps["merge_service_topology"],
                "fetch_compute_health": deps["fetch_compute_health"],
                "fetch_compute_status": deps["fetch_compute_status"],
                "fetch_runtime_health": deps["fetch_runtime_health"],
                "fetch_runtime_status": deps["fetch_runtime_status"],
                "as_dict": deps["as_dict"],
                "load_daily_scan_state": deps["load_daily_scan_state"],
                "count_active_today_targets": deps["count_active_today_targets"],
                "build_statusz_compute_payload": deps["build_statusz_compute_payload"],
                "build_statusz_live_readiness": deps["build_statusz_live_readiness"],
                "build_statusz_runtime_payload": deps["build_statusz_runtime_payload"],
                "collect_storage_health": deps["collect_storage_health"],
                "get_state_payload": deps["get_state_payload"],
                "normalize_two_factor_state_with_runtime": deps["normalize_two_factor_state_with_runtime"],
                "ibkr_2fa_state_key": deps["ibkr_2fa_state_key"],
                "ibkr_2fa_state_date": deps["ibkr_2fa_state_date"],
                "compute_base_url": deps["compute_base_url"],
            },
        )
    )

    exports.update(
        register_startup_routes(
            app,
            deps={
                "pb": deps["pb"],
                "normalize_environment": deps["normalize_environment"],
                "time_strings": deps["time_strings"],
                "get_state_payload": deps["get_state_payload"],
                "normalize_startup_state": deps["normalize_startup_state"],
                "build_startup_cycle_id": deps["build_startup_cycle_id"],
                "build_startup_label": deps["build_startup_label"],
                "startup_chat_id": deps["startup_chat_id"],
                "default_startup_steps": deps["default_startup_steps"],
                "normalize_startup_fields": deps["normalize_startup_fields"],
                "merge_startup_steps": deps["merge_startup_steps"],
                "deliver_startup_progress_card": deps["deliver_startup_progress_card"],
                "resolve_startup_step_label": deps["resolve_startup_step_label"],
                "write_system_event_record": deps["write_system_event_record"],
                "ibkr_startup_state_key": deps["ibkr_startup_state_key"],
                "ibkr_startup_state_date": deps["ibkr_startup_state_date"],
                "as_dict": deps["as_dict"],
            },
        )
    )

    exports.update(
        register_storage_routes(
            app,
            deps={
                "pb": deps["pb"],
                "normalize_environment": deps["normalize_environment"],
                "parse_boolean": deps["parse_boolean"],
                "config_value": deps["config_value"],
            },
        )
    )

    exports.update(
        register_data_quality_routes(
            app,
            deps={
                "pb": deps["pb"],
                "normalize_environment": deps["normalize_environment"],
            },
        )
    )

    exports.update(
        register_universe_routes(
            app,
            deps={
                "pb": deps["pb"],
                "normalize_environment": deps["normalize_environment"],
                "escape_filter_string": deps["escape_filter_string"],
                "time_strings": deps["time_strings"],
                "request_json_request": deps["request_json_request"],
                "compute_base_url": deps["compute_base_url"],
            },
        )
    )

    exports.update(
        register_account_routes(
            app,
            deps={
                "pb": deps["pb"],
                "normalize_environment": deps["normalize_environment"],
                "request_json_request": deps["request_json_request"],
                "runtime_base_url": deps["runtime_base_url"],
            },
        )
    )

    return exports
