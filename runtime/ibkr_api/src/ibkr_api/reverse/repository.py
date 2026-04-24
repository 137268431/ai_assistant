from __future__ import annotations

from typing import Any, Callable

from ibkr_api.reverse.normalize import normalize_reverse_record
from ibkr_api.reverse.shared import (
    DEFAULT_REVERSE_PRIORITY,
    LIVE_ENVIRONMENT,
    REVERSE_DEDUPE_LOOKBACK_LIMIT,
    REVERSE_SIGNALS_COLLECTION,
    ensure_object,
    escape_filter_string,
    first_non_empty,
    get_reverse_extra,
    merge_record_patch,
    normalize_environment_value,
    parse_integer,
    parse_triggered_signals,
    record_value,
    to_float,
    to_int,
    to_text,
)


def _to_number(value: Any, fallback: float = 0.0) -> float:
    parsed = to_float(value)
    return fallback if parsed is None else parsed


def fetch_reverse_record(
    pb: Any,
    reverse_id: str,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
) -> Any:
    return pb.get_first_record(
        REVERSE_SIGNALS_COLLECTION,
        filter=f'id = "{escape_filter(reverse_id)}"',
    )


def build_reverse_duplicate_criteria(payload: dict[str, Any] | None) -> dict[str, str]:
    data = payload or {}
    extra = ensure_object(data.get("extra"))
    source = to_text(data.get("source") or extra.get("source") or "indicator")
    return {
        "environment": normalize_environment_value(data.get("environment") or extra.get("environment"), LIVE_ENVIRONMENT),
        "symbol": to_text(data.get("symbol")).upper(),
        "direction": to_text(data.get("direction")).lower(),
        "reverse_kind": to_text(first_non_empty(extra.get("reverse_kind"), "signal_conflict" if source == "signal" else "indicator_conflict")),
        "target_state": to_text(first_non_empty(extra.get("target_state"), "")),
        "action_type": to_text(data.get("action_type") or extra.get("action_type")),
        "trade_group_id": to_text(first_non_empty(extra.get("trade_group_id"), "")),
        "origin_signal_id": to_text(first_non_empty(extra.get("origin_signal_id"), extra.get("signal_id_orig"), "")),
        "new_direction": to_text(first_non_empty(extra.get("new_direction"), "")),
    }


def find_pending_reverse_duplicate(
    pb: Any,
    criteria: dict[str, Any] | None,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
    per_page: int = REVERSE_DEDUPE_LOOKBACK_LIMIT,
) -> Any:
    lookup = dict(criteria or {})
    symbol = to_text(lookup.get("symbol")).upper()
    if not symbol:
        return None
    environment = normalize_environment_value(lookup.get("environment"), LIVE_ENVIRONMENT)
    records = list(
        pb.get_records(
            REVERSE_SIGNALS_COLLECTION,
            filter=(
                f'symbol = "{escape_filter(symbol)}" && '
                f'environment = "{escape_filter(environment)}" && '
                'status = "pending"'
            ),
            sort="-created",
            per_page=per_page,
            page=1,
        )
        or []
    )
    for record in records:
        normalized = normalize_reverse_record(record, default_environment=environment, parse_triggered_signals=parse_triggered_signals)
        if lookup.get("direction") and normalized.get("direction") != lookup.get("direction"):
            continue
        if to_text(lookup.get("reverse_kind")) != to_text(normalized.get("reverse_kind")):
            continue
        if to_text(lookup.get("target_state")) != to_text(normalized.get("target_state")):
            continue
        if to_text(lookup.get("action_type")) != to_text(normalized.get("action_type")):
            continue
        if to_text(lookup.get("trade_group_id")) != to_text(normalized.get("trade_group_id")):
            continue
        if to_text(lookup.get("origin_signal_id")) != to_text(normalized.get("origin_signal_id")):
            continue
        if to_text(lookup.get("new_direction")) != to_text(normalized.get("new_direction")):
            continue
        return record
    return None


def upsert_reverse_record(
    pb: Any,
    payload: dict[str, Any],
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
) -> dict[str, Any]:
    data = dict(payload or {})
    extra = ensure_object(data.get("extra"))
    criteria = build_reverse_duplicate_criteria(data)
    existing = None if data.get("dedupe") is False else find_pending_reverse_duplicate(pb, criteria, escape_filter=escape_filter)
    existing_extra = get_reverse_extra(existing)
    triggered_signals = parse_triggered_signals(
        data.get("triggered_signals") if data.get("triggered_signals") is not None else extra.get("triggered_signals")
    )
    record_payload = {
        "symbol": criteria["symbol"],
        "environment": criteria["environment"],
        "direction": criteria["direction"],
        "source": to_text(data.get("source") or "indicator") or "indicator",
        "priority": parse_integer(data.get("priority"), default=DEFAULT_REVERSE_PRIORITY, minimum=0),
        "strength": to_text(data.get("strength") or "weak") or "weak",
        "score": _to_number(data.get("score"), 0),
        "triggered_signals": triggered_signals,
        "action_type": criteria["action_type"] or "cancel",
        "status": to_text(data.get("status") or "pending") or "pending",
        "reason": to_text(data.get("reason")),
        "bar_time_ms": to_int(data.get("bar_time_ms"), 0),
        "us_time": to_text(data.get("us_time")),
        "cn_time": to_text(data.get("cn_time")),
        "extra": {
            **existing_extra,
            **extra,
            "environment": criteria["environment"],
            "reverse_kind": criteria["reverse_kind"],
            "target_state": criteria["target_state"],
            "triggered_signals": triggered_signals,
        },
    }

    if existing:
        record_id = to_text(record_value(existing, "id"))
        updated = pb.update_record(REVERSE_SIGNALS_COLLECTION, record_id, record_payload)
        saved = updated if updated is not None else merge_record_patch(existing, record_payload)
        return {"record": saved, "created": False}

    created = pb.create_record(REVERSE_SIGNALS_COLLECTION, record_payload)
    return {"record": created, "created": True}


__all__ = [
    "build_reverse_duplicate_criteria",
    "fetch_reverse_record",
    "find_pending_reverse_duplicate",
    "upsert_reverse_record",
]
