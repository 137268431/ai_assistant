from __future__ import annotations

import json
from typing import Any

from ibkr_compute.core.broker_mode import resolve_data_environment


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
    payload = get_state_payload_fn(daily_scan_state_key, resolve_data_environment(environment), date="global")
    data = as_dict(payload.get("data"))
    data["result"] = as_dict(data.get("result"))
    return data


def _as_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "active", "passed", "pass"}


def _row_is_effective_daily_scan_active(row: dict[str, Any] | None) -> bool:
    if str((row or {}).get("status") or "").strip().lower() != "active":
        return False
    extra = _as_json_object((row or {}).get("extra"))
    source = str(extra.get("source") or "").strip().lower()
    if source == "daily_scan":
        return _truthy(extra.get("active_gate_passed"))
    return source in {"tradingview", "tv", "tv_webhook", "webhook_tv"}


def count_active_today_targets(pb, environment: str, market_date: str, *, normalize_environment, escape_filter_string) -> int:
    normalized_market_date = str(market_date or "").strip()
    if not normalized_market_date:
        return 0
    runtime_environment = resolve_data_environment(normalize_environment(environment))
    target_filter = (
        f'date = "{escape_filter_string(normalized_market_date)}" && '
        f'environment = "{escape_filter_string(runtime_environment)}" && '
        '(status = "candidate" || status = "active")'
    )
    try:
        rows = pb.get_all_records("ibkr_targets", filter=target_filter, max_pages=25)
    except Exception:
        return 0
    return sum(1 for row in rows or [] if _row_is_effective_daily_scan_active(row if isinstance(row, dict) else {}))
