from __future__ import annotations

from typing import Any, Callable

from ibkr_api.reverse.shared import (
    first_non_empty,
    get_reverse_extra,
    parse_triggered_signals as default_parse_triggered_signals,
    record_value,
    to_float,
    to_int,
    to_text,
)


def _to_number(value: Any, fallback: float = 0.0) -> float:
    parsed = to_float(value)
    return fallback if parsed is None else parsed


def _infer_reverse_kind(source: str, extra: dict[str, Any]) -> str:
    return to_text(extra.get("reverse_kind")) or ("signal_conflict" if source == "signal" else "indicator_conflict")


def _infer_target_state(extra: dict[str, Any], order_status: str, action_type: str) -> str:
    explicit = to_text(extra.get("target_state"))
    if explicit:
        return explicit
    if order_status == "Filled":
        return "filled_position"
    if order_status == "Submitted":
        return "pending_entry"
    if action_type in {"close", "adjust_sl", "adjust_tp"}:
        return "filled_position"
    if action_type == "cancel":
        return "pending_entry"
    return ""


def _normalize_triggered_signals(value: Any, extra: dict[str, Any], *, parse_triggered_signals) -> list[str]:
    candidate = value if value is not None else extra.get("triggered_signals")
    return parse_triggered_signals(candidate)


def normalize_reverse_record(
    record_or_data: Any,
    *,
    default_environment: str = "live",
    parse_triggered_signals: Callable[[Any], list[str]] = default_parse_triggered_signals,
) -> dict[str, Any]:
    extra = get_reverse_extra(record_or_data)
    source = to_text(record_value(record_or_data, "source") or extra.get("source"))
    action_type = to_text(record_value(record_or_data, "action_type") or extra.get("action_type"))
    order_status = to_text(first_non_empty(extra.get("order_status"), extra.get("target_order_status"), ""))
    trade_group_id = to_text(first_non_empty(extra.get("trade_group_id"), record_value(record_or_data, "trade_group_id"), ""))
    entry_order_unique_id = to_text(
        first_non_empty(extra.get("entry_order_unique_id"), record_value(record_or_data, "entry_order_unique_id"), "")
    )
    order_unique_id = to_text(first_non_empty(extra.get("order_unique_id"), entry_order_unique_id, trade_group_id, ""))
    broker_order_id = to_text(first_non_empty(extra.get("broker_order_id"), extra.get("order_id"), ""))
    signal_id = to_text(first_non_empty(extra.get("signal_id"), ""))
    origin_signal_id = to_text(first_non_empty(extra.get("origin_signal_id"), extra.get("signal_id_orig"), ""))
    environment = to_text(record_value(record_or_data, "environment") or extra.get("environment") or default_environment)
    return {
        "id": to_text(record_value(record_or_data, "id")),
        "environment": environment or default_environment,
        "symbol": to_text(record_value(record_or_data, "symbol") or extra.get("symbol")),
        "direction": to_text(record_value(record_or_data, "direction") or extra.get("direction")),
        "source": source,
        "reverse_kind": _infer_reverse_kind(source, extra),
        "target_state": _infer_target_state(extra, order_status, action_type),
        "target_order_status": order_status,
        "priority": to_int(first_non_empty(record_value(record_or_data, "priority"), extra.get("priority"), 0), 0),
        "strength": to_text(record_value(record_or_data, "strength") or extra.get("strength")),
        "score": _to_number(first_non_empty(record_value(record_or_data, "score"), extra.get("score"), 0), 0),
        "action_type": action_type,
        "status": to_text(record_value(record_or_data, "status") or extra.get("status") or "pending"),
        "reason": to_text(record_value(record_or_data, "reason") or extra.get("reason")),
        "processed_time": to_text(record_value(record_or_data, "processed_time") or extra.get("processed_time")),
        "triggered_signals": _normalize_triggered_signals(record_value(record_or_data, "triggered_signals"), extra, parse_triggered_signals=parse_triggered_signals),
        "signal_id": signal_id,
        "origin_signal_id": origin_signal_id,
        "trade_group_id": trade_group_id,
        "entry_order_unique_id": entry_order_unique_id,
        "order_unique_id": order_unique_id,
        "broker_order_id": broker_order_id,
        "order_id": broker_order_id,
        "relation_status": to_text(first_non_empty(extra.get("relation_status"), "")),
        "position_side": to_text(first_non_empty(extra.get("position_side"), "")),
        "current_direction": to_text(
            first_non_empty(extra.get("current_direction"), record_value(record_or_data, "direction"), extra.get("direction"), "")
        ),
        "new_direction": to_text(first_non_empty(extra.get("new_direction"), "")),
        "entry_price": _to_number(first_non_empty(extra.get("entry_price"), extra.get("limit_price"), extra.get("fill_price"), 0), 0),
        "quantity": _to_number(first_non_empty(extra.get("quantity"), 0), 0),
        "take_profit": _to_number(first_non_empty(extra.get("take_profit"), extra.get("tp_price"), 0), 0),
        "stop_loss": _to_number(first_non_empty(extra.get("stop_loss"), extra.get("sl_price"), 0), 0),
        "old_sl": _to_number(first_non_empty(extra.get("old_sl"), 0), 0),
        "new_sl": _to_number(first_non_empty(extra.get("new_sl"), 0), 0),
        "old_tp": _to_number(first_non_empty(extra.get("old_tp"), 0), 0),
        "new_tp": _to_number(first_non_empty(extra.get("new_tp"), 0), 0),
        "executed_action": to_text(first_non_empty(extra.get("executed_action"), "")),
        "result_status": to_text(first_non_empty(extra.get("result_status"), "")),
        "manual_requested": bool(extra.get("manual_requested")),
        "manual_requested_at": to_text(first_non_empty(extra.get("manual_requested_at"), "")),
        "bar_time_ms": to_int(first_non_empty(record_value(record_or_data, "bar_time_ms"), extra.get("bar_time_ms"), 0), 0),
        "us_time": to_text(record_value(record_or_data, "us_time") or extra.get("us_time")),
        "cn_time": to_text(record_value(record_or_data, "cn_time") or extra.get("cn_time")),
        "created": to_text(record_value(record_or_data, "created") or extra.get("created")),
        "updated": to_text(record_value(record_or_data, "updated") or extra.get("updated")),
        "extra": extra,
    }


__all__ = ["normalize_reverse_record"]
