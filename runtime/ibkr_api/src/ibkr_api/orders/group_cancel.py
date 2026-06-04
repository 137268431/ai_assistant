from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.modes import request_broker_mode
from ibkr_api.orders.group_common import (
    CANCEL_GROUP_ACTION,
    append_group_order_detail,
    broker_cancel_response_looks_closed,
    is_closed_status,
    load_order_action_context,
    normalize_order_row,
    resolve_cancelable_broker_order_id,
)
from ibkr_api.orders.group_common import build_group_status_patch
from ibkr_api.orders.values import ensure_object, to_text


CancelBrokerOrder = Callable[[str, str, dict[str, Any]], dict[str, Any]]


CANCEL_STATUS_HINTS = {
    "Filled": "订单已成交，无法取消",
    "Canceled": "订单已取消，无需重复操作",
    "Closed": "订单已平仓，无法取消",
}
SIGNAL_CANCEL_ALLOWED_STATUSES = {
    "",
    "awaiting_confirm",
    "pending",
    "submitted",
    "submitted_waiting_fill",
    "entry_missed_limit_cap",
}
SIGNAL_CANCEL_TERMINAL_STATUSES = {
    "cancelled",
    "canceled",
    "closed",
    "expired",
    "rejected",
    "executed",
    "filled_position",
    "protected_active",
    "protection_incomplete",
    "protection_reprice_failed",
}


def _warning_response(
    message: str,
    *,
    target_id: str,
    symbol: str,
    trade_group_id: str,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    return (
        {
            "ok": False,
            "warning": True,
            "action": CANCEL_GROUP_ACTION,
            "message": message,
            "target_id": target_id,
            "symbol": symbol,
            "trade_group_id": trade_group_id,
            "source": "ibkr-api",
            **(extra or {}),
        },
        200,
    )


def _error_response(message: str, status_code: int, *, target_id: str) -> tuple[dict[str, Any], int]:
    return (
        {
            "ok": False,
            "error": message,
            "action": CANCEL_GROUP_ACTION,
            "target_id": target_id,
            "source": "ibkr-api",
        },
        status_code,
    )


def _cancel_open_broker_orders(
    cancel_ids: list[str],
    *,
    environment: str,
    payload: dict[str, Any],
    cancel_broker_order: CancelBrokerOrder,
) -> tuple[list[str], list[dict[str, Any]]]:
    cancelled_ids: list[str] = []
    failed_ids: list[dict[str, Any]] = []
    for order_id in cancel_ids:
        result = cancel_broker_order(environment, order_id, payload)
        if result.get("ok"):
            cancelled_ids.append(order_id)
            continue
        if broker_cancel_response_looks_closed(result):
            cancelled_ids.append(order_id)
            continue
        failed_ids.append(
            {
                "order_id": order_id,
                "error": to_text(
                    result.get("error")
                    or result.get("message")
                    or result.get("payload", {}).get("error")
                    or result.get("payload", {}).get("message")
                    or f"status_{result.get('status_code') or 500}"
                ),
            }
        )
    return cancelled_ids, failed_ids


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _signal_extra(signal_row: dict[str, Any] | None) -> dict[str, Any]:
    extra = (signal_row or {}).get("extra") if isinstance(signal_row, dict) else {}
    if isinstance(extra, dict):
        return dict(extra)
    if isinstance(extra, str):
        try:
            parsed = json.loads(extra)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return ensure_object(extra)


def _merge_signal_extra(signal_row: dict[str, Any] | None, patch: dict[str, Any]) -> dict[str, Any]:
    return {**_signal_extra(signal_row), **ensure_object(patch)}


def _load_signal_record(
    pb: Any,
    *,
    signal_id: str,
    environment: str,
    escape_filter_string: Callable[[Any], str],
) -> Any:
    return pb.get_first_record(
        "ibkr_signals",
        filter=(
            f'(id = "{escape_filter_string(signal_id)}" || signal_id = "{escape_filter_string(signal_id)}") && '
            f'environment = "{escape_filter_string(environment)}"'
        ),
    )


def _payload_data_environment(payload: dict[str, Any], primary_row: dict[str, Any] | None, broker_environment: str) -> str:
    extra = primary_row.get("extra") if isinstance(primary_row, dict) and isinstance(primary_row.get("extra"), dict) else {}
    return (
        to_text((payload or {}).get("data_environment"))
        or to_text((payload or {}).get("market_data_mode"))
        or to_text(extra.get("data_environment"))
        or to_text(extra.get("market_data_mode"))
        or ("live" if broker_environment in {"paper", "live"} else broker_environment)
    ).lower()


def _load_related_signal(
    pb: Any,
    *,
    signal_id: str,
    data_environment: str,
    broker_environment: str,
    escape_filter_string: Callable[[Any], str],
) -> dict[str, Any] | None:
    if not signal_id:
        return None
    for environment in [data_environment, broker_environment, "live"]:
        if not environment:
            continue
        try:
            row = _load_signal_record(
                pb,
                signal_id=signal_id,
                environment=environment,
                escape_filter_string=escape_filter_string,
            )
        except Exception:
            row = None
        if isinstance(row, dict) and row.get("id"):
            return dict(row)
    return None


def _signal_mode_status(signal_row: dict[str, Any], broker_environment: str) -> str:
    extra = _signal_extra(signal_row)
    by_mode = extra.get("execution_by_mode") if isinstance(extra.get("execution_by_mode"), dict) else {}
    for key in (broker_environment, broker_environment.lower(), broker_environment.upper()):
        payload = by_mode.get(key) if isinstance(by_mode, dict) else {}
        status = to_text(payload.get("status") if isinstance(payload, dict) else "").lower()
        if status:
            return status
    return to_text(signal_row.get("status")).lower()


def _signal_cancel_patch(
    signal_row: dict[str, Any],
    *,
    broker_environment: str,
    data_environment: str,
    reason: str,
    trade_group_id: str,
    cancelled_order_ids: list[str],
    updated_order_ids: list[str],
    source: str,
) -> dict[str, Any]:
    cancelled_at = _now_iso_utc()
    extra_update = {
        "status_reason": "order_cancelled_by_user",
        "cancel_reason": reason or "页面取消主单",
        "cancelled_by": source or "orders/cancel_group",
        "cancelled_at": cancelled_at,
        "cancelled_order_ids": list(cancelled_order_ids or []),
        "cancelled_trade_group_id": trade_group_id,
        "cancelled_order_record_ids": list(updated_order_ids or []),
        "broker_mode": broker_environment,
        "data_environment": data_environment,
    }
    merged = _merge_signal_extra(signal_row, extra_update)
    execution_by_mode = merged.get("execution_by_mode") if isinstance(merged.get("execution_by_mode"), dict) else {}
    broker_payload = execution_by_mode.get(broker_environment) if isinstance(execution_by_mode, dict) else {}
    if not isinstance(broker_payload, dict):
        broker_payload = {}
    execution_by_mode = dict(execution_by_mode)
    execution_by_mode[broker_environment] = {
        **broker_payload,
        "status": "cancelled",
        "note": "manual_order_cancelled",
        "broker_mode": broker_environment,
        "data_environment": data_environment,
        "status_reason": "order_cancelled_by_user",
        "cancel_reason": reason or "页面取消主单",
        "updated_at": cancelled_at,
        "source": source or "orders/cancel_group",
    }
    merged["execution_by_mode"] = execution_by_mode
    merged["last_runtime_broker_mode"] = broker_environment
    merged["last_runtime_data_environment"] = data_environment

    patch: dict[str, Any] = {"extra": merged}
    if broker_environment == data_environment == "live":
        patch.update({"status": "cancelled", "note": "manual_order_cancelled"})
    return patch


def _sync_signal_cancel_from_order_group(
    pb: Any,
    *,
    payload: dict[str, Any],
    primary_row: dict[str, Any],
    primary_snapshot: dict[str, Any],
    broker_environment: str,
    trade_group_id: str,
    reason: str,
    source: str,
    cancelled_order_ids: list[str],
    updated_order_ids: list[str],
    escape_filter_string: Callable[[Any], str],
) -> dict[str, Any]:
    if primary_snapshot.get("role") != "entry" or float(primary_snapshot.get("filled_qty") or 0.0) > 0:
        return {"status": "skipped", "reason": "primary_not_unfilled_entry"}
    primary_extra = primary_row.get("extra") if isinstance(primary_row.get("extra"), dict) else {}
    signal_id = to_text(primary_row.get("signal_id") or primary_extra.get("signal_id"))
    if not signal_id:
        return {"status": "skipped", "reason": "missing_signal_id"}
    data_environment = _payload_data_environment(payload, primary_row, broker_environment)
    signal_row = _load_related_signal(
        pb,
        signal_id=signal_id,
        data_environment=data_environment,
        broker_environment=broker_environment,
        escape_filter_string=escape_filter_string,
    )
    if not signal_row:
        return {"status": "skipped", "reason": "signal_not_found", "signal_id": signal_id}
    current_status = _signal_mode_status(signal_row, broker_environment)
    if current_status in SIGNAL_CANCEL_TERMINAL_STATUSES and current_status not in {"entry_missed_limit_cap"}:
        return {"status": "skipped", "reason": f"signal_status_{current_status}", "signal_id": signal_id}
    if current_status not in SIGNAL_CANCEL_ALLOWED_STATUSES:
        return {"status": "skipped", "reason": f"signal_status_{current_status}", "signal_id": signal_id}
    patch = _signal_cancel_patch(
        signal_row,
        broker_environment=broker_environment,
        data_environment=data_environment,
        reason=reason,
        trade_group_id=trade_group_id,
        cancelled_order_ids=cancelled_order_ids,
        updated_order_ids=updated_order_ids,
        source=source,
    )
    try:
        updated = pb.update_record("ibkr_signals", to_text(signal_row.get("id")), patch)
    except Exception as exc:
        return {"status": "failed", "reason": "signal_update_failed", "signal_id": signal_id, "error": str(exc)}
    return {
        "status": "cancelled",
        "signal_id": signal_id,
        "record_id": to_text((updated or {}).get("id") or signal_row.get("id")),
        "broker_mode": broker_environment,
        "data_environment": data_environment,
    }


def build_order_cancel_group_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    cancel_broker_order: CancelBrokerOrder,
) -> tuple[dict[str, Any], int]:
    environment = request_broker_mode(payload)
    context = load_order_action_context(
        pb,
        payload=payload,
        environment=environment,
        escape_filter_string=escape_filter_string,
    )
    target_id = context["target_id"] or context["broker_lookup_id"]
    if not target_id:
        return _error_response("缺少订单ID", 400, target_id="")

    action_row = context["action_row"]
    primary_row = context["primary_row"] or action_row
    if not action_row or not primary_row:
        return _error_response("找不到订单", 404, target_id=target_id)

    action_snapshot = normalize_order_row(action_row)
    primary_snapshot = normalize_order_row(primary_row)
    trade_group_id = context["trade_group_id"] or primary_snapshot["trade_group_id"]
    symbol = primary_snapshot["symbol"] or action_snapshot["symbol"] or target_id

    if action_snapshot["role"] and action_snapshot["role"] != "entry":
        return _warning_response(
            "止盈/止损等子单不能直接取消，请操作主入场单",
            target_id=target_id,
            symbol=symbol,
            trade_group_id=trade_group_id,
        )
    if primary_snapshot["filled_qty"] > 0:
        return _warning_response(
            "主单已部分成交，不能直接取消，请改用平仓整组",
            target_id=target_id,
            symbol=symbol,
            trade_group_id=trade_group_id,
        )
    if primary_snapshot["status"] in CANCEL_STATUS_HINTS:
        signal_result: dict[str, Any] = {}
        if primary_snapshot["status"] == "Canceled" and primary_snapshot["filled_qty"] <= 0:
            related_rows = context["related_rows"] or [primary_row]
            cancelled_ids = [
                order_id
                for order_id in (resolve_cancelable_broker_order_id(row) for row in related_rows)
                if order_id
            ]
            signal_result = _sync_signal_cancel_from_order_group(
                pb,
                payload=payload,
                primary_row=primary_row,
                primary_snapshot=primary_snapshot,
                broker_environment=environment,
                trade_group_id=trade_group_id,
                reason="订单已取消，同步信号为已取消",
                source=to_text(payload.get("source")) or "orders/cancel_group/already_cancelled",
                cancelled_order_ids=list(dict.fromkeys(cancelled_ids)),
                updated_order_ids=[],
                escape_filter_string=escape_filter_string,
            )
        return _warning_response(
            CANCEL_STATUS_HINTS[primary_snapshot["status"]],
            target_id=target_id,
            symbol=symbol,
            trade_group_id=trade_group_id,
            extra={"signal_result": signal_result} if signal_result else None,
        )

    related_rows = context["related_rows"] or [primary_row]
    cancel_ids: list[str] = []
    for row in related_rows:
        snapshot = normalize_order_row(row)
        if is_closed_status(snapshot["status"]):
            continue
        cancel_id = resolve_cancelable_broker_order_id(row)
        if cancel_id and cancel_id not in cancel_ids:
            cancel_ids.append(cancel_id)

    cancelled_ids: list[str] = []
    failed_ids: list[dict[str, Any]] = []
    if cancel_ids:
        cancelled_ids, failed_ids = _cancel_open_broker_orders(
            cancel_ids,
            environment=environment,
            payload=payload,
            cancel_broker_order=cancel_broker_order,
        )
    if failed_ids:
        return (
            {
                "ok": False,
                "error": f"账户撤单失败 {len(failed_ids)} 条",
                "action": CANCEL_GROUP_ACTION,
                "target_id": target_id,
                "trade_group_id": trade_group_id,
                "cancelled_order_ids": cancelled_ids,
                "failed_order_ids": failed_ids,
                "source": "ibkr-api",
            },
            500,
        )

    updated_record_ids: list[str] = []
    detail_record_ids: list[str] = []
    source = to_text(payload.get("source")) or "orders/cancel_group"
    reason = to_text(payload.get("reason")) or "页面取消主单"
    for row in related_rows:
        snapshot = normalize_order_row(row)
        if is_closed_status(snapshot["status"]):
            continue
        patch, event_times = build_group_status_patch(
            row,
            next_status="Canceled",
            source=source,
            reason=reason,
        )
        updated_row = pb.update_record("orders", to_text(row.get("id")), patch)
        updated_record_ids.append(to_text((updated_row or {}).get("id") or row.get("id") or snapshot["unique_id"]))
        detail_row = append_group_order_detail(
            pb,
            updated_row if isinstance(updated_row, dict) else {**row, **patch},
            source=source,
            reason=reason,
            event_times=event_times,
            extra_patch={
                "previous_status": snapshot["status"],
                "action": "cancel",
                "trade_group_id": trade_group_id,
                "cancelled_order_ids": list(cancelled_ids),
            },
        )
        detail_record_ids.append(to_text((detail_row or {}).get("id")))

    signal_result = _sync_signal_cancel_from_order_group(
        pb,
        payload=payload,
        primary_row=primary_row,
        primary_snapshot=primary_snapshot,
        broker_environment=environment,
        trade_group_id=trade_group_id,
        reason=reason,
        source=source,
        cancelled_order_ids=cancelled_ids,
        updated_order_ids=updated_record_ids,
        escape_filter_string=escape_filter_string,
    )

    return (
        {
            "ok": True,
            "action": CANCEL_GROUP_ACTION,
            "target_id": target_id,
            "symbol": symbol,
            "environment": environment,
            "trade_group_id": trade_group_id,
            "cancelled_order_ids": cancelled_ids,
            "failed_order_ids": [],
            "updated_record_ids": updated_record_ids,
            "detail_record_ids": detail_record_ids,
            "signal_result": signal_result,
            "source": "ibkr-api",
        },
        200,
    )
