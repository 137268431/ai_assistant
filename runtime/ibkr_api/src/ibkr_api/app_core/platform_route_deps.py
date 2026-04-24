from __future__ import annotations

from typing import Any, Callable


def build_platform_route_deps(
    *,
    globals_dict: dict[str, Any],
    pb: Any,
    config: Any,
    build_service_topology: Callable[[], dict[str, Any]],
    compute_base_url: str,
    runtime_base_url: str,
    ibkr_2fa_state_key: str,
    ibkr_2fa_state_date: str,
    ibkr_startup_state_key: str,
    ibkr_startup_state_date: str,
) -> dict[str, Any]:
    return {
        "pb": pb,
        "config": config,
        "build_service_topology": build_service_topology,
        "normalize_environment": globals_dict["_normalize_environment"],
        "parse_boolean": globals_dict["_parse_boolean"],
        "escape_filter_string": globals_dict["_escape_filter_string"],
        "pick_effective_config_rows": globals_dict["_pick_effective_config_rows"],
        "serialize_config_rows": globals_dict["_serialize_config_rows"],
        "merge_service_topology": lambda *payloads: globals_dict["_merge_service_topology"](*payloads),
        "fetch_compute_health": lambda environment: globals_dict["_fetch_compute_health"](environment),
        "fetch_compute_status": lambda environment, include_engines=False: globals_dict["_fetch_compute_status"](
            environment,
            include_engines=include_engines,
        ),
        "fetch_runtime_health": lambda environment: globals_dict["_fetch_runtime_health"](environment),
        "fetch_runtime_status": lambda environment: globals_dict["_fetch_runtime_status"](environment),
        "as_dict": globals_dict["_as_dict"],
        "load_daily_scan_state": lambda environment: globals_dict["_load_daily_scan_state"](environment),
        "count_active_today_targets": lambda environment, market_date: globals_dict["_count_active_today_targets"](environment, market_date),
        "build_statusz_compute_payload": lambda compute_payload, include_engines: globals_dict["_build_statusz_compute_payload"](
            compute_payload,
            include_engines,
        ),
        "build_statusz_live_readiness": lambda compute_payload, runtime_payload: globals_dict["_build_statusz_live_readiness"](
            compute_payload,
            runtime_payload,
        ),
        "build_statusz_runtime_payload": (
            lambda runtime_payload, include_warmup_details, *, live_readiness, fallback_state=None: globals_dict[
                "_build_statusz_runtime_payload"
            ](
                runtime_payload,
                include_warmup_details,
                live_readiness=live_readiness,
                fallback_state=fallback_state,
            )
        ),
        "get_state_payload": lambda state_key, environment, date="global": globals_dict["_get_state_payload"](
            state_key,
            environment,
            date=date,
        ),
        "normalize_two_factor_state_with_runtime": lambda state_data, runtime_status: globals_dict[
            "_normalize_two_factor_state_with_runtime"
        ](state_data, runtime_status),
        "ibkr_2fa_state_key": ibkr_2fa_state_key,
        "ibkr_2fa_state_date": ibkr_2fa_state_date,
        "compute_base_url": compute_base_url,
        "time_strings": globals_dict["_time_strings"],
        "normalize_startup_state": lambda value, environment: globals_dict["_normalize_startup_state"](value, environment),
        "build_startup_cycle_id": lambda environment: globals_dict["_build_startup_cycle_id"](environment),
        "build_startup_label": lambda environment, startup_seq, started_at: globals_dict["_build_startup_label"](
            environment,
            startup_seq,
            started_at,
        ),
        "startup_chat_id": lambda environment: globals_dict["_startup_chat_id"](environment),
        "default_startup_steps": globals_dict["_default_startup_steps"],
        "normalize_startup_fields": globals_dict["_normalize_startup_fields"],
        "merge_startup_steps": globals_dict["_merge_startup_steps"],
        "deliver_startup_progress_card": lambda state, environment: globals_dict["_deliver_startup_progress_card"](
            state,
            environment,
        ),
        "resolve_startup_step_label": lambda state: globals_dict["_resolve_startup_step_label"](state),
        "write_system_event_record": lambda *args, **kwargs: globals_dict["_write_system_event_record"](*args, **kwargs),
        "ibkr_startup_state_key": ibkr_startup_state_key,
        "ibkr_startup_state_date": ibkr_startup_state_date,
        "config_value": globals_dict["_config_value"],
        "signal_chat_id": globals_dict["_signal_chat_id"],
        "console_base_url": globals_dict["_console_base_url"],
        "feishu_send_interactive": globals_dict["_feishu_send_interactive"],
        "feishu_update_interactive": globals_dict["_feishu_update_interactive"],
        "request_two_factor_approval": globals_dict["_request_two_factor_approval"],
        "emit_system_event": globals_dict["_emit_system_event"],
        "label_title_with_environment": globals_dict["_label_title_with_environment"],
        "add_environment_to_detail": globals_dict["_add_environment_to_detail"],
        "deliver_system_event_notification": lambda *args, **kwargs: globals_dict["_deliver_system_event_notification"](
            *args,
            **kwargs,
        ),
        "build_cron_payload": globals_dict["build_cron_payload"],
        "scheduler_status": lambda environment: globals_dict["_scheduler_status"](environment),
        "build_scheduler_summary": lambda environment, payload: globals_dict["_build_scheduler_summary"](environment, payload),
        "augment_scheduler_summary": lambda summary, items: globals_dict["_augment_scheduler_summary"](summary, items),
        "build_system_monitor_payload": lambda environment: globals_dict["_build_system_monitor_payload"](environment),
        "build_system_summary_payload": lambda environment, lite_mode=False: globals_dict["_build_system_summary_payload"](
            environment,
            lite_mode=lite_mode,
        ),
        "build_signal_expiry_response": lambda *args, **kwargs: globals_dict["build_signal_expiry_response"](*args, **kwargs),
        "build_order_detail_integrity_response": lambda *args, **kwargs: globals_dict["build_order_detail_integrity_response"](
            *args,
            **kwargs,
        ),
        "build_today_targets_response": lambda payload: globals_dict["build_today_targets_response"](
            pb,
            payload=payload,
            normalize_environment=globals_dict["_normalize_environment"],
            time_strings=globals_dict["_time_strings"],
        ),
        "system_status_chat_id": globals_dict["_system_status_chat_id"],
        "cancel_broker_order": lambda environment, order_id, payload=None: globals_dict[
            "_cancel_broker_order_via_runtime_support"
        ](
            environment,
            order_id,
            payload,
            request_json_request=globals_dict["_request_json_request"],
            runtime_base_url=runtime_base_url,
            normalize_environment=globals_dict["_normalize_environment"],
            as_dict=globals_dict["_as_dict"],
        ),
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
    }


__all__ = ["build_platform_route_deps"]
