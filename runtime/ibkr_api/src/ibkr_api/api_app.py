from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, jsonify, request

from ibkr_api.app_core.config_store import (
    load_effective_config_map as _load_effective_config_map_support,
    load_effective_config_rows as _load_effective_config_rows_support,
    load_recent_system_events as _load_recent_system_events_support,
    pick_effective_config_rows as _pick_effective_config_rows_support,
    serialize_config_rows as _serialize_config_rows_support,
)
from ibkr_api.app_core.compat_registrar import register_compat_proxy_routes
from ibkr_api.app_core.http import (
    build_response_from_upstream as _build_response_from_upstream_support,
    feishu_callback_response as _feishu_callback_response_support,
    json_response as _json_response_support,
    request_json as _request_json_support,
    request_json_request as _request_json_request_support,
)
from ibkr_api.app_core.platform_route_deps import build_platform_route_deps
from ibkr_api.app_core.platform_registrar import register_platform_routes
from ibkr_api.app_core.presentation import (
    add_environment_to_detail as _add_environment_to_detail_support,
    config_value as _config_value_support,
    console_base_url as _console_base_url_support,
    environment_tag as _environment_tag_support,
    is_enabled_text as _is_enabled_text_support,
    label_title_with_environment as _label_title_with_environment_support,
    signal_chat_id as _signal_chat_id_support,
    system_page_url as _system_page_url_support,
    system_status_chat_id as _system_status_chat_id_support,
    time_strings as _time_strings_support,
    runtime_page_url as _runtime_page_url_support,
)
from ibkr_api.app_core.proxying import (
    forward_request as _forward_request_support,
    proxy_custom_to_pb as _proxy_custom_to_pb_support,
    proxy_webhook_to_pb as _proxy_webhook_to_pb_support,
)
from ibkr_api.app_core.state_access import (
    count_active_today_targets as _count_active_today_targets_support,
    get_state_payload as _get_state_payload_support,
    load_daily_scan_state as _load_daily_scan_state_support,
)
from ibkr_api.app_core.trading_route_deps import build_trading_route_deps
from ibkr_api.app_core.trading_registrar import register_trading_routes
from ibkr_api.app_core.value_utils import (
    as_dict as _as_dict_support,
    escape_filter_string as _escape_filter_string_support,
    normalize_environment as _normalize_environment_support,
    normalize_symbol_list as _normalize_symbol_list_support,
    parse_boolean as _parse_boolean_support,
    parse_time_ms as _parse_time_ms_support,
    trim_array as _trim_array_support,
    trim_object_entries as _trim_object_entries_support,
)
from ibkr_api.callbacks.feishu import (
    callback_toast as _callback_toast_support,
    dispatch_feishu_2fa_callback as _dispatch_feishu_2fa_callback_support,
    dispatch_feishu_order_callback as _dispatch_feishu_order_callback_support,
    dispatch_feishu_signal_callback as _dispatch_feishu_signal_callback_support,
    handle_feishu_callback as _handle_feishu_callback_support,
)
from ibkr_api.callbacks.runtime_dispatch import (
    build_dispatch_feishu_2fa_callback,
    build_dispatch_feishu_order_callback,
    build_dispatch_feishu_signal_callback,
)
from ibkr_api.compat.bootstrap import build_compat_proxy_deps
from ibkr_api.control.runtime_guard import (
    build_runtime_environment_mismatch_payload,
    inspect_requested_runtime_environment,
)
from ibkr_api.integrations.runtime_orders import cancel_broker_order_via_runtime as _cancel_broker_order_via_runtime_support
from ibkr_api.universe.today_targets import build_today_targets_response
from ibkr_api.orders.cancel_sync import build_order_cancel_sync_response
from ibkr_api.orders.group_cancel import build_order_cancel_group_response
from ibkr_api.orders.group_close import build_order_close_group_response
from ibkr_api.orders.integrity import build_order_detail_integrity_response
from ibkr_api.orders.webhooks import build_order_cancel_webhook_response, build_order_close_webhook_response
from ibkr_api.orders.upsert import build_order_upsert_response
from ibkr_api.orders.reconcile import build_orders_reconcile_response
from ibkr_api.reverse.actions import build_reverse_ack_response, build_reverse_dispatch_response
from ibkr_api.reverse.calculate import build_reverse_calculate_response
from ibkr_api.reverse.queries import build_reverse_list_response, build_reverse_pending_response
from ibkr_api.signals.expiry import build_signal_expiry_response
from ibkr_api.signals.ingest import build_signal_ingest_response, build_signals_ingest_response
from ibkr_api.signals.webhooks import build_signal_cancel_webhook_response, build_signal_confirm_webhook_response
from ibkr_api.runtime.status_support import (
    build_statusz_compute_payload as _build_statusz_compute_payload_support,
    build_statusz_live_readiness as _build_statusz_live_readiness_support,
    build_statusz_runtime_payload as _build_statusz_runtime_payload_support,
    fetch_compute_health as _fetch_compute_health_support,
    fetch_compute_monitor as _fetch_compute_monitor_support,
    fetch_compute_status as _fetch_compute_status_support,
    fetch_runtime_health as _fetch_runtime_health_support,
    fetch_runtime_status as _fetch_runtime_status_support,
    merge_service_topology as _merge_service_topology_support,
)
from ibkr_api.runtime.two_factor import normalize_two_factor_state_with_runtime as _normalize_two_factor_state_with_runtime_support
from ibkr_api.startup.progress import (
    default_startup_steps as _default_startup_steps,
    merge_startup_steps as _merge_startup_steps,
    normalize_startup_fields as _normalize_startup_fields,
    resolve_startup_step_label as _resolve_startup_step_label,
)
from ibkr_api.system.bootstrap import build_system_bootstrap
from ibkr_api.system.runtime_events import emit_system_event as _emit_system_event_support
from ibkr_api.system.pocketbase_disk import (
    build_pocketbase_disk_flags as _build_pocketbase_disk_flags,
    collect_pocketbase_disk_snapshot as _collect_pocketbase_disk_snapshot,
    enrich_monitor_payload_with_pocketbase_disk as _enrich_monitor_payload_with_pocketbase_disk,
    format_bytes as _format_bytes,
    merge_monitor_flags as _merge_monitor_flags,
    merge_monitor_status as _merge_monitor_status,
)
from ibkr_api.system.monitor_support import (
    build_system_monitor_payload as _build_system_monitor_payload_support,
    derive_monitor_service_map as _derive_monitor_service_map_support,
    probe_console_status as _probe_console_status_support,
)
from ibkr_api.system.runtime_monitor import (
    build_augment_scheduler_summary,
    build_derive_monitor_service_map,
    build_extract_cursor_interval,
    build_probe_console_status,
    build_scheduler_job_states,
    build_scheduler_status,
    build_scheduler_summary,
    build_system_monitor_payload,
    build_system_summary_payload,
)
from ibkr_api.system.scheduler_support import (
    augment_scheduler_summary as _augment_scheduler_summary_support,
    build_scheduler_summary as _build_scheduler_summary_support,
    extract_cursor_interval as _extract_cursor_interval_support,
    scheduler_job_states as _scheduler_job_states_support,
    scheduler_status as _scheduler_status_support,
)
from ibkr_api.system.summary_support import build_system_summary_payload as _build_system_summary_payload_support
from ibkr_api.tradingview.ingest import upsert_tv_indicator as _upsert_tv_indicator_support, upsert_tv_signal as _upsert_tv_signal_support
from ibkr_api.tradingview.runtime_adapters import build_upsert_tv_indicator, build_upsert_tv_signal
from ibkr_compute.api.service_topology import build_service_topology
from ibkr_scheduler.cron_registry import build_cron_payload
from ibkr_compute.core.config import Config
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_api.signals.api import build_signals_ack_response, build_signals_pending_response


REQUEST_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("IBKR_API_PROXY_TIMEOUT_SEC", "60")))
PB_BASE_URL = str(os.environ.get("PB_BASE_URL") or "http://127.0.0.1:8090").rstrip("/")
COMPUTE_BASE_URL = str(os.environ.get("IBKR_COMPUTE_INTERNAL_URL") or "http://127.0.0.1:5100").rstrip("/")
RUNTIME_BASE_URL = str(os.environ.get("IBKR_RUNTIME_INTERNAL_URL") or "http://127.0.0.1:5101").rstrip("/")
SCHEDULER_BASE_URL = str(os.environ.get("IBKR_SCHEDULER_INTERNAL_URL") or "http://127.0.0.1:5103").rstrip("/")
ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")
IBKR_2FA_STATE_KEY = "ibkr_2fa"
IBKR_2FA_STATE_DATE = "global"
IBKR_STARTUP_STATE_KEY = "ibkr_runtime_startup"
IBKR_STARTUP_STATE_DATE = "global"
IBKR_DAILY_SCAN_STATE_KEY = "ibkr_daily_scan_state"
DEFAULT_CONSOLE_BASE_URL = str(
    os.environ.get("CONSOLE_BASE_URL")
    or os.environ.get("QUANT_BASE_URL")
    or os.environ.get("IBKR_CONSOLE_PUBLIC_URL")
    or "https://quant.lzw-glory.top"
).rstrip("/")
DEFAULT_FEISHU_APP_ID = str(os.environ.get("FEISHU_APP_ID") or "cli_a936b8d2cc79dccb").strip()
DEFAULT_FEISHU_APP_SECRET = str(os.environ.get("FEISHU_APP_SECRET") or "ZZySOkZPaKBVkNhhk4upvfROPnXcSsry").strip()
DEFAULT_FEISHU_SYSTEM_CHAT_ID = str(os.environ.get("FEISHU_SYSTEM_CHAT_ID") or "oc_b7b52fc28816d90e27ce50ca7922a9ac").strip()
DEFAULT_FEISHU_2FA_CHAT_ID = str(os.environ.get("FEISHU_2FA_CHAT_ID") or "oc_c48c10447685e80cfea0c003864aa51f").strip()
DEFAULT_FEISHU_ALERT_CHAT_ID = str(os.environ.get("FEISHU_ALERT_CHAT_ID") or "oc_91aa4f84bc6fedb125b1a263d91d4104").strip()
DEFAULT_FEISHU_STARTUP_CHAT_ID = str(os.environ.get("FEISHU_STARTUP_CHAT_ID") or "oc_cc5d0a950797b1c2c010953e14bceeff").strip()
DEFAULT_FEISHU_SIGNAL_CHAT_ID = str(os.environ.get("FEISHU_SIGNAL_CHAT_ID") or "oc_edb26dcc52938b7833ac9f32ae6b1620").strip()
MONITOR_CONFIG_KEYS = (
    "ibkr_target_subscription_limit",
    "ibkr_history_request_spacing",
    "ibkr_target_refresh_sec",
    "ibkr_watchlist_backfill_interval_min",
    "system_monitor_ws_message_age_regular_warn_sec",
    "system_monitor_ws_message_age_regular_critical_sec",
    "system_monitor_ws_message_age_late_session_warn_sec",
    "system_monitor_ws_message_age_late_session_critical_sec",
)
ENVIRONMENT_LABELS = {
    "live": "LIVE",
    "paper": "PAPER",
    "backtest": "BACKTEST",
}
_FEISHU_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}

app = Flask(__name__)
# Avoid recursively routing runtime-config reads back into this API service.
pb = PBClient(base_url=PB_BASE_URL, prefer_runtime_config_api=False)
config = Config(pb_client=pb)


EXCLUDED_RESPONSE_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection"}
FORWARDED_REQUEST_HEADERS = {"Accept", "Authorization", "Content-Type"}


_normalize_environment = _normalize_environment_support
_parse_boolean = _parse_boolean_support
_escape_filter_string = _escape_filter_string_support
_as_dict = _as_dict_support
_normalize_symbol_list = _normalize_symbol_list_support
_trim_array = _trim_array_support
_trim_object_entries = _trim_object_entries_support


def _parse_et_time_ms(value: Any) -> int:
    return _parse_time_ms_support(value, default_tz=ET)


def _pick_effective_config_rows(rows: list[dict[str, Any]], environment: str) -> list[dict[str, Any]]:
    return _pick_effective_config_rows_support(rows, environment, normalize_environment=_normalize_environment)


def _serialize_config_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _serialize_config_rows_support(rows)


def _load_effective_config_rows(environment: str) -> list[dict[str, Any]]:
    return _load_effective_config_rows_support(
        pb,
        environment,
        normalize_environment=_normalize_environment,
        pick_effective_config_rows_fn=_pick_effective_config_rows,
    )


def _load_effective_config_map(environment: str, selected_keys: tuple[str, ...] | list[str] | set[str] | None = None) -> dict[str, str]:
    return _load_effective_config_map_support(
        pb,
        environment,
        normalize_environment=_normalize_environment,
        load_effective_config_rows_fn=lambda pb_client, runtime_environment: _load_effective_config_rows_support(
            pb_client,
            runtime_environment,
            normalize_environment=_normalize_environment,
            pick_effective_config_rows_fn=_pick_effective_config_rows,
        ),
        selected_keys=selected_keys,
    )


def _load_recent_system_events(environment: str, limit: int = 20) -> list[dict[str, Any]]:
    return _load_recent_system_events_support(
        pb,
        environment,
        limit=limit,
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
    )


def _is_enabled_text(value: Any) -> bool:
    return _is_enabled_text_support(value)


def _fetch_compute_monitor(environment: str) -> dict[str, Any]:
    return _fetch_compute_monitor_support(
        environment,
        request_json=_request_json,
        compute_base_url=COMPUTE_BASE_URL,
    )


def _time_strings(now_ts: float | None = None) -> dict[str, str]:
    return _time_strings_support(now_ts=now_ts, et_tz=ET, cn_tz=CN)


def _environment_tag(environment: str) -> str:
    return _environment_tag_support(
        environment,
        normalize_environment=_normalize_environment,
        environment_labels=ENVIRONMENT_LABELS,
    )


def _label_title_with_environment(title: Any, environment: str) -> str:
    return _label_title_with_environment_support(
        title,
        environment,
        environment_tag_fn=_environment_tag,
    )


def _add_environment_to_detail(detail: Any, environment: str) -> dict[str, Any]:
    return _add_environment_to_detail_support(
        detail,
        environment,
        normalize_environment=_normalize_environment,
    )


def _console_base_url() -> str:
    return _console_base_url_support(default_console_base_url=DEFAULT_CONSOLE_BASE_URL)


def _runtime_page_url(environment: str) -> str:
    return _runtime_page_url_support(
        environment,
        console_base_url_fn=_console_base_url,
        normalize_environment=_normalize_environment,
    )


def _system_page_url(environment: str) -> str:
    return _system_page_url_support(
        environment,
        console_base_url_fn=_console_base_url,
        normalize_environment=_normalize_environment,
    )


def _config_value(key: str, default: str, environment: str) -> str:
    return _config_value_support(
        config,
        key,
        default,
        environment,
        normalize_environment=_normalize_environment,
    )


def _signal_chat_id(environment: str) -> str:
    return _signal_chat_id_support(
        environment,
        config_value_fn=_config_value,
        default_chat_id=DEFAULT_FEISHU_SIGNAL_CHAT_ID,
    )


def _system_status_chat_id(environment: str) -> str:
    return _system_status_chat_id_support(
        environment,
        config_value_fn=_config_value,
        default_chat_id=DEFAULT_FEISHU_SYSTEM_CHAT_ID,
    )

_system_bootstrap = build_system_bootstrap(
    pb=pb,
    requests_module=requests,
    feishu_token_cache=_FEISHU_TOKEN_CACHE,
    default_feishu_app_id=DEFAULT_FEISHU_APP_ID,
    default_feishu_app_secret=DEFAULT_FEISHU_APP_SECRET,
    default_feishu_2fa_chat_id=DEFAULT_FEISHU_2FA_CHAT_ID,
    default_feishu_alert_chat_id=DEFAULT_FEISHU_ALERT_CHAT_ID,
    default_feishu_system_chat_id=DEFAULT_FEISHU_SYSTEM_CHAT_ID,
    default_feishu_startup_chat_id=DEFAULT_FEISHU_STARTUP_CHAT_ID,
    normalize_environment=_normalize_environment,
    config_value=_config_value,
    is_enabled_text=_is_enabled_text,
    add_environment_to_detail=_add_environment_to_detail,
    time_strings=_time_strings,
    system_page_url=_system_page_url,
    label_title_with_environment=_label_title_with_environment,
    environment_tag=_environment_tag,
    runtime_page_url=_runtime_page_url,
    console_base_url=_console_base_url,
)
_feishu_send_interactive = _system_bootstrap["_feishu_send_interactive"]
_feishu_update_interactive = _system_bootstrap["_feishu_update_interactive"]
_deliver_system_event_notification = _system_bootstrap["_deliver_system_event_notification"]
_write_system_event_record = _system_bootstrap["_write_system_event_record"]
_request_two_factor_approval = _system_bootstrap["_request_two_factor_approval"]
_startup_chat_id = _system_bootstrap["_startup_chat_id"]
_build_startup_label = _system_bootstrap["_build_startup_label"]
_build_startup_cycle_id = _system_bootstrap["_build_startup_cycle_id"]
_normalize_startup_state = _system_bootstrap["_normalize_startup_state"]
_deliver_startup_progress_card = _system_bootstrap["_deliver_startup_progress_card"]


def _emit_system_event(
    *,
    event_type: str,
    level: str,
    source: str,
    title: str,
    detail: Any,
    environment: str,
    message_id: str = "",
) -> dict[str, Any]:
    return _emit_system_event_support(
        deliver_system_event_notification=_deliver_system_event_notification,
        write_system_event_record=_write_system_event_record,
        event_type=event_type,
        level=level,
        source=source,
        title=title,
        detail=detail,
        environment=environment,
        message_id=message_id,
    )


def _json_response(payload: dict[str, Any], status_code: int = 200, headers: dict[str, str] | None = None):
    return _json_response_support(jsonify_fn=jsonify, payload=payload, status_code=status_code, headers=headers)


def _feishu_callback_response(payload: dict[str, Any], *, update_token: str = "", status_code: int = 200):
    return _feishu_callback_response_support(
        jsonify_fn=jsonify,
        payload=payload,
        update_token=update_token,
        status_code=status_code,
    )


def _build_response_from_upstream(response: requests.Response) -> Response:
    return _build_response_from_upstream_support(
        response=response,
        response_class=Response,
        excluded_headers=EXCLUDED_RESPONSE_HEADERS,
    )


def _request_json(base_url: str, path: str, *, params: list[tuple[str, str]] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    return _request_json_support(
        requests_module=requests,
        base_url=base_url,
        path=path,
        params=params,
        timeout=timeout,
    )


def _request_json_request(
    method: str,
    base_url: str,
    path: str,
    *,
    params: list[tuple[str, str]] | None = None,
    json_body: Any = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    return _request_json_request_support(
        requests_module=requests,
        method=method,
        base_url=base_url,
        path=path,
        params=params,
        json_body=json_body,
        timeout=timeout,
    )


def _get_state_payload(state_key: str, environment: str, *, date: str = "global") -> dict[str, Any]:
    return _get_state_payload_support(
        pb,
        state_key,
        environment,
        as_dict=_as_dict,
        normalize_environment=_normalize_environment,
        date=date,
    )


def _load_daily_scan_state(environment: str) -> dict[str, Any]:
    return _load_daily_scan_state_support(
        environment,
        as_dict=_as_dict,
        get_state_payload_fn=_get_state_payload,
        daily_scan_state_key=IBKR_DAILY_SCAN_STATE_KEY,
    )


def _count_active_today_targets(environment: str, market_date: str) -> int:
    return _count_active_today_targets_support(
        pb,
        environment,
        market_date,
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
    )


def _fetch_compute_status(environment: str, include_engines: bool = False) -> dict[str, Any]:
    return _fetch_compute_status_support(
        environment,
        request_json=_request_json,
        compute_base_url=COMPUTE_BASE_URL,
        include_engines=include_engines,
    )


def _fetch_compute_health(environment: str) -> dict[str, Any]:
    return _fetch_compute_health_support(
        environment,
        request_json=_request_json,
        compute_base_url=COMPUTE_BASE_URL,
    )


def _fetch_runtime_status(environment: str) -> dict[str, Any]:
    return _fetch_runtime_status_support(
        environment,
        request_json=_request_json,
        compute_base_url=COMPUTE_BASE_URL,
        runtime_base_url=RUNTIME_BASE_URL,
        as_dict=_as_dict,
    )


def _fetch_runtime_health(environment: str) -> dict[str, Any]:
    return _fetch_runtime_health_support(
        environment,
        request_json=_request_json,
        runtime_base_url=RUNTIME_BASE_URL,
        as_dict=_as_dict,
    )


def _merge_service_topology(*payloads: Any) -> dict[str, Any]:
    return _merge_service_topology_support(*payloads, build_service_topology=build_service_topology)


def _normalize_two_factor_state_with_runtime(state_data: dict[str, Any], runtime_status: dict[str, Any]) -> dict[str, Any]:
    return _normalize_two_factor_state_with_runtime_support(
        state_data,
        runtime_status,
        as_dict=_as_dict,
        parse_et_time_ms=_parse_et_time_ms,
    )


def _build_statusz_compute_payload(compute_payload: dict[str, Any], include_engines: bool) -> dict[str, Any]:
    return _build_statusz_compute_payload_support(compute_payload, include_engines, as_dict=_as_dict)


def _build_statusz_live_readiness(compute_payload: dict[str, Any], runtime_payload: dict[str, Any]) -> dict[str, Any]:
    return _build_statusz_live_readiness_support(
        compute_payload,
        runtime_payload,
        as_dict=_as_dict,
        normalize_environment=_normalize_environment,
        normalize_symbol_list=_normalize_symbol_list,
    )


def _build_statusz_runtime_payload(
    runtime_payload: dict[str, Any],
    include_warmup_details: bool,
    *,
    live_readiness: dict[str, Any],
    fallback_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _build_statusz_runtime_payload_support(
        runtime_payload,
        include_warmup_details,
        live_readiness=live_readiness,
        fallback_state=fallback_state,
        as_dict=_as_dict,
        normalize_symbol_list=_normalize_symbol_list,
        trim_array=_trim_array,
        trim_object_entries=_trim_object_entries,
    )


def _forward_request(base_url: str, path: str, *, params: list[tuple[str, str]] | None = None, json_body: Any = None) -> Response:
    return _forward_request_support(
        requests_module=requests,
        request_obj=request,
        jsonify_fn=jsonify,
        request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        forwarded_request_headers=FORWARDED_REQUEST_HEADERS,
        build_response_from_upstream_fn=_build_response_from_upstream,
        build_service_topology_fn=build_service_topology,
        base_url=base_url,
        path=path,
        params=params,
        json_body=json_body,
    )


def _proxy_custom_to_pb(subpath: str) -> Response:
    return _proxy_custom_to_pb_support(
        subpath,
        forward_request_fn=_forward_request,
        pb_base_url=PB_BASE_URL,
    )


def _proxy_webhook_to_pb(subpath: str) -> Response:
    return _proxy_webhook_to_pb_support(
        subpath,
        forward_request_fn=_forward_request,
        pb_base_url=PB_BASE_URL,
    )


_extract_cursor_interval = build_extract_cursor_interval(support=_extract_cursor_interval_support)
_build_scheduler_summary = build_scheduler_summary(support=_build_scheduler_summary_support)
_scheduler_status = build_scheduler_status(
    globals_dict=globals(),
    scheduler_base_url=SCHEDULER_BASE_URL,
    support=_scheduler_status_support,
)
_scheduler_job_states = build_scheduler_job_states(globals_dict=globals(), support=_scheduler_job_states_support)
_augment_scheduler_summary = build_augment_scheduler_summary(support=_augment_scheduler_summary_support)
_build_system_summary_payload = build_system_summary_payload(globals_dict=globals(), support=_build_system_summary_payload_support)
_probe_console_status = build_probe_console_status(globals_dict=globals(), support=_probe_console_status_support)
_derive_monitor_service_map = build_derive_monitor_service_map(
    globals_dict=globals(),
    support=_derive_monitor_service_map_support,
)
_build_system_monitor_payload = build_system_monitor_payload(
    globals_dict=globals(),
    config=config,
    build_cron_payload=build_cron_payload,
    build_service_topology=build_service_topology,
    pb_base_url=PB_BASE_URL,
    monitor_config_keys=MONITOR_CONFIG_KEYS,
    support=_build_system_monitor_payload_support,
)


_platform_route_handlers = register_platform_routes(
    app,
    deps=build_platform_route_deps(
        globals_dict=globals(),
        pb=pb,
        config=config,
        build_service_topology=build_service_topology,
        compute_base_url=COMPUTE_BASE_URL,
        runtime_base_url=RUNTIME_BASE_URL,
        scheduler_base_url=SCHEDULER_BASE_URL,
        ibkr_2fa_state_key=IBKR_2FA_STATE_KEY,
        ibkr_2fa_state_date=IBKR_2FA_STATE_DATE,
        ibkr_startup_state_key=IBKR_STARTUP_STATE_KEY,
        ibkr_startup_state_date=IBKR_STARTUP_STATE_DATE,
    ),
)
globals().update(_platform_route_handlers)


_callback_toast = _callback_toast_support
_upsert_tv_indicator = build_upsert_tv_indicator(
    globals_dict=globals(),
    pb=pb,
    support=_upsert_tv_indicator_support,
)
_upsert_tv_signal = build_upsert_tv_signal(
    globals_dict=globals(),
    pb=pb,
    support=_upsert_tv_signal_support,
)
_dispatch_feishu_2fa_callback = build_dispatch_feishu_2fa_callback(
    globals_dict=globals(),
    pb_base_url=PB_BASE_URL,
    support=_dispatch_feishu_2fa_callback_support,
)
_dispatch_feishu_signal_callback = build_dispatch_feishu_signal_callback(
    globals_dict=globals(),
    pb=pb,
    support=_dispatch_feishu_signal_callback_support,
)
_dispatch_feishu_order_callback = build_dispatch_feishu_order_callback(
    globals_dict=globals(),
    pb=pb,
    support=_dispatch_feishu_order_callback_support,
)


def _cancel_broker_order_via_runtime(environment: str, order_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return _cancel_broker_order_via_runtime_support(
        environment,
        order_id,
        payload,
        request_json_request=_request_json_request,
        runtime_base_url=RUNTIME_BASE_URL,
        normalize_environment=_normalize_environment,
        as_dict=_as_dict,
    )


_trading_route_handlers = register_trading_routes(
    app,
    deps=build_trading_route_deps(
        globals_dict=globals(),
        pb=pb,
        runtime_base_url=RUNTIME_BASE_URL,
    ),
)
globals().update(_trading_route_handlers)


_compat_route_handlers = register_compat_proxy_routes(
    app,
    deps=build_compat_proxy_deps(
        pb_base_url=PB_BASE_URL,
        compute_base_url=COMPUTE_BASE_URL,
        runtime_base_url=RUNTIME_BASE_URL,
        scheduler_base_url=SCHEDULER_BASE_URL,
        build_service_topology=build_service_topology,
        config=config,
        normalize_environment=_normalize_environment,
        scheduler_status=lambda environment: _scheduler_status(environment),
        scheduler_job_states=lambda environment="live": _scheduler_job_states(environment),
        build_cron_payload=build_cron_payload,
        build_scheduler_summary=lambda environment, payload: _build_scheduler_summary(environment, payload),
        augment_scheduler_summary=lambda summary, items: _augment_scheduler_summary(summary, items),
        forward_request=lambda base_url, path, params=None, json_body=None: _forward_request(
            base_url,
            path,
            params=params,
            json_body=json_body,
        ),
        proxy_custom_to_pb=lambda subpath: _proxy_custom_to_pb(subpath),
        proxy_webhook_to_pb=lambda subpath: _proxy_webhook_to_pb(subpath),
    ),
)
globals().update(_compat_route_handlers)
