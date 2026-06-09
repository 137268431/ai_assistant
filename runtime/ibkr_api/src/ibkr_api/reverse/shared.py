from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ibkr_api.orders.values import escape_filter_string, parse_boolean, parse_integer, to_float, to_int, to_text


LIVE_ENVIRONMENT = "live"
ET = ZoneInfo("America/New_York")
REVERSE_SIGNALS_COLLECTION = "ibkr_reverse_signals"
INDICATORS_COLLECTION = "ibkr_indicators"
ORDERS_COLLECTION = "orders"
DEFAULT_REVERSE_LIST_LIMIT = 200
MAX_REVERSE_LIST_LIMIT = 500
DEFAULT_REVERSE_PRIORITY = 5
DEFAULT_REVERSE_THRESHOLD = 6
ACTIVE_ENTRY_ORDER_LIMIT = 20
REVERSE_DEDUPE_LOOKBACK_LIMIT = 50
ALLOWED_REVERSE_ACTION_TYPES = ("cancel", "close", "adjust_sl", "adjust_tp")
TRADINGVIEW_REVERSE_SOURCES = {"tradingview", "tv", "tv_webhook", "webhook_tv"}


def normalize_environment_value(value: Any, default: str = LIVE_ENVIRONMENT) -> str:
    normalized = to_text(value).lower()
    return normalized or default


def ensure_object(value: Any) -> dict[str, Any]:
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


def record_value(record_or_data: Any, field_name: str, default: Any = None) -> Any:
    if record_or_data is None:
        return default
    if isinstance(record_or_data, dict):
        return record_or_data.get(field_name, default)
    getter = getattr(record_or_data, "get", None)
    if callable(getter):
        value = getter(field_name)
        return default if value is None else value
    return getattr(record_or_data, field_name, default)


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def get_reverse_extra(record_or_data: Any) -> dict[str, Any]:
    return ensure_object(record_value(record_or_data, "extra"))


def is_tradingview_reverse_source(record_or_data: Any) -> bool:
    extra = get_reverse_extra(record_or_data)
    source = to_text(record_value(record_or_data, "source") or extra.get("source")).lower()
    return source in TRADINGVIEW_REVERSE_SOURCES


def merge_reverse_extra(record_or_data: Any, patch: dict[str, Any] | None) -> dict[str, Any]:
    return {
        **get_reverse_extra(record_or_data),
        **ensure_object(patch),
    }


def normalize_status_filters(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value).split(",")
    normalized: list[str] = []
    for item in items:
        status = to_text(item)
        if status:
            normalized.append(status)
    return normalized


def parse_triggered_signals(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [to_text(item) for item in value if to_text(item)]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [to_text(item) for item in parsed if to_text(item)]
        return [to_text(item) for item in text.split(",") if to_text(item)]
    return []


def clamp_reverse_limit(value: Any, default: int = DEFAULT_REVERSE_LIST_LIMIT) -> int:
    return parse_integer(value, default=default, minimum=1, maximum=MAX_REVERSE_LIST_LIMIT)


def build_date_range(date_text: Any) -> dict[str, int] | None:
    text = to_text(date_text)
    if not text:
        return None
    try:
        start = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=ET)
    except ValueError:
        return None
    end = start + timedelta(days=1)
    return {
        "start_ms": int(start.timestamp() * 1000),
        "end_ms": int(end.timestamp() * 1000),
    }


def resolve_timestamp_text(clock: Callable[[], Any] | None = None) -> str:
    if callable(clock):
        value = clock()
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return to_text(value)
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def merge_record_patch(record_or_data: Any, patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(record_or_data) if isinstance(record_or_data, dict) else {}
    if not merged and record_or_data is not None:
        for field in (
            "id",
            "symbol",
            "direction",
            "strength",
            "score",
            "triggered_signals",
            "action_type",
            "status",
            "reason",
            "extra",
            "processed_time",
            "bar_time_ms",
            "us_time",
            "cn_time",
            "source",
            "priority",
            "environment",
            "created",
            "updated",
        ):
            value = record_value(record_or_data, field)
            if value is not None:
                merged[field] = value
    merged.update(patch)
    return merged


__all__ = [
    "ACTIVE_ENTRY_ORDER_LIMIT",
    "ALLOWED_REVERSE_ACTION_TYPES",
    "DEFAULT_REVERSE_LIST_LIMIT",
    "DEFAULT_REVERSE_PRIORITY",
    "DEFAULT_REVERSE_THRESHOLD",
    "INDICATORS_COLLECTION",
    "LIVE_ENVIRONMENT",
    "MAX_REVERSE_LIST_LIMIT",
    "ORDERS_COLLECTION",
    "REVERSE_DEDUPE_LOOKBACK_LIMIT",
    "REVERSE_SIGNALS_COLLECTION",
    "TRADINGVIEW_REVERSE_SOURCES",
    "build_date_range",
    "clamp_reverse_limit",
    "ensure_object",
    "escape_filter_string",
    "first_non_empty",
    "get_reverse_extra",
    "is_tradingview_reverse_source",
    "merge_record_patch",
    "merge_reverse_extra",
    "normalize_environment_value",
    "normalize_status_filters",
    "parse_boolean",
    "parse_integer",
    "parse_triggered_signals",
    "record_value",
    "resolve_timestamp_text",
    "to_float",
    "to_int",
    "to_text",
]
