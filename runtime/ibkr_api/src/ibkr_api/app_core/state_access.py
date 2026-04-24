from __future__ import annotations

from typing import Any


def get_state_payload(pb, state_key: str, environment: str, *, as_dict, normalize_environment, date: str = "global") -> dict[str, Any]:
    runtime_environment = normalize_environment(environment)
    try:
        record = pb.get_state(state_key, runtime_environment, date=date)
    except Exception:
        record = None
    payload = as_dict((record or {}).get("data") if isinstance(record, dict) else {})
    record_date = str(((record or {}).get("date") if isinstance(record, dict) else "") or date).strip() or date
    return {
        "environment": runtime_environment,
        "date": record_date,
        "record": record if isinstance(record, dict) else {},
        "data": payload,
    }


def load_daily_scan_state(
    environment: str,
    *,
    as_dict,
    get_state_payload_fn,
    daily_scan_state_key: str,
) -> dict[str, Any]:
    payload = get_state_payload_fn(daily_scan_state_key, environment, date="global")
    data = as_dict(payload.get("data"))
    data["result"] = as_dict(data.get("result"))
    return data


def count_active_today_targets(pb, environment: str, market_date: str, *, normalize_environment, escape_filter_string) -> int:
    normalized_market_date = str(market_date or "").strip()
    if not normalized_market_date:
        return 0
    runtime_environment = normalize_environment(environment)
    target_filter = (
        f'date = "{escape_filter_string(normalized_market_date)}" && '
        f'environment = "{escape_filter_string(runtime_environment)}" && '
        '(status = "candidate" || status = "active")'
    )
    try:
        rows = pb.get_all_records("ibkr_targets", filter=target_filter, max_pages=25)
    except Exception:
        return 0
    return len(rows or [])
