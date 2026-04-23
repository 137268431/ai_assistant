from __future__ import annotations

import json
import time
from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, first_defined, parse_boolean, to_text

WATCHLIST_ROLE_TRADE = "trade"
WATCHLIST_ROLE_MARKET_MONITOR = "market_monitor"
VALID_WATCHLIST_ROLES = {WATCHLIST_ROLE_TRADE, WATCHLIST_ROLE_MARKET_MONITOR}

RequestJsonRequest = Callable[..., dict[str, Any]]
TimeStrings = Callable[[], dict[str, str]]
NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]


def parse_json_object(value: Any) -> dict[str, Any]:
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


def normalize_watchlist_role(value: Any, *, default: str = WATCHLIST_ROLE_TRADE) -> str:
    normalized = to_text(value).lower()
    if normalized in VALID_WATCHLIST_ROLES:
        return normalized
    return default


def normalize_record_environment(value: Any, *, runtime_environment: str) -> str:
    normalized = to_text(value).lower()
    if normalized in {"global", runtime_environment}:
        return normalized
    if normalized in {"", "all", "default"}:
        return runtime_environment
    return normalized or runtime_environment


def normalize_direction_bias(value: Any, *, default: str = "neutral") -> str:
    normalized = to_text(value).lower()
    return normalized or default


def normalize_target_status(value: Any, *, default: str = "candidate") -> str:
    normalized = to_text(value).lower()
    return normalized or default


def get_runtime_market_date(
    environment: str,
    *,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
    time_strings: TimeStrings,
) -> str:
    fallback_date = to_text((time_strings() or {}).get("date"))
    result = request_json_request(
        "GET",
        compute_base_url,
        "/ibkr/status",
        params=[("environment", environment)],
        timeout=8.0,
    )
    payload = ensure_object(result.get("payload"))
    market_universe = ensure_object(payload.get("market_universe"))
    runtime_date = to_text(first_defined(market_universe.get("market_date"), payload.get("market_date")))
    return runtime_date or fallback_date


def call_universe_reconcile(
    environment: str,
    payload: dict[str, Any],
    *,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
) -> dict[str, Any]:
    request_body = {
        **(dict(payload or {}) if isinstance(payload, dict) else {}),
        "environment": environment,
    }
    result = request_json_request(
        "POST",
        compute_base_url,
        "/ibkr/universe/reconcile",
        json_body=request_body,
        timeout=180.0,
    )
    return {
        "statusCode": int(result.get("status_code") or 200),
        "payload": ensure_object(result.get("payload")),
        "upstream": to_text(result.get("target_url")) or f"{compute_base_url.rstrip('/')}/ibkr/universe/reconcile",
    }


def build_record_filter(record_id: str, *, escape_filter_string: EscapeFilterString) -> str:
    return f'id = "{escape_filter_string(record_id)}"'


def find_record_by_id_or_filter(
    pb: Any,
    collection: str,
    *,
    record_id: str = "",
    filter_expr: str = "",
    escape_filter_string: EscapeFilterString,
) -> dict[str, Any] | None:
    if record_id:
        try:
            row = pb.get_first_record(collection, filter=build_record_filter(record_id, escape_filter_string=escape_filter_string))
            if row:
                return dict(row)
        except Exception:
            pass
    if filter_expr:
        try:
            row = pb.get_first_record(collection, filter=filter_expr)
            if row:
                return dict(row)
        except Exception:
            pass
    return None


def record_needs_update(existing: dict[str, Any] | None, data: dict[str, Any], *, compare_fields: list[str] | None = None) -> bool:
    row = dict(existing or {})
    fields = compare_fields or sorted(data.keys())
    for field in fields:
        next_value = data.get(field)
        current_value = row.get(field)
        if isinstance(next_value, dict):
            if ensure_object(current_value) != dict(next_value):
                return True
            continue
        if isinstance(next_value, list):
            if list(current_value or []) != list(next_value):
                return True
            continue
        if current_value != next_value:
            return True
    return False


def upsert_record(
    pb: Any,
    collection: str,
    *,
    filter_expr: str,
    data: dict[str, Any],
    compare_fields: list[str] | None = None,
) -> dict[str, Any]:
    existing = None
    try:
        existing = pb.get_first_record(collection, filter=filter_expr)
    except Exception:
        existing = None

    if existing and existing.get("id"):
        existing_row = dict(existing)
        if compare_fields is not None and not record_needs_update(existing_row, data, compare_fields=compare_fields):
            return {"action": "skipped", "record": existing_row}
        return {
            "action": "updated",
            "record": pb.update_record(collection, to_text(existing_row.get("id")), data),
        }

    return {
        "action": "created",
        "record": pb.create_record(collection, data),
    }


def delete_collection_record(pb: Any, record: dict[str, Any] | None) -> bool:
    record_id = to_text((record or {}).get("id"))
    if not record_id:
        return False
    collection = to_text((record or {}).get("collectionName") or (record or {}).get("collection"))
    if not collection:
        return False
    pb.delete_record(collection, record_id)
    return True


def delete_record_by_id(pb: Any, collection: str, record_id: str) -> bool:
    normalized_id = to_text(record_id)
    if not normalized_id:
        return False
    return bool(pb.delete_record(collection, normalized_id))


def find_watchlist_record_for_symbol(
    pb: Any,
    symbol: str,
    environment: str,
    *,
    escape_filter_string: EscapeFilterString,
) -> dict[str, Any] | None:
    normalized_symbol = to_text(symbol).upper()
    normalized_environment = to_text(environment).lower() or "live"
    if not normalized_symbol:
        return None
    filter_expr = (
        f'symbol = "{escape_filter_string(normalized_symbol)}" && '
        f'environment = "{escape_filter_string(normalized_environment)}"'
    )
    try:
        row = pb.get_first_record("watchlist", filter=filter_expr)
        return dict(row) if row else None
    except Exception:
        return None


def list_active_today_targets(
    pb: Any,
    symbol: str,
    environment: str,
    market_date: str,
    *,
    escape_filter_string: EscapeFilterString,
) -> list[dict[str, Any]]:
    normalized_symbol = to_text(symbol).upper()
    normalized_environment = to_text(environment).lower() or "live"
    normalized_date = to_text(market_date)
    if not normalized_symbol or not normalized_date:
        return []
    filter_expr = (
        f'symbol = "{escape_filter_string(normalized_symbol)}" && '
        f'date = "{escape_filter_string(normalized_date)}" && '
        f'environment = "{escape_filter_string(normalized_environment)}" && '
        '(status = "candidate" || status = "active")'
    )
    try:
        rows = pb.get_records("ibkr_targets", filter=filter_expr, sort="-updated", per_page=200, page=1)
    except Exception:
        return []
    return [dict(row) for row in rows or []]


def has_effective_watchlist_member(
    pb: Any,
    symbol: str,
    environment: str,
    *,
    escape_filter_string: EscapeFilterString,
) -> bool:
    normalized_symbol = to_text(symbol).upper()
    normalized_environment = to_text(environment).lower() or "live"
    if not normalized_symbol:
        return False
    filter_expr = (
        f'symbol = "{escape_filter_string(normalized_symbol)}" && '
        f'(environment = "{escape_filter_string(normalized_environment)}" || environment = "global" || environment = "")'
    )
    try:
        rows = pb.get_records("watchlist", filter=filter_expr, sort="-updated", per_page=20, page=1)
    except Exception:
        return False
    return bool(rows)


def ensure_target_watchlist_record(
    pb: Any,
    *,
    symbol: str,
    environment: str,
    exchange: Any,
    industry: Any,
    escape_filter_string: EscapeFilterString,
    time_strings: TimeStrings,
) -> dict[str, Any]:
    times = time_strings() or {}
    normalized_symbol = to_text(symbol).upper()
    normalized_environment = to_text(environment).lower() or "live"
    if not normalized_symbol:
        return {"action": "skipped", "manual_member": False}

    filter_expr = (
        f'symbol = "{escape_filter_string(normalized_symbol)}" && '
        f'environment = "{escape_filter_string(normalized_environment)}"'
    )
    existing = None
    try:
        existing = pb.get_first_record("watchlist", filter=filter_expr)
    except Exception:
        existing = None
    existing_row = dict(existing or {})
    preserved_manual_member = parse_boolean(existing_row.get("manual_member"), False)
    payload = {
        "symbol": normalized_symbol,
        "environment": normalized_environment,
        "exchange": to_text(first_defined(exchange, existing_row.get("exchange"), "SMART")).upper(),
        "industry": to_text(first_defined(industry, existing_row.get("industry"))),
        "note": to_text(existing_row.get("note")),
        "symbol_role": normalize_watchlist_role(existing_row.get("symbol_role")),
        "manual_member": preserved_manual_member,
        "created_us": to_text(first_defined(existing_row.get("created_us"), times.get("us"))),
        "created_cn": to_text(first_defined(existing_row.get("created_cn"), times.get("cn"))),
        "updated_us": to_text(times.get("us")),
        "updated_cn": to_text(times.get("cn")),
        "us_time": to_text(times.get("us")),
        "cn_time": to_text(times.get("cn")),
        "bar_time_ms": int(time.time() * 1000),
    }
    result = upsert_record(
        pb,
        "watchlist",
        filter_expr=filter_expr,
        data=payload,
    )
    return {
        "action": result.get("action") or "updated",
        "id": to_text(ensure_object(result.get("record")).get("id")),
        "manual_member": preserved_manual_member,
    }


def remove_auto_watchlist_record_if_eligible(
    pb: Any,
    symbol: str,
    environment: str,
    market_date: str,
    *,
    escape_filter_string: EscapeFilterString,
) -> dict[str, Any]:
    remaining_targets = list_active_today_targets(
        pb,
        symbol,
        environment,
        market_date,
        escape_filter_string=escape_filter_string,
    )
    if remaining_targets:
        return {"removed": False, "reason": "target_still_active"}

    record = find_watchlist_record_for_symbol(
        pb,
        symbol,
        environment,
        escape_filter_string=escape_filter_string,
    )
    if not record:
        return {"removed": False, "reason": "watchlist_missing"}
    if parse_boolean(record.get("manual_member"), True):
        return {"removed": False, "reason": "manual_watchlist_retained", "id": to_text(record.get("id"))}

    delete_record_by_id(pb, "watchlist", to_text(record.get("id")))
    return {"removed": True, "reason": "auto_target_watchlist_removed", "id": to_text(record.get("id"))}


__all__ = [
    "WATCHLIST_ROLE_MARKET_MONITOR",
    "WATCHLIST_ROLE_TRADE",
    "call_universe_reconcile",
    "ensure_target_watchlist_record",
    "find_record_by_id_or_filter",
    "find_watchlist_record_for_symbol",
    "get_runtime_market_date",
    "has_effective_watchlist_member",
    "list_active_today_targets",
    "normalize_direction_bias",
    "normalize_record_environment",
    "normalize_target_status",
    "normalize_watchlist_role",
    "parse_json_object",
    "remove_auto_watchlist_record_if_eligible",
    "upsert_record",
]
