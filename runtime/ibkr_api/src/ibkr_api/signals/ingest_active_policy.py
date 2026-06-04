from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, parse_boolean, to_float, to_text
from ibkr_api.signals.values import get_signal_extra


EscapeFilterString = Callable[[Any], str]

MUTABLE_SIGNAL_STATUSES = {"awaiting_confirm", "pending"}
BROKER_CONTROLLED_SIGNAL_STATUSES = {
    "submitted",
    "submitted_waiting_fill",
    "filled_repricing_protection",
    "filled_position",
    "protected_active",
    "executed",
}
PROTECTION_BLOCK_SIGNAL_STATUSES = {"protection_incomplete", "protection_reprice_failed"}
ACTIVE_SIGNAL_STATUSES = MUTABLE_SIGNAL_STATUSES | BROKER_CONTROLLED_SIGNAL_STATUSES | PROTECTION_BLOCK_SIGNAL_STATUSES
INACTIVE_SIGNAL_STATUSES = {"rejected", "expired", "closed", "cancelled", "canceled", "dropped"}
STRONG_REVERSE_SCORE = 6.0
EXECUTION_PARAM_FIELDS = ("entry", "limit_price", "take_profit", "stop_loss", "shares")
PRICE_EXECUTION_FIELDS = {"entry", "limit_price", "take_profit", "stop_loss"}


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _broker_scoped(broker_mode: str, data_environment: str) -> bool:
    return bool(broker_mode and data_environment and not (broker_mode == data_environment == "live"))


def _with_broker_execution(
    extra: dict[str, Any],
    *,
    broker_mode: str,
    data_environment: str,
    status: str,
    note: str,
    source: str,
) -> dict[str, Any]:
    merged = dict(extra if isinstance(extra, dict) else {})
    execution_by_mode = merged.get("execution_by_mode") if isinstance(merged.get("execution_by_mode"), dict) else {}
    broker_payload = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
    if not isinstance(broker_payload, dict):
        broker_payload = {}
    execution_by_mode = dict(execution_by_mode)
    execution_by_mode[broker_mode] = {
        **broker_payload,
        "status": status,
        "note": note,
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "status_reason": note,
        "updated_at": _now_iso_utc(),
        "source": source,
    }
    merged["execution_by_mode"] = execution_by_mode
    merged["last_runtime_broker_mode"] = broker_mode
    merged["last_runtime_data_environment"] = data_environment
    return merged


def _to_number(value: Any) -> float | None:
    return to_float(value)


def _append_unique(values: Any, value: str) -> list[str]:
    result = [to_text(item) for item in values if to_text(item)] if isinstance(values, list) else []
    if value and value not in result:
        result.append(value)
    return result


def _is_truthy(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return parse_boolean(value, False)


def _execution_compare_value(field: str, value: Any) -> float | str | None:
    if value in (None, ""):
        return None
    parsed = _to_number(value)
    if parsed is None:
        text = to_text(value)
        return text or None
    if field in {"limit_price", "shares"} and float(parsed) == 0.0:
        return None
    if field in PRICE_EXECUTION_FIELDS:
        return round(float(parsed), 2)
    return round(float(parsed), 4)


def changed_execution_fields(existing: dict[str, Any], incoming: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for field in EXECUTION_PARAM_FIELDS:
        if _execution_compare_value(field, existing.get(field)) != _execution_compare_value(field, incoming.get(field)):
            changed.append(field)
    return changed


def _execution_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    extra = get_signal_extra(row)
    return {
        "signal_id": to_text(row.get("signal_id")),
        "status": to_text(row.get("status")),
        "entry": row.get("entry"),
        "limit_price": row.get("limit_price"),
        "take_profit": row.get("take_profit"),
        "stop_loss": row.get("stop_loss"),
        "shares": row.get("shares"),
        "confirmed_at": to_text(extra.get("confirmed_at")),
        "confirmed_by": to_text(extra.get("confirmed_by")),
    }


def has_order_trace(row: dict[str, Any]) -> bool:
    extra = get_signal_extra(row)
    scalar_keys = (
        "trade_group_id",
        "entry_order_unique_id",
        "bracket_group",
        "order_unique_id",
        "order_id",
        "broker_order_id",
    )
    for key in scalar_keys:
        if to_text(row.get(key) or extra.get(key)):
            return True
    list_keys = (
        "order_results",
        "child_orders",
        "cancelled_order_ids",
        "missing_order_ids",
        "missing_protection_roles",
    )
    for key in list_keys:
        value = extra.get(key)
        if isinstance(value, list) and value:
            return True
    return bool(
        _is_truthy(extra.get("ack_partial"))
        or _is_truthy(extra.get("protection_incomplete"))
        or to_text(extra.get("last_ack_status"))
        or to_text(extra.get("last_ack_note"))
    )


def map_signal_strength(score: Any) -> str:
    numeric = float(_to_number(score) or 0.0)
    if numeric >= 6:
        return "strong"
    if numeric >= 3:
        return "medium"
    return "weak"


def calculate_signal_strength(signal_payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(signal_payload or {})
    extra = ensure_object(data.get("extra"))
    for key in (
        "signal_strength_score",
        "reverse_score",
        "signal_score",
        "score",
    ):
        parsed = _to_number(extra.get(key) if key in extra else data.get(key))
        if parsed is not None:
            return {
                "score": float(parsed),
                "level": map_signal_strength(parsed),
                "triggered_signals": list(extra.get("triggered_signals") or []),
                "source": key,
            }

    direction = to_text(data.get("direction")).lower()
    score = 0.0
    triggered: list[str] = []
    crsi = _to_number(extra.get("crsi"))
    vwap_dist = _to_number(extra.get("vwap_dist"))

    if direction == "long" and crsi is not None and crsi < 30:
        score += 2
        triggered.append("cRSI超卖")
    elif direction == "short" and crsi is not None and crsi > 70:
        score += 2
        triggered.append("cRSI超买")

    if direction == "long":
        divergence = _is_truthy(extra.get("crsi_bull_div")) or _is_truthy(extra.get("obv_bull_div"))
        fractal_or_channel = _is_truthy(extra.get("fractal_bull")) or _is_truthy(extra.get("sd_lower"))
        ema_touch = _is_truthy(extra.get("ema_bull_touch"))
    else:
        divergence = _is_truthy(extra.get("crsi_bear_div")) or _is_truthy(extra.get("obv_bear_div"))
        fractal_or_channel = _is_truthy(extra.get("fractal_bear")) or _is_truthy(extra.get("sd_upper"))
        ema_touch = _is_truthy(extra.get("ema_bear_touch"))

    if divergence:
        score += 3
        triggered.append("背离")
    if fractal_or_channel:
        score += 2
        triggered.append("分形/SD通道")
    if (vwap_dist is not None and abs(vwap_dist) > 2) or ema_touch:
        score += 1
        triggered.append("VWAP偏离/EMA触碰")

    if score <= 0 and to_text(data.get("signal")):
        score = STRONG_REVERSE_SCORE
        triggered.append("完整反向交易信号")

    deduped: list[str] = []
    for item in triggered:
        if item not in deduped:
            deduped.append(item)
    return {
        "score": float(score),
        "level": map_signal_strength(score),
        "triggered_signals": deduped,
        "source": "derived_signal_components" if deduped else "default",
    }


def effective_broker_signal_status(row: dict[str, Any], broker_mode: str, data_environment: str) -> str:
    extra = get_signal_extra(row)
    execution_by_mode = extra.get("execution_by_mode") if isinstance(extra, dict) else {}
    broker_execution = execution_by_mode.get(broker_mode) if isinstance(execution_by_mode, dict) else {}
    broker_status = to_text(broker_execution.get("status") if isinstance(broker_execution, dict) else "").lower()
    if broker_status:
        return broker_status
    top_level_status = to_text(row.get("status")).lower()
    if broker_mode == data_environment == "live":
        return top_level_status
    return top_level_status if top_level_status in MUTABLE_SIGNAL_STATUSES else ""


def _active_sort_key(row: dict[str, Any], broker_mode: str, data_environment: str) -> tuple[int, int, str]:
    status = effective_broker_signal_status(row, broker_mode, data_environment)
    status_weight = 3 if status in BROKER_CONTROLLED_SIGNAL_STATUSES else 2 if status in MUTABLE_SIGNAL_STATUSES else 1
    return (
        status_weight,
        int(to_float(row.get("bar_time_ms")) or 0),
        to_text(row.get("updated") or row.get("created")),
    )


def find_active_symbol_signal(
    pb: Any,
    data: dict[str, Any],
    environment: str,
    *,
    broker_mode: str | None = None,
    escape_filter_string: EscapeFilterString,
) -> dict[str, Any] | None:
    symbol = to_text(data.get("symbol")).upper()
    signal_id = to_text(data.get("signal_id"))
    runtime_environment = to_text(environment or data.get("environment") or "live").lower() or "live"
    runtime_broker_mode = to_text(broker_mode or data.get("broker_mode") or runtime_environment).lower() or runtime_environment
    if not symbol:
        return None

    rows = pb.get_records(
        "ibkr_signals",
        filter=(
            f'environment = "{escape_filter_string(runtime_environment)}" && '
            f'symbol = "{escape_filter_string(symbol)}"'
        ),
        sort="-updated,-created",
        per_page=25,
        page=1,
    )
    candidates: list[dict[str, Any]] = []
    for row in rows or []:
        record = dict(row) if isinstance(row, dict) else {}
        if signal_id and to_text(record.get("signal_id")) == signal_id:
            continue
        status = effective_broker_signal_status(record, runtime_broker_mode, runtime_environment)
        if status in INACTIVE_SIGNAL_STATUSES or status not in ACTIVE_SIGNAL_STATUSES:
            continue
        candidates.append(record)
    if not candidates:
        return None
    candidates.sort(key=lambda row: _active_sort_key(row, runtime_broker_mode, runtime_environment), reverse=True)
    return candidates[0]


def build_active_signal_refresh_payload(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    environment: str,
    *,
    broker_mode: str = "",
) -> dict[str, Any]:
    existing_extra = get_signal_extra(existing)
    incoming_extra = ensure_object(incoming.get("extra"))
    incoming_signal_id = to_text(incoming.get("signal_id"))
    refreshed_ids = _append_unique(existing_extra.get("merged_signal_ids"), incoming_signal_id)
    now_text = _now_iso_utc()
    next_extra = {
        **existing_extra,
        **incoming_extra,
        "environment": environment,
        "merged_signal_ids": refreshed_ids,
        "latest_merged_signal_id": incoming_signal_id,
        "latest_merged_at": now_text,
        "merge_policy": "same_symbol_same_direction_refresh_before_broker_submit",
        "status_reason": to_text(existing_extra.get("status_reason") or existing.get("note") or incoming.get("note")),
    }
    return {
        **incoming,
        "signal_id": to_text(existing.get("signal_id")) or incoming_signal_id,
        "status": to_text(existing.get("status") or incoming.get("status")),
        "note": to_text(existing.get("note") or incoming.get("note")),
        "extra": next_extra,
    }


def build_confirmed_signal_reconfirm_payload(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    environment: str,
    *,
    broker_mode: str = "",
) -> dict[str, Any]:
    existing_extra = get_signal_extra(existing)
    incoming_extra = ensure_object(incoming.get("extra"))
    incoming_signal_id = to_text(incoming.get("signal_id"))
    changed_fields = changed_execution_fields(existing, incoming)
    now_text = _now_iso_utc()
    next_extra = {
        **existing_extra,
        **incoming_extra,
        "environment": environment,
        "merged_signal_ids": _append_unique(existing_extra.get("merged_signal_ids"), incoming_signal_id),
        "followup_signal_ids": _append_unique(existing_extra.get("followup_signal_ids"), incoming_signal_id),
        "latest_merged_signal_id": incoming_signal_id,
        "latest_merged_at": now_text,
        "latest_followup_signal_id": incoming_signal_id,
        "latest_followup_at": now_text,
        "latest_followup_entry": incoming.get("entry"),
        "latest_followup_take_profit": incoming.get("take_profit"),
        "latest_followup_stop_loss": incoming.get("stop_loss"),
        "latest_followup_bar_time_ms": incoming.get("bar_time_ms"),
        "latest_followup_extra": incoming_extra,
        "followup_requires_reconfirm": True,
        "confirmation_stale": True,
        "reconfirm_reason": "same_direction_followup_changed_execution_params",
        "reconfirm_changed_fields": changed_fields,
        "previous_confirmed_snapshot": _execution_snapshot(existing),
        "merge_policy": "same_symbol_same_direction_reconfirm_after_manual_confirm",
        "status_reason": "followup_requires_reconfirm",
    }
    top_level_status = "awaiting_confirm"
    top_level_note = "followup_requires_reconfirm"
    if _broker_scoped(to_text(broker_mode), environment):
        next_extra = _with_broker_execution(
            next_extra,
            broker_mode=to_text(broker_mode),
            data_environment=environment,
            status="awaiting_confirm",
            note="followup_requires_reconfirm",
            source="ingest_followup_reconfirm",
        )
        top_level_status = to_text(existing.get("status") or incoming.get("status") or "pending")
        top_level_note = to_text(existing.get("note"))
    return {
        **incoming,
        "signal_id": to_text(existing.get("signal_id")) or incoming_signal_id,
        "status": top_level_status,
        "note": top_level_note,
        "extra": next_extra,
    }


def build_same_direction_followup_patch(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    reason: str = "same_direction_broker_order_active",
) -> dict[str, Any]:
    existing_extra = get_signal_extra(existing)
    incoming_extra = ensure_object(incoming.get("extra"))
    incoming_signal_id = to_text(incoming.get("signal_id"))
    strength = calculate_signal_strength(incoming)
    return {
        "extra": {
            **existing_extra,
            "followup_signal_ids": _append_unique(existing_extra.get("followup_signal_ids"), incoming_signal_id),
            "latest_followup_signal_id": incoming_signal_id,
            "latest_followup_at": _now_iso_utc(),
            "latest_followup_entry": incoming.get("entry"),
            "latest_followup_take_profit": incoming.get("take_profit"),
            "latest_followup_stop_loss": incoming.get("stop_loss"),
            "latest_followup_bar_time_ms": incoming.get("bar_time_ms"),
            "latest_followup_extra": incoming_extra,
            "suppressed_reason": reason,
            "signal_strength_score": strength["score"],
            "signal_strength_level": strength["level"],
        }
    }


def build_reverse_suppressed_patch(existing: dict[str, Any], incoming: dict[str, Any], *, reason: str) -> dict[str, Any]:
    existing_extra = get_signal_extra(existing)
    incoming_signal_id = to_text(incoming.get("signal_id"))
    strength = calculate_signal_strength(incoming)
    return {
        "extra": {
            **existing_extra,
            "suppressed_reverse_signal_ids": _append_unique(existing_extra.get("suppressed_reverse_signal_ids"), incoming_signal_id),
            "latest_suppressed_reverse_signal_id": incoming_signal_id,
            "latest_suppressed_reverse_at": _now_iso_utc(),
            "latest_suppressed_reverse_reason": reason,
            "latest_suppressed_reverse_direction": to_text(incoming.get("direction")).lower(),
            "suppressed_reason": reason,
            "signal_strength_score": strength["score"],
            "signal_strength_level": strength["level"],
        }
    }


def build_opposite_entry_block_patch(existing: dict[str, Any], incoming: dict[str, Any], *, reason: str) -> dict[str, Any]:
    existing_extra = get_signal_extra(existing)
    incoming_extra = ensure_object(incoming.get("extra"))
    incoming_signal_id = to_text(incoming.get("signal_id"))
    strength = calculate_signal_strength(incoming)
    now_text = _now_iso_utc()
    return {
        "extra": {
            **existing_extra,
            "blocked_opposite_entry_signal_ids": _append_unique(
                existing_extra.get("blocked_opposite_entry_signal_ids"),
                incoming_signal_id,
            ),
            "latest_blocked_opposite_entry_signal_id": incoming_signal_id,
            "latest_blocked_opposite_entry_at": now_text,
            "latest_blocked_opposite_entry_reason": reason,
            "latest_blocked_opposite_entry_direction": to_text(incoming.get("direction")).lower(),
            "latest_blocked_opposite_entry_extra": incoming_extra,
            "suppressed_reason": reason,
            "signal_strength_score": strength["score"],
            "signal_strength_level": strength["level"],
        }
    }


def build_stale_active_close_patch(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    reason: str,
    broker_mode: str = "",
    data_environment: str = "",
) -> dict[str, Any]:
    existing_extra = get_signal_extra(existing)
    incoming_signal_id = to_text(incoming.get("signal_id"))
    strength = calculate_signal_strength(incoming)
    now_text = _now_iso_utc()
    next_extra = {
        **existing_extra,
        "status_reason": reason,
        "closed_reason": reason,
        "closed_at": now_text,
        "reverse_policy": "skip_stale_active_without_order_trace",
        "latest_suppressed_reverse_signal_id": incoming_signal_id,
        "latest_suppressed_reverse_at": now_text,
        "latest_suppressed_reverse_reason": reason,
        "latest_suppressed_reverse_direction": to_text(incoming.get("direction")).lower(),
        "suppressed_reason": reason,
        "signal_strength_score": strength["score"],
        "signal_strength_level": strength["level"],
    }
    if _broker_scoped(to_text(broker_mode), to_text(data_environment)):
        return {
            "extra": _with_broker_execution(
                next_extra,
                broker_mode=to_text(broker_mode),
                data_environment=to_text(data_environment),
                status="closed",
                note=reason,
                source="ingest_stale_active_close",
            )
        }
    return {
        "status": "closed",
        "note": reason,
        "extra": next_extra,
    }


def build_superseded_patch(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    broker_mode: str = "",
    data_environment: str = "",
) -> dict[str, Any]:
    existing_extra = get_signal_extra(existing)
    incoming_signal_id = to_text(incoming.get("signal_id"))
    next_extra = {
        **existing_extra,
        "status_reason": "superseded_by_strong_reverse_signal",
        "superseded_by_signal_id": incoming_signal_id,
        "superseded_at": _now_iso_utc(),
        "reverse_policy": "supersede_unsubmitted_signal",
    }
    if _broker_scoped(to_text(broker_mode), to_text(data_environment)):
        return {
            "extra": _with_broker_execution(
                next_extra,
                broker_mode=to_text(broker_mode),
                data_environment=to_text(data_environment),
                status="expired",
                note="superseded_by_strong_reverse_signal",
                source="ingest_superseded",
            )
        }
    return {
        "status": "expired",
        "note": "superseded_by_strong_reverse_signal",
        "extra": next_extra,
    }


def build_reverse_record_payload(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    environment: str,
    *,
    data_environment: str = "live",
    active_status: str = "",
) -> dict[str, Any]:
    existing_extra = get_signal_extra(existing)
    incoming_extra = ensure_object(incoming.get("extra"))
    strength = calculate_signal_strength(incoming)
    existing_status = to_text(active_status or existing.get("status")).lower()
    action_type = (
        "close"
        if existing_status
        in {"protected_active", "filled_repricing_protection", "filled_position", "protection_reprice_failed", "executed"}
        else "cancel"
    )
    target_state = "filled_position" if action_type == "close" else "pending_entry"
    trade_group_id = to_text(
        existing.get("trade_group_id")
        or existing.get("entry_order_unique_id")
        or existing_extra.get("trade_group_id")
        or existing_extra.get("entry_order_unique_id")
        or existing_extra.get("bracket_group")
    )
    return {
        "symbol": to_text(existing.get("symbol") or incoming.get("symbol")).upper(),
        "environment": environment,
        "direction": to_text(existing.get("direction")).lower(),
        "source": "signal",
        "priority": 1,
        "strength": strength["level"],
        "score": strength["score"],
        "triggered_signals": strength["triggered_signals"] or ["方向冲突"],
        "action_type": action_type,
        "status": "pending",
        "reason": "strong_reverse_signal_auto_reversal",
        "bar_time_ms": incoming.get("bar_time_ms") or existing.get("bar_time_ms") or 0,
        "us_time": incoming.get("us_time") or existing.get("us_time") or "",
        "cn_time": incoming.get("cn_time") or existing.get("cn_time") or "",
        "extra": {
            "environment": environment,
            "broker_mode": environment,
            "data_environment": data_environment,
            "shared_market_data": data_environment == "live",
            "reverse_kind": "signal_conflict",
            "target_state": target_state,
            "reverse_policy": "full_auto_reverse",
            "reverse_stage": "cancel_old_order" if action_type == "cancel" else "close_old_position",
            "origin_signal_id": to_text(existing.get("signal_id")),
            "source_signal_id": to_text(incoming.get("signal_id")),
            "new_direction": to_text(incoming.get("direction")).lower(),
            "current_direction": to_text(existing.get("direction")).lower(),
            "old_signal_status": existing_status,
            "trade_group_id": trade_group_id,
            "entry_order_unique_id": trade_group_id,
            "signal_strength_source": strength["source"],
            "signal_strength_score": strength["score"],
            "signal_strength_level": strength["level"],
            "reentry_signal_payload": {
                **incoming,
                "extra": {
                    **incoming_extra,
                    "reverse_policy": "full_auto_reverse",
                    "reverse_source_signal_id": to_text(existing.get("signal_id")),
                    "signal_strength_score": strength["score"],
                    "signal_strength_level": strength["level"],
                    "reverse_stage": "awaiting_old_risk_resolution",
                },
            },
        },
    }


__all__ = [
    "ACTIVE_SIGNAL_STATUSES",
    "BROKER_CONTROLLED_SIGNAL_STATUSES",
    "MUTABLE_SIGNAL_STATUSES",
    "PROTECTION_BLOCK_SIGNAL_STATUSES",
    "STRONG_REVERSE_SCORE",
    "build_active_signal_refresh_payload",
    "build_confirmed_signal_reconfirm_payload",
    "build_reverse_record_payload",
    "build_reverse_suppressed_patch",
    "build_opposite_entry_block_patch",
    "build_same_direction_followup_patch",
    "build_stale_active_close_patch",
    "build_superseded_patch",
    "calculate_signal_strength",
    "changed_execution_fields",
    "effective_broker_signal_status",
    "find_active_symbol_signal",
    "has_order_trace",
    "map_signal_strength",
]
