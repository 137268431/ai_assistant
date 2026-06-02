from __future__ import annotations

from typing import Any, Callable

from ibkr_api.reverse.common import (
    escape_filter_string,
    fetch_reverse_record,
    get_reverse_extra,
    is_tradingview_reverse_source,
    merge_record_patch,
    normalize_reverse_record,
    record_value,
    resolve_timestamp_text,
)


def _coerce_patch_number(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    try:
        return float(value)
    except Exception:
        return fallback


def _update_reverse_record(pb: Any, record: Any, patch: dict[str, Any]) -> Any:
    record_id = str(record_value(record, "id") or "").strip()
    if not record_id:
        raise ValueError("Execution action not found")
    updated = pb.update_record("ibkr_reverse_signals", record_id, patch)
    return updated if updated is not None else merge_record_patch(record, patch)


def _notify_status(
    notify_status: Callable[[str, Any, dict[str, Any]], Any] | None,
    event_name: str,
    record: Any,
    message: str,
) -> None:
    if not callable(notify_status):
        return
    try:
        notify_status(event_name, record, {"message": message})
    except Exception:
        return


def _build_dispatch_cancel_patch(record: Any, *, reason: str, now_text: str) -> dict[str, Any]:
    extra = get_reverse_extra(record)
    return {
        "status": "cancelled",
        "reason": reason or "页面取消执行动作",
        "processed_time": now_text,
        "extra": {
            **extra,
            "dispatch_action": "cancel",
            "dispatch_source": "page",
            "result_status": "cancelled_by_page",
        },
    }


def _build_dispatch_execute_patch(record: Any, *, reason: str, now_text: str) -> dict[str, Any]:
    extra = get_reverse_extra(record)
    current_priority = record_value(record, "priority")
    if current_priority in {None, ""}:
        current_priority = 5
    try:
        priority = min(int(current_priority), 1)
    except Exception:
        priority = 1
    patch: dict[str, Any] = {
        "priority": priority,
        "extra": {
            **extra,
            "manual_requested": True,
            "manual_requested_at": now_text,
            "manual_requested_source": "page",
            "manual_requested_reason": reason or "",
            "dispatch_action": "execute",
            "dispatch_source": "page",
        },
    }
    if reason:
        patch["reason"] = reason
    return patch


def build_reverse_dispatch_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    clock: Callable[[], Any] | None = None,
    escape_filter: Callable[[Any], str] = escape_filter_string,
    notify_status: Callable[[str, Any, dict[str, Any]], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    reverse_id = str(payload.get("reverse_id") or payload.get("signal_id") or "").strip()
    action = str(payload.get("action") or "").strip()
    reason = str(payload.get("reason") or "").strip()

    if not reverse_id:
        return {"error": "Missing reverse_id"}, 400
    if action not in {"execute", "cancel"}:
        return {"error": "Invalid action"}, 400

    try:
        record = fetch_reverse_record(pb, reverse_id, escape_filter=escape_filter)
        if not record or not record_value(record, "id"):
            return {"error": "Execution action not found"}, 404
        if not is_tradingview_reverse_source(record):
            return {"error": "Execution action is not TradingView sourced", "reason": "non_tv_action_disabled"}, 400

        if str(record_value(record, "status") or "") != "pending":
            return {"success": True, "signal": normalize_reverse_record(record)}, 200

        now_text = resolve_timestamp_text(clock)
        if action == "cancel":
            patch = _build_dispatch_cancel_patch(record, reason=reason, now_text=now_text)
            updated = _update_reverse_record(pb, record, patch)
            _notify_status(
                notify_status,
                "cancel",
                updated,
                reason or "页面已取消该执行动作",
            )
            return {"success": True, "signal": normalize_reverse_record(updated)}, 200

        patch = _build_dispatch_execute_patch(record, reason=reason, now_text=now_text)
        updated = _update_reverse_record(pb, record, patch)
        _notify_status(
            notify_status,
            "execute_request",
            updated,
            reason or "已请求 IBKR 优先执行该执行动作",
        )
        return {"success": True, "signal": normalize_reverse_record(updated)}, 200
    except Exception as exc:
        return {"error": str(exc)}, 500


def _build_ack_patch(record: Any, payload: dict[str, Any], *, now_text: str) -> dict[str, Any]:
    extra = get_reverse_extra(record)
    status = str(payload.get("status") or "confirmed").strip() or "confirmed"
    reason = str(payload.get("reason") or "").strip()
    return {
        "status": status,
        "reason": reason,
        "processed_time": now_text,
        "extra": {
            **extra,
            "broker_order_id": payload.get("broker_order_id") or payload.get("order_id") or extra.get("broker_order_id") or extra.get("order_id") or "",
            "order_id": payload.get("order_id") or payload.get("broker_order_id") or extra.get("order_id") or extra.get("broker_order_id") or "",
            "order_unique_id": payload.get("order_unique_id") or extra.get("order_unique_id") or "",
            "trade_group_id": payload.get("trade_group_id") or extra.get("trade_group_id") or "",
            "entry_order_unique_id": payload.get("entry_order_unique_id") or extra.get("entry_order_unique_id") or "",
            "signal_id": payload.get("signal_id_orig") or payload.get("origin_signal_id") or extra.get("signal_id") or "",
            "origin_signal_id": payload.get("origin_signal_id") or extra.get("origin_signal_id") or "",
            "current_direction": payload.get("current_direction") or extra.get("current_direction") or "",
            "new_direction": payload.get("new_direction") or extra.get("new_direction") or "",
            "executed_action": payload.get("executed_action") or payload.get("action_type") or extra.get("executed_action") or "",
            "result_status": payload.get("result_status") or extra.get("result_status") or "",
            "old_sl": _coerce_patch_number(payload.get("old_sl"), extra.get("old_sl")),
            "new_sl": _coerce_patch_number(payload.get("new_sl"), extra.get("new_sl")),
            "old_tp": _coerce_patch_number(payload.get("old_tp"), extra.get("old_tp")),
            "new_tp": _coerce_patch_number(payload.get("new_tp"), extra.get("new_tp")),
            "entry_price": _coerce_patch_number(payload.get("entry_price"), extra.get("entry_price")),
            "quantity": _coerce_patch_number(payload.get("quantity"), extra.get("quantity")),
            "take_profit": _coerce_patch_number(payload.get("take_profit"), extra.get("take_profit")),
            "stop_loss": _coerce_patch_number(payload.get("stop_loss"), extra.get("stop_loss")),
            "target_state": payload.get("target_state") or extra.get("target_state") or "",
            "target_order_status": payload.get("target_order_status") or payload.get("order_status") or extra.get("target_order_status") or extra.get("order_status") or "",
            "relation_status": payload.get("relation_status") or extra.get("relation_status") or "",
            "position_side": payload.get("position_side") or extra.get("position_side") or "",
            "manual_requested": False,
            "manual_requested_at": extra.get("manual_requested_at") or "",
        },
    }


def build_reverse_ack_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    clock: Callable[[], Any] | None = None,
    escape_filter: Callable[[Any], str] = escape_filter_string,
    notify_status: Callable[[str, Any, dict[str, Any]], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    reverse_id = str(payload.get("signal_id") or payload.get("reverse_id") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    status = str(payload.get("status") or "confirmed").strip() or "confirmed"

    if not reverse_id:
        return {"error": "Missing signal_id"}, 400

    try:
        record = fetch_reverse_record(pb, reverse_id, escape_filter=escape_filter)
        if not record or not record_value(record, "id"):
            return {"error": "Execution action not found"}, 404
        if not is_tradingview_reverse_source(record):
            return {"error": "Execution action is not TradingView sourced", "reason": "non_tv_action_disabled"}, 400

        patch = _build_ack_patch(record, {**payload, "status": status, "reason": reason}, now_text=resolve_timestamp_text(clock))
        updated = _update_reverse_record(pb, record, patch)
        _notify_status(
            notify_status,
            "ack",
            updated,
            reason or f"IBKR 已回写 {status}",
        )
        return {"success": True, "signal": normalize_reverse_record(updated)}, 200
    except Exception as exc:
        return {"error": str(exc)}, 500


__all__ = [
    "build_reverse_ack_response",
    "build_reverse_dispatch_response",
]
