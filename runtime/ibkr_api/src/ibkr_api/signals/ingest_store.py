from __future__ import annotations

import json
from typing import Any

from ibkr_api.orders.values import to_float, to_text
from ibkr_compute.observability.prometheus import record_signal_record_created


VOLATILE_COMPARE_KEYS = {
    "computed_at_ms": True,
    "computed_at_us": True,
    "computed_at_cn": True,
}


def _normalize_compare(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_compare(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}") or text.startswith("[") and text.endswith("]"):
            try:
                return _normalize_compare(json.loads(text))
            except Exception:
                pass
        if text.lower() == "true":
            return True
        if text.lower() == "false":
            return False
        parsed_number = to_float(text)
        if parsed_number is not None:
            if text.replace("-", "", 1).isdigit():
                return int(parsed_number)
            return parsed_number
        return value
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            if VOLATILE_COMPARE_KEYS.get(str(key), False):
                continue
            normalized[str(key)] = _normalize_compare(value[key])
        return normalized
    if isinstance(value, (int, float)) and value == value:
        return value
    return None if value is None else value


def _values_equal(left: Any, right: Any) -> bool:
    return json.dumps(_normalize_compare(left), sort_keys=True, ensure_ascii=True) == json.dumps(
        _normalize_compare(right),
        sort_keys=True,
        ensure_ascii=True,
    )


def _record_needs_update(record: dict[str, Any], next_payload: dict[str, Any]) -> bool:
    return any(not _values_equal(record.get(key), next_payload.get(key)) for key in next_payload)


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                parsed = json.loads(text)
                return dict(parsed) if isinstance(parsed, dict) else {}
            except Exception:
                return {}
    return {}


def _record_created_signal_metric(row: dict[str, Any]) -> None:
    extra = _as_object(row.get("extra"))
    try:
        record_signal_record_created(
            environment=to_text(row.get("environment")),
            signal_source=to_text(extra.get("signal_source") or extra.get("source") or row.get("source")),
            direction=to_text(row.get("direction")),
            initial_status=to_text(row.get("status") or "unknown"),
        )
    except Exception:
        pass


def upsert_signal_record(pb: Any, existing_row: dict[str, Any] | None, next_payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    existing = dict(existing_row or {})
    if existing.get("id"):
        if not _record_needs_update(existing, next_payload):
            return existing, "skipped"
        updated = pb.update_record("ibkr_signals", to_text(existing.get("id")), next_payload)
        return dict(updated) if isinstance(updated, dict) else {**existing, **next_payload}, "updated"
    created = pb.create_record("ibkr_signals", next_payload)
    created_row = dict(created) if isinstance(created, dict) else {**next_payload}
    _record_created_signal_metric(created_row)
    return created_row, "created"


__all__ = ["upsert_signal_record"]
