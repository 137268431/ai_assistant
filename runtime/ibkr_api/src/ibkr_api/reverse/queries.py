from __future__ import annotations

from typing import Any, Callable

from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.reverse.common import (
    DEFAULT_REVERSE_LIST_LIMIT,
    REVERSE_SIGNALS_COLLECTION,
    build_date_range,
    clamp_reverse_limit,
    escape_filter_string,
    is_tradingview_reverse_source,
    normalize_environment_value,
    normalize_reverse_record,
    normalize_status_filters,
)


def _load_reverse_records(
    pb: Any,
    *,
    environment: str,
    date_str: str,
    per_page: int,
    escape_filter: Callable[[Any], str],
) -> list[Any]:
    runtime_environment = normalize_environment_value(environment)
    date_range = build_date_range(date_str)
    records: list[Any] = []
    if date_range:
        records = pb.get_records(
            REVERSE_SIGNALS_COLLECTION,
            filter=(
                f'environment = "{escape_filter(runtime_environment)}" && '
                f'source = "{escape_filter("tradingview")}" && '
                f'bar_time_ms >= {date_range["start_ms"]} && '
                f'bar_time_ms <= {date_range["end_ms"]}'
            ),
            sort="-created",
            per_page=per_page,
            page=1,
        ) or []
    if records:
        return list(records)
    return list(
        pb.get_records(
            REVERSE_SIGNALS_COLLECTION,
            filter=(
                f'environment = "{escape_filter(runtime_environment)}" && '
                f'source = "{escape_filter("tradingview")}"'
            ),
            sort="-created",
            per_page=per_page,
            page=1,
        )
        or []
    )


def build_reverse_list_response(
    pb: Any,
    *,
    environment: str,
    data_environment: str | None = None,
    date_str: str = "",
    symbol: str = "",
    statuses: Any = None,
    limit: Any = DEFAULT_REVERSE_LIST_LIMIT,
    normalize_environment: Callable[[Any, str], str] = normalize_environment_value,
    escape_filter: Callable[[Any], str] = escape_filter_string,
) -> tuple[dict[str, Any], int]:
    runtime_environment = request_broker_mode({"broker_mode": environment})
    data_environment = request_market_data_mode({"data_environment": data_environment})
    normalized_symbol = str(symbol or "").strip().upper()
    allowed_statuses = set(normalize_status_filters(statuses))
    per_page = clamp_reverse_limit(limit)

    try:
        records = _load_reverse_records(
            pb,
            environment=runtime_environment,
            date_str=str(date_str or "").strip(),
            per_page=per_page,
            escape_filter=escape_filter,
        )
        signals = [
            normalize_reverse_record(record, default_environment=runtime_environment)
            for record in records
            if is_tradingview_reverse_source(record)
        ]
        if normalized_symbol:
            signals = [signal for signal in signals if signal.get("symbol") == normalized_symbol]
        if allowed_statuses:
            signals = [signal for signal in signals if signal.get("status") in allowed_statuses]
        return {
            "ibkr_signals": signals,
            "broker_mode": runtime_environment,
            "data_environment": data_environment,
        }, 200
    except Exception as exc:
        return {"error": str(exc)}, 500


def build_reverse_pending_response(
    pb: Any,
    *,
    environment: str,
    data_environment: str | None = None,
    limit: Any = DEFAULT_REVERSE_LIST_LIMIT,
    normalize_environment: Callable[[Any, str], str] = normalize_environment_value,
    escape_filter: Callable[[Any], str] = escape_filter_string,
) -> tuple[dict[str, Any], int]:
    runtime_environment = request_broker_mode({"broker_mode": environment})
    data_environment = request_market_data_mode({"data_environment": data_environment})
    per_page = clamp_reverse_limit(limit)

    try:
        records = pb.get_records(
            REVERSE_SIGNALS_COLLECTION,
            filter=(
                'status = "pending" && '
                f'environment = "{escape_filter(runtime_environment)}" && '
                f'source = "{escape_filter("tradingview")}"'
            ),
            sort="-priority,-bar_time_ms",
            per_page=per_page,
            page=1,
        ) or []
        return {
            "ibkr_signals": [
                normalize_reverse_record(record, default_environment=runtime_environment)
                for record in records
                if is_tradingview_reverse_source(record)
            ],
            "broker_mode": runtime_environment,
            "data_environment": data_environment,
        }, 200
    except Exception as exc:
        return {"error": str(exc)}, 500


__all__ = [
    "build_reverse_list_response",
    "build_reverse_pending_response",
]
