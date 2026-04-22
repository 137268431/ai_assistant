from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ibkr_api.orders.values import ensure_object, first_defined, to_int


ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")


def format_timestamp_ms(timestamp_ms: Any) -> dict[str, Any]:
    bar_time_ms = to_int(timestamp_ms, int(datetime.now(tz=timezone.utc).timestamp() * 1000))
    utc_dt = datetime.fromtimestamp(bar_time_ms / 1000.0, tz=timezone.utc)
    return {
        "us_time": utc_dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S"),
        "cn_time": utc_dt.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S"),
        "bar_time_ms": bar_time_ms,
    }


def resolve_order_event_times(options: dict[str, Any] | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    opts = options or {}
    fallback_data = fallback or {}
    raw_bar_time_ms = first_defined(
        opts.get("bar_time_ms"),
        opts.get("barTimeMs"),
        fallback_data.get("bar_time_ms"),
        fallback_data.get("barTimeMs"),
    )
    normalized = format_timestamp_ms(raw_bar_time_ms)
    return {
        "us_time": first_defined(
            opts.get("us_time"),
            opts.get("usTime"),
            fallback_data.get("us_time"),
            fallback_data.get("usTime"),
            normalized["us_time"],
        )
        or normalized["us_time"],
        "cn_time": first_defined(
            opts.get("cn_time"),
            opts.get("cnTime"),
            fallback_data.get("cn_time"),
            fallback_data.get("cnTime"),
            normalized["cn_time"],
        )
        or normalized["cn_time"],
        "bar_time_ms": normalized["bar_time_ms"],
    }


def resolve_order_status_event_times(existing_row: dict[str, Any] | None, options: dict[str, Any]) -> dict[str, Any]:
    existing = existing_row or {}
    extra = ensure_object(existing.get("extra"))
    candidate = resolve_order_event_times(options, extra)
    previous_status = str(first_defined(options.get("previous_status"), existing.get("status"), extra.get("current_status")) or "")
    current_status = str(first_defined(options.get("status"), previous_status) or "")
    has_explicit_time_input = any(
        key in options
        for key in ("us_time", "usTime", "cn_time", "cnTime", "bar_time_ms", "barTimeMs")
    )
    current_us_time = str(first_defined(existing.get("us_time"), extra.get("us_time")) or "")
    current_cn_time = str(first_defined(existing.get("cn_time"), extra.get("cn_time")) or "")
    current_bar_time_ms = to_int(first_defined(existing.get("bar_time_ms"), extra.get("bar_time_ms")), 0)
    unchanged = (
        str(candidate["us_time"]) == current_us_time
        and str(candidate["cn_time"]) == current_cn_time
        and to_int(candidate["bar_time_ms"], 0) == current_bar_time_ms
    )
    if previous_status and current_status and previous_status != current_status and (not has_explicit_time_input or unchanged):
        return format_timestamp_ms(None)
    return candidate
