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
from ibkr_api.integrations.feishu import feishu_send_interactive, feishu_suppressed, feishu_token, feishu_update_interactive
from ibkr_api.integrations.runtime_orders import cancel_broker_order_via_runtime as _cancel_broker_order_via_runtime_support
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
CHALLENGE_RESET_RECOMMEND_MS = 120 * 1000
RECOVERY_FIELDS = (
    "cycle_id",
    "recovery_phase",
    "recovery_class",
    "recovery_reason",
    "interruption_kind",
    "manual_takeover_active",
    "manual_takeover_started_at",
    "manual_takeover_until",
    "probe_started_at",
    "probe_last_checked_at",
    "probe_attempts",
    "probe_result",
    "auto_restart_scheduled",
    "last_runtime_authenticated_at",
    "last_gateway_status_code",
    "last_recovery_source",
    "lock_owner",
    "lock_expires_at",
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
    return _request_json(
        COMPUTE_BASE_URL,
        "/ibkr/monitor",
        params=[("environment", environment)],
        timeout=10,
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
    return _request_json(
        COMPUTE_BASE_URL,
        "/status",
        params=[("environment", environment)],
        timeout=10,
    )


def _fetch_compute_health(environment: str) -> dict[str, Any]:
    return _request_json(
        COMPUTE_BASE_URL,
        "/health",
        params=[("environment", environment)],
        timeout=10,
    )


def _fetch_runtime_status(environment: str) -> dict[str, Any]:
    proxy_result = _request_json(
        COMPUTE_BASE_URL,
        "/ibkr/status",
        params=[("environment", environment)],
        timeout=10,
    )
    proxy_upstream = f"{COMPUTE_BASE_URL}/ibkr/status"
    direct_upstream = f"{RUNTIME_BASE_URL}/ibkr/status"
    selected_result = proxy_result
    selected_upstream = proxy_upstream
    error = str(proxy_result.get("error") or "")

    if (not bool(proxy_result.get("ok"))) and RUNTIME_BASE_URL:
        direct_result = _request_json(
            RUNTIME_BASE_URL,
            "/ibkr/status",
            params=[("environment", environment)],
            timeout=10,
        )
        if bool(direct_result.get("ok")):
            selected_result = direct_result
            selected_upstream = direct_upstream
            error = ""
        elif not error:
            error = str(direct_result.get("error") or "")

    return {
        "payload": _as_dict(selected_result.get("payload")),
        "ok": bool(selected_result.get("ok")),
        "error": error,
        "selected_upstream": selected_upstream,
        "proxy_upstream": proxy_upstream,
        "direct_upstream": direct_upstream,
        "status_code": int(selected_result.get("status_code") or 0),
    }


def _fetch_runtime_health(environment: str) -> dict[str, Any]:
    result = _request_json(
        RUNTIME_BASE_URL,
        "/health",
        params=[("environment", environment)],
        timeout=10,
    )
    return {
        "payload": _as_dict(result.get("payload")),
        "ok": bool(result.get("ok")),
        "error": str(result.get("error") or ""),
        "upstream": f"{RUNTIME_BASE_URL}/health",
        "status_code": int(result.get("status_code") or 0),
    }


def _merge_service_topology(*payloads: Any) -> dict[str, Any]:
    merged = build_service_topology()
    merged_services = dict(merged.get("services") if isinstance(merged.get("services"), dict) else {})
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        topology = (
            payload
            if isinstance(payload.get("services"), dict)
            else payload.get("service_topology")
        )
        if not isinstance(topology, dict):
            continue
        for key, value in topology.items():
            if key == "services":
                continue
            merged[key] = value
        if isinstance(topology.get("services"), dict):
            merged_services.update(topology.get("services") or {})
    merged["services"] = merged_services
    return merged


def _normalize_two_factor_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "requested"
    if text in {"pending", "waiting_mobile_approval", "mobile_approval", "awaiting_mobile_approval"}:
        return "waiting_confirm"
    if text in {"complete", "completed", "authenticated"}:
        return "success"
    if text == "error":
        return "failed"
    return text


def _is_server_boot_resume_recovery_state(state_data: dict[str, Any]) -> bool:
    state = _as_dict(state_data)
    interruption_kind = str(state.get("interruption_kind") or "").strip().lower()
    recovery_phase = str(state.get("recovery_phase") or "").strip().lower()
    recovery_reason = str(state.get("recovery_reason") or "").strip().lower()
    last_recovery_source = str(state.get("last_recovery_source") or "").strip().lower()
    return (
        interruption_kind == "server_boot_resume"
        or recovery_phase == "resume_waiting_manual"
        or (recovery_reason == "auto_restore" and last_recovery_source == "server_boot")
    )


def _is_manual_auth_required_recovery_state(state_data: dict[str, Any]) -> bool:
    state = _as_dict(state_data)
    recovery_class = str(state.get("recovery_class") or "").strip().lower()
    recovery_phase = str(state.get("recovery_phase") or "").strip().lower()
    probe_result = str(state.get("probe_result") or "").strip().lower()
    return (
        recovery_class == "manual_auth_required"
        or recovery_phase == "requested"
        or probe_result == "manual_trigger_required"
        or probe_result == "timeout_after_self_heal"
    )


def _is_silent_recovery_state(state_data: dict[str, Any]) -> bool:
    state = _as_dict(state_data)
    if _is_manual_auth_required_recovery_state(state):
        return False
    if _is_server_boot_resume_recovery_state(state):
        return True
    recovery_phase = str(state.get("recovery_phase") or "").strip().lower()
    recovery_class = str(state.get("recovery_class") or "").strip().lower()
    probe_result = str(state.get("probe_result") or "").strip().lower()
    return recovery_phase == "silent_probe" and (
        recovery_class == "scheduled_restart"
        or recovery_class == "stale_broker"
        or bool(state.get("auto_restart_scheduled"))
        or probe_result in {
            "pending",
            "self_heal",
            "self_heal_pending",
            "stale_broker_restart_scheduled",
            "stale_broker_restart_failed",
        }
    )


def _build_silent_recovery_message(state_data: dict[str, Any]) -> dict[str, str]:
    state = _as_dict(state_data)
    recovery_class = str(state.get("recovery_class") or "").strip().lower()
    interruption_kind = str(state.get("interruption_kind") or "").strip().lower()
    probe_result = str(state.get("probe_result") or "").strip().lower()
    if recovery_class == "stale_broker" or probe_result.startswith("stale_broker"):
        if bool(state.get("auto_restart_scheduled")):
            return {
                "message": "检测到运行态内 broker 连接失配，系统已安排自动重启 Runtime 以恢复主连接；暂不需要立即重新 2FA。",
                "last_result": "已识别 stale in-process broker，正在等待自动重启恢复主连接。",
            }
        return {
            "message": "检测到运行态内 broker 连接失配，系统正在尝试本地恢复主连接；暂不需要立即重新 2FA。",
            "last_result": "已识别 stale in-process broker，正在继续静默恢复。",
        }
    if interruption_kind == "gateway_down":
        return {
            "message": "Gateway 刚经历中断或重启，系统正在静默探测并恢复当前会话；暂不需要立即重新 2FA。",
            "last_result": "已进入 Gateway 中断后的静默恢复窗口。",
        }
    return {
        "message": "检测到会话认证中断，系统正在静默探测与本地重连；暂不需要立即重新 2FA。",
        "last_result": "已进入静默恢复窗口，等待会话自动恢复。",
    }


def _derive_2fa_action_state(state_data: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    current_ms = int(now_ms or 0) or int(datetime.now(tz=ET).timestamp() * 1000)
    state = _as_dict(state_data)
    status = _normalize_two_factor_status(state.get("status") or "")
    response_status = str(state.get("response_status") or "").strip().lower()
    challenge_code = str(state.get("challenge_code") or "").strip()
    feedback = str(state.get("challenge_feedback") or "").strip()
    recovery_phase = str(state.get("recovery_phase") or "").strip().lower()
    submitted_ms = _parse_et_time_ms(state.get("response_submitted_at"))
    rejected_ms = _parse_et_time_ms(state.get("response_rejected_at"))
    submitted_age_ms = max(0, current_ms - submitted_ms) if submitted_ms > 0 else 0
    rejected_age_ms = max(0, current_ms - rejected_ms) if rejected_ms > 0 else 0
    operator_action = "request_approval"
    reset_recommended = bool(state.get("reset_recommended"))
    reset_reason = str(state.get("reset_reason") or "").strip()

    if recovery_phase == "panic_resetting":
        operator_action = "panic_resetting"
    elif bool(state.get("manual_takeover_active")):
        operator_action = "manual_takeover"
    elif status == "waiting_response":
        if response_status == "received":
            operator_action = "wait_browser_submit"
        elif response_status == "submitted":
            operator_action = "wait_auth_restore"
            if not reset_recommended and submitted_age_ms >= CHALLENGE_RESET_RECOMMEND_MS:
                operator_action = "panic_reset"
                reset_recommended = True
                reset_reason = reset_reason or "submitted_no_recovery"
        elif response_status == "gateway_rejected":
            operator_action = "retry_response_same_challenge"
            if not reset_recommended and rejected_age_ms >= CHALLENGE_RESET_RECOMMEND_MS:
                operator_action = "panic_reset"
                reset_recommended = True
                reset_reason = reset_reason or "gateway_rejected_no_recovery"
        elif response_status == "submit_failed":
            operator_action = "retry_response_same_challenge"
        else:
            operator_action = "submit_response" if challenge_code else "wait_challenge"
    elif status == "waiting_confirm":
        operator_action = "confirm_push"
    elif status == "triggered":
        operator_action = "wait_for_mode"
    elif status in {"resume_pending", "recovering"}:
        operator_action = "check_runtime_status" if recovery_phase == "resume_waiting_manual" else "wait_auth_restore"
    elif status == "success":
        operator_action = "none"
    elif status in {"timeout", "failed"}:
        operator_action = "request_new_cycle"

    state["status"] = status
    state["response_status"] = response_status
    state["challenge_feedback"] = feedback
    state["operator_action"] = operator_action
    state["reset_recommended"] = reset_recommended
    state["reset_reason"] = reset_reason
    state["response_submitted_age_sec"] = round(submitted_age_ms / 1000) if submitted_age_ms > 0 else 0
    state["response_rejected_age_sec"] = round(rejected_age_ms / 1000) if rejected_age_ms > 0 else 0
    state.setdefault("business_deadline_at", "")
    state.setdefault("business_deadline_cn", "")
    state.setdefault("business_deadline_label", "")
    state.setdefault("business_deadline_overdue", False)
    state["confirm_window_seconds"] = int(state.get("confirm_window_seconds") or 180)
    state.setdefault("confirm_deadline_at", "")
    state.setdefault("confirm_deadline_cn", "")
    state.setdefault("confirm_deadline_overdue", False)
    return state


def _normalize_two_factor_state_with_runtime(state_data: dict[str, Any], runtime_status: dict[str, Any]) -> dict[str, Any]:
    state = _as_dict(state_data)
    runtime = _as_dict(runtime_status)
    auth_recovery = _as_dict(runtime.get("auth_recovery"))
    runtime_started = bool(
        runtime.get("starting")
        or _as_dict(runtime.get("session")).get("running")
        or _as_dict(runtime.get("websocket")).get("running")
        or _as_dict(runtime.get("order_tracker")).get("running")
    )
    runtime_authenticated = bool(_as_dict(runtime.get("session")).get("authenticated"))
    gateway = _as_dict(runtime.get("gateway"))
    gateway_reachable = bool(gateway.get("running") or gateway.get("reachable"))
    gateway_status_code = int(gateway.get("status_code") or 0)

    state["runtime_started"] = runtime_started
    state["runtime_authenticated"] = runtime_authenticated
    state["gateway_status_code"] = gateway_status_code
    state["gateway_reachable"] = gateway_reachable
    for key in RECOVERY_FIELDS:
        if key in auth_recovery:
            state[key] = auth_recovery.get(key)
    if not str(state.get("recovery_phase") or "").strip():
        state["recovery_phase"] = "recovered" if runtime_authenticated else "idle"
    normalized_status = _normalize_two_factor_status(state.get("status") or "")
    server_boot_resume_pending = _is_server_boot_resume_recovery_state(state) and not runtime_authenticated

    if runtime_authenticated and gateway_reachable and gateway_status_code != 401:
        state.update(
            {
                "status": "success",
                "message": "Gateway 会话有效，无需再次确认。",
                "last_result": "运行态会话正常。",
                "last_error": "",
                "mode": "",
                "challenge_code": "",
                "challenge_detected_at": "",
                "response_code": "",
                "response_status": "",
                "response_received_at": "",
                "response_submitted_at": "",
                "response_rejected_at": "",
                "challenge_feedback": "",
                "page_title": "",
                "page_url": "",
                "gateway_trace": "",
                "browser_authenticated": True,
                "gateway_authenticated": True,
                "backend_authenticated": True,
                "recovery_phase": "recovered",
                "auto_restart_scheduled": False,
                "manual_takeover_active": False,
            }
        )
        return _derive_2fa_action_state(state)

    if (not runtime_authenticated) or gateway_status_code == 401:
        state["gateway_authenticated"] = False
        state["backend_authenticated"] = False
        if not runtime_started:
            state["browser_authenticated"] = False
        active_cycle = normalized_status in {"triggered", "waiting_confirm", "waiting_response"}
        if server_boot_resume_pending and not active_cycle:
            state.update(
                {
                    "status": "resume_pending",
                    "message": (
                        "静默恢复尚未自动成功；当前不会自动补发新的 2FA，如需立即恢复请去 Runtime 页面人工处理。"
                        if str(state.get("probe_result") or "").strip().lower() == "resume_probe_timeout"
                        else "Compute 重启后正在静默复用现有 Gateway Session，本轮不会自动重开 2FA。"
                    ),
                    "last_result": (
                        "静默恢复未自动成功，当前保持被动等待，不会自动新开 2FA。"
                        if str(state.get("probe_result") or "").strip().lower() == "resume_probe_timeout"
                        else "已进入 server_boot 静默恢复窗口。"
                    ),
                    "last_error": "",
                    "mode": "",
                    "challenge_code": "",
                    "challenge_detected_at": "",
                    "response_code": "",
                    "response_status": "",
                    "response_received_at": "",
                    "response_submitted_at": "",
                    "response_rejected_at": "",
                    "challenge_feedback": "",
                    "page_title": "",
                    "page_url": "",
                    "gateway_trace": "",
                }
            )
        elif _is_silent_recovery_state(state) and not active_cycle:
            silent_recovery = _build_silent_recovery_message(state)
            state.update(
                {
                    "status": "recovering",
                    "message": silent_recovery.get("message") or "",
                    "last_result": silent_recovery.get("last_result") or "",
                    "last_error": "",
                    "mode": "",
                    "challenge_code": "",
                    "challenge_detected_at": "",
                    "response_code": "",
                    "response_status": "",
                    "response_received_at": "",
                    "response_submitted_at": "",
                    "response_rejected_at": "",
                    "challenge_feedback": "",
                    "page_title": "",
                    "page_url": "",
                    "gateway_trace": "",
                }
            )
        elif _is_manual_auth_required_recovery_state(state) and not active_cycle:
            state["status"] = "requested"
            state["message"] = "静默恢复窗口已结束，当前需要手动触发 2FA。"
            state["last_result"] = "静默恢复未完成，等待手动触发新的 2FA 轮次。"
        elif normalized_status == "success":
            state["status"] = "requested"
            state["message"] = "旧 Gateway 认证已失效，请重新触发 2FA。"
            state["last_result"] = "旧 Gateway 认证已失效，等待重新触发 2FA。"

    return _derive_2fa_action_state(state)


def _split_symbol_list_by_monitor(values: Any, monitor_symbols: Any) -> dict[str, list[str]]:
    monitor_set = set(_normalize_symbol_list(monitor_symbols))
    blocking: list[str] = []
    monitor: list[str] = []
    for symbol in _normalize_symbol_list(values):
        if symbol in monitor_set:
            monitor.append(symbol)
        else:
            blocking.append(symbol)
    return {"blocking": blocking, "monitor": monitor}


def _split_reason_map_by_monitor(value: Any, monitor_symbols: Any) -> dict[str, dict[str, Any]]:
    monitor_set = set(_normalize_symbol_list(monitor_symbols))
    blocking: dict[str, Any] = {}
    monitor: dict[str, Any] = {}
    if not isinstance(value, dict):
        return {"blocking": blocking, "monitor": monitor}
    for symbol, reason in value.items():
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            continue
        if normalized_symbol in monitor_set:
            monitor[normalized_symbol] = reason
        else:
            blocking[normalized_symbol] = reason
    return {"blocking": blocking, "monitor": monitor}


def _build_statusz_compute_payload(compute_payload: dict[str, Any], include_engines: bool) -> dict[str, Any]:
    payload = _as_dict(compute_payload)
    engine_map = payload.get("engines") if isinstance(payload.get("engines"), dict) else {}
    total_engines = int(payload.get("total_engines") or len(engine_map))
    payload["total_engines"] = total_engines
    payload["ready_engines"] = int(payload.get("ready_engines") or 0)
    payload["engines_available"] = total_engines > 0
    payload["engines_included"] = bool(include_engines)
    payload["statusz_mode"] = "full" if include_engines else "lite"
    if include_engines:
        payload["engines"] = dict(engine_map)
    else:
        payload.pop("engines", None)
    return payload


def _build_statusz_live_readiness(compute_payload: dict[str, Any], runtime_payload: dict[str, Any]) -> dict[str, Any]:
    compute = _as_dict(compute_payload)
    runtime = _as_dict(runtime_payload)
    engine_map = compute.get("engines") if isinstance(compute.get("engines"), dict) else {}
    warmup = _as_dict(runtime.get("warmup"))
    market_universe = _as_dict(runtime.get("market_universe"))
    service_topology = _as_dict(runtime.get("service_topology"))
    environment = _normalize_environment(runtime.get("environment") or compute.get("environment"), "live")
    required_interval = str(warmup.get("required_interval") or "5m").strip() or "5m"
    trade_symbols = _normalize_symbol_list(
        warmup.get("trade_symbols")
        if isinstance(warmup.get("trade_symbols"), list) and warmup.get("trade_symbols")
        else market_universe.get("active_trade_symbols")
    )
    monitor_symbols = _normalize_symbol_list(
        warmup.get("monitor_symbols")
        if isinstance(warmup.get("monitor_symbols"), list) and warmup.get("monitor_symbols")
        else market_universe.get("market_ws_symbols")
    )
    symbols = _normalize_symbol_list(
        warmup.get("symbols")
        if isinstance(warmup.get("symbols"), list) and warmup.get("symbols")
        else (
            market_universe.get("data_symbols")
            if isinstance(market_universe.get("data_symbols"), list)
            else trade_symbols + monitor_symbols
        )
    )
    if not symbols:
        derived_symbols: list[str] = []
        for key in engine_map:
            parts = str(key or "").split("/")
            if len(parts) != 3:
                continue
            if parts[0] == environment and parts[2] == required_interval:
                derived_symbols.append(parts[1])
        symbols = _normalize_symbol_list(derived_symbols)

    symbol_set = set(symbols + trade_symbols + monitor_symbols)
    all_symbols = sorted(symbol_set)
    trade_set = set(trade_symbols)
    monitor_set = set(monitor_symbols)
    ready_symbols = 0
    ready_trade_symbols = 0
    ready_monitor_symbols = 0
    ready_set: set[str] = set()
    for symbol in all_symbols:
        engine = engine_map.get(f"{environment}/{symbol}/{required_interval}") if isinstance(engine_map, dict) else None
        if not isinstance(engine, dict) or not bool(engine.get("is_ready")):
            continue
        ready_set.add(symbol)
        ready_symbols += 1
        if symbol in trade_set:
            ready_trade_symbols += 1
        if symbol in monitor_set:
            ready_monitor_symbols += 1
    non_monitor_pending_total = len([symbol for symbol in all_symbols if symbol not in ready_set and symbol not in monitor_set])
    monitor_pending_total = len([symbol for symbol in all_symbols if symbol not in ready_set and symbol in monitor_set])
    gate_open = len(trade_symbols) > 0 and ready_trade_symbols >= len(trade_symbols)
    phase = "idle"
    if all_symbols:
        phase = "ready" if non_monitor_pending_total == 0 else "pending"

    snapshot_symbols_total = int(warmup.get("symbols_total") or 0)
    snapshot_ready_symbols = int(warmup.get("ready_symbols") or 0)
    snapshot_ready_trade_symbols = int(warmup.get("ready_trade_symbols") or 0)
    snapshot_ready_monitor_symbols = int(warmup.get("ready_monitor_symbols") or 0)
    snapshot_phase = str(warmup.get("phase") or "").strip().lower() or "idle"
    snapshot_finished_at = warmup.get("finished_at") or ""
    snapshot_trade_symbols_total = int(warmup.get("trade_symbols_total") or len(trade_symbols))
    snapshot_monitor_symbols_total = int(warmup.get("monitor_symbols_total") or len(monitor_symbols))
    snapshot_pending_symbols_total = (
        len(warmup.get("pending_symbols"))
        if isinstance(warmup.get("pending_symbols"), list)
        else int(warmup.get("pending_symbols_total") or 0)
    )
    snapshot_monitor_pending_total = int(warmup.get("monitor_pending_symbols_total") or 0) or max(
        0, snapshot_monitor_symbols_total - snapshot_ready_monitor_symbols
    )
    snapshot_blocking_pending_total = int(warmup.get("blocking_pending_symbols_total") or 0) or max(
        0, snapshot_pending_symbols_total - snapshot_monitor_pending_total
    )
    snapshot_gate_open = bool(warmup.get("trading_gate_open"))
    snapshot_gate_reason = str(warmup.get("trading_gate_reason") or "").strip().lower() or (
        "ready" if snapshot_trade_symbols_total > 0 and snapshot_gate_open else (
            "warmup_incomplete" if snapshot_trade_symbols_total > 0 else "no_trade_symbols"
        )
    )
    snapshot_present = bool(
        snapshot_symbols_total
        or snapshot_ready_symbols
        or snapshot_ready_trade_symbols
        or snapshot_ready_monitor_symbols
        or str(warmup.get("phase") or "").strip()
        or str(warmup.get("finished_at") or "").strip()
    )
    snapshot_differs = bool(
        snapshot_present
        and (
            (snapshot_symbols_total > 0 and snapshot_symbols_total != len(all_symbols))
            or snapshot_ready_symbols != ready_symbols
            or snapshot_ready_trade_symbols != ready_trade_symbols
            or snapshot_ready_monitor_symbols != ready_monitor_symbols
        )
    )
    runtime_mode = str(
        runtime.get("runtime_mode")
        or compute.get("runtime_mode")
        or service_topology.get("runtime_mode")
        or ""
    ).strip().lower()
    runtime_snapshot_available = snapshot_present and (
        snapshot_symbols_total > 0
        or snapshot_ready_symbols > 0
        or snapshot_trade_symbols_total > 0
        or snapshot_monitor_symbols_total > 0
        or snapshot_phase != "idle"
        or bool(snapshot_finished_at)
    )
    if (runtime_mode == "remote" or not engine_map) and runtime_snapshot_available:
        return {
            "available": True,
            "engine_snapshot_available": bool(engine_map),
            "source": "runtime_warmup_snapshot",
            "environment": environment,
            "required_interval": required_interval,
            "computed_at": datetime.now(tz=ET).isoformat(),
            "phase": snapshot_phase,
            "gate_open": snapshot_gate_open,
            "gate_reason": snapshot_gate_reason,
            "symbols_total": snapshot_symbols_total or len(all_symbols),
            "trade_symbols_total": snapshot_trade_symbols_total,
            "monitor_symbols_total": snapshot_monitor_symbols_total,
            "ready_symbols": snapshot_ready_symbols,
            "ready_trade_symbols": snapshot_ready_trade_symbols,
            "ready_monitor_symbols": snapshot_ready_monitor_symbols,
            "pending_symbols_total": snapshot_pending_symbols_total,
            "non_monitor_pending_symbols_total": snapshot_blocking_pending_total,
            "blocking_pending_symbols_total": snapshot_blocking_pending_total,
            "monitor_pending_symbols_total": snapshot_monitor_pending_total,
            "snapshot_differs": False,
            "snapshot_phase": snapshot_phase,
            "snapshot_finished_at": snapshot_finished_at,
        }

    return {
        "available": bool(all_symbols) and bool(engine_map),
        "engine_snapshot_available": bool(engine_map),
        "source": "compute_engines",
        "environment": environment,
        "required_interval": required_interval,
        "computed_at": datetime.now(tz=ET).isoformat(),
        "phase": phase,
        "gate_open": gate_open,
        "gate_reason": "ready" if gate_open else ("live_not_ready" if trade_symbols else "no_trade_symbols"),
        "symbols_total": len(all_symbols),
        "trade_symbols_total": len(trade_symbols),
        "monitor_symbols_total": len(monitor_symbols),
        "ready_symbols": ready_symbols,
        "ready_trade_symbols": ready_trade_symbols,
        "ready_monitor_symbols": ready_monitor_symbols,
        "pending_symbols_total": max(0, len(all_symbols) - ready_symbols),
        "non_monitor_pending_symbols_total": non_monitor_pending_total,
        "blocking_pending_symbols_total": non_monitor_pending_total,
        "monitor_pending_symbols_total": monitor_pending_total,
        "snapshot_differs": snapshot_differs,
        "snapshot_phase": snapshot_phase,
        "snapshot_finished_at": snapshot_finished_at,
    }


def _build_statusz_runtime_payload(
    runtime_payload: dict[str, Any],
    include_warmup_details: bool,
    *,
    live_readiness: dict[str, Any],
    fallback_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _as_dict(runtime_payload)
    fallback = _as_dict(fallback_state)
    gateway = _as_dict(payload.get("gateway"))
    session = _as_dict(payload.get("session"))
    auth_recovery = _as_dict(payload.get("auth_recovery"))
    websocket = _as_dict(payload.get("websocket"))
    realtime_quotes = _as_dict(payload.get("realtime_quotes"))
    canonical_5m = _as_dict(payload.get("canonical_5m"))
    data_backfill = _as_dict(payload.get("data_backfill"))
    order_tracker = _as_dict(payload.get("order_tracker"))
    warmup = _as_dict(payload.get("warmup"))
    realtime_compute = _as_dict(payload.get("realtime_compute"))
    realtime_result = _as_dict(realtime_compute.get("last_result"))
    market_universe = _as_dict(payload.get("market_universe"))
    daily_scan = _as_dict(payload.get("daily_scan"))
    persisted_daily_scan = _as_dict(fallback.get("daily_scan"))
    if not daily_scan and persisted_daily_scan:
        daily_scan = dict(persisted_daily_scan)
    if int(market_universe.get("active_target_count") or 0) <= 0:
        fallback_active_target_count = int(fallback.get("active_target_count") or 0)
        if fallback_active_target_count > 0:
            market_universe["active_target_count"] = fallback_active_target_count
    if not str(market_universe.get("active_target_date") or "").strip():
        fallback_market_date = str(
            fallback.get("active_target_date") or daily_scan.get("market_date") or ""
        ).strip()
        if fallback_market_date:
            market_universe["active_target_date"] = fallback_market_date

    auth_recovery_summary = {
        "cycle_id": str(auth_recovery.get("cycle_id") or ""),
        "recovery_phase": str(auth_recovery.get("recovery_phase") or ""),
        "recovery_class": str(auth_recovery.get("recovery_class") or ""),
        "recovery_reason": str(auth_recovery.get("recovery_reason") or ""),
        "interruption_kind": str(auth_recovery.get("interruption_kind") or ""),
        "last_runtime_authenticated_at": auth_recovery.get("last_runtime_authenticated_at") or "",
        "last_gateway_status_code": int(auth_recovery.get("last_gateway_status_code") or 0),
        "last_recovery_source": str(auth_recovery.get("last_recovery_source") or ""),
        "probe_result": str(auth_recovery.get("probe_result") or ""),
        "probe_last_checked_at": auth_recovery.get("probe_last_checked_at") or "",
        "probe_attempts": int(auth_recovery.get("probe_attempts") or 0),
        "auto_restart_scheduled": bool(auth_recovery.get("auto_restart_scheduled")),
        "manual_takeover_active": bool(auth_recovery.get("manual_takeover_active")),
        "lock_owner": str(auth_recovery.get("lock_owner") or ""),
    }

    monitor_symbols = _normalize_symbol_list(warmup.get("monitor_symbols"))
    pending_symbols = _normalize_symbol_list(warmup.get("pending_symbols"))
    integrity_pending_symbols_all = _normalize_symbol_list(warmup.get("integrity_pending_symbols"))
    pending_split = _split_symbol_list_by_monitor(pending_symbols, monitor_symbols)
    integrity_pending_split = _split_symbol_list_by_monitor(integrity_pending_symbols_all, monitor_symbols)
    integrity_repair_reasons = _as_dict(warmup.get("integrity_repair_reasons")) if include_warmup_details else {}
    reason_split = _split_reason_map_by_monitor(integrity_repair_reasons, monitor_symbols)

    return {
        "ok": payload.get("ok") if payload else None,
        "starting": bool(payload.get("starting")),
        "startup_complete": bool(payload.get("startup_complete")),
        "runtime_phase": str(payload.get("runtime_phase") or ""),
        "environment": str(payload.get("environment") or ""),
        "service_profile": str(payload.get("service_profile") or ""),
        "runtime_mode": str(payload.get("runtime_mode") or ""),
        "service_topology": _as_dict(payload.get("service_topology")),
        "market_session": _as_dict(payload.get("market_session")),
        "warmup_details_included": bool(include_warmup_details),
        "live_readiness": _as_dict(live_readiness),
        "gateway": {
            "running": bool(gateway.get("running")),
            "reachable": bool(gateway.get("reachable")),
            "managed_by": str(gateway.get("managed_by") or ""),
            "status_code": int(gateway.get("status_code") or 0),
            "pid": int(gateway.get("pid") or 0),
            "uptime_s": int(gateway.get("uptime_s") or 0),
        },
        "session": {
            "authenticated": bool(session.get("authenticated")),
            "running": bool(session.get("running")),
            "consecutive_failures": int(session.get("consecutive_failures") or 0),
            "last_check": session.get("last_check") or session.get("last_tickle") or "",
            "last_tickle": session.get("last_tickle") or session.get("last_check") or "",
        },
        "auth_recovery": auth_recovery_summary,
        "websocket": {
            "connected": bool(websocket.get("connected")),
            "ready": bool(websocket.get("ready")),
            "running": bool(websocket.get("running")),
            "last_message": websocket.get("last_message") or "",
            "message_count": int(websocket.get("message_count") or 0),
            "subscribed_count": (
                len(websocket.get("subscribed_conids"))
                if isinstance(websocket.get("subscribed_conids"), list)
                else int(websocket.get("subscribed_count") or 0)
            ),
            "pending_count": (
                len(websocket.get("pending_conids"))
                if isinstance(websocket.get("pending_conids"), list)
                else int(websocket.get("pending_count") or 0)
            ),
        },
        "realtime_quotes": {
            "total_quotes": int(realtime_quotes.get("total_quotes") or 0),
            "stale_quotes": int(realtime_quotes.get("stale_quotes") or 0),
            "tick_count": int(realtime_quotes.get("tick_count") or 0),
            "update_count": int(realtime_quotes.get("update_count") or 0),
        },
        "canonical_5m": {
            "enabled": bool(canonical_5m.get("enabled", True)),
            "driver": str(canonical_5m.get("driver") or ""),
            "close_delay_sec": int(canonical_5m.get("close_delay_sec") or 0),
            "request_period": str(canonical_5m.get("request_period") or ""),
            "last_run": canonical_5m.get("last_run") or "",
            "last_due_bucket_ms": int(canonical_5m.get("last_due_bucket_ms") or 0),
            "last_completed_bucket_ms": int(canonical_5m.get("last_completed_bucket_ms") or 0),
            "lag_s": int(canonical_5m.get("lag_s") or 0),
            "last_written_bars": int(canonical_5m.get("last_written_bars") or 0),
            "written_symbols": _trim_array(canonical_5m.get("written_symbols"), 24),
            "written_symbols_total": (
                len(canonical_5m.get("written_symbols"))
                if isinstance(canonical_5m.get("written_symbols"), list)
                else int(canonical_5m.get("written_symbols_total") or 0)
            ),
            "pending_symbols": _trim_array(canonical_5m.get("pending_symbols"), 24),
            "pending_symbols_total": (
                len(canonical_5m.get("pending_symbols"))
                if isinstance(canonical_5m.get("pending_symbols"), list)
                else int(canonical_5m.get("pending_symbols_total") or 0)
            ),
            "last_error": str(canonical_5m.get("last_error") or ""),
        },
        "data_backfill": {
            "total_backfilled": int(data_backfill.get("total_backfilled") or 0),
        },
        "order_tracker": {
            "running": bool(order_tracker.get("running")),
            "last_poll": order_tracker.get("last_poll") or "",
            "tracked_orders": int(order_tracker.get("tracked_orders") or 0),
        },
        "warmup": {
            "phase": str(warmup.get("phase") or ""),
            "trading_gate_open": bool(warmup.get("trading_gate_open")),
            "trading_gate_reason": str(warmup.get("trading_gate_reason") or ""),
            "required_interval": str(warmup.get("required_interval") or ""),
            "symbols_total": int(warmup.get("symbols_total") or 0),
            "trade_symbols_total": int(warmup.get("trade_symbols_total") or 0),
            "monitor_symbols_total": int(warmup.get("monitor_symbols_total") or 0),
            "ready_symbols": int(warmup.get("ready_symbols") or 0),
            "ready_trade_symbols": int(warmup.get("ready_trade_symbols") or 0),
            "ready_monitor_symbols": int(warmup.get("ready_monitor_symbols") or 0),
            "pending_symbols": (
                _trim_array(pending_symbols, len(pending_symbols))
                if include_warmup_details
                else _trim_array(pending_symbols, 12)
            ),
            "pending_symbols_total": (
                len(warmup.get("pending_symbols"))
                if isinstance(warmup.get("pending_symbols"), list)
                else int(warmup.get("pending_symbols_total") or len(pending_symbols))
            ),
            "blocking_pending_symbols": _trim_array(
                pending_split.get("blocking"), len(pending_split.get("blocking", [])) if include_warmup_details else 12
            ),
            "blocking_pending_symbols_total": len(pending_split.get("blocking") or []),
            "monitor_pending_symbols": _trim_array(
                pending_split.get("monitor"), len(pending_split.get("monitor", [])) if include_warmup_details else 12
            ),
            "monitor_pending_symbols_total": len(pending_split.get("monitor") or []),
            "requested_at": warmup.get("requested_at") or "",
            "started_at": warmup.get("started_at") or "",
            "finished_at": warmup.get("finished_at") or "",
            "last_success_at": warmup.get("last_success_at") or "",
            "last_error": str(warmup.get("last_error") or ""),
            "reason": str(warmup.get("reason") or ""),
            "target_date": str(warmup.get("target_date") or ""),
            "symbols": (
                _trim_array(warmup.get("symbols"), len(warmup.get("symbols")))
                if include_warmup_details and isinstance(warmup.get("symbols"), list)
                else []
            ),
            "trade_symbols": (
                _trim_array(warmup.get("trade_symbols"), len(warmup.get("trade_symbols")))
                if include_warmup_details and isinstance(warmup.get("trade_symbols"), list)
                else []
            ),
            "monitor_symbols": (
                _trim_array(warmup.get("monitor_symbols"), len(warmup.get("monitor_symbols")))
                if include_warmup_details and isinstance(warmup.get("monitor_symbols"), list)
                else []
            ),
            "ready_symbols_list": (
                _trim_array(warmup.get("ready_symbols_list"), len(warmup.get("ready_symbols_list")))
                if include_warmup_details and isinstance(warmup.get("ready_symbols_list"), list)
                else []
            ),
            "symbol_status": (
                _trim_array(warmup.get("symbol_status"), len(warmup.get("symbol_status")))
                if include_warmup_details and isinstance(warmup.get("symbol_status"), list)
                else []
            ),
            "integrity_pending_symbols": (
                _trim_array(integrity_pending_symbols_all, len(integrity_pending_symbols_all))
                if include_warmup_details
                else []
            ),
            "integrity_pending_symbols_total": (
                len(warmup.get("integrity_pending_symbols"))
                if isinstance(warmup.get("integrity_pending_symbols"), list)
                else len(integrity_pending_symbols_all)
            ),
            "blocking_integrity_pending_symbols": (
                _trim_array(integrity_pending_split.get("blocking"), len(integrity_pending_split.get("blocking", [])))
                if include_warmup_details
                else []
            ),
            "blocking_integrity_pending_symbols_total": len(integrity_pending_split.get("blocking") or []),
            "monitor_integrity_pending_symbols": (
                _trim_array(integrity_pending_split.get("monitor"), len(integrity_pending_split.get("monitor", [])))
                if include_warmup_details
                else []
            ),
            "monitor_integrity_pending_symbols_total": len(integrity_pending_split.get("monitor") or []),
            "integrity_repair_reasons": integrity_repair_reasons,
            "blocking_integrity_repair_reasons": reason_split.get("blocking") or {},
            "monitor_integrity_repair_reasons": reason_split.get("monitor") or {},
            "preflight_repair": _as_dict(warmup.get("preflight_repair")) if include_warmup_details else {},
        },
        "realtime_compute": {
            "runs": int(realtime_compute.get("runs") or 0),
            "queue_size": int(realtime_compute.get("queue_size") or 0),
            "thread_alive": bool(realtime_compute.get("thread_alive")),
            "inflight": bool(realtime_compute.get("inflight")),
            "inflight_age_s": int(realtime_compute.get("inflight_age_s") or 0),
            "inflight_timeout_threshold_s": int(realtime_compute.get("inflight_timeout_threshold_s") or 0),
            "stalled": bool(realtime_compute.get("stalled")),
            "stall_reason": str(realtime_compute.get("stall_reason") or ""),
            "last_started": realtime_compute.get("last_started") or "",
            "last_run": realtime_compute.get("last_run") or "",
            "last_bar_close": realtime_compute.get("last_bar_close") or "",
            "last_elapsed_s": float(realtime_compute.get("last_elapsed_s") or realtime_result.get("elapsed_s") or 0),
            "last_processed": int(realtime_result.get("processed") or 0),
            "last_signals": int(realtime_result.get("signals") or 0),
            "last_errors": int(realtime_result.get("errors") or 0),
        },
        "daily_scan": {
            "market_date": str(daily_scan.get("market_date") or ""),
            "status": str(daily_scan.get("status") or ""),
            "reason": str(daily_scan.get("reason") or ""),
            "started_at": daily_scan.get("started_at") or "",
            "finished_at": daily_scan.get("finished_at") or "",
            "last_error": str(daily_scan.get("last_error") or ""),
            "result": _as_dict(daily_scan.get("result")),
        },
        "market_universe": {
            "market_date": str(market_universe.get("market_date") or ""),
            "last_daily_reset": market_universe.get("last_daily_reset") or "",
            "watchlist_pool_count": int(market_universe.get("watchlist_pool_count") or 0),
            "active_target_date": str(market_universe.get("active_target_date") or ""),
            "active_target_count": int(market_universe.get("active_target_count") or 0),
            "active_trade_symbols": _trim_array(
                market_universe.get("active_trade_symbols"),
                len(market_universe.get("active_trade_symbols")) if isinstance(market_universe.get("active_trade_symbols"), list) else 0,
            ),
            "active_trade_symbols_total": (
                len(market_universe.get("active_trade_symbols"))
                if isinstance(market_universe.get("active_trade_symbols"), list)
                else int(market_universe.get("active_trade_symbols_total") or 0)
            ),
            "last_target_refresh": market_universe.get("last_target_refresh") or "",
            "active_repair_interval_min": int(market_universe.get("active_repair_interval_min") or 0),
            "last_active_repair": market_universe.get("last_active_repair") or "",
            "last_active_repair_symbols": _trim_array(market_universe.get("last_active_repair_symbols"), 12),
            "last_active_repair_symbols_total": (
                len(market_universe.get("last_active_repair_symbols"))
                if isinstance(market_universe.get("last_active_repair_symbols"), list)
                else int(market_universe.get("last_active_repair_symbols_total") or 0)
            ),
            "last_active_repair_reasons": _trim_object_entries(market_universe.get("last_active_repair_reasons"), 12),
            "watchlist_backfill_interval_min": int(market_universe.get("watchlist_backfill_interval_min") or 0),
            "last_watchlist_backfill": market_universe.get("last_watchlist_backfill") or "",
        },
        "runtime_control": _as_dict(payload.get("runtime_control")),
    }


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


@app.route("/health", methods=["GET"])
def health() -> Response:
    scheduler_jobs = _scheduler_job_states()
    return jsonify(
        {
            "ok": True,
            "status": "running",
            "service_profile": str(os.environ.get("IBKR_SERVICE_PROFILE") or "api"),
            "service_topology": build_service_topology(),
            "upstreams": {
                "pocketbase": PB_BASE_URL,
                "compute": COMPUTE_BASE_URL,
                "runtime": RUNTIME_BASE_URL,
                "scheduler": SCHEDULER_BASE_URL,
            },
            "scheduler_job_count": len(scheduler_jobs),
        }
    )


@app.route("/status", methods=["GET"])
def status() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    scheduler_status = _scheduler_status(environment)
    scheduler_jobs = scheduler_status.get("jobs") if isinstance(scheduler_status.get("jobs"), dict) else {}
    config.refresh()
    scheduler_items = build_cron_payload(config, environment, scheduler_jobs)
    return jsonify(
        {
            "ok": True,
            "status": "running",
            "service_profile": str(os.environ.get("IBKR_SERVICE_PROFILE") or "api"),
            "service_topology": build_service_topology(),
            "compatibility": {
                "direct_proxy_routes": sorted({path for (_, path) in DIRECT_PROXY_MAP}),
                "proxy_action_routes": sorted(ACTION_PROXY_MAP),
                "native_custom_routes": [
                    "ibkr/2fa/status",
                    "ibkr/healthz",
                    "ibkr/orders/cancel_group",
                    "ibkr/orders/close_group",
                    "ibkr/orders/reconcile",
                    "ibkr/orders/upsert",
                    "ibkr/reverse/ack",
                    "ibkr/reverse/calculate",
                    "ibkr/reverse/dispatch",
                    "ibkr/reverse/list",
                    "ibkr/reverse/pending",
                    "ibkr/signal",
                    "ibkr/signals",
                    "ibkr/runtime/config",
                    "ibkr/signals/ack",
                    "ibkr/signals/pending",
                    "ibkr/startup/progress",
                    "ibkr/startup/status",
                    "ibkr/statusz",
                    "system/cronz",
                    "system/event",
                    "system/healthz",
                    "system/monitorz",
                    "system/summaryz",
                    "system/schedulerz",
                ],
                "native_webhook_routes": [
                    "feishu/callback",
                    "order/cancel",
                    "order/close",
                    "signal/cancel",
                    "signal/confirm",
                    "tv",
                ],
                "delegated_pocketbase_custom_routes": DELEGATED_POCKETBASE_CUSTOM_ROUTES,
                "delegated_pocketbase_webhook_routes": DELEGATED_POCKETBASE_WEBHOOK_ROUTES,
                "pocketbase_proxy_routes": {
                    "custom": "/api/custom/*",
                    "webhook": "/webhook/*",
                },
                "fallback_to_pocketbase_custom": True,
                "fallback_to_pocketbase_webhooks": True,
            },
            "scheduler_jobs": scheduler_jobs,
            "scheduler": _augment_scheduler_summary(_build_scheduler_summary(environment, scheduler_status), scheduler_items),
        }
    )


@app.route("/api/collections/<path:subpath>", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
def collections_proxy(subpath: str) -> Response:
    return _forward_request(PB_BASE_URL, f"/api/collections/{subpath}")

@app.route("/api/custom/ibkr/runtime/config", methods=["GET"])
def custom_ibkr_runtime_config() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    scope = str(request.args.get("scope") or "").strip().lower() or "effective"
    rows = pb.get_runtime_config(scope="all", environment=environment)
    if scope != "all":
        rows = _pick_effective_config_rows(rows, environment)
    return jsonify(
        {
            "ok": True,
            "environment": environment,
            "scope": "all" if scope == "all" else "effective",
            "items": _serialize_config_rows(rows),
            "source": "ibkr-api",
            "service_topology": build_service_topology(),
        }
    )


@app.route("/api/custom/system/event", methods=["POST"])
def custom_system_event() -> Response:
    payload = request.get_json(silent=True) or {}
    environment = _normalize_environment(payload.get("environment"), "live")
    raw_title = str(payload.get("title") or "").strip()
    raw_detail = payload.get("detail") if payload.get("detail") is not None else {}
    event_type = str(payload.get("event_type") or "status_change").strip() or "status_change"
    level = str(payload.get("level") or "info").strip().lower() or "info"
    source = str(payload.get("source") or "ibkr-api").strip() or "ibkr-api"
    message_id = str(payload.get("message_id") or "").strip()

    if not raw_title:
        return jsonify({"ok": False, "environment": environment, "error": "title required", "source": "ibkr-api"}), 400

    config.refresh()
    delivery = _deliver_system_event_notification(
        event_type,
        level,
        source,
        raw_title,
        raw_detail,
        environment,
        message_id=message_id,
    )
    notified = bool(delivery.get("success")) and not bool(delivery.get("suppressed"))
    persisted = bool(_write_system_event_record(event_type, level, source, raw_title, raw_detail, environment, notified))
    return jsonify(
        {
            "ok": True,
            "environment": environment,
            "notified": notified,
            "persisted": persisted,
            "message_id": str(delivery.get("message_id") or message_id),
            "updated": bool(delivery.get("updated")),
            "skipped": bool(delivery.get("skipped")),
            "suppressed": bool(delivery.get("suppressed")),
            "error": str(delivery.get("error") or ""),
            "source": "ibkr-api",
        }
    )


@app.route("/api/custom/system/cronz", methods=["GET"])
def custom_system_cronz() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    config.refresh()
    scheduler_status = _scheduler_status(environment)
    scheduler_jobs = scheduler_status.get("jobs") if isinstance(scheduler_status.get("jobs"), dict) else {}
    items = build_cron_payload(config, environment, scheduler_jobs)
    return jsonify(
        {
            "ok": True,
            "items": items,
            "scheduler": _augment_scheduler_summary(_build_scheduler_summary(environment, scheduler_status), items),
            "source": "ibkr-api",
            "service_topology": build_service_topology(),
        }
    )


@app.route("/api/custom/system/healthz", methods=["GET"])
def custom_system_healthz() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    payload = _build_system_monitor_payload(environment)
    return jsonify(
        {
            "ok": bool(payload.get("ok", False)),
            "status": str(payload.get("status") or "offline"),
            "environment": environment,
            "requested_environment": payload.get("requested_environment") or environment,
            "actual_runtime_environment": payload.get("actual_runtime_environment") or environment,
            "runtime_environment_mismatch": bool(payload.get("runtime_environment_mismatch")),
            "service_topology": payload.get("service_topology") if isinstance(payload.get("service_topology"), dict) else build_service_topology(),
            "service_monitor": payload.get("service_monitor") if isinstance(payload.get("service_monitor"), dict) else {},
            "scheduler": payload.get("scheduler") if isinstance(payload.get("scheduler"), dict) else {},
            "source": "ibkr-api",
        }
    )


@app.route("/api/custom/system/schedulerz", methods=["GET"])
def custom_system_schedulerz() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    config.refresh()
    scheduler_status = _scheduler_status(environment)
    scheduler_jobs = scheduler_status.get("jobs") if isinstance(scheduler_status.get("jobs"), dict) else {}
    items = build_cron_payload(config, environment, scheduler_jobs)
    summary = _augment_scheduler_summary(_build_scheduler_summary(environment, scheduler_status), items)
    return jsonify(
        {
            "ok": bool(scheduler_status.get("ok", False)),
            "status": str(summary.get("status") or "offline"),
            "environment": environment,
            "scheduler": summary,
            "items": items,
            "source": "ibkr-api",
            "service_topology": build_service_topology(),
        }
    )


@app.route("/api/custom/system/jobs/signal_expiry", methods=["POST"])
def custom_system_job_signal_expiry() -> Response:
    payload, status_code = build_signal_expiry_response(
        pb,
        payload=request.get_json(silent=True) or {},
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        config_value=_config_value,
        send_interactive=_feishu_send_interactive,
        update_interactive=_feishu_update_interactive,
        signal_chat_id_fn=_signal_chat_id,
        console_base_url=_console_base_url(),
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/system/jobs/order_detail_integrity", methods=["POST"])
def custom_system_job_order_detail_integrity() -> Response:
    payload, status_code = build_order_detail_integrity_response(
        pb,
        payload=request.get_json(silent=True) or {},
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/system/summaryz", methods=["GET"])
def custom_system_summaryz() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    lite_mode = _parse_boolean(request.args.get("lite"), False)
    return jsonify(_build_system_summary_payload(environment, lite_mode=lite_mode))


@app.route("/api/custom/system/monitorz", methods=["GET"])
def custom_system_monitorz() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    return jsonify(_build_system_monitor_payload(environment))


@app.route("/api/custom/ibkr/healthz", methods=["GET"])
def custom_ibkr_healthz() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    compute_result = _fetch_compute_health(environment)
    runtime_result = _fetch_runtime_health(environment)
    compute_payload = _as_dict(compute_result.get("payload"))
    runtime_payload = _as_dict(runtime_result.get("payload"))
    service_topology = _merge_service_topology(compute_payload, runtime_payload)
    runtime_expected = str(service_topology.get("runtime_mode") or "").strip().lower() == "remote"
    ok = bool(compute_result.get("ok")) and (not runtime_expected or bool(runtime_result.get("ok")))
    degraded = bool(compute_result.get("ok")) or bool(runtime_result.get("ok")) or bool(compute_payload) or bool(runtime_payload)
    errors = {
        key: value
        for key, value in {
            "compute": str(compute_result.get("error") or ""),
            "runtime": str(runtime_result.get("error") or ""),
        }.items()
        if value
    }
    return jsonify(
        {
            "ok": ok,
            "status": "running" if ok else ("degraded" if degraded else "offline"),
            "environment": environment,
            "requested_environment": environment,
            "actual_runtime_environment": _normalize_environment(runtime_payload.get("environment") or environment, environment),
            "compute": compute_payload,
            "runtime": runtime_payload,
            "service_topology": service_topology,
            "source": "ibkr-api",
            "proxy_upstream_compute": f"{COMPUTE_BASE_URL}/health",
            "proxy_upstream_runtime": runtime_result.get("upstream") or "",
            "error": "; ".join(f"{key}: {value}" for key, value in errors.items()),
            "errors": errors,
        }
    )


@app.route("/api/custom/ibkr/statusz", methods=["GET"])
def custom_ibkr_statusz() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    include_engines = _parse_boolean(request.args.get("full"), False) or not _parse_boolean(request.args.get("lite"), True)
    include_warmup_details = (
        _parse_boolean(request.args.get("warmup"), False)
        or _parse_boolean(request.args.get("warmup_full"), False)
        or include_engines
    )

    compute_result = _fetch_compute_status(environment)
    runtime_result = _fetch_runtime_status(environment)
    compute_payload = _as_dict(compute_result.get("payload"))
    runtime_payload = _as_dict(runtime_result.get("payload"))
    persisted_daily_scan = _load_daily_scan_state(environment)
    fallback_active_target_date = str(persisted_daily_scan.get("market_date") or "").strip()
    fallback_active_target_count = (
        _count_active_today_targets(environment, fallback_active_target_date)
        if fallback_active_target_date
        else 0
    )

    compute_data = _build_statusz_compute_payload(compute_payload, include_engines)
    live_readiness = _build_statusz_live_readiness(compute_payload, runtime_payload)
    runtime_data = _build_statusz_runtime_payload(
        runtime_payload,
        include_warmup_details,
        live_readiness=live_readiness,
        fallback_state={
            "daily_scan": persisted_daily_scan,
            "active_target_date": fallback_active_target_date,
            "active_target_count": fallback_active_target_count,
        },
    )
    service_topology = _merge_service_topology(compute_data, runtime_data)
    actual_runtime_environment = _normalize_environment(
        runtime_data.get("environment") or compute_data.get("environment") or environment,
        environment,
    )
    errors = {
        key: value
        for key, value in {
            "compute": str(compute_result.get("error") or ""),
            "runtime": str(runtime_result.get("error") or ""),
        }.items()
        if value
    }
    ok = (
        not errors
        and compute_data.get("ok") is not False
        and (runtime_data.get("ok") is not False or not runtime_data)
    )
    degraded = bool(compute_result.get("ok")) or bool(runtime_result.get("ok")) or bool(compute_data) or bool(runtime_payload)
    response = dict(compute_data)
    if runtime_data.get("ok") is not False:
        response.update(runtime_data)
    response.update(
        {
            "compute": compute_data,
            "runtime": runtime_data,
            "service_topology": service_topology,
            "warmup_details_included": bool(include_warmup_details),
            "requested_environment": environment,
            "actual_runtime_environment": actual_runtime_environment,
            "runtime_environment_mismatch": actual_runtime_environment != environment,
            "ok": ok,
            "status": "running" if ok else ("degraded" if degraded else "offline"),
            "source": "ibkr-api",
            "proxy_upstream_compute": f"{COMPUTE_BASE_URL}/status",
            "proxy_upstream_runtime": runtime_result.get("selected_upstream") or "",
            "proxy_upstream_runtime_proxy": runtime_result.get("proxy_upstream") or "",
            "proxy_upstream_runtime_direct": runtime_result.get("direct_upstream") or "",
        }
    )
    if errors:
        response["errors"] = errors
        response["error"] = "; ".join(f"{key}: {value}" for key, value in errors.items())
    return jsonify(response)


@app.route("/api/custom/ibkr/startup/progress", methods=["POST"])
def custom_ibkr_startup_progress() -> Response:
    payload = request.get_json(silent=True) or {}
    environment = _normalize_environment(payload.get("environment"), "live")
    times = _time_strings()
    action = str(payload.get("action") or "update").strip().lower() or "update"
    create_if_missing = bool(payload.get("create_if_missing") is True)
    current_payload = _get_state_payload(IBKR_STARTUP_STATE_KEY, environment, date=IBKR_STARTUP_STATE_DATE)
    current = _normalize_startup_state(current_payload.get("data"), environment)
    next_state = _normalize_startup_state(current, environment)

    should_create_cycle = action == "begin" or (not bool(current.get("active")) and create_if_missing)
    if should_create_cycle:
        next_seq = max(0, int(current.get("startup_seq") or 0)) + 1
        next_state.update(
            {
                "cycle_id": _build_startup_cycle_id(environment),
                "startup_seq": next_seq,
                "startup_label": _build_startup_label(environment, next_seq, times["us"]),
                "active": True,
                "status": "active",
                "started_at": times["us"],
                "finished_at": "",
                "startup_chat_id": _startup_chat_id(environment),
                "message_id": "",
                "last_delivery_mode": "",
                "last_delivery_at": "",
                "last_delivery_error": "",
                "fields": {},
                "steps": _default_startup_steps(),
            }
        )
    elif not next_state.get("cycle_id"):
        next_seq = max(1, int(current.get("startup_seq") or 1))
        next_state["cycle_id"] = _build_startup_cycle_id(environment)
        next_state["startup_seq"] = next_seq
        next_state["startup_label"] = current.get("startup_label") or _build_startup_label(environment, next_seq, current.get("started_at") or times["us"])

    next_state["startup_chat_id"] = _startup_chat_id(environment)
    if "title" in payload:
        next_state["title"] = str(payload.get("title") or next_state.get("title") or "IBKR Runtime 启动中")
    if "summary" in payload:
        next_state["summary"] = str(payload.get("summary") or "")
    if "current_step" in payload:
        next_state["current_step"] = str(payload.get("current_step") or "")
    if "current_blocker" in payload:
        next_state["current_blocker"] = str(payload.get("current_blocker") or "")
    if "operator_action" in payload:
        next_state["operator_action"] = str(payload.get("operator_action") or "")
    if "reason" in payload:
        next_state["reason"] = str(payload.get("reason") or "")
    if "source" in payload:
        next_state["source"] = str(payload.get("source") or "")
    if "runtime_phase" in payload:
        next_state["runtime_phase"] = str(payload.get("runtime_phase") or "")
    if "runtime_url" in payload:
        next_state["runtime_url"] = str(payload.get("runtime_url") or "")
    if "trigger_login" in payload:
        next_state["trigger_login"] = bool(payload.get("trigger_login") is True)

    if isinstance(payload.get("fields"), dict):
        next_state["fields"] = {
            **_normalize_startup_fields(next_state.get("fields")),
            **_normalize_startup_fields(payload.get("fields")),
        }
    next_state["steps"] = _merge_startup_steps(next_state.get("steps"), payload.get("steps"), bool(next_state.get("trigger_login")))

    if action in {"begin", "update", "pause"}:
        next_state["active"] = True
        next_state["status"] = str(payload.get("status") or "active").strip().lower() or "active"
        if not next_state.get("started_at"):
            next_state["started_at"] = times["us"]
        next_state["finished_at"] = ""
    elif action == "complete":
        next_state["active"] = False
        next_state["status"] = "completed"
        next_state["finished_at"] = times["us"]
    elif action == "fail":
        next_state["active"] = False
        next_state["status"] = "failed"
        next_state["finished_at"] = times["us"]
    elif action in {"abort", "clear"}:
        next_state["active"] = False
        next_state["status"] = "aborted"
        next_state["finished_at"] = times["us"]

    if action == "complete":
        next_state["steps"] = _merge_startup_steps(
            next_state.get("steps"),
            {
                "runtime_resume": {"status": "done", "detail": "认证恢复后 Runtime 已回到可运行状态。"},
                "health_check": {"status": "done", "detail": "启动后健康检查已通过。"},
            },
            bool(next_state.get("trigger_login")),
        )

    next_state["last_update_at"] = times["us"]

    try:
        saved_record = pb.upsert_state(IBKR_STARTUP_STATE_KEY, environment, next_state, date=IBKR_STARTUP_STATE_DATE)
    except Exception as exc:
        return jsonify({"ok": False, "environment": environment, "error": str(exc), "source": "ibkr-api"}), 500

    saved_state = _normalize_startup_state((saved_record or {}).get("data") if isinstance(saved_record, dict) else next_state, environment)
    delivery = _deliver_startup_progress_card(saved_state, environment)
    if delivery.get("message_id"):
        saved_state["message_id"] = str(delivery.get("message_id") or "")
    saved_state["last_delivery_mode"] = "update" if current.get("message_id") else "send"
    saved_state["last_delivery_at"] = times["us"]
    saved_state["last_delivery_error"] = "" if delivery.get("success") else str(delivery.get("error") or "")
    try:
        pb.upsert_state(IBKR_STARTUP_STATE_KEY, environment, saved_state, date=IBKR_STARTUP_STATE_DATE)
    except Exception:
        pass

    if bool(payload.get("record_event") is True):
        event_type = str(payload.get("event_type") or "status_change").strip() or "status_change"
        level = str(payload.get("level") or "info").strip().lower() or "info"
        event_source = str(payload.get("event_source") or payload.get("source") or "ibkr_compute").strip() or "ibkr_compute"
        event_detail = payload.get("event_detail") if isinstance(payload.get("event_detail"), dict) else {
            "cycle_id": saved_state.get("cycle_id") or "",
            "current_step": _resolve_startup_step_label(saved_state),
            "current_blocker": saved_state.get("current_blocker") or saved_state.get("summary") or "",
            "operator_action": saved_state.get("operator_action") or "",
        }
        _write_system_event_record(
            event_type,
            level,
            event_source,
            str(payload.get("event_title") or saved_state.get("title") or "IBKR Runtime 启动中"),
            event_detail,
            environment,
            bool(delivery.get("success")),
        )

    return jsonify(
        {
            "ok": True,
            "environment": environment,
            "date": IBKR_STARTUP_STATE_DATE,
            "cycle_id": saved_state.get("cycle_id") or "",
            "startup_label": saved_state.get("startup_label") or "",
            "message_id": saved_state.get("message_id") or "",
            "state": saved_state,
            "delivery_ok": bool(delivery.get("success")),
            "delivery_error": str(delivery.get("error") or ""),
            "source": "ibkr-api",
        }
    )


@app.route("/api/custom/ibkr/startup/status", methods=["GET"])
def custom_ibkr_startup_status() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    payload = _get_state_payload(IBKR_STARTUP_STATE_KEY, environment, date=IBKR_STARTUP_STATE_DATE)
    raw_state = _as_dict(payload.get("data"))
    state = {
        **raw_state,
        "active": bool(raw_state.get("active")),
        "status": str(raw_state.get("status") or "idle").strip().lower() or "idle",
        "startup_label": str(raw_state.get("startup_label") or ""),
        "current_step": str(raw_state.get("current_step") or ""),
        "current_blocker": str(raw_state.get("current_blocker") or ""),
        "operator_action": str(raw_state.get("operator_action") or ""),
        "summary": str(raw_state.get("summary") or ""),
        "reason": str(raw_state.get("reason") or ""),
        "runtime_phase": str(raw_state.get("runtime_phase") or ""),
        "steps": _as_dict(raw_state.get("steps")),
        "fields": _as_dict(raw_state.get("fields")),
    }
    return jsonify(
        {
            "ok": True,
            "environment": payload.get("environment") or environment,
            "date": payload.get("date") or IBKR_STARTUP_STATE_DATE,
            "startup_label": state.get("startup_label") or "",
            "state": state,
            "source": "ibkr-api",
        }
    )


@app.route("/api/custom/ibkr/2fa/status", methods=["GET"])
def custom_ibkr_two_factor_status() -> Response:
    environment = _normalize_environment(request.args.get("environment"), "live")
    payload = _get_state_payload(IBKR_2FA_STATE_KEY, environment, date=IBKR_2FA_STATE_DATE)
    state = _as_dict(payload.get("data"))
    if not str(state.get("status") or "").strip():
        state["status"] = "requested"
    if not str(state.get("recovery_phase") or "").strip():
        state["recovery_phase"] = "idle"

    runtime_result = _fetch_runtime_status(environment)
    runtime_payload = _as_dict(runtime_result.get("payload"))
    if runtime_payload:
        state = _normalize_two_factor_state_with_runtime(state, runtime_payload)
        actual_runtime_environment = _normalize_environment(runtime_payload.get("environment") or environment, environment)
        state["requested_environment"] = environment
        state["actual_runtime_environment"] = actual_runtime_environment
        state["runtime_environment_mismatch"] = actual_runtime_environment != environment
        if state["runtime_environment_mismatch"]:
            state["message"] = (
                f"当前 {environment.upper()} 页面没有独立 runtime；实际运行中的是 "
                f"{actual_runtime_environment.upper()}，2FA 动作已阻止。"
            )
            state["last_result"] = f"当前显示的是 {actual_runtime_environment.upper()} 运行态。"
    if runtime_result.get("error"):
        state["runtime_status_error"] = str(runtime_result.get("error") or "")

    return jsonify(
        {
            "ok": True,
            "environment": payload.get("environment") or environment,
            "date": payload.get("date") or IBKR_2FA_STATE_DATE,
            "state": state,
            "source": "ibkr-api",
        }
    )


@app.route("/api/custom/ibkr/proxy", methods=["POST"])
def custom_ibkr_proxy() -> Response:
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()
    target = ACTION_PROXY_MAP.get(action)
    if not target:
        return _proxy_custom_to_pb("ibkr/proxy")

    base_url, target_path = target
    proxy_body = {key: value for key, value in payload.items() if key != "action"}
    if action == "recompute":
        proxy_body = None
    return _forward_request(base_url, target_path, json_body=proxy_body)


@app.route("/api/custom/ibkr/signal", methods=["POST"])
def custom_ibkr_signal() -> Response:
    payload, status_code = build_signal_ingest_response(
        pb,
        payload=request.get_json(silent=True) or {},
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        config_value=_config_value,
        send_interactive=_feishu_send_interactive,
        update_interactive=_feishu_update_interactive,
        signal_chat_id_fn=_signal_chat_id,
        console_base_url=_console_base_url(),
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/signals", methods=["POST"])
def custom_ibkr_signals() -> Response:
    payload, status_code = build_signals_ingest_response(
        pb,
        payload=request.get_json(silent=True) or {},
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        config_value=_config_value,
        send_interactive=_feishu_send_interactive,
        update_interactive=_feishu_update_interactive,
        signal_chat_id_fn=_signal_chat_id,
        console_base_url=_console_base_url(),
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/signals/pending", methods=["GET"])
def custom_ibkr_signals_pending() -> Response:
    payload, status_code = build_signals_pending_response(
        pb,
        environment=request.args.get("environment"),
        date_str=request.args.get("date") or "",
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        as_dict=_as_dict,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/signals/ack", methods=["POST"])
def custom_ibkr_signals_ack() -> Response:
    payload, status_code = build_signals_ack_response(
        pb,
        payload=request.get_json(silent=True) or {},
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        order_upsert_builder=build_order_upsert_response,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/orders/upsert", methods=["POST"])
def custom_ibkr_orders_upsert() -> Response:
    payload, status_code = build_order_upsert_response(
        pb,
        payload=request.get_json(silent=True) or {},
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/orders/reconcile", methods=["POST"])
def custom_ibkr_orders_reconcile() -> Response:
    payload, status_code = build_orders_reconcile_response(
        pb,
        payload=request.get_json(silent=True) or {},
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/orders/cancel_group", methods=["POST"])
def custom_ibkr_orders_cancel_group() -> Response:
    payload, status_code = build_order_cancel_group_response(
        pb,
        payload=request.get_json(silent=True) or {},
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        cancel_broker_order=_cancel_broker_order_via_runtime,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/orders/close_group", methods=["POST"])
def custom_ibkr_orders_close_group() -> Response:
    payload, status_code = build_order_close_group_response(
        pb,
        payload=request.get_json(silent=True) or {},
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/reverse/list", methods=["GET"])
def custom_ibkr_reverse_list() -> Response:
    payload, status_code = build_reverse_list_response(
        pb,
        environment=request.args.get("environment"),
        date_str=request.args.get("date") or "",
        symbol=request.args.get("symbol") or "",
        statuses=request.args.get("status") or "",
        limit=request.args.get("limit"),
        normalize_environment=_normalize_environment,
        escape_filter=_escape_filter_string,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/reverse/calculate", methods=["POST"])
def custom_ibkr_reverse_calculate() -> Response:
    payload, status_code = build_reverse_calculate_response(
        pb,
        payload=request.get_json(silent=True) or {},
        escape_filter=_escape_filter_string,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/reverse/pending", methods=["GET"])
def custom_ibkr_reverse_pending() -> Response:
    payload, status_code = build_reverse_pending_response(
        pb,
        environment=request.args.get("environment"),
        limit=request.args.get("limit"),
        normalize_environment=_normalize_environment,
        escape_filter=_escape_filter_string,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/reverse/dispatch", methods=["POST"])
def custom_ibkr_reverse_dispatch() -> Response:
    payload, status_code = build_reverse_dispatch_response(
        pb,
        payload=request.get_json(silent=True) or {},
        escape_filter=_escape_filter_string,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/api/custom/ibkr/reverse/ack", methods=["POST"])
def custom_ibkr_reverse_ack() -> Response:
    payload, status_code = build_reverse_ack_response(
        pb,
        payload=request.get_json(silent=True) or {},
        escape_filter=_escape_filter_string,
    )
    response = jsonify(payload)
    return response if status_code == 200 else (response, status_code)


@app.route("/webhook/tv", methods=["POST"])
def webhook_tv() -> Response:
    payload = request.get_json(silent=True) or {}
    data_type = str(payload.get("type") or "signal").strip().lower() or "signal"
    if data_type == "indicator":
        return _upsert_tv_indicator(payload)
    return _upsert_tv_signal(payload)


@app.route("/webhook/signal/confirm", methods=["GET"])
def webhook_signal_confirm() -> Response:
    payload, status_code = build_signal_confirm_webhook_response(
        pb,
        payload={
            "id": request.args.get("id") or "",
            "environment": request.args.get("environment") or "",
        },
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        update_signal_card=_feishu_update_interactive,
        console_base_url=_console_base_url(),
    )
    return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}


@app.route("/webhook/signal/cancel", methods=["GET"])
def webhook_signal_cancel() -> Response:
    payload, status_code = build_signal_cancel_webhook_response(
        pb,
        payload={
            "id": request.args.get("id") or "",
            "environment": request.args.get("environment") or "",
        },
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        cancel_broker_order=_cancel_broker_order_via_runtime,
        update_signal_card=_feishu_update_interactive,
        console_base_url=_console_base_url(),
    )
    return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}


@app.route("/webhook/order/cancel", methods=["GET"])
def webhook_order_cancel() -> Response:
    payload, status_code = build_order_cancel_webhook_response(
        pb,
        payload={
            "id": request.args.get("id") or "",
            "environment": request.args.get("environment") or "",
        },
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        cancel_broker_order=_cancel_broker_order_via_runtime,
    )
    return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}


@app.route("/webhook/order/close", methods=["GET"])
def webhook_order_close() -> Response:
    payload, status_code = build_order_close_webhook_response(
        pb,
        payload={
            "id": request.args.get("id") or "",
            "environment": request.args.get("environment") or "",
        },
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
    )
    return payload.get("body") or "", int(status_code or 200), {"Content-Type": str(payload.get("content_type") or "text/html; charset=utf-8")}


@app.route("/webhook/feishu/callback", methods=["POST"])
def webhook_feishu_callback():
    return _handle_feishu_callback_support(
        request.get_json(silent=True) or {},
        as_dict=_as_dict,
        normalize_environment=_normalize_environment,
        dispatch_feishu_2fa_callback_fn=_dispatch_feishu_2fa_callback,
        dispatch_feishu_order_callback_fn=_dispatch_feishu_order_callback,
        dispatch_feishu_signal_callback_fn=_dispatch_feishu_signal_callback,
        callback_toast_fn=_callback_toast,
        callback_response_fn=_feishu_callback_response,
    )


@app.route("/api/custom/<path:subpath>", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
def custom_proxy(subpath: str) -> Response:
    direct_target = DIRECT_PROXY_MAP.get((request.method.upper(), subpath))
    if direct_target:
        base_url, target_path = direct_target
        return _forward_request(base_url, target_path)
    return _proxy_custom_to_pb(subpath)


@app.route("/webhook/<path:subpath>", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
def webhook_proxy(subpath: str) -> Response:
    return _proxy_webhook_to_pb(subpath)
