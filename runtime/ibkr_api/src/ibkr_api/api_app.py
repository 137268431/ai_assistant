from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
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
    order_chat_id as _order_chat_id_support,
    signal_chat_id as _signal_chat_id_support,
    system_page_url as _system_page_url_support,
    system_status_chat_id as _system_status_chat_id_support,
    time_strings as _time_strings_support,
    trade_ledger_chat_id as _trade_ledger_chat_id_support,
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
from ibkr_api.home.current_metrics import publish_current_signal_metrics
from ibkr_api.compat.bootstrap import build_compat_proxy_deps
from ibkr_api.control.runtime_guard import (
    build_runtime_environment_mismatch_payload,
    inspect_requested_runtime_environment,
)
from ibkr_api.integrations.runtime_orders import cancel_broker_order_via_runtime as _cancel_broker_order_via_runtime_support
from ibkr_api.universe.active_window_progress import build_active_window_progress_response
from ibkr_api.universe.today_targets import build_today_targets_response
from ibkr_api.orders.cancel_sync import build_order_cancel_sync_response
from ibkr_api.orders.daily_stats import build_daily_order_stats, empty_daily_order_stats
from ibkr_api.orders.group_cancel import build_order_cancel_group_response
from ibkr_api.orders.group_close import build_order_close_group_response
from ibkr_api.orders.integrity import build_order_detail_integrity_response
from ibkr_api.orders.notifications import sync_order_callback_ledger_notification, sync_order_status_notification
from ibkr_api.orders.webhooks import build_order_cancel_webhook_response, build_order_close_webhook_response
from ibkr_api.orders.upsert import build_order_upsert_response
from ibkr_api.orders.reconcile import build_orders_reconcile_response
from ibkr_api.orders.realized_pnl_stats import build_realized_pnl_stats
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
    fetch_backtest_health as _fetch_backtest_health_support,
    fetch_backtest_status as _fetch_backtest_status_support,
    fetch_compute_health as _fetch_compute_health_support,
    fetch_compute_monitor as _fetch_compute_monitor_support,
    fetch_compute_status as _fetch_compute_status_support,
    fetch_runtime_health as _fetch_runtime_health_support,
    fetch_runtime_status as _fetch_runtime_status_support,
    merge_service_topology as _merge_service_topology_support,
)
from ibkr_api.runtime.strategy_capacity import normalize_strategy_capacity_snapshot, unavailable_strategy_capacity
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
from ibkr_api.system.storage_health import collect_storage_health as _collect_storage_health_support
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
from ibkr_api.tradingview.tv_primary import process_tv_primary_event as _process_tv_primary_event_support
from ibkr_compute.api.service_topology import build_service_topology
from ibkr_compute.core.broker_mode import resolve_data_environment
from ibkr_scheduler.cron_registry import build_cron_payload
from ibkr_compute.core.config import Config
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.observability.prometheus import install_flask_metrics
from ibkr_api.signals.api import build_signals_ack_response, build_signals_pending_response


REQUEST_TIMEOUT_SECONDS = max(1.0, float(os.environ.get("IBKR_API_PROXY_TIMEOUT_SEC", "60")))
PB_BASE_URL = str(os.environ.get("PB_BASE_URL") or "http://127.0.0.1:8090").rstrip("/")
COMPUTE_BASE_URL = str(os.environ.get("IBKR_COMPUTE_INTERNAL_URL") or "http://127.0.0.1:5100").rstrip("/")
BACKTEST_BASE_URL = str(os.environ.get("IBKR_BACKTEST_INTERNAL_URL") or "http://127.0.0.1:5105").rstrip("/")
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
DEFAULT_FEISHU_ORDER_CHAT_ID = str(os.environ.get("FEISHU_ORDER_CHAT_ID") or "oc_5ca4585e1fd108c2c662dfc358684945").strip()
DEFAULT_FEISHU_TRADE_LEDGER_CHAT_ID = str(os.environ.get("FEISHU_TRADE_LEDGER_CHAT_ID") or "oc_c5f7f750a38692b48220b8f6c58e0ac9").strip()
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
install_flask_metrics(app)
# Avoid recursively routing runtime-config reads back into this API service.
pb = PBClient(base_url=PB_BASE_URL, prefer_runtime_config_api=False)
config = Config(pb_client=pb)
try:
    config.refresh()
except Exception as exc:
    print(f"[ibkr_api] startup config refresh failed: {exc}")


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


def _pb_count_records(collection: str, filter_expr: str) -> int:
    request_fn = getattr(pb, "_request", None)
    if not callable(request_fn):
        get_all_records = getattr(pb, "get_all_records", None)
        if callable(get_all_records):
            return len(get_all_records(collection, filter=filter_expr, max_pages=100) or [])
        return len(pb.get_records(collection, filter=filter_expr, per_page=200, page=1) or [])
    url = f"{pb.base_url}/api/collections/{collection}/records"
    response = request_fn(
        "GET",
        url,
        params={"filter": filter_expr, "perPage": 1, "page": 1},
        timeout=15,
    )
    return int((response.json() or {}).get("totalItems") or 0)


def _pb_load_records_for_count(collection: str, filter_expr: str, *, max_pages: int = 100) -> list[dict[str, Any]]:
    get_all_records = getattr(pb, "get_all_records", None)
    if callable(get_all_records):
        return list(get_all_records(collection, filter=filter_expr, max_pages=max_pages) or [])
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        batch = list(pb.get_records(collection, filter=filter_expr, per_page=200, page=page) or [])
        rows.extend(batch)
        if len(batch) < 200:
            break
    return rows


def _market_date_bounds_ms(market_date: str) -> tuple[int, int]:
    start_dt = datetime.strptime(str(market_date or "").strip(), "%Y-%m-%d").replace(
        tzinfo=ET,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    end_dt = start_dt + timedelta(days=1)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def _sqlite_today_market_count(collection: str, environment: str, market_date: str) -> int | None:
    if collection != "ibkr_bars":
        return None
    try:
        from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite

        start_ms, end_ms = _market_date_bounds_ms(market_date)
        with open_pb_sqlite(readonly=True, timeout=2.0) as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM {collection}
                WHERE environment = ?
                  AND interval = ?
                  AND bar_time_ms >= ?
                  AND bar_time_ms < ?
                """,
                (str(environment or "live"), "5m", start_ms, end_ms),
            ).fetchone()
        return int((row["total"] if row else 0) or 0)
    except Exception:
        return None


PROTECTIVE_ORDER_ROLES = {"take_profit", "stop_loss", "repair_tp", "repair_sl", "tp", "sl"}
CLOSE_ORDER_ROLES = {"close", "manual_close", "market_close", "close_order", "reverse_close"}
ENTRY_ORDER_TYPES = {"entry", "entryorder"}


def _order_field(row: dict[str, Any], field: str) -> str:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    return str(row.get(field) or extra.get(field) or "").strip()


def _order_group_count_key(row: dict[str, Any], index: int) -> str:
    for field in ("trade_group_id", "entry_order_unique_id", "signal_id"):
        value = _order_field(row, field)
        if value:
            return value
    return str(row.get("id") or row.get("unique_id") or f"row-{index}").strip() or f"row-{index}"


def _count_order_groups(rows: list[dict[str, Any]]) -> int:
    return len({_order_group_count_key(row, index) for index, row in enumerate(rows or []) if isinstance(row, dict)})


def _is_main_order_row(row: dict[str, Any]) -> bool:
    role = _order_field(row, "role").lower()
    order_type = _order_field(row, "order_type").lower()
    if role in PROTECTIVE_ORDER_ROLES or order_type in {"takeprofit", "stoploss"}:
        return False
    return role == "entry" or order_type in ENTRY_ORDER_TYPES


def _count_main_order_rows(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows or [] if isinstance(row, dict) and _is_main_order_row(row))


def _is_filled_exit_order_for_stats(row: dict[str, Any]) -> bool:
    role = _order_field(row, "role").lower()
    order_type = _order_field(row, "order_type").lower().replace(" ", "").replace("_", "")
    unique_id = _order_field(row, "unique_id").lower()
    status = _order_field(row, "status").lower()
    if status != "filled":
        return False
    if role in PROTECTIVE_ORDER_ROLES or role in CLOSE_ORDER_ROLES:
        return True
    if order_type in {"takeprofit", "takeprofitorder", "tp", "stoploss", "stoplossorder", "sl", "stop"}:
        return True
    return order_type in {"mkt", "market", "marketclose"} and unique_id.startswith("close_")


def _chunk_values(values: list[str], size: int = 25) -> list[list[str]]:
    safe_size = max(1, int(size or 1))
    return [values[index : index + safe_size] for index in range(0, len(values), safe_size)]


def _equals_any_clause(field: str, values: list[str]) -> str:
    return " || ".join(f'{field} = "{_escape_filter_string(value)}"' for value in values if value)


def _load_linked_entry_rows_for_stats(order_rows: list[dict[str, Any]], environment: str) -> list[dict[str, Any]]:
    unique_ids: set[str] = set()
    trade_group_ids: set[str] = set()
    signal_ids: set[str] = set()
    for row in order_rows or []:
        if not isinstance(row, dict) or not _is_filled_exit_order_for_stats(row):
            continue
        for field in ("entry_order_unique_id", "parent_order_unique_id"):
            value = _order_field(row, field)
            if value:
                unique_ids.add(value)
        trade_group_id = _order_field(row, "trade_group_id")
        if trade_group_id:
            trade_group_ids.add(trade_group_id)
        signal_id = _order_field(row, "signal_id")
        if signal_id:
            signal_ids.add(signal_id)

    filters: list[str] = []
    for field, values in (
        ("unique_id", sorted(unique_ids)),
        ("trade_group_id", sorted(trade_group_ids)),
        ("signal_id", sorted(signal_ids)),
    ):
        for chunk in _chunk_values(values):
            clause = _equals_any_clause(field, chunk)
            if clause:
                filters.append(clause)
    if not filters:
        return []

    existing_keys = {
        str(row.get("id") or row.get("unique_id") or "").strip()
        for row in order_rows or []
        if isinstance(row, dict)
    }
    entries: list[dict[str, Any]] = []
    seen = set(existing_keys)
    env = _escape_filter_string(environment)
    for clause in filters:
        entry_filter = f'environment = "{env}" && role = "entry" && ({clause})'
        for row in _pb_load_records_for_count("orders", entry_filter, max_pages=20):
            if not isinstance(row, dict) or not _is_main_order_row(row):
                continue
            key = str(row.get("id") or row.get("unique_id") or "").strip()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            entries.append(row)
    return entries


def _order_match_ids_for_stats(row: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for field in ("order_id", "broker_order_id", "ib_order_id", "orderId"):
        value = _order_field(row, field)
        if value and value not in seen:
            seen.add(value)
            ids.append(value)
    return ids


def _load_execution_fills_for_stats(order_rows: list[dict[str, Any]], environment: str) -> list[dict[str, Any]]:
    order_ids: list[str] = []
    seen: set[str] = set()
    for row in order_rows or []:
        if not isinstance(row, dict):
            continue
        for order_id in _order_match_ids_for_stats(row):
            if order_id and order_id not in seen:
                seen.add(order_id)
                order_ids.append(order_id)
    if not order_ids:
        return []

    fills: list[dict[str, Any]] = []
    env = _escape_filter_string(environment)
    for chunk in _chunk_values(order_ids, 24):
        clause = _equals_any_clause("order_id", chunk)
        if not clause:
            continue
        fills.extend(
            _pb_load_records_for_count(
                "ibkr_execution_fills",
                f'environment = "{env}" && ({clause})',
                max_pages=10,
            )
        )
    return fills


def _apply_actual_realized_pnl_stats(order_stats: dict[str, Any], actual_stats: dict[str, Any]) -> dict[str, Any]:
    updated = dict(order_stats)
    updated.update(
        {
            "realized_gross_pnl": actual_stats.get("realized_gross_pnl", 0.0),
            "realized_net_pnl": actual_stats.get("realized_net_pnl", 0.0),
            "commission": actual_stats.get("commission", 0.0),
            "winning_trades": int(actual_stats.get("win_count") or 0),
            "losing_trades": int(actual_stats.get("loss_count") or 0),
            "flat_trades": int(actual_stats.get("flat_count") or 0),
            "profit_amount": actual_stats.get("profit_amount", 0.0),
            "loss_amount": actual_stats.get("loss_amount", 0.0),
            "pnl_missing_count": int(actual_stats.get("missing_count") or 0),
            "actual_exit_count": int(actual_stats.get("exit_count") or 0),
            "commission_missing_count": int(actual_stats.get("commission_missing_count") or 0),
            "entry_missing_count": int(actual_stats.get("entry_missing_count") or 0),
            "fill_missing_count": int(actual_stats.get("fill_missing_count") or 0),
            "currency_mismatch_count": int(actual_stats.get("currency_mismatch_count") or 0),
            "unsupported_asset_count": int(actual_stats.get("unsupported_asset_count") or 0),
            "ibkr_realized_pnl_mismatch_count": int(actual_stats.get("ibkr_realized_pnl_mismatch_count") or 0),
        }
    )
    return updated


def _load_today_counts(environment: str, market_date: str) -> dict[str, Any]:
    runtime_environment = _normalize_environment(environment, "live")
    data_environment = resolve_data_environment(runtime_environment)
    date_token = str(market_date or _time_strings()["date"]).strip() or _time_strings()["date"]
    try:
        next_date = (datetime.strptime(date_token, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        date_token = _time_strings()["date"]
        next_date = (datetime.strptime(date_token, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    env = _escape_filter_string(runtime_environment)
    data_env = _escape_filter_string(data_environment)
    start_us = _escape_filter_string(f"{date_token} 00:00:00")
    end_us = _escape_filter_string(f"{next_date} 00:00:00")
    start_ms, end_ms = _market_date_bounds_ms(date_token)
    date_filter = _escape_filter_string(date_token)
    specs = {
        "ibkr_bars": (
            "ibkr_bars",
            f'environment = "{data_env}" && interval = "5m" && us_time >= "{start_us}" && us_time < "{end_us}"',
        ),
        "ibkr_signals": (
            "ibkr_signals",
            (
                f'environment = "{data_env}" && '
                f'((us_time >= "{start_us}" && us_time < "{end_us}") || '
                f"(bar_time_ms >= {start_ms} && bar_time_ms < {end_ms}))"
            ),
        ),
        "tv_webhook_events": (
            "tv_webhook_events",
            f'environment = "{data_env}" && date = "{date_filter}"',
        ),
        "orders": (
            "orders",
            f'environment = "{env}" && us_time >= "{start_us}" && us_time < "{end_us}"',
        ),
        "events": (
            "system_events",
            f'environment = "{env}" && us_time >= "{start_us}" && us_time < "{end_us}"',
        ),
        "ibkr_targets": (
            "ibkr_targets",
            f'environment = "{data_env}" && date = "{date_filter}"',
        ),
    }
    counts: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, (collection, filter_expr) in specs.items():
        try:
            count_environment = data_environment if key in {"ibkr_bars", "ibkr_signals", "ibkr_targets", "tv_webhook_events"} else runtime_environment
            sqlite_count = _sqlite_today_market_count(collection, count_environment, date_token)
            counts[key] = sqlite_count if sqlite_count is not None else _pb_count_records(collection, filter_expr)
        except Exception as exc:
            counts[key] = 0
            errors[key] = str(exc)
    try:
        order_rows = _pb_load_records_for_count("orders", specs["orders"][1])
        main_orders = _count_main_order_rows(order_rows)
        counts["main_orders"] = main_orders
        counts["order_groups"] = main_orders
        stats_rows = list(order_rows)
        try:
            stats_rows.extend(_load_linked_entry_rows_for_stats(order_rows, runtime_environment))
        except Exception as exc:
            errors["order_entry_links"] = str(exc)
        order_stats = build_daily_order_stats(stats_rows)
        try:
            execution_fills = _load_execution_fills_for_stats(stats_rows, runtime_environment)
            actual_stats = build_realized_pnl_stats(stats_rows, execution_fills, start_ms=start_ms, end_ms=end_ms)
            if int(actual_stats.get("exit_count") or 0) > 0 or int(actual_stats.get("missing_count") or 0) > 0:
                order_stats = _apply_actual_realized_pnl_stats(order_stats, actual_stats)
        except Exception as exc:
            errors["execution_fills_stats"] = str(exc)
        counts.update(order_stats)
    except Exception as exc:
        fallback_orders = int(counts.get("orders") or 0)
        counts["main_orders"] = fallback_orders
        counts["order_groups"] = fallback_orders
        counts.update(empty_daily_order_stats())
        errors["order_groups"] = str(exc)
    if errors:
        counts["errors"] = errors
    return counts


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


@app.before_request
def _refresh_api_business_metrics_for_prometheus():
    if request.path != "/metrics":
        return None
    try:
        publish_current_signal_metrics(pb, payload={}, time_strings=_time_strings)
    except Exception as exc:
        app.logger.debug("Failed to refresh API business metrics for Prometheus: %s", exc)
    return None


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


def _order_chat_id(environment: str) -> str:
    return _order_chat_id_support(
        environment,
        config_value_fn=_config_value,
        default_chat_id=DEFAULT_FEISHU_ORDER_CHAT_ID,
    )


def _trade_ledger_chat_id(environment: str) -> str:
    return _trade_ledger_chat_id_support(
        environment,
        config_value_fn=_config_value,
        default_chat_id=DEFAULT_FEISHU_TRADE_LEDGER_CHAT_ID,
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


def _notify_order_status(status: str, order_row: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    data = options if isinstance(options, dict) else {}
    extra = (order_row or {}).get("extra") if isinstance((order_row or {}).get("extra"), dict) else {}
    raw_environment = (order_row or {}).get("environment") or extra.get("environment") or data.get("environment") or "live"
    environment = str(raw_environment).strip() or "live"
    return sync_order_status_notification(
        pb,
        order_row,
        action=status,
        message=str(data.get("message") or ""),
        message_id=str(data.get("message_id") or data.get("messageId") or ""),
        related_rows=data.get("related_rows") if isinstance(data.get("related_rows"), list) else None,
        send_interactive=_feishu_send_interactive,
        update_interactive=_feishu_update_interactive,
        order_chat_id=_order_chat_id(environment),
        console_base_url=_console_base_url(),
    )


def _notify_order_callback_ledger(status: str, order_row: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    data = options if isinstance(options, dict) else {}
    extra = (order_row or {}).get("extra") if isinstance((order_row or {}).get("extra"), dict) else {}
    raw_environment = (order_row or {}).get("environment") or extra.get("environment") or data.get("environment") or "live"
    environment = str(raw_environment).strip() or "live"
    return sync_order_callback_ledger_notification(
        pb,
        order_row,
        previous_order=data.get("previous_order") if isinstance(data.get("previous_order"), dict) else None,
        send_interactive=_feishu_send_interactive,
        trade_ledger_chat_id=_trade_ledger_chat_id(environment),
        console_base_url=_console_base_url(),
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


def _trigger_runtime_signal_wakeup(payload: dict[str, Any]) -> dict[str, Any]:
    body = dict(payload or {})
    body.setdefault("source", "tv_webhook")
    body.setdefault("reason", "tv_primary_routed")
    result = _request_json_request(
        "POST",
        RUNTIME_BASE_URL,
        "/ibkr/signals/wakeup",
        json_body=body,
        timeout=1.0,
    )
    response_payload = result.get("payload") if isinstance(result, dict) else {}
    response_payload = response_payload if isinstance(response_payload, dict) else {}
    error = str(result.get("error") or response_payload.get("error") or "") if isinstance(result, dict) else ""
    return {
        "ok": bool((result or {}).get("ok") and response_payload.get("ok", True)),
        "woke": bool(response_payload.get("woke")),
        "reason": str(response_payload.get("reason") or ""),
        "error": error,
        "status_code": int((result or {}).get("status_code") or 0),
        "target_url": str((result or {}).get("target_url") or ""),
        "elapsed_ms": (result or {}).get("elapsed_ms"),
        "timeout_s": (result or {}).get("timeout_s"),
        "payload": response_payload,
    }


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


def _fetch_backtest_health(environment: str) -> dict[str, Any]:
    return _fetch_backtest_health_support(
        environment,
        request_json=_request_json,
        backtest_base_url=BACKTEST_BASE_URL,
        as_dict=_as_dict,
    )


def _fetch_backtest_status(environment: str) -> dict[str, Any]:
    return _fetch_backtest_status_support(
        environment,
        request_json=_request_json,
        backtest_base_url=BACKTEST_BASE_URL,
        as_dict=_as_dict,
    )


def _fetch_runtime_status(environment: str) -> dict[str, Any]:
    return _fetch_runtime_status_support(
        environment,
        request_json=_request_json,
        compute_base_url=COMPUTE_BASE_URL,
        runtime_base_url=RUNTIME_BASE_URL,
        as_dict=_as_dict,
    )


def _strategy_capacity_snapshot(environment: str) -> dict[str, Any]:
    result = _fetch_runtime_status(environment)
    payload = _as_dict(result.get("payload") if isinstance(result, dict) else {})
    if not payload:
        return unavailable_strategy_capacity(result.get("error") if isinstance(result, dict) else "runtime_status_unavailable")
    return normalize_strategy_capacity_snapshot(payload)


def _fetch_runtime_health(environment: str) -> dict[str, Any]:
    return _fetch_runtime_health_support(
        environment,
        request_json=_request_json,
        runtime_base_url=RUNTIME_BASE_URL,
        as_dict=_as_dict,
    )


def _merge_service_topology(*payloads: Any) -> dict[str, Any]:
    return _merge_service_topology_support(*payloads, build_service_topology=build_service_topology)


def _collect_storage_health(environment: str, config_map: dict[str, Any] | None = None) -> dict[str, Any]:
    return _collect_storage_health_support(environment, config_map=config_map)


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
        backtest_base_url=BACKTEST_BASE_URL,
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


def _process_tv_primary_event(payload: dict, *, api_received_at_ms: int | None = None):
    environment = resolve_data_environment((payload or {}).get("market_data_mode") or (payload or {}).get("data_environment") or (payload or {}).get("environment"))
    async_route = _parse_boolean(_config_value("tv_webhook_async_route_enabled", "TRUE", environment), True)
    runtime_wakeup = (
        _trigger_runtime_signal_wakeup
        if _parse_boolean(_config_value("tv_webhook_runtime_wakeup_enabled", "TRUE", environment), True)
        else None
    )
    return _process_tv_primary_event_support(
        pb,
        payload=payload,
        api_received_at_ms=api_received_at_ms,
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape_filter_string,
        build_signal_ingest_response=build_signal_ingest_response,
        config_value=_config_value,
        send_interactive=_feishu_send_interactive,
        update_interactive=_feishu_update_interactive,
        signal_chat_id_fn=_signal_chat_id,
        console_base_url=_console_base_url(),
        strategy_capacity_getter=_strategy_capacity_snapshot,
        runtime_wakeup=runtime_wakeup,
        async_route=async_route,
        spool_on_persist_failure=async_route,
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
        backtest_base_url=BACKTEST_BASE_URL,
        runtime_base_url=RUNTIME_BASE_URL,
        scheduler_base_url=SCHEDULER_BASE_URL,
        build_service_topology=build_service_topology,
        config=config,
        normalize_environment=_normalize_environment,
        scheduler_status=lambda environment, **kwargs: _scheduler_status(environment, **kwargs),
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
