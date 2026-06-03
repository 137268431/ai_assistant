from __future__ import annotations

from typing import Any, Callable

from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode, resolve_data_environment

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


def _key_text(value: Any) -> str:
    return to_text(value).lower().replace("-", "_")


def _sequence_number(value: Any) -> int:
    try:
        if value in (None, ""):
            return 0
        return int(float(value))
    except Exception:
        return 0


def _is_tv_risk_update_criteria(criteria: dict[str, Any]) -> bool:
    return (
        to_text(criteria.get("action_type")) == "adjust_bracket"
        and to_text(criteria.get("reverse_kind")) == "tv_risk_update"
    )


def _record_risk_update_type(record: Any) -> str:
    extra = get_reverse_extra(record)
    return _key_text(first_non_empty(record_value(record, "risk_update_type"), extra.get("risk_update_type"), ""))


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
    broker_mode = normalize_broker_mode(
        data.get("broker_mode") or data.get("environment") or extra.get("broker_mode") or extra.get("environment"),
        configured_broker_mode(),
    )
    return {
        "environment": broker_mode,
        "broker_mode": broker_mode,
        "symbol": to_text(data.get("symbol")).upper(),
        "direction": to_text(data.get("direction")).lower(),
        "reverse_kind": to_text(first_non_empty(extra.get("reverse_kind"), "signal_conflict" if source == "signal" else "indicator_conflict")),
        "target_state": to_text(first_non_empty(extra.get("target_state"), "")),
        "action_type": to_text(data.get("action_type") or extra.get("action_type")),
        "trade_group_id": to_text(first_non_empty(extra.get("trade_group_id"), "")),
        "origin_signal_id": to_text(first_non_empty(extra.get("origin_signal_id"), extra.get("signal_id_orig"), "")),
        "new_direction": to_text(first_non_empty(extra.get("new_direction"), "")),
        "risk_update_type": _key_text(first_non_empty(data.get("risk_update_type"), extra.get("risk_update_type"), "")),
    }


def find_pending_reverse_duplicate(
    pb: Any,
    criteria: dict[str, Any] | None,
    *,
    escape_filter: Callable[[Any], str] = escape_filter_string,
    per_page: int = REVERSE_DEDUPE_LOOKBACK_LIMIT,
) -> Any:
    lookup = dict(criteria or {})
    environment = normalize_broker_mode(lookup.get("environment"), configured_broker_mode())
    symbol = to_text(lookup.get("symbol")).upper()
    action_type = to_text(lookup.get("action_type"))
    risk_update_lookup = _is_tv_risk_update_criteria(lookup)
    if risk_update_lookup:
        if not (to_text(lookup.get("origin_signal_id")) or to_text(lookup.get("trade_group_id"))):
            return None
        if not _key_text(lookup.get("risk_update_type")):
            return None
    elif not symbol:
        return None

    filter_parts = [
        f'environment = "{escape_filter(environment)}"',
        'status = "pending"',
    ]
    if not risk_update_lookup:
        filter_parts.insert(0, f'symbol = "{escape_filter(symbol)}"')
    if action_type:
        filter_parts.append(f'action_type = "{escape_filter(action_type)}"')
    records = list(
        pb.get_records(
            REVERSE_SIGNALS_COLLECTION,
            filter=" && ".join(filter_parts),
            sort="-created",
            per_page=per_page,
            page=1,
        )
        or []
    )
    for record in records:
        normalized = normalize_reverse_record(record, default_environment=environment, parse_triggered_signals=parse_triggered_signals)
        if risk_update_lookup:
            if to_text(lookup.get("action_type")) != to_text(normalized.get("action_type")):
                continue
            if to_text(lookup.get("reverse_kind")) != to_text(normalized.get("reverse_kind")):
                continue
            if to_text(lookup.get("trade_group_id")) != to_text(normalized.get("trade_group_id")):
                continue
            if to_text(lookup.get("origin_signal_id")) != to_text(normalized.get("origin_signal_id")):
                continue
            if _key_text(lookup.get("risk_update_type")) != _record_risk_update_type(record):
                continue
            return record
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
        if _key_text(lookup.get("risk_update_type")) != _record_risk_update_type(record):
            continue
        return record
    return None


def _risk_update_seq(payload: dict[str, Any]) -> int:
    extra = ensure_object(payload.get("extra"))
    return _sequence_number(first_non_empty(extra.get("risk_update_seq"), payload.get("risk_update_seq"), 0))


def _superseded_risk_update_summary(existing: Any) -> dict[str, Any]:
    extra = get_reverse_extra(existing)
    return {
        "id": to_text(record_value(existing, "id")),
        "created": to_text(record_value(existing, "created") or extra.get("created")),
        "updated": to_text(record_value(existing, "updated") or extra.get("updated")),
        "reason": to_text(record_value(existing, "reason") or extra.get("risk_update_reason") or extra.get("reason")),
        "risk_update_seq": to_int(extra.get("risk_update_seq"), 0),
        "risk_update_type": to_text(extra.get("risk_update_type")),
        "new_sl": _to_number(extra.get("new_sl"), 0),
        "new_tp": _to_number(extra.get("new_tp"), 0),
    }


def _should_skip_stale_risk_update(existing: Any, incoming_payload: dict[str, Any]) -> bool:
    criteria = build_reverse_duplicate_criteria(incoming_payload)
    if not _is_tv_risk_update_criteria(criteria):
        return False
    incoming_seq = _risk_update_seq(incoming_payload)
    existing_seq = _risk_update_seq(normalize_reverse_record(existing, parse_triggered_signals=parse_triggered_signals))
    return existing_seq > 0 and incoming_seq <= existing_seq


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
    if existing and _should_skip_stale_risk_update(existing, data):
        return {
            "record": merge_record_patch(existing, {}),
            "created": False,
            "skipped": True,
            "reason": "stale_risk_update_seq",
        }
    existing_extra = get_reverse_extra(existing)
    triggered_signals = parse_triggered_signals(
        data.get("triggered_signals") if data.get("triggered_signals") is not None else extra.get("triggered_signals")
    )
    record_payload = {
        "symbol": criteria["symbol"],
        "environment": criteria["environment"],
        "direction": criteria["direction"],
        "source": to_text(data.get("source") or "indicator") or "indicator",
        "priority": parse_integer(data.get("priority"), default=DEFAULT_REVERSE_PRIORITY, minimum=1, maximum=10),
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
            "broker_mode": criteria["environment"],
            "data_environment": resolve_data_environment(data.get("data_environment") or extra.get("data_environment")),
            "shared_market_data": resolve_data_environment(data.get("data_environment") or extra.get("data_environment")) == "live",
            "reverse_kind": criteria["reverse_kind"],
            "target_state": criteria["target_state"],
            "risk_update_type": criteria["risk_update_type"],
            "triggered_signals": triggered_signals,
        },
    }

    if existing:
        if criteria.get("reverse_kind") == "tv_risk_update" and criteria.get("action_type") == "adjust_bracket":
            prior_updates = existing_extra.get("superseded_updates")
            if not isinstance(prior_updates, list):
                prior_updates = []
            record_payload["extra"]["superseded_updates"] = (
                prior_updates + [_superseded_risk_update_summary(existing)]
            )[-5:]
            record_payload["extra"]["superseded_by_latest_risk_update"] = True
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
