from __future__ import annotations

from functools import partial
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
from ibkr_api.app_core.platform_registrar import register_platform_routes
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
from ibkr_api.control.runtime_guard import (
    build_runtime_environment_mismatch_payload,
    inspect_requested_runtime_environment,
)
from ibkr_api.integrations.feishu import feishu_send_interactive, feishu_suppressed, feishu_token, feishu_update_interactive
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
    STARTUP_LEGACY_STEP_KEY_MAP,
    STARTUP_STEP_LABELS,
    STARTUP_STEP_ORDER,
    STARTUP_STEP_STATUS_META,
    build_startup_card as _build_startup_card_support,
    build_startup_cycle_id as _build_startup_cycle_id_support,
    build_startup_label as _build_startup_label_support,
    default_startup_steps as _default_startup_steps,
    deliver_startup_progress_card as _deliver_startup_progress_card_support,
    merge_startup_steps as _merge_startup_steps,
    normalize_startup_fields as _normalize_startup_fields,
    normalize_startup_state as _normalize_startup_state_support,
    normalize_startup_step_status,
    resolve_startup_step_label as _resolve_startup_step_label,
    startup_chat_id as _startup_chat_id_support,
)
from ibkr_api.system.events import (
    build_system_event_card as _build_system_event_card_support,
    deliver_system_event_notification as _deliver_system_event_notification_support,
    system_event_chat_id as _system_event_chat_id_support,
    system_event_level_meta,
    should_notify_system_event as _should_notify_system_event_support,
    write_system_event_record as _write_system_event_record_support,
)
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
from ibkr_api.system.scheduler_support import (
    augment_scheduler_summary as _augment_scheduler_summary_support,
    build_scheduler_summary as _build_scheduler_summary_support,
    extract_cursor_interval as _extract_cursor_interval_support,
    scheduler_job_states as _scheduler_job_states_support,
    scheduler_status as _scheduler_status_support,
)
from ibkr_api.system.summary_support import build_system_summary_payload as _build_system_summary_payload_support
from ibkr_api.tradingview.ingest import upsert_tv_indicator as _upsert_tv_indicator_support, upsert_tv_signal as _upsert_tv_signal_support
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


DIRECT_PROXY_MAP: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "ibkr/rules"): (COMPUTE_BASE_URL, "/ibkr/rules"),
    ("GET", "ibkr/screener"): (COMPUTE_BASE_URL, "/screener"),
    ("GET", "ibkr/contracts/search"): (COMPUTE_BASE_URL, "/contracts/search"),
    ("GET", "ibkr/quotes"): (RUNTIME_BASE_URL, "/ibkr/quotes"),
    ("GET", "ibkr/quotes/forming_bar"): (RUNTIME_BASE_URL, "/ibkr/quotes/forming_bar"),
    ("POST", "ibkr/ingest/close"): (RUNTIME_BASE_URL, "/ibkr/ingest/close"),
    ("POST", "ibkr/start"): (RUNTIME_BASE_URL, "/ibkr/start"),
    ("POST", "ibkr/stop"): (RUNTIME_BASE_URL, "/ibkr/stop"),
    ("POST", "ibkr/gateway/start"): (RUNTIME_BASE_URL, "/ibkr/gateway/start"),
    ("POST", "ibkr/gateway/stop"): (RUNTIME_BASE_URL, "/ibkr/gateway/stop"),
    ("POST", "ibkr/gateway/restart"): (RUNTIME_BASE_URL, "/ibkr/gateway/restart"),
    ("POST", "ibkr/orders/cancel"): (RUNTIME_BASE_URL, "/ibkr/orders/cancel"),
    ("POST", "ibkr/orders/cancel_all"): (RUNTIME_BASE_URL, "/ibkr/orders/cancel_all"),
    ("POST", "ibkr/orders/modify"): (RUNTIME_BASE_URL, "/ibkr/orders/modify"),
    ("POST", "ibkr/orders/place"): (RUNTIME_BASE_URL, "/ibkr/orders/place"),
    ("POST", "ibkr/positions/close"): (RUNTIME_BASE_URL, "/ibkr/positions/close"),
    ("GET", "ibkr/account"): (RUNTIME_BASE_URL, "/ibkr/account"),
    ("GET", "ibkr/positions"): (RUNTIME_BASE_URL, "/ibkr/positions"),
    ("GET", "ibkr/orders/live"): (RUNTIME_BASE_URL, "/ibkr/orders/live"),
    ("GET", "ibkr/orders/history"): (RUNTIME_BASE_URL, "/ibkr/orders/history"),
    ("GET", "ibkr/history/rebuild/status"): (COMPUTE_BASE_URL, "/ibkr/history/rebuild/status"),
    ("POST", "ibkr/history/rebuild/start"): (COMPUTE_BASE_URL, "/ibkr/history/rebuild/start"),
    ("GET", "ibkr/backtest/status"): (COMPUTE_BASE_URL, "/backtest/status"),
    ("POST", "ibkr/backtest/run"): (COMPUTE_BASE_URL, "/backtest/run"),
    ("POST", "ibkr/backtest/cancel"): (COMPUTE_BASE_URL, "/backtest/cancel"),
    ("GET", "ibkr/backtest/replay"): (COMPUTE_BASE_URL, "/backtest/replay"),
    ("POST", "ibkr/backtest/cleanup"): (COMPUTE_BASE_URL, "/backtest/cleanup"),
    ("POST", "ibkr/data_quality/rescan"): (COMPUTE_BASE_URL, "/ibkr/data-quality/scan"),
    ("POST", "ibkr/data_quality/repair"): (COMPUTE_BASE_URL, "/ibkr/data-quality/repair"),
    ("POST", "ibkr/data_quality/truth_audit"): (COMPUTE_BASE_URL, "/ibkr/data-quality/truth-audit"),
}

ACTION_PROXY_MAP: dict[str, tuple[str, str]] = {
    "compute": (COMPUTE_BASE_URL, "/compute"),
    "scan": (COMPUTE_BASE_URL, "/scan"),
    "recompute": (COMPUTE_BASE_URL, "/recompute"),
    "chart/timeline": (COMPUTE_BASE_URL, "/chart/timeline"),
    "chart/compare": (COMPUTE_BASE_URL, "/chart/compare"),
}

DELEGATED_POCKETBASE_CUSTOM_ROUTES = [
]

DELEGATED_POCKETBASE_WEBHOOK_ROUTES = [
]


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
    return str(value or "true").strip().lower() not in {"", "0", "false", "no", "off"}


def _fetch_compute_monitor(environment: str) -> dict[str, Any]:
    return _fetch_compute_monitor_support(
        environment,
        request_json=_request_json,
        compute_base_url=COMPUTE_BASE_URL,
    )


def _time_strings(now_ts: float | None = None) -> dict[str, str]:
    current = float(now_ts if now_ts is not None else time.time())
    now_et = datetime.fromtimestamp(current, tz=ET)
    now_cn = datetime.fromtimestamp(current, tz=CN)
    return {
        "us": now_et.strftime("%Y-%m-%d %H:%M:%S"),
        "cn": now_cn.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now_et.strftime("%Y-%m-%d"),
    }


def _environment_tag(environment: str) -> str:
    runtime_environment = _normalize_environment(environment, "live")
    return f"[{ENVIRONMENT_LABELS.get(runtime_environment, runtime_environment.upper())}]"


def _label_title_with_environment(title: Any, environment: str) -> str:
    text = str(title or "").strip()
    tag = _environment_tag(environment)
    if not text:
        return tag
    return text if text.startswith(tag) else f"{tag} {text}"


def _add_environment_to_detail(detail: Any, environment: str) -> dict[str, Any]:
    runtime_environment = _normalize_environment(environment, "live")
    if isinstance(detail, dict):
        return {"environment": runtime_environment, **detail}
    if detail is None or detail == "":
        return {"environment": runtime_environment}
    return {"environment": runtime_environment, "detail": str(detail)}


def _console_base_url() -> str:
    return str(
        os.environ.get("CONSOLE_BASE_URL")
        or os.environ.get("QUANT_BASE_URL")
        or os.environ.get("IBKR_CONSOLE_PUBLIC_URL")
        or DEFAULT_CONSOLE_BASE_URL
    ).rstrip("/")


def _runtime_page_url(environment: str) -> str:
    base_url = _console_base_url()
    if not base_url:
        return ""
    return f"{base_url}/ibkr_runtime.html?environment={_normalize_environment(environment, 'live')}"


def _system_page_url(environment: str) -> str:
    base_url = _console_base_url()
    if not base_url:
        return ""
    return f"{base_url}/ibkr_system.html?environment={_normalize_environment(environment, 'live')}"


def _config_value(key: str, default: str, environment: str) -> str:
    try:
        return str(config.get_for_environment(key, _normalize_environment(environment, "live"), default) or default)
    except Exception:
        return default


def _signal_chat_id(environment: str) -> str:
    return _config_value("signal_chat_id", DEFAULT_FEISHU_SIGNAL_CHAT_ID, environment)


def _system_status_chat_id(environment: str) -> str:
    return _config_value("system_status_chat_id", DEFAULT_FEISHU_SYSTEM_CHAT_ID, environment)


_feishu_suppressed = partial(feishu_suppressed, normalize_environment=_normalize_environment)
_feishu_token = partial(
    feishu_token,
    app_id=DEFAULT_FEISHU_APP_ID,
    app_secret=DEFAULT_FEISHU_APP_SECRET,
    requests_module=requests,
    cache=_FEISHU_TOKEN_CACHE,
)
_feishu_send_interactive = partial(
    feishu_send_interactive,
    normalize_environment=_normalize_environment,
    token_loader=_feishu_token,
    requests_module=requests,
)
_feishu_update_interactive = partial(
    feishu_update_interactive,
    normalize_environment=_normalize_environment,
    token_loader=_feishu_token,
    requests_module=requests,
)

_system_event_level_meta = system_event_level_meta
_build_system_event_card = partial(
    _build_system_event_card_support,
    normalize_environment=_normalize_environment,
    add_environment_to_detail=_add_environment_to_detail,
    time_strings=_time_strings,
    system_page_url=_system_page_url,
    label_title_with_environment=_label_title_with_environment,
)
_should_notify_system_event = partial(
    _should_notify_system_event_support,
    normalize_environment=_normalize_environment,
    config_value=_config_value,
    is_enabled_text=_is_enabled_text,
)
_system_event_chat_id = partial(
    _system_event_chat_id_support,
    normalize_environment=_normalize_environment,
    config_value=_config_value,
    default_2fa_chat_id=DEFAULT_FEISHU_2FA_CHAT_ID,
    default_alert_chat_id=DEFAULT_FEISHU_ALERT_CHAT_ID,
    default_system_chat_id=DEFAULT_FEISHU_SYSTEM_CHAT_ID,
)
_deliver_system_event_notification = partial(
    _deliver_system_event_notification_support,
    normalize_environment=_normalize_environment,
    should_notify_system_event_fn=_should_notify_system_event,
    build_system_event_card_fn=_build_system_event_card,
    system_event_chat_id_fn=_system_event_chat_id,
    send_interactive=_feishu_send_interactive,
    update_interactive=_feishu_update_interactive,
)
_write_system_event_record = partial(
    _write_system_event_record_support,
    pb=pb,
    normalize_environment=_normalize_environment,
    time_strings=_time_strings,
    label_title_with_environment=_label_title_with_environment,
    add_environment_to_detail=_add_environment_to_detail,
)


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
    delivery = _deliver_system_event_notification(
        event_type,
        level,
        source,
        title,
        detail,
        environment,
        message_id=message_id,
    )
    notified = bool(delivery.get("success")) and not bool(delivery.get("suppressed"))
    persisted = bool(_write_system_event_record(event_type, level, source, title, detail, environment, notified))
    return {
        "ok": True,
        "notified": notified,
        "persisted": persisted,
        "message_id": str(delivery.get("message_id") or message_id),
        "updated": bool(delivery.get("updated")),
        "skipped": bool(delivery.get("skipped")),
        "suppressed": bool(delivery.get("suppressed")),
        "error": str(delivery.get("error") or ""),
    }


def _request_two_factor_approval(
    *,
    environment: str,
    reason: str,
    source: str,
    message: str,
    detail: dict[str, Any] | None = None,
    force_reset: bool = False,
    force_new: bool = False,
) -> dict[str, Any]:
    return pb.request_ibkr_2fa(
        reason=reason,
        detail=detail or {},
        source=source,
        environment=environment,
        message=message,
        force_reset=force_reset,
        force_new=force_new,
    )

_normalize_startup_step_status = normalize_startup_step_status
_startup_chat_id = partial(
    _startup_chat_id_support,
    config_value=_config_value,
    default_chat_id=DEFAULT_FEISHU_STARTUP_CHAT_ID,
)
_build_startup_label = partial(
    _build_startup_label_support,
    normalize_environment=_normalize_environment,
    time_strings=_time_strings,
)
_build_startup_cycle_id = partial(
    _build_startup_cycle_id_support,
    normalize_environment=_normalize_environment,
)
_normalize_startup_state = partial(
    _normalize_startup_state_support,
    normalize_environment=_normalize_environment,
    startup_chat_id_fn=_startup_chat_id,
)
_build_startup_card = partial(
    _build_startup_card_support,
    normalize_environment=_normalize_environment,
    normalize_startup_state_fn=_normalize_startup_state,
    environment_tag=_environment_tag,
    label_title_with_environment=_label_title_with_environment,
    runtime_page_url=_runtime_page_url,
    system_page_url=_system_page_url,
    console_base_url=_console_base_url,
)
_deliver_startup_progress_card = partial(
    _deliver_startup_progress_card_support,
    normalize_environment=_normalize_environment,
    normalize_startup_state_fn=_normalize_startup_state,
    build_startup_card_fn=_build_startup_card,
    send_interactive=_feishu_send_interactive,
    update_interactive=_feishu_update_interactive,
    startup_chat_id_fn=_startup_chat_id,
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
    runtime_environment = _normalize_environment(environment)
    try:
        record = pb.get_state(state_key, runtime_environment, date=date)
    except Exception:
        record = None
    payload = _as_dict((record or {}).get("data") if isinstance(record, dict) else {})
    record_date = str(((record or {}).get("date") if isinstance(record, dict) else "") or date).strip() or date
    return {
        "environment": runtime_environment,
        "date": record_date,
        "record": record if isinstance(record, dict) else {},
        "data": payload,
    }


def _load_daily_scan_state(environment: str) -> dict[str, Any]:
    payload = _get_state_payload(IBKR_DAILY_SCAN_STATE_KEY, environment, date="global")
    data = _as_dict(payload.get("data"))
    data["result"] = _as_dict(data.get("result"))
    return data


def _count_active_today_targets(environment: str, market_date: str) -> int:
    normalized_market_date = str(market_date or "").strip()
    if not normalized_market_date:
        return 0
    runtime_environment = _normalize_environment(environment)
    target_filter = (
        f'date = "{_escape_filter_string(normalized_market_date)}" && '
        f'environment = "{_escape_filter_string(runtime_environment)}" && '
        '(status = "candidate" || status = "active")'
    )
    try:
        rows = pb.get_all_records("ibkr_targets", filter=target_filter, max_pages=25)
    except Exception:
        return 0
    return len(rows or [])


def _fetch_compute_status(environment: str) -> dict[str, Any]:
    return _fetch_compute_status_support(
        environment,
        request_json=_request_json,
        compute_base_url=COMPUTE_BASE_URL,
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
    headers = {
        key: value
        for key, value in request.headers.items()
        if key in FORWARDED_REQUEST_HEADERS and value
    }
    target_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        upstream_response = requests.request(
            method=request.method,
            url=target_url,
            params=params if params is not None else list(request.args.items(multi=True)),
            data=None if json_body is not None else request.get_data(cache=True),
            json=json_body,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return jsonify(
            {
                "ok": False,
                "status": "offline",
                "error": str(exc),
                "upstream": target_url,
                "service_topology": build_service_topology(),
            }
        ), 502
    return _build_response_from_upstream(upstream_response)


def _proxy_custom_to_pb(subpath: str) -> Response:
    return _forward_request(PB_BASE_URL, f"/api/custom/{subpath}")


def _proxy_webhook_to_pb(subpath: str) -> Response:
    return _forward_request(PB_BASE_URL, f"/webhook/{subpath}")


def _extract_cursor_interval(cursor_payload: dict[str, Any], interval: str = "5m") -> dict[str, Any]:
    return _extract_cursor_interval_support(cursor_payload, interval)



def _build_scheduler_summary(environment: str, scheduler_payload: dict[str, Any]) -> dict[str, Any]:
    return _build_scheduler_summary_support(environment, scheduler_payload)



def _scheduler_status(environment: str = "live") -> dict[str, Any]:
    return _scheduler_status_support(
        environment,
        request_json=_request_json,
        scheduler_base_url=SCHEDULER_BASE_URL,
    )



def _scheduler_job_states(environment: str = "live") -> dict[str, Any]:
    return _scheduler_job_states_support(environment, scheduler_status_fn=_scheduler_status)



def _augment_scheduler_summary(summary: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    return _augment_scheduler_summary_support(summary, items)



def _build_system_summary_payload(environment: str, *, lite_mode: bool) -> dict[str, Any]:
    return _build_system_summary_payload_support(
        environment,
        lite_mode=lite_mode,
        normalize_environment=_normalize_environment,
        load_effective_config_map=_load_effective_config_map,
        is_enabled_text=_is_enabled_text,
        fetch_compute_health=_fetch_compute_health,
        fetch_compute_status=_fetch_compute_status,
        fetch_runtime_status=_fetch_runtime_status,
        as_dict=_as_dict,
        merge_service_topology=_merge_service_topology,
        load_recent_system_events=_load_recent_system_events,
        time_strings=_time_strings,
    )



def _probe_console_status() -> dict[str, Any]:
    return _probe_console_status_support(_console_base_url())



def _derive_monitor_service_map(
    environment: str,
    base_payload: dict[str, Any],
    scheduler_summary: dict[str, Any],
    *,
    console_probe: dict[str, Any],
    pb_health: dict[str, Any],
    build_service_topology_fn=None,
    build_service_topology: Any = None,
) -> dict[str, Any]:
    topology_builder = build_service_topology_fn or build_service_topology or globals().get("build_service_topology")
    return _derive_monitor_service_map_support(
        environment,
        base_payload,
        scheduler_summary,
        console_probe=console_probe,
        pb_health=pb_health,
        build_service_topology=topology_builder,
    )



def _build_system_monitor_payload(environment: str) -> dict[str, Any]:
    return _build_system_monitor_payload_support(
        environment,
        normalize_environment=_normalize_environment,
        fetch_compute_monitor=_fetch_compute_monitor,
        as_dict=_as_dict,
        config_refresh=config.refresh,
        scheduler_status=_scheduler_status,
        build_cron_payload=build_cron_payload,
        config=config,
        build_scheduler_summary=_build_scheduler_summary,
        augment_scheduler_summary=_augment_scheduler_summary,
        request_json=_request_json,
        pb_base_url=PB_BASE_URL,
        console_base_url=_console_base_url(),
        probe_console_status=_probe_console_status_support,
        load_effective_config_map=_load_effective_config_map,
        monitor_config_keys=MONITOR_CONFIG_KEYS,
        load_recent_system_events=_load_recent_system_events,
        enrich_monitor_payload_with_pocketbase_disk=_enrich_monitor_payload_with_pocketbase_disk,
        derive_monitor_service_map=_derive_monitor_service_map,
        merge_service_topology=_merge_service_topology,
        build_service_topology=build_service_topology,
        service_profile=str(os.environ.get("IBKR_SERVICE_PROFILE") or "api"),
    )


_platform_route_handlers = register_platform_routes(
    app,
    deps={
        "pb": pb,
        "config": config,
        "build_service_topology": build_service_topology,
        "normalize_environment": _normalize_environment,
        "parse_boolean": _parse_boolean,
        "escape_filter_string": _escape_filter_string,
        "pick_effective_config_rows": _pick_effective_config_rows,
        "serialize_config_rows": _serialize_config_rows,
        "merge_service_topology": lambda *payloads: _merge_service_topology(*payloads),
        "fetch_compute_health": lambda environment: _fetch_compute_health(environment),
        "fetch_compute_status": lambda environment: _fetch_compute_status(environment),
        "fetch_runtime_health": lambda environment: _fetch_runtime_health(environment),
        "fetch_runtime_status": lambda environment: _fetch_runtime_status(environment),
        "as_dict": _as_dict,
        "load_daily_scan_state": lambda environment: _load_daily_scan_state(environment),
        "count_active_today_targets": lambda environment, market_date: _count_active_today_targets(environment, market_date),
        "build_statusz_compute_payload": lambda compute_payload, include_engines: _build_statusz_compute_payload(compute_payload, include_engines),
        "build_statusz_live_readiness": lambda compute_payload, runtime_payload: _build_statusz_live_readiness(compute_payload, runtime_payload),
        "build_statusz_runtime_payload": (
            lambda runtime_payload, include_warmup_details, *, live_readiness, fallback_state=None: _build_statusz_runtime_payload(
                runtime_payload,
                include_warmup_details,
                live_readiness=live_readiness,
                fallback_state=fallback_state,
            )
        ),
        "get_state_payload": lambda state_key, environment, date="global": _get_state_payload(state_key, environment, date=date),
        "normalize_two_factor_state_with_runtime": lambda state_data, runtime_status: _normalize_two_factor_state_with_runtime(
            state_data,
            runtime_status,
        ),
        "ibkr_2fa_state_key": IBKR_2FA_STATE_KEY,
        "ibkr_2fa_state_date": IBKR_2FA_STATE_DATE,
        "compute_base_url": COMPUTE_BASE_URL,
        "time_strings": _time_strings,
        "normalize_startup_state": lambda value, environment: _normalize_startup_state(value, environment),
        "build_startup_cycle_id": lambda environment: _build_startup_cycle_id(environment),
        "build_startup_label": lambda environment, startup_seq, started_at: _build_startup_label(environment, startup_seq, started_at),
        "startup_chat_id": lambda environment: _startup_chat_id(environment),
        "default_startup_steps": _default_startup_steps,
        "normalize_startup_fields": _normalize_startup_fields,
        "merge_startup_steps": _merge_startup_steps,
        "deliver_startup_progress_card": lambda state, environment: _deliver_startup_progress_card(state, environment),
        "resolve_startup_step_label": lambda state: _resolve_startup_step_label(state),
        "write_system_event_record": lambda *args, **kwargs: _write_system_event_record(*args, **kwargs),
        "ibkr_startup_state_key": IBKR_STARTUP_STATE_KEY,
        "ibkr_startup_state_date": IBKR_STARTUP_STATE_DATE,
        "config_value": _config_value,
        "signal_chat_id": _signal_chat_id,
        "console_base_url": _console_base_url,
        "feishu_send_interactive": _feishu_send_interactive,
        "feishu_update_interactive": _feishu_update_interactive,
        "request_two_factor_approval": _request_two_factor_approval,
        "emit_system_event": _emit_system_event,
        "label_title_with_environment": _label_title_with_environment,
        "add_environment_to_detail": _add_environment_to_detail,
        "deliver_system_event_notification": lambda *args, **kwargs: _deliver_system_event_notification(*args, **kwargs),
        "build_cron_payload": build_cron_payload,
        "scheduler_status": lambda environment: _scheduler_status(environment),
        "build_scheduler_summary": lambda environment, payload: _build_scheduler_summary(environment, payload),
        "augment_scheduler_summary": lambda summary, items: _augment_scheduler_summary(summary, items),
        "build_system_monitor_payload": lambda environment: _build_system_monitor_payload(environment),
        "build_system_summary_payload": lambda environment, lite_mode=False: _build_system_summary_payload(environment, lite_mode=lite_mode),
        "build_signal_expiry_response": lambda *args, **kwargs: build_signal_expiry_response(*args, **kwargs),
        "build_order_detail_integrity_response": lambda *args, **kwargs: build_order_detail_integrity_response(*args, **kwargs),
        "build_today_targets_response": lambda payload: build_today_targets_response(
            pb,
            payload=payload,
            normalize_environment=_normalize_environment,
            time_strings=_time_strings,
        ),
        "system_status_chat_id": _system_status_chat_id,
        "cancel_broker_order": lambda environment, order_id, payload=None: _cancel_broker_order_via_runtime_support(
            environment,
            order_id,
            payload,
            request_json_request=_request_json_request,
            runtime_base_url=RUNTIME_BASE_URL,
            normalize_environment=_normalize_environment,
            as_dict=_as_dict,
        ),
        "request_json_request": lambda method, base_url, path, params=None, json_body=None, timeout=5.0: _request_json_request(
            method,
            base_url,
            path,
            params=params,
            json_body=json_body,
            timeout=timeout,
        ),
        "runtime_base_url": RUNTIME_BASE_URL,
    },
)
globals().update(_platform_route_handlers)


_callback_toast = _callback_toast_support


def _upsert_tv_indicator(payload: dict[str, Any]):
    return _upsert_tv_indicator_support(
        payload,
        pb=pb,
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        jsonify_fn=jsonify,
    )


def _upsert_tv_signal(payload: dict[str, Any]):
    return _upsert_tv_signal_support(
        payload,
        pb=pb,
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        jsonify_fn=jsonify,
        time_strings=_time_strings,
    )


def _dispatch_feishu_2fa_callback(action: str, environment: str) -> dict[str, Any]:
    return _dispatch_feishu_2fa_callback_support(
        action,
        environment,
        request_json_request=_request_json_request,
        pb_base_url=PB_BASE_URL,
        as_dict=_as_dict,
        callback_toast_fn=_callback_toast,
    )


def _dispatch_feishu_signal_callback(action: str, signal_id: str, environment: str) -> tuple[dict[str, Any], int]:
    return _dispatch_feishu_signal_callback_support(
        action,
        signal_id,
        environment,
        pb=pb,
        escape_filter_string=_escape_filter_string,
        callback_toast_fn=_callback_toast,
    )


def _dispatch_feishu_order_callback(action: str, order_id: str, environment: str) -> tuple[dict[str, Any], int]:
    return _dispatch_feishu_order_callback_support(
        action,
        order_id,
        environment,
        pb=pb,
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        cancel_broker_order=_cancel_broker_order_via_runtime,
        build_order_cancel_group_response_fn=build_order_cancel_group_response,
        build_order_close_group_response_fn=build_order_close_group_response,
        callback_toast_fn=_callback_toast,
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
    deps={
        "pb": pb,
        "normalize_environment": _normalize_environment,
        "escape_filter_string": _escape_filter_string,
        "as_dict": _as_dict,
        "console_base_url": _console_base_url,
        "signal_chat_id": _signal_chat_id,
        "feishu_send_interactive": _feishu_send_interactive,
        "feishu_update_interactive": _feishu_update_interactive,
        "cancel_broker_order": lambda environment, order_id, payload=None: _cancel_broker_order_via_runtime(
            environment,
            order_id,
            payload,
        ),
        "build_signal_ingest_response": lambda *args, **kwargs: build_signal_ingest_response(*args, **kwargs),
        "build_signals_ingest_response": lambda *args, **kwargs: build_signals_ingest_response(*args, **kwargs),
        "build_signals_pending_response": lambda *args, **kwargs: build_signals_pending_response(*args, **kwargs),
        "build_signals_ack_response": lambda *args, **kwargs: build_signals_ack_response(*args, **kwargs),
        "build_order_upsert_response": lambda *args, **kwargs: build_order_upsert_response(*args, **kwargs),
        "build_signal_confirm_webhook_response": lambda *args, **kwargs: build_signal_confirm_webhook_response(*args, **kwargs),
        "build_signal_cancel_webhook_response": lambda *args, **kwargs: build_signal_cancel_webhook_response(*args, **kwargs),
        "config_value": _config_value,
        "get_state_payload": lambda state_key, environment, date="global": _get_state_payload(state_key, environment, date=date),
        "upsert_state": lambda state_key, environment, data, date="global": pb.upsert_state(state_key, environment, data, date=date),
        "build_orders_reconcile_response": lambda *args, **kwargs: build_orders_reconcile_response(*args, **kwargs),
        "build_order_cancel_sync_response": lambda *args, **kwargs: build_order_cancel_sync_response(*args, **kwargs),
        "build_order_cancel_group_response": lambda *args, **kwargs: build_order_cancel_group_response(*args, **kwargs),
        "build_order_close_group_response": lambda *args, **kwargs: build_order_close_group_response(*args, **kwargs),
        "build_order_cancel_webhook_response": lambda *args, **kwargs: build_order_cancel_webhook_response(*args, **kwargs),
        "build_order_close_webhook_response": lambda *args, **kwargs: build_order_close_webhook_response(*args, **kwargs),
        "build_reverse_list_response": lambda *args, **kwargs: build_reverse_list_response(*args, **kwargs),
        "build_reverse_calculate_response": lambda *args, **kwargs: build_reverse_calculate_response(*args, **kwargs),
        "build_reverse_pending_response": lambda *args, **kwargs: build_reverse_pending_response(*args, **kwargs),
        "build_reverse_dispatch_response": lambda *args, **kwargs: build_reverse_dispatch_response(*args, **kwargs),
        "build_reverse_ack_response": lambda *args, **kwargs: build_reverse_ack_response(*args, **kwargs),
        "upsert_tv_indicator": lambda payload: _upsert_tv_indicator(payload),
        "upsert_tv_signal": lambda payload: _upsert_tv_signal(payload),
        "request_json_request": lambda method, base_url, path, params=None, json_body=None, timeout=5.0: _request_json_request(
            method,
            base_url,
            path,
            params=params,
            json_body=json_body,
            timeout=timeout,
        ),
        "runtime_base_url": RUNTIME_BASE_URL,
        "inspect_runtime_environment": lambda environment: inspect_requested_runtime_environment(
            environment,
            normalize_environment=_normalize_environment,
            fetch_runtime_status=_fetch_runtime_status,
            as_dict=_as_dict,
        ),
        "build_runtime_environment_mismatch_payload": build_runtime_environment_mismatch_payload,
        "emit_system_event": _emit_system_event,
        "fetch_runtime_status": lambda environment: _fetch_runtime_status(environment),
        "merge_startup_steps": _merge_startup_steps,
        "deliver_startup_progress_card": lambda state, environment: _deliver_startup_progress_card(state, environment),
        "handle_feishu_callback": _handle_feishu_callback_support,
        "dispatch_feishu_2fa_callback": lambda action, environment: _dispatch_feishu_2fa_callback(action, environment),
        "dispatch_feishu_order_callback": lambda action, order_id, environment: _dispatch_feishu_order_callback(
            action,
            order_id,
            environment,
        ),
        "dispatch_feishu_signal_callback": lambda action, signal_id, environment: _dispatch_feishu_signal_callback(
            action,
            signal_id,
            environment,
        ),
        "callback_toast": _callback_toast,
        "callback_response": _feishu_callback_response,
    },
)
globals().update(_trading_route_handlers)


_compat_route_handlers = register_compat_proxy_routes(
    app,
    deps={
        "pb_base_url": PB_BASE_URL,
        "compute_base_url": COMPUTE_BASE_URL,
        "runtime_base_url": RUNTIME_BASE_URL,
        "scheduler_base_url": SCHEDULER_BASE_URL,
        "direct_proxy_map": DIRECT_PROXY_MAP,
        "action_proxy_map": ACTION_PROXY_MAP,
        "delegated_pocketbase_custom_routes": DELEGATED_POCKETBASE_CUSTOM_ROUTES,
        "delegated_pocketbase_webhook_routes": DELEGATED_POCKETBASE_WEBHOOK_ROUTES,
        "build_service_topology": build_service_topology,
        "config": config,
        "normalize_environment": _normalize_environment,
        "scheduler_status": lambda environment: _scheduler_status(environment),
        "scheduler_job_states": lambda environment="live": _scheduler_job_states(environment),
        "build_cron_payload": build_cron_payload,
        "build_scheduler_summary": lambda environment, payload: _build_scheduler_summary(environment, payload),
        "augment_scheduler_summary": lambda summary, items: _augment_scheduler_summary(summary, items),
        "forward_request": lambda base_url, path, params=None, json_body=None: _forward_request(
            base_url,
            path,
            params=params,
            json_body=json_body,
        ),
        "proxy_custom_to_pb": lambda subpath: _proxy_custom_to_pb(subpath),
        "proxy_webhook_to_pb": lambda subpath: _proxy_webhook_to_pb(subpath),
    },
)
globals().update(_compat_route_handlers)
