from __future__ import annotations

from typing import Any, Callable

from ibkr_api.home.common import escape_filter, load_records, to_text
from ibkr_api.home.dashboard import (
    SIGNAL_STATUS_COUNT_BUCKETS,
    _build_market_time_filter,
    _market_date,
    _signal_effective_status,
)
from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_compute.observability.prometheus import set_current_signal_count_metrics


TimeStrings = Callable[[], dict[str, str]]

TERMINAL_SIGNAL_STATUSES = {
    "closed",
    "expired",
    "rejected",
    "cancelled",
    "canceled",
    "entry_missed",
    "entry_missed_limit_cap",
}


def _direction(value: Any) -> str:
    text = to_text(value).lower()
    if text in {"long", "buy"}:
        return "long"
    if text in {"short", "sell"}:
        return "short"
    return ""


def summarize_current_signal_counts(rows: list[dict[str, Any]], *, broker_mode: str) -> dict[str, int]:
    counts = {"long": 0, "short": 0}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        status = _signal_effective_status(row, broker_mode)
        bucket = SIGNAL_STATUS_COUNT_BUCKETS.get(status, status)
        if bucket in TERMINAL_SIGNAL_STATUSES:
            continue
        direction = _direction(row.get("direction"))
        if direction in counts:
            counts[direction] += 1
    return counts


def publish_current_signal_metrics(
    pb: Any,
    *,
    payload: dict[str, Any] | None = None,
    time_strings: TimeStrings,
) -> dict[str, int]:
    request_payload = payload if isinstance(payload, dict) else {}
    broker_mode = request_broker_mode(request_payload)
    data_environment = request_market_data_mode(request_payload)
    market_date, start_ms, end_ms = _market_date(request_payload, time_strings)
    data_filter = escape_filter(data_environment)
    today_data_filter = _build_market_time_filter(data_filter, market_date, start_ms, end_ms)
    rows = load_records(pb, "ibkr_signals", filter_expr=today_data_filter, sort="-created", per_page=500, max_pages=20)
    counts = summarize_current_signal_counts(rows, broker_mode=broker_mode)
    set_current_signal_count_metrics(counts, environment=data_environment, state="active", service_name="ibkr-api")
    return counts


__all__ = ["publish_current_signal_metrics", "summarize_current_signal_counts"]
