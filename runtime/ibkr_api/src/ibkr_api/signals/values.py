from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, escape_filter_string, first_defined, to_text


SIGNALS_COLLECTION = "ibkr_signals"
SIGNAL_MUTABLE_STATUSES = {"pending", "awaiting_confirm"}


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


def get_signal_extra(record_or_data: Any) -> dict[str, Any]:
    value = record_value(record_or_data, "extra")
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
    return ensure_object(value)


def merge_signal_extra(record_or_data: Any, patch: dict[str, Any] | None) -> dict[str, Any]:
    return {
        **get_signal_extra(record_or_data),
        **ensure_object(patch),
    }


def signal_symbol(record_or_data: Any, fallback: Any = "") -> str:
    return to_text(record_value(record_or_data, "symbol") or fallback)


def signal_status(record_or_data: Any) -> str:
    return to_text(record_value(record_or_data, "status"))


def now_iso_utc(clock: Callable[[], Any] | None = None) -> str:
    if callable(clock):
        value = clock()
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return to_text(value)
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp_iso_utc(clock: Callable[[], Any] | None = None) -> str:
    return now_iso_utc(clock)


def build_signal_lookup_filter(
    signal_id: Any,
    environment: Any,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
) -> str:
    return (
        f'(id = "{escape_filter(signal_id)}" || signal_id = "{escape_filter(signal_id)}") && '
        f'environment = "{escape_filter(environment)}"'
    )


def load_signal_record(
    pb: Any,
    signal_id: Any,
    environment: Any,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
) -> Any:
    return pb.get_first_record(
        SIGNALS_COLLECTION,
        filter=build_signal_lookup_filter(signal_id, environment, escape_filter=escape_filter),
    )


def load_signal_by_identifier(
    pb: Any,
    *,
    signal_id: Any,
    environment: Any,
    escape_filter_string: Callable[[Any], str],
) -> dict[str, Any] | None:
    try:
        row = load_signal_record(
            pb,
            signal_id,
            environment,
            escape_filter=escape_filter_string,
        )
    except Exception:
        return None
    return dict(row) if isinstance(row, dict) and row.get("id") else None


def normalize_signal_row(signal_row: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(signal_row or {})
    extra = get_signal_extra(row)
    signal_id = to_text(first_defined(row.get("signal_id"), extra.get("signal_id"), row.get("id")))
    return {
        "id": to_text(first_defined(row.get("id"), signal_id)),
        "signal_id": signal_id,
        "status": to_text(first_defined(row.get("status"), extra.get("status"))),
        "symbol": to_text(first_defined(row.get("symbol"), extra.get("symbol"), signal_id)),
        "environment": to_text(first_defined(row.get("environment"), extra.get("environment"), "live")) or "live",
        "extra": extra,
    }


def build_signal_update_patch(
    signal_row: dict[str, Any] | None,
    *,
    status: Any,
    note: Any,
    extra_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": to_text(status),
        "note": to_text(note),
        "extra": merge_signal_extra(signal_row, extra_patch),
    }
