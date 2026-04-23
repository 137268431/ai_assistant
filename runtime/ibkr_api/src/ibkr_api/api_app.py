from __future__ import annotations

from functools import partial
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, jsonify, request

from ibkr_api.callbacks.feishu import (
    callback_toast as _callback_toast_support,
    dispatch_feishu_2fa_callback as _dispatch_feishu_2fa_callback_support,
    dispatch_feishu_order_callback as _dispatch_feishu_order_callback_support,
    dispatch_feishu_signal_callback as _dispatch_feishu_signal_callback_support,
    handle_feishu_callback as _handle_feishu_callback_support,
)
from ibkr_api.callbacks.routes import register_callback_routes
from ibkr_api.compat.routes import register_compat_routes
from ibkr_api.control.routes import register_control_routes
from ibkr_api.control.runtime_guard import (
    build_runtime_environment_mismatch_payload,
    inspect_requested_runtime_environment,
)
from ibkr_api.integrations.feishu import feishu_send_interactive, feishu_suppressed, feishu_token, feishu_update_interactive
from ibkr_api.integrations.runtime_orders import cancel_broker_order_via_runtime as _cancel_broker_order_via_runtime_support
from ibkr_api.orders.routes import register_order_routes
from ibkr_api.orders.group_cancel import build_order_cancel_group_response
from ibkr_api.orders.group_close import build_order_close_group_response
from ibkr_api.orders.integrity import build_order_detail_integrity_response
from ibkr_api.orders.webhooks import build_order_cancel_webhook_response, build_order_close_webhook_response
from ibkr_api.orders.upsert import build_order_upsert_response
from ibkr_api.orders.reconcile import build_orders_reconcile_response
from ibkr_api.reverse.actions import build_reverse_ack_response, build_reverse_dispatch_response
from ibkr_api.reverse.routes import register_reverse_routes
from ibkr_api.reverse.calculate import build_reverse_calculate_response
from ibkr_api.reverse.queries import build_reverse_list_response, build_reverse_pending_response
from ibkr_api.signals.expiry import build_signal_expiry_response
from ibkr_api.signals.routes import register_signal_routes
from ibkr_api.signals.ingest import build_signal_ingest_response, build_signals_ingest_response
from ibkr_api.signals.webhooks import build_signal_cancel_webhook_response, build_signal_confirm_webhook_response
from ibkr_api.runtime.routes import register_runtime_routes
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
from ibkr_api.startup.routes import register_startup_routes
from ibkr_api.state.routes import register_state_routes
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
from ibkr_api.system.routes import register_system_routes
from ibkr_api.two_factor.routes import register_two_factor_routes
from ibkr_api.tradingview.ingest import upsert_tv_indicator as _upsert_tv_indicator_support, upsert_tv_signal as _upsert_tv_signal_support
from ibkr_api.tradingview.routes import register_tradingview_routes
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
DEFAULT_CONSOLE_BASE_URL = str(os.environ.get("CONSOLE_BASE_URL") or os.environ.get("PB_PUBLIC_URL") or "https://pb.lzw-glory.top").rstrip("/")
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
pb = PBClient(base_url=PB_BASE_URL)
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
    ("GET", "ibkr/account_snapshot"): (RUNTIME_BASE_URL, "/ibkr/account"),
    ("GET", "ibkr/positions"): (RUNTIME_BASE_URL, "/ibkr/positions"),
    ("GET", "ibkr/orders/live"): (RUNTIME_BASE_URL, "/ibkr/orders/live"),
    ("GET", "ibkr/orders/history"): (RUNTIME_BASE_URL, "/ibkr/orders/history"),
    ("POST", "ibkr/2fa/takeover"): (RUNTIME_BASE_URL, "/ibkr/2fa/takeover"),
    ("POST", "ibkr/2fa/probe"): (RUNTIME_BASE_URL, "/ibkr/2fa/probe"),
    ("POST", "ibkr/2fa/panic-reset"): (RUNTIME_BASE_URL, "/ibkr/panic-reset"),
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


def _normalize_environment(value: Any, default: str = "live") -> str:
    text = str(value or "").strip().lower()
    return text or default


def _parse_boolean(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    return bool(default)


def _escape_filter_string(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _normalize_symbol_list(values: Any) -> list[str]:
    source = values if isinstance(values, list) else [values]
    items: list[str] = []
    seen: set[str] = set()

    def _append_symbol(raw: Any) -> None:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen:
            return
        seen.add(symbol)
        items.append(symbol)

    for value in source:
        if isinstance(value, list):
            for nested in value:
                _append_symbol(nested)
            continue
        _append_symbol(value)
    return items


def _trim_array(values: Any, limit: int) -> list[Any]:
    if not isinstance(values, list):
        return []
    max_items = max(0, int(limit or 0))
    return list(values[:max_items]) if max_items > 0 else []


def _trim_object_entries(value: Any, limit: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    max_items = max(0, int(limit or 0))
    if max_items <= 0 or len(value) <= max_items:
        return dict(value)
    trimmed: dict[str, Any] = {}
    for key, item in list(value.items())[:max_items]:
        trimmed[str(key)] = item
    return trimmed


def _parse_et_time_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return int(parsed.timestamp() * 1000)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=ET)
            return int(parsed.timestamp() * 1000)
        except Exception:
            continue
    return 0


def _pick_effective_config_rows(rows: list[dict[str, Any]], environment: str) -> list[dict[str, Any]]:
    runtime_environment = _normalize_environment(environment)
    priority = {"": 0, "global": 1, runtime_environment: 2}
    selected: dict[str, dict[str, Any]] = {}

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        row_environment = str(row.get("environment") or "").strip().lower()
        row_priority = priority.get(row_environment, -1)
        current = selected.get(key)
        current_priority = priority.get(str((current or {}).get("environment") or "").strip().lower(), -1)
        if current is None or row_priority >= current_priority:
            selected[key] = row

    return [selected[key] for key in sorted(selected)]


def _serialize_config_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "key": str(row.get("key") or ""),
                "value": row.get("value") or "",
                "environment": str(row.get("environment") or ""),
                "updated": row.get("updated") or "",
            }
        )
    return items


def _load_effective_config_rows(environment: str) -> list[dict[str, Any]]:
    runtime_environment = _normalize_environment(environment, "live")
    rows: list[dict[str, Any]] = []
    try:
        rows = pb.get_runtime_config(scope="all", environment=runtime_environment)
    except Exception:
        rows = []
    if not rows:
        try:
            rows = pb.get_all_records("config", sort="sort_order,key", max_pages=20)
        except Exception:
            rows = []
    return _pick_effective_config_rows(rows, runtime_environment)


def _load_effective_config_map(environment: str, selected_keys: tuple[str, ...] | list[str] | set[str] | None = None) -> dict[str, str]:
    allowed = {str(key or "").strip() for key in (selected_keys or []) if str(key or "").strip()}
    config_map: dict[str, str] = {}
    for row in _load_effective_config_rows(environment):
        key = str(row.get("key") or "").strip()
        if not key or (allowed and key not in allowed):
            continue
        config_map[key] = str(row.get("value") or "")
    return config_map


def _load_recent_system_events(environment: str, limit: int = 20) -> list[dict[str, Any]]:
    runtime_environment = _normalize_environment(environment, "live")
    safe_limit = max(1, min(200, int(limit or 20)))
    filter_expr = f'environment = "{_escape_filter_string(runtime_environment)}"'
    rows: list[dict[str, Any]] = []
    try:
        get_records = getattr(pb, "get_records", None)
        if callable(get_records):
            rows = get_records("system_events", filter=filter_expr, sort="-created", per_page=safe_limit, page=1)
        else:
            rows = (pb.get_all_records("system_events", filter=filter_expr, sort="-created", max_pages=1) or [])[:safe_limit]
    except Exception:
        rows = []

    items: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "id": str(row.get("id") or ""),
                "event_type": str(row.get("event_type") or ""),
                "level": str(row.get("level") or ""),
                "source": str(row.get("source") or ""),
                "environment": str(row.get("environment") or runtime_environment),
                "title": str(row.get("title") or ""),
                "notified": bool(row.get("notified")),
                "us_time": row.get("us_time") or "",
                "created": row.get("created") or "",
            }
        )
    return items


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
    return str(os.environ.get("CONSOLE_BASE_URL") or os.environ.get("PB_PUBLIC_URL") or DEFAULT_CONSOLE_BASE_URL).rstrip("/")


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
    response = jsonify(payload)
    if hasattr(response, "headers") and isinstance(headers, dict):
        for key, value in headers.items():
            response.headers[str(key)] = str(value)
    if hasattr(response, "headers"):
        return response, int(status_code or 200)
    if int(status_code or 200) == 200:
        return payload
    return payload, int(status_code or 200)


def _feishu_callback_response(payload: dict[str, Any], *, update_token: str = "", status_code: int = 200):
    headers = {"update_card_token": update_token} if update_token else {}
    return _json_response(payload, status_code=status_code, headers=headers)


def _build_response_from_upstream(response: requests.Response) -> Response:
    headers = [
        (key, value)
        for key, value in response.headers.items()
        if key.lower() not in EXCLUDED_RESPONSE_HEADERS
    ]
    return Response(response.content, status=response.status_code, headers=headers)


def _request_json(base_url: str, path: str, *, params: list[tuple[str, str]] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    target_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        response = requests.get(
            target_url,
            params=params,
            timeout=max(1.0, float(timeout or 0)),
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status_code": 0,
            "payload": {},
            "error": str(exc),
            "target_url": target_url,
        }

    payload: Any = {}
    try:
        payload = response.json() if response.content else {}
    except Exception:
        payload = {}
    return {
        "ok": bool(response.ok),
        "status_code": int(response.status_code),
        "payload": payload if isinstance(payload, dict) else {},
        "target_url": target_url,
        "error": "",
    }


def _request_json_request(
    method: str,
    base_url: str,
    path: str,
    *,
    params: list[tuple[str, str]] | None = None,
    json_body: Any = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    target_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        response = requests.request(
            method=method.upper(),
            url=target_url,
            params=params,
            json=json_body,
            timeout=max(1.0, float(timeout or 0)),
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status_code": 0,
            "payload": {},
            "error": str(exc),
            "target_url": target_url,
        }

    payload: Any = {}
    try:
        payload = response.json() if response.content else {}
    except Exception:
        payload = {}
    return {
        "ok": bool(response.ok),
        "status_code": int(response.status_code),
        "payload": payload if isinstance(payload, dict) else {},
        "target_url": target_url,
        "error": "",
    }


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
    intervals = cursor_payload.get("intervals") if isinstance(cursor_payload.get("intervals"), dict) else {}
    bucket = intervals.get(interval) if isinstance(intervals, dict) else {}
    return dict(bucket) if isinstance(bucket, dict) else {}


def _build_scheduler_summary(environment: str, scheduler_payload: dict[str, Any]) -> dict[str, Any]:
    payload = scheduler_payload if isinstance(scheduler_payload, dict) else {}
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), dict) else {}
    ingest_cursor = payload.get("ingest_cursor") if isinstance(payload.get("ingest_cursor"), dict) else {}
    dispatch_cursor = payload.get("compute_dispatch_cursor") if isinstance(payload.get("compute_dispatch_cursor"), dict) else {}
    ingest_5m = _extract_cursor_interval(ingest_cursor, "5m")
    dispatch_5m = _extract_cursor_interval(dispatch_cursor, "5m")
    latest_ingested_bar_time_ms = int(ingest_5m.get("latest_bar_time_ms") or 0)
    latest_dispatched_bar_time_ms = int(dispatch_5m.get("latest_bar_time_ms") or 0)
    lag_ms = max(0, latest_ingested_bar_time_ms - latest_dispatched_bar_time_ms) if latest_ingested_bar_time_ms else 0

    status_counts: dict[str, int] = {}
    for state in jobs.values():
        normalized = str((state or {}).get("status") or "idle").strip().lower() or "idle"
        status_counts[normalized] = status_counts.get(normalized, 0) + 1

    return {
        "ok": bool(payload.get("ok", False)) if payload else False,
        "status": str(payload.get("status") or ("running" if payload else "offline")).strip().lower() or "offline",
        "environment": str(payload.get("environment") or environment).strip().lower() or environment,
        "loop_interval_seconds": float(payload.get("loop_interval_seconds") or 0),
        "job_count": len(jobs),
        "job_status_counts": status_counts,
        "jobs": jobs,
        "ingest_cursor": ingest_cursor,
        "compute_dispatch_cursor": dispatch_cursor,
        "latest_ingested_bar_time_ms": latest_ingested_bar_time_ms,
        "latest_dispatched_bar_time_ms": latest_dispatched_bar_time_ms,
        "last_dispatch_at_ms": int(dispatch_5m.get("last_dispatched_at_ms") or 0),
        "dispatch_lag_ms": lag_ms,
        "dispatch_lag_min": round(lag_ms / 60000.0, 2) if lag_ms else 0.0,
    }


def _scheduler_status(environment: str = "live") -> dict[str, Any]:
    result = _request_json(
        SCHEDULER_BASE_URL,
        "/status",
        params=[("environment", environment)],
        timeout=5,
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if payload:
        return {
            **payload,
            "ok": bool(payload.get("ok", result.get("ok", False))),
            "_meta": {
                "target_url": result.get("target_url"),
                "status_code": result.get("status_code"),
                "error": result.get("error") or "",
            },
        }
    return {
        "ok": False,
        "status": "offline",
        "environment": environment,
        "jobs": {},
        "ingest_cursor": {},
        "compute_dispatch_cursor": {},
        "_meta": {
            "target_url": result.get("target_url"),
            "status_code": result.get("status_code"),
            "error": result.get("error") or "scheduler_unavailable",
        },
    }


def _scheduler_job_states(environment: str = "live") -> dict[str, Any]:
    payload = _scheduler_status(environment)
    jobs = payload.get("jobs") if isinstance(payload, dict) else {}
    return jobs if isinstance(jobs, dict) else {}


def _augment_scheduler_summary(summary: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    definitions = items if isinstance(items, list) else []
    return {
        **(summary if isinstance(summary, dict) else {}),
        "enabled_job_count": sum(1 for item in definitions if bool(item.get("effective_enabled"))),
        "native_job_count": sum(1 for item in definitions if str(item.get("runner_kind") or "").startswith("native_")),
        "compatibility_job_count": sum(
            1 for item in definitions if str(item.get("runner_kind") or "").strip().lower() == "compatibility_pending"
        ),
    }


def _build_system_summary_payload(environment: str, *, lite_mode: bool) -> dict[str, Any]:
    runtime_environment = _normalize_environment(environment, "live")
    config_map = _load_effective_config_map(runtime_environment)
    compute_enabled = runtime_environment != "backtest" and _is_enabled_text(config_map.get("ibkr_compute_enabled", "TRUE"))
    trading_enabled = runtime_environment != "backtest" and _is_enabled_text(
        config_map.get("ibkr_trading_enabled", config_map.get("trading_enabled", "TRUE"))
    )

    compute_health = _fetch_compute_health(runtime_environment)
    compute_status = _fetch_compute_status(runtime_environment)
    runtime_status = _fetch_runtime_status(runtime_environment)

    compute_health_payload = _as_dict(compute_health.get("payload"))
    compute_status_payload = _as_dict(compute_status.get("payload"))
    runtime_payload = _as_dict(runtime_status.get("payload"))

    compute_summary = {
        "ok": bool(compute_health.get("ok")) or bool(compute_status.get("ok")) or bool(compute_health_payload) or bool(compute_status_payload),
        "status": str(
            compute_status_payload.get("status")
            or compute_health_payload.get("status")
            or ("running" if (compute_health.get("ok") or compute_status.get("ok")) else "offline")
        ).strip().lower() or "offline",
        "engines": (
            compute_status_payload.get("engines")
            if isinstance(compute_status_payload.get("engines"), dict) and not lite_mode
            else {}
        ),
        "total_engines": int(compute_status_payload.get("total_engines") or compute_health_payload.get("total_engines") or 0),
        "ready_engines": int(compute_status_payload.get("ready_engines") or compute_health_payload.get("ready_engines") or 0),
        "compute_count": int(compute_health_payload.get("compute_count") or compute_status_payload.get("compute_count") or 0),
        "error_count": int(compute_health_payload.get("error_count") or compute_status_payload.get("error_count") or 0),
        "uptime_s": int(compute_health_payload.get("uptime_s") or 0),
        "last_compute": compute_health_payload.get("last_compute") or compute_status_payload.get("last_compute"),
        "last_scan": compute_health_payload.get("last_scan") or compute_status_payload.get("last_scan"),
        "compute_startup_preload": _as_dict(
            compute_status_payload.get("compute_startup_preload") or compute_health_payload.get("compute_startup_preload")
        ),
        "service_topology": _merge_service_topology(compute_status_payload, compute_health_payload),
    }
    if compute_health.get("error") or compute_status.get("error"):
        compute_summary["error"] = "; ".join(
            part for part in (str(compute_health.get("error") or ""), str(compute_status.get("error") or "")) if part
        )

    merged_topology = _merge_service_topology(compute_summary, runtime_payload)
    runtime_summary = {
        "ok": bool(runtime_status.get("ok")) or bool(runtime_payload),
        "status": str(runtime_payload.get("status") or ("running" if runtime_payload else "offline")).strip().lower() or "offline",
        "environment": _normalize_environment(runtime_payload.get("environment") or runtime_environment, runtime_environment),
        "service_topology": merged_topology,
        "proxy_upstream": runtime_status.get("selected_upstream") or runtime_status.get("proxy_upstream") or "",
    }
    if runtime_status.get("error"):
        runtime_summary["error"] = str(runtime_status.get("error") or "")

    actual_runtime_environment = _normalize_environment(runtime_summary.get("environment") or runtime_environment, runtime_environment)
    ok = bool(compute_summary.get("ok")) and (bool(runtime_summary.get("ok")) or not runtime_payload)
    degraded = bool(compute_summary.get("ok")) or bool(runtime_summary.get("ok")) or bool(runtime_payload)
    return {
        "ok": ok,
        "status": "running" if ok else ("degraded" if degraded else "offline"),
        "timestamp": _time_strings()["us"],
        "environment": runtime_environment,
        "requested_environment": runtime_environment,
        "actual_runtime_environment": actual_runtime_environment,
        "runtime_environment_mismatch": actual_runtime_environment != runtime_environment,
        "compute_enabled": compute_enabled,
        "ibkr_trading_enabled": trading_enabled,
        "config": config_map,
        "today": {
            "ibkr_signals": 0,
            "ibkr_indicators": 0,
            "orders": 0,
            "ibkr_bars": 0,
            "ibkr_targets": 0,
            "events": 0,
        },
        "ibkr_compute": compute_summary,
        "ibkr_runtime": runtime_summary,
        "service_topology": merged_topology,
        "recent_events": _load_recent_system_events(runtime_environment, 20),
        "data_freshness": [],
        "lite_mode": bool(lite_mode),
        "source": "ibkr-api",
    }


def _probe_console_status() -> dict[str, Any]:
    console_base_url = str(os.environ.get("CONSOLE_BASE_URL") or os.environ.get("PB_PUBLIC_URL") or "").rstrip("/")
    if not console_base_url:
        return {
            "ok": False,
            "status_code": 0,
            "target_url": "",
            "error": "console_base_url_missing",
        }
    target_url = f"{console_base_url}/index.html"
    try:
        response = requests.get(target_url, timeout=5)
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status_code": 0,
            "target_url": target_url,
            "error": str(exc),
        }
    return {
        "ok": bool(response.ok),
        "status_code": int(response.status_code),
        "target_url": target_url,
        "error": "",
    }


def _derive_monitor_service_map(
    environment: str,
    base_payload: dict[str, Any],
    scheduler_summary: dict[str, Any],
    *,
    console_probe: dict[str, Any],
    pb_health: dict[str, Any],
) -> dict[str, Any]:
    topology = base_payload.get("service_topology") if isinstance(base_payload.get("service_topology"), dict) else build_service_topology()
    services = topology.get("services") if isinstance(topology.get("services"), dict) else {}
    runtime = base_payload.get("runtime") if isinstance(base_payload.get("runtime"), dict) else {}
    gateway = runtime.get("gateway") if isinstance(runtime.get("gateway"), dict) else {}
    compute = base_payload.get("compute") if isinstance(base_payload.get("compute"), dict) else {}
    monitor_status = str(base_payload.get("status") or "").strip().lower()

    def _topology_meta(name: str) -> dict[str, Any]:
        item = services.get(name) if isinstance(services.get(name), dict) else {}
        return dict(item)

    def _detail_parts(*parts: Any) -> str:
        normalized = [str(part).strip() for part in parts if str(part or "").strip()]
        return " · ".join(normalized)

    console_meta = _topology_meta("ibkr-console")
    console_running = bool(console_probe.get("ok"))
    pb_meta = _topology_meta("pocketbase")
    pb_disk = ((base_payload.get("pocketbase") or {}).get("disk") or {}) if isinstance(base_payload.get("pocketbase"), dict) else {}
    pb_flags = [item for item in (base_payload.get("flags") or []) if str((item or {}).get("code") or "").startswith("pb_")]
    pb_status = "running" if pb_health.get("ok") else "offline"
    if pb_status == "running" and pb_flags:
        pb_status = "degraded"
    elif pb_status != "running" and pb_disk.get("status") == "partial":
        pb_status = "degraded"

    compute_status = "running"
    if monitor_status in {"offline", "error"}:
        compute_status = "offline"
    elif monitor_status in {"warning", "warn", "degraded"}:
        compute_status = "degraded"

    runtime_status = "running" if runtime else "offline"
    if runtime and not bool(gateway.get("running") or gateway.get("reachable")):
        runtime_status = "degraded"
    if not runtime and compute_status != "running":
        runtime_status = "offline"

    gateway_status = "running" if bool(gateway.get("running") or gateway.get("reachable")) else "offline"
    scheduler_status = str(scheduler_summary.get("status") or "").strip().lower() or "offline"
    if scheduler_status == "running" and float(scheduler_summary.get("dispatch_lag_min") or 0) >= 10:
        scheduler_status = "degraded"

    service_map = {
        "ibkr-console": {
            **console_meta,
            "status": "running" if console_running else "offline",
            "detail": _detail_parts(
                "static console",
                console_probe.get("target_url"),
                f"http {console_probe.get('status_code')}" if console_probe.get("status_code") else console_probe.get("error"),
            ),
        },
        "ibkr-api": {
            **_topology_meta("ibkr-api"),
            "status": "running",
            "detail": _detail_parts(
                "compat routes active",
                f"env {environment}",
                f"scheduler jobs {int(scheduler_summary.get('job_count') or 0)}",
            ),
        },
        "ibkr-scheduler": {
            **_topology_meta("ibkr-scheduler"),
            "status": scheduler_status,
            "detail": _detail_parts(
                f"loop {int(float(scheduler_summary.get('loop_interval_seconds') or 0))}s" if scheduler_summary.get("loop_interval_seconds") else "",
                (
                    f"lag {float(scheduler_summary.get('dispatch_lag_min') or 0):.2f}m"
                    if scheduler_summary.get("latest_ingested_bar_time_ms")
                    else "awaiting bars"
                ),
                f"jobs {int(scheduler_summary.get('job_count') or 0)}",
            ),
        },
        "ibkr-compute": {
            **_topology_meta("ibkr-compute"),
            "status": compute_status,
            "detail": _detail_parts(
                f"engines {int(compute.get('ready_engines') or 0)}/{int(compute.get('total_engines') or 0)}",
                f"compute {int(compute.get('compute_count') or 0)}",
                f"tracked {int(compute.get('tracked_cursors') or 0)}",
            ),
        },
        "ibkr-runtime": {
            **_topology_meta("ibkr-runtime"),
            "status": runtime_status,
            "detail": _detail_parts(
                f"phase {runtime.get('runtime_phase') or '--'}",
                f"session {'AUTHED' if ((runtime.get('session') or {}).get('authenticated')) else 'PENDING'}",
                f"ws {'READY' if ((runtime.get('websocket') or {}).get('connected')) else 'PENDING'}",
            ),
        },
        "ibkr-gateway": {
            **_topology_meta("ibkr-gateway"),
            "status": gateway_status,
            "detail": _detail_parts(
                f"managed_by {gateway.get('managed_by') or '--'}",
                f"pid {int(gateway.get('pid') or 0)}" if gateway.get("pid") else "",
                "reachable" if gateway.get("reachable") else "not reachable",
            ),
        },
        "pocketbase": {
            **pb_meta,
            "status": pb_status,
            "detail": _detail_parts(
                f"pb_data {pb_disk.get('data_path') or '--'}",
                f"size {pb_disk.get('status') or 'unknown'}",
                f"http {pb_health.get('status_code')}" if pb_health.get("status_code") else pb_health.get("error"),
            ),
        },
    }

    counts: dict[str, int] = {}
    for service in service_map.values():
        normalized = str(service.get("status") or "unknown").strip().lower() or "unknown"
        counts[normalized] = counts.get(normalized, 0) + 1
    return {
        "environment": environment,
        "services": service_map,
        "status_counts": counts,
    }


def _build_system_monitor_payload(environment: str) -> dict[str, Any]:
    runtime_environment = _normalize_environment(environment, "live")
    base_monitor_result = _fetch_compute_monitor(runtime_environment)
    base_payload = _as_dict(base_monitor_result.get("payload"))
    config.refresh()
    scheduler_status = _scheduler_status(runtime_environment)
    scheduler_jobs = scheduler_status.get("jobs") if isinstance(scheduler_status.get("jobs"), dict) else {}
    scheduler_items = build_cron_payload(config, runtime_environment, scheduler_jobs)
    scheduler_summary = _augment_scheduler_summary(_build_scheduler_summary(runtime_environment, scheduler_status), scheduler_items)
    pb_health = _request_json(PB_BASE_URL, "/api/health", timeout=5)
    console_probe = _probe_console_status()

    merged_payload = dict(base_payload)
    merged_payload.setdefault("ok", bool(base_monitor_result.get("ok", False)))
    merged_payload["status"] = str(
        merged_payload.get("status") or ("offline" if merged_payload.get("ok") is False else "ok")
    ).strip().lower() or "ok"
    actual_runtime_environment = _normalize_environment(
        merged_payload.get("environment") or _as_dict(merged_payload.get("runtime")).get("environment") or runtime_environment,
        runtime_environment,
    )
    merged_payload["requested_environment"] = runtime_environment
    merged_payload["actual_runtime_environment"] = actual_runtime_environment
    merged_payload["runtime_environment_mismatch"] = actual_runtime_environment != runtime_environment
    merged_payload["config"] = _load_effective_config_map(runtime_environment, MONITOR_CONFIG_KEYS)
    merged_payload["recent_events"] = _load_recent_system_events(runtime_environment, 20)
    merged_payload["source"] = "ibkr-api"
    merged_payload["upstream_monitor"] = {
        "ok": bool(base_monitor_result.get("ok", False)),
        "status_code": int(base_monitor_result.get("status_code") or 0),
        "target_url": base_monitor_result.get("target_url") or "",
        "error": base_monitor_result.get("error") or "",
    }
    merged_payload["scheduler"] = scheduler_summary
    merged_payload["control_plane"] = {
        "api": {
            "ok": True,
            "status": "running",
            "service_profile": str(os.environ.get("IBKR_SERVICE_PROFILE") or "api"),
        },
        "scheduler": scheduler_summary,
    }
    merged_payload["service_topology"] = _merge_service_topology(merged_payload, build_service_topology())
    merged_payload = _enrich_monitor_payload_with_pocketbase_disk(merged_payload)
    merged_payload["service_monitor"] = _derive_monitor_service_map(
        runtime_environment,
        merged_payload,
        scheduler_summary,
        console_probe=console_probe,
        pb_health=pb_health,
    )
    return merged_payload


_system_route_handlers = register_system_routes(
    app,
    deps={
        "pb": pb,
        "config": config,
        "normalize_environment": _normalize_environment,
        "parse_boolean": _parse_boolean,
        "escape_filter_string": _escape_filter_string,
        "config_value": _config_value,
        "signal_chat_id": _signal_chat_id,
        "console_base_url": _console_base_url,
        "feishu_send_interactive": _feishu_send_interactive,
        "feishu_update_interactive": _feishu_update_interactive,
        "emit_system_event": _emit_system_event,
        "label_title_with_environment": _label_title_with_environment,
        "add_environment_to_detail": _add_environment_to_detail,
        "write_system_event_record": lambda *args, **kwargs: _write_system_event_record(*args, **kwargs),
        "deliver_system_event_notification": lambda *args, **kwargs: _deliver_system_event_notification(*args, **kwargs),
        "build_cron_payload": build_cron_payload,
        "scheduler_status": lambda environment: _scheduler_status(environment),
        "build_scheduler_summary": lambda environment, payload: _build_scheduler_summary(environment, payload),
        "augment_scheduler_summary": lambda summary, items: _augment_scheduler_summary(summary, items),
        "build_system_monitor_payload": lambda environment: _build_system_monitor_payload(environment),
        "build_system_summary_payload": lambda environment, lite_mode=False: _build_system_summary_payload(environment, lite_mode=lite_mode),
        "build_service_topology": build_service_topology,
        "time_strings": _time_strings,
        "get_state_payload": _get_state_payload,
        "normalize_two_factor_state_with_runtime": _normalize_two_factor_state_with_runtime,
        "fetch_runtime_status": _fetch_runtime_status,
        "request_two_factor_approval": _request_two_factor_approval,
        "build_signal_expiry_response": lambda *args, **kwargs: build_signal_expiry_response(*args, **kwargs),
        "build_order_detail_integrity_response": lambda *args, **kwargs: build_order_detail_integrity_response(*args, **kwargs),
        "cancel_broker_order": lambda environment, order_id, payload=None: _cancel_broker_order_via_runtime_support(
            environment,
            order_id,
            payload,
            request_json_request=_request_json_request,
            runtime_base_url=RUNTIME_BASE_URL,
            normalize_environment=_normalize_environment,
            as_dict=_as_dict,
        ),
    },
)
custom_ibkr_health_report = _system_route_handlers["custom_ibkr_health_report"]
custom_ibkr_notify = _system_route_handlers["custom_ibkr_notify"]
custom_system_event = _system_route_handlers["custom_system_event"]
custom_system_cronz = _system_route_handlers["custom_system_cronz"]
custom_system_healthz = _system_route_handlers["custom_system_healthz"]
custom_system_schedulerz = _system_route_handlers["custom_system_schedulerz"]
custom_system_summaryz = _system_route_handlers["custom_system_summaryz"]
custom_system_monitorz = _system_route_handlers["custom_system_monitorz"]
custom_system_job_signal_expiry = _system_route_handlers["custom_system_job_signal_expiry"]
custom_system_job_order_detail_integrity = _system_route_handlers["custom_system_job_order_detail_integrity"]
custom_system_job_order_expiry = _system_route_handlers["custom_system_job_order_expiry"]
custom_system_job_auth_edge_guard = _system_route_handlers["custom_system_job_auth_edge_guard"]
custom_system_job_auth_pending_guard = _system_route_handlers["custom_system_job_auth_pending_guard"]
custom_system_job_data_gap_guard = _system_route_handlers["custom_system_job_data_gap_guard"]
custom_system_job_two_factor_hourly_check = _system_route_handlers["custom_system_job_two_factor_hourly_check"]
custom_system_job_weekly_reauth_reminder = _system_route_handlers["custom_system_job_weekly_reauth_reminder"]
custom_system_job_weekly_reauth_followup = _system_route_handlers["custom_system_job_weekly_reauth_followup"]
custom_system_job_market_open_reminder = _system_route_handlers["custom_system_job_market_open_reminder"]
custom_system_job_daily_report = _system_route_handlers["custom_system_job_daily_report"]


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


_runtime_route_handlers = register_runtime_routes(
    app,
    deps={
        "pb": pb,
        "build_service_topology": build_service_topology,
        "normalize_environment": _normalize_environment,
        "parse_boolean": _parse_boolean,
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
        "normalize_two_factor_state_with_runtime": lambda state_data, runtime_status: _normalize_two_factor_state_with_runtime(state_data, runtime_status),
        "ibkr_2fa_state_key": IBKR_2FA_STATE_KEY,
        "ibkr_2fa_state_date": IBKR_2FA_STATE_DATE,
        "compute_base_url": COMPUTE_BASE_URL,
    },
)
custom_ibkr_runtime_config = _runtime_route_handlers["custom_ibkr_runtime_config"]
custom_ibkr_healthz = _runtime_route_handlers["custom_ibkr_healthz"]
custom_ibkr_statusz = _runtime_route_handlers["custom_ibkr_statusz"]
custom_ibkr_two_factor_status = _runtime_route_handlers["custom_ibkr_two_factor_status"]


_startup_route_handlers = register_startup_routes(
    app,
    deps={
        "pb": pb,
        "normalize_environment": _normalize_environment,
        "time_strings": _time_strings,
        "get_state_payload": lambda state_key, environment, date="global": _get_state_payload(state_key, environment, date=date),
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
        "as_dict": _as_dict,
    },
)
custom_ibkr_startup_progress = _startup_route_handlers["custom_ibkr_startup_progress"]
custom_ibkr_startup_status = _startup_route_handlers["custom_ibkr_startup_status"]


_signal_route_handlers = register_signal_routes(
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
        "cancel_broker_order": lambda environment, order_id, payload=None: _cancel_broker_order_via_runtime(environment, order_id, payload),
        "build_signal_ingest_response": lambda *args, **kwargs: build_signal_ingest_response(*args, **kwargs),
        "build_signals_ingest_response": lambda *args, **kwargs: build_signals_ingest_response(*args, **kwargs),
        "build_signals_pending_response": lambda *args, **kwargs: build_signals_pending_response(*args, **kwargs),
        "build_signals_ack_response": lambda *args, **kwargs: build_signals_ack_response(*args, **kwargs),
        "build_order_upsert_response": lambda *args, **kwargs: build_order_upsert_response(*args, **kwargs),
        "build_signal_confirm_webhook_response": lambda *args, **kwargs: build_signal_confirm_webhook_response(*args, **kwargs),
        "build_signal_cancel_webhook_response": lambda *args, **kwargs: build_signal_cancel_webhook_response(*args, **kwargs),
        "config_value": _config_value,
    },
)
custom_ibkr_signal = _signal_route_handlers["custom_ibkr_signal"]
custom_ibkr_signals = _signal_route_handlers["custom_ibkr_signals"]
custom_ibkr_signals_pending = _signal_route_handlers["custom_ibkr_signals_pending"]
custom_ibkr_signals_ack = _signal_route_handlers["custom_ibkr_signals_ack"]
webhook_signal_confirm = _signal_route_handlers["webhook_signal_confirm"]
webhook_signal_cancel = _signal_route_handlers["webhook_signal_cancel"]


_state_route_handlers = register_state_routes(
    app,
    deps={
        "normalize_environment": _normalize_environment,
        "get_state_payload": lambda state_key, environment, date="global": _get_state_payload(state_key, environment, date=date),
        "upsert_state": lambda state_key, environment, data, date="global": pb.upsert_state(state_key, environment, data, date=date),
        "as_dict": _as_dict,
    },
)
custom_ibkr_state_signals_get = _state_route_handlers["custom_ibkr_state_signals_get"]
custom_ibkr_state_signals_post = _state_route_handlers["custom_ibkr_state_signals_post"]
custom_ibkr_state_orders_get = _state_route_handlers["custom_ibkr_state_orders_get"]
custom_ibkr_state_orders_post = _state_route_handlers["custom_ibkr_state_orders_post"]


_order_route_handlers = register_order_routes(
    app,
    deps={
        "pb": pb,
        "normalize_environment": _normalize_environment,
        "escape_filter_string": _escape_filter_string,
        "cancel_broker_order": lambda environment, order_id, payload=None: _cancel_broker_order_via_runtime(environment, order_id, payload),
        "build_order_upsert_response": lambda *args, **kwargs: build_order_upsert_response(*args, **kwargs),
        "build_orders_reconcile_response": lambda *args, **kwargs: build_orders_reconcile_response(*args, **kwargs),
        "build_order_cancel_group_response": lambda *args, **kwargs: build_order_cancel_group_response(*args, **kwargs),
        "build_order_close_group_response": lambda *args, **kwargs: build_order_close_group_response(*args, **kwargs),
        "build_order_cancel_webhook_response": lambda *args, **kwargs: build_order_cancel_webhook_response(*args, **kwargs),
        "build_order_close_webhook_response": lambda *args, **kwargs: build_order_close_webhook_response(*args, **kwargs),
    },
)
custom_ibkr_orders_upsert = _order_route_handlers["custom_ibkr_orders_upsert"]
custom_ibkr_orders_reconcile = _order_route_handlers["custom_ibkr_orders_reconcile"]
custom_ibkr_orders_cancel_group = _order_route_handlers["custom_ibkr_orders_cancel_group"]
custom_ibkr_orders_close_group = _order_route_handlers["custom_ibkr_orders_close_group"]
webhook_order_cancel = _order_route_handlers["webhook_order_cancel"]
webhook_order_close = _order_route_handlers["webhook_order_close"]


_reverse_route_handlers = register_reverse_routes(
    app,
    deps={
        "pb": pb,
        "normalize_environment": _normalize_environment,
        "escape_filter_string": _escape_filter_string,
        "build_reverse_list_response": lambda *args, **kwargs: build_reverse_list_response(*args, **kwargs),
        "build_reverse_calculate_response": lambda *args, **kwargs: build_reverse_calculate_response(*args, **kwargs),
        "build_reverse_pending_response": lambda *args, **kwargs: build_reverse_pending_response(*args, **kwargs),
        "build_reverse_dispatch_response": lambda *args, **kwargs: build_reverse_dispatch_response(*args, **kwargs),
        "build_reverse_ack_response": lambda *args, **kwargs: build_reverse_ack_response(*args, **kwargs),
    },
)
custom_ibkr_reverse_list = _reverse_route_handlers["custom_ibkr_reverse_list"]
custom_ibkr_reverse_calculate = _reverse_route_handlers["custom_ibkr_reverse_calculate"]
custom_ibkr_reverse_pending = _reverse_route_handlers["custom_ibkr_reverse_pending"]
custom_ibkr_reverse_dispatch = _reverse_route_handlers["custom_ibkr_reverse_dispatch"]
custom_ibkr_reverse_ack = _reverse_route_handlers["custom_ibkr_reverse_ack"]


_tradingview_route_handlers = register_tradingview_routes(
    app,
    deps={
        "upsert_tv_indicator": lambda payload: _upsert_tv_indicator(payload),
        "upsert_tv_signal": lambda payload: _upsert_tv_signal(payload),
    },
)
webhook_tv = _tradingview_route_handlers["webhook_tv"]


_control_route_handlers = register_control_routes(
    app,
    deps={
        "pb": pb,
        "normalize_environment": _normalize_environment,
        "escape_filter_string": _escape_filter_string,
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
        "as_dict": _as_dict,
    },
)
custom_ibkr_emergency_stop = _control_route_handlers["custom_ibkr_emergency_stop"]
custom_ibkr_recover = _control_route_handlers["custom_ibkr_recover"]
custom_ibkr_reauth = _control_route_handlers["custom_ibkr_reauth"]


_two_factor_route_handlers = register_two_factor_routes(
    app,
    deps={
        "pb": pb,
        "normalize_environment": _normalize_environment,
        "as_dict": _as_dict,
        "request_json_request": lambda method, base_url, path, params=None, json_body=None, timeout=5.0: _request_json_request(
            method,
            base_url,
            path,
            params=params,
            json_body=json_body,
            timeout=timeout,
        ),
        "runtime_base_url": RUNTIME_BASE_URL,
        "fetch_runtime_status": lambda environment: _fetch_runtime_status(environment),
        "inspect_runtime_environment": lambda environment: inspect_requested_runtime_environment(
            environment,
            normalize_environment=_normalize_environment,
            fetch_runtime_status=_fetch_runtime_status,
            as_dict=_as_dict,
        ),
        "build_runtime_environment_mismatch_payload": build_runtime_environment_mismatch_payload,
        "console_base_url": _console_base_url,
        "config_value": _config_value,
        "feishu_send_interactive": _feishu_send_interactive,
        "feishu_update_interactive": _feishu_update_interactive,
        "emit_system_event": _emit_system_event,
        "merge_startup_steps": _merge_startup_steps,
        "deliver_startup_progress_card": lambda state, environment: _deliver_startup_progress_card(state, environment),
    },
)
custom_ibkr_two_factor_request = _two_factor_route_handlers["custom_ibkr_two_factor_request"]
custom_ibkr_two_factor_result = _two_factor_route_handlers["custom_ibkr_two_factor_result"]
custom_ibkr_two_factor_respond = _two_factor_route_handlers["custom_ibkr_two_factor_respond"]


_callback_route_handlers = register_callback_routes(
    app,
    deps={
        "handle_feishu_callback": _handle_feishu_callback_support,
        "as_dict": _as_dict,
        "normalize_environment": _normalize_environment,
        "dispatch_feishu_2fa_callback": lambda action, environment: _dispatch_feishu_2fa_callback(action, environment),
        "dispatch_feishu_order_callback": lambda action, order_id, environment: _dispatch_feishu_order_callback(action, order_id, environment),
        "dispatch_feishu_signal_callback": lambda action, signal_id, environment: _dispatch_feishu_signal_callback(action, signal_id, environment),
        "callback_toast": _callback_toast,
        "callback_response": _feishu_callback_response,
    },
)
webhook_feishu_callback = _callback_route_handlers["webhook_feishu_callback"]


_compat_route_handlers = register_compat_routes(
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
health = _compat_route_handlers["health"]
status = _compat_route_handlers["status"]
collections_proxy = _compat_route_handlers["collections_proxy"]
custom_ibkr_proxy = _compat_route_handlers["custom_ibkr_proxy"]
custom_proxy = _compat_route_handlers["custom_proxy"]
webhook_proxy = _compat_route_handlers["webhook_proxy"]
