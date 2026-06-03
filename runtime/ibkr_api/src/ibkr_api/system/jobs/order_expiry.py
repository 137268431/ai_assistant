from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.orders.group_common import (
    append_group_order_detail,
    broker_cancel_response_looks_closed,
    build_related_group_aliases,
    build_group_status_patch,
    dedupe_order_rows,
    is_closed_status,
    normalize_order_row,
    pick_primary_order_row,
    query_order_rows_by_aliases,
    resolve_cancelable_broker_order_id,
    resolve_trade_group_id,
)
from ibkr_api.orders.values import to_text
from ibkr_api.signals.notifications import SendInteractive, UpdateInteractive, sync_signal_status_notification
from ibkr_api.signals.values import get_signal_extra, merge_signal_extra
from ibkr_compute.core.broker_mode import (
    configured_broker_mode,
    normalize_broker_mode,
    resolve_market_data_mode,
)


ConfigValue = Callable[[str, str, str], str]
EscapeFilterString = Callable[[Any], str]
NormalizeEnvironment = Callable[[Any, str], str]
CancelBrokerOrder = Callable[[str, str, dict[str, Any]], dict[str, Any]]
ListLiveBrokerOrderIds = Callable[[str], list[str] | set[str] | tuple[str, ...] | None]
SignalChatId = Callable[[str], str]

ORDER_QUERY_SORT = "-created,-updated,-bar_time_ms"
ORDER_QUERY_LIMIT = 200
DEFAULT_VALIDITY_MINUTES = 30
ORDER_EXPIRY_SIGNAL_STATUSES = {"awaiting_confirm", "pending", "submitted"}


def _validity_minutes(config_value: ConfigValue | None, environment: str) -> int:
    if not callable(config_value):
        return DEFAULT_VALIDITY_MINUTES
    try:
        raw = int(str(config_value("order_validity_minutes", str(DEFAULT_VALIDITY_MINUTES), environment) or DEFAULT_VALIDITY_MINUTES).strip())
    except Exception:
        raw = DEFAULT_VALIDITY_MINUTES
    return max(1, raw)


def _request_broker_mode(payload: dict[str, Any], normalize_environment: NormalizeEnvironment) -> str:
    configured = configured_broker_mode()
    requested = payload.get("broker_mode") or payload.get("environment")
    if requested:
        return normalize_broker_mode(requested, configured)
    return configured


def _request_market_data_mode(payload: dict[str, Any]) -> str:
    requested = payload.get("market_data_mode") or payload.get("data_environment") or payload.get("environment")
    if requested:
        return resolve_market_data_mode(requested)
    return resolve_market_data_mode(None)


def _query_order_rows(pb: Any, filter_value: str) -> list[dict[str, Any]]:
    rows = pb.get_records(
        "orders",
        filter=filter_value,
        sort=ORDER_QUERY_SORT,
        per_page=ORDER_QUERY_LIMIT,
        page=1,
    )
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _signal_chat_id(signal_chat_id_fn: SignalChatId | None, environment: str) -> str:
    if not callable(signal_chat_id_fn):
        return ""
    try:
        return to_text(signal_chat_id_fn(environment))
    except Exception:
        return ""


def _load_signal_for_order_group(
    pb: Any,
    rows: list[dict[str, Any]],
    *,
    environment: str,
    escape_filter_string: EscapeFilterString,
) -> dict[str, Any] | None:
    signal_id = ""
    for row in rows:
        signal_id = to_text((row or {}).get("signal_id"))
        if signal_id:
            break
    if not signal_id:
        return None
    try:
        record = pb.get_first_record(
            "ibkr_signals",
            filter=(
                f'signal_id = "{escape_filter_string(signal_id)}" && '
                f'environment = "{escape_filter_string(environment)}"'
            ),
        )
    except Exception:
        return None
    return dict(record) if isinstance(record, dict) and record.get("id") else None


def _apply_signal_notification_patch(pb: Any, row: dict[str, Any], notify_result: dict[str, Any]) -> dict[str, Any]:
    extra_patch = notify_result.get("extra_patch") if isinstance(notify_result, dict) else None
    record_id = to_text((row or {}).get("id"))
    if not record_id or not isinstance(extra_patch, dict) or not extra_patch:
        return dict(row or {})
    updated = pb.update_record("ibkr_signals", record_id, {"extra": extra_patch})
    return dict(updated) if isinstance(updated, dict) else {**dict(row or {}), "extra": extra_patch}


def _with_broker_execution(
    signal_row: dict[str, Any],
    extra_patch: dict[str, Any],
    *,
    broker_mode: str,
    market_data_mode: str,
    status: str,
    note: str,
    updated_at: str,
) -> dict[str, Any]:
    merged = merge_signal_extra(signal_row, extra_patch)
    execution_by_mode = merged.get("execution_by_mode")
    if not isinstance(execution_by_mode, dict):
        execution_by_mode = {}
    broker_payload = execution_by_mode.get(broker_mode)
    if not isinstance(broker_payload, dict):
        broker_payload = {}
    execution_by_mode[broker_mode] = {
        **broker_payload,
        "status": status,
        "note": note,
        "data_environment": market_data_mode,
        "market_data_mode": market_data_mode,
        "source": "order_expiry_check",
        "updated_at": updated_at,
    }
    merged["execution_by_mode"] = execution_by_mode
    merged["broker_mode"] = broker_mode
    merged["data_environment"] = market_data_mode
    merged["market_data_mode"] = market_data_mode
    return merged


def _clear_scoped_signal_note_payload(signal_row: dict[str, Any], broker_mode: str) -> dict[str, str]:
    note = to_text((signal_row or {}).get("note")).lower()
    if note.startswith(f"{broker_mode}:") or "history_repair_pending" in note:
        return {"note": ""}
    return {}


def _expire_signal_for_order_group(
    pb: Any,
    signal_row: dict[str, Any] | None,
    *,
    environment: str,
    market_data_mode: str,
    trade_group_id: str,
    validity_minutes: int,
    cutoff_ms: int,
    updated_order_ids: list[str],
    cancelled_order_id: str,
    send_interactive: SendInteractive | None,
    update_interactive: UpdateInteractive | None,
    signal_chat_id: str,
    console_base_url: str,
) -> dict[str, Any]:
    if not isinstance(signal_row, dict) or not signal_row.get("id"):
        return {"status": "skipped", "reason": "signal_not_found"}
    current_status = to_text(signal_row.get("status")).lower()
    signal_id = to_text(signal_row.get("signal_id") or signal_row.get("id"))
    if current_status not in ORDER_EXPIRY_SIGNAL_STATUSES:
        return {"status": "skipped", "reason": f"signal_status_{current_status or 'unknown'}", "signal_id": signal_id}

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    extra_patch = _with_broker_execution(
        signal_row,
        {
            "expired_by": "order_expiry_check",
            "expired_at": now_iso,
            "expired_reason": "order_expired",
            "status_reason": "order_expired",
            "order_expiry_trade_group_id": trade_group_id,
            "order_expiry_validity_minutes": validity_minutes,
            "order_expiry_cutoff_ms": cutoff_ms,
            "order_expiry_updated_record_ids": [item for item in updated_order_ids if item],
            "order_expiry_cancelled_broker_order_id": cancelled_order_id,
        },
        broker_mode=environment,
        market_data_mode=market_data_mode,
        status="expired",
        note="order_expired",
        updated_at=now_iso,
    )
    update_payload = {
        "extra": extra_patch,
        **_clear_scoped_signal_note_payload(signal_row, environment),
    }
    if environment == market_data_mode == "live":
        update_payload.update({"status": "expired", "note": "order_expired"})
    updated = pb.update_record(
        "ibkr_signals",
        to_text(signal_row.get("id")),
        update_payload,
    )
    updated_row = dict(updated) if isinstance(updated, dict) else {**signal_row, **update_payload}
    updated_row["status"] = "expired"
    updated_row["note"] = "order_expired"
    notify_result = sync_signal_status_notification(
        updated_row,
        action="expired",
        message=f"挂单超时自动取消，信号已失效（订单有效期 {validity_minutes} 分钟）",
        send_interactive=send_interactive,
        signal_chat_id=signal_chat_id,
        update_interactive=update_interactive,
        console_base_url=console_base_url,
    )
    updated_row = _apply_signal_notification_patch(pb, updated_row, notify_result)
    return {
        "status": "expired",
        "signal_id": to_text(updated_row.get("signal_id") or signal_id),
        "record_id": to_text(updated_row.get("id") or signal_row.get("id")),
        "notify_result": to_text((notify_result or {}).get("feishu_signal_notify_last_result") or ""),
    }


def _load_expired_entry_rows(
    pb: Any,
    *,
    environment: str,
    cutoff_ms: int,
    escape_filter_string: EscapeFilterString,
) -> list[dict[str, Any]]:
    env = escape_filter_string(environment)
    filter_expr = (
        '(status = "Init" || status = "Submitted") && '
        f'environment = "{env}" && '
        f"bar_time_ms <= {int(cutoff_ms)} && "
        '(role = "entry" || role = "")'
    )
    return _query_order_rows(pb, filter_expr)


def _load_stale_child_rows(
    pb: Any,
    *,
    environment: str,
    escape_filter_string: EscapeFilterString,
) -> list[dict[str, Any]]:
    env = escape_filter_string(environment)
    filter_expr = (
        '(status = "Init" || status = "Submitted") && '
        f'environment = "{env}" && '
        '(role = "take_profit" || role = "stop_loss" || role = "tp" || role = "sl")'
    )
    return _query_order_rows(pb, filter_expr)


def _has_canceled_unfilled_entry(related_rows: list[dict[str, Any]] | None) -> bool:
    entry_rows = []
    for row in related_rows or []:
        snapshot = normalize_order_row(row)
        if snapshot.get("role") == "entry":
            entry_rows.append(snapshot)
    if not entry_rows:
        return False
    for snapshot in entry_rows:
        status = to_text(snapshot.get("status")).lower()
        if status not in {"canceled", "cancelled", "closed", "inactive", "rejected", "expired"}:
            return False
        if float(snapshot.get("filled_qty") or 0.0) > 0:
            return False
    return True


def _child_broker_order_ids(related_rows: list[dict[str, Any]] | None) -> list[str]:
    order_ids: list[str] = []
    for row in related_rows or []:
        snapshot = normalize_order_row(row)
        role = to_text(snapshot.get("role")).lower()
        if role not in {"take_profit", "stop_loss", "tp", "sl"}:
            continue
        if is_closed_status(snapshot.get("status")):
            continue
        order_id = resolve_cancelable_broker_order_id(row)
        if order_id and order_id not in order_ids:
            order_ids.append(order_id)
    return order_ids


def _child_orders_absent_from_live_broker(
    related_rows: list[dict[str, Any]] | None,
    live_broker_order_ids: set[str] | None,
) -> bool:
    if live_broker_order_ids is None:
        return False
    child_ids = _child_broker_order_ids(related_rows)
    if not child_ids:
        return False
    return not any(order_id in live_broker_order_ids for order_id in child_ids)


def _load_related_trade_group_rows(
    pb: Any,
    *,
    environment: str,
    trade_group_id: str,
    seed_rows: list[dict[str, Any]] | None = None,
    escape_filter_string: EscapeFilterString,
) -> list[dict[str, Any]]:
    return query_order_rows_by_aliases(
        pb,
        fields=("trade_group_id", "entry_order_unique_id", "unique_id"),
        aliases=build_related_group_aliases(seed_rows, trade_group_id),
        environment=environment,
        escape_filter_string=escape_filter_string,
    )


def _cancel_group_broker_orders(
    related_rows: list[dict[str, Any]] | None,
    *,
    environment: str,
    cancel_broker_order: CancelBrokerOrder,
    trade_group_id: str,
) -> dict[str, Any]:
    cancel_order_ids: list[str] = []
    for row in related_rows or []:
        snapshot = normalize_order_row(row)
        if is_closed_status(snapshot.get("status")):
            continue
        cancel_order_id = resolve_cancelable_broker_order_id(row)
        if cancel_order_id and cancel_order_id not in cancel_order_ids:
            cancel_order_ids.append(cancel_order_id)
    if not cancel_order_ids:
        return {"ok": True, "skipped": True, "reason": "no_cancelable_broker_order_id"}
    cancelled_order_ids: list[str] = []
    treated_as_closed_ids: list[str] = []
    failed_order_ids: list[dict[str, Any]] = []
    for cancel_order_id in cancel_order_ids:
        result = dict(cancel_broker_order(environment, cancel_order_id, {"trade_group_id": trade_group_id}) or {})
        if result.get("ok"):
            cancelled_order_ids.append(cancel_order_id)
            continue
        if broker_cancel_response_looks_closed(result):
            cancelled_order_ids.append(cancel_order_id)
            treated_as_closed_ids.append(cancel_order_id)
            continue
        failed_order_ids.append(
            {
                "order_id": cancel_order_id,
                "error": to_text(result.get("error") or result.get("message") or "cancel_failed"),
                "payload": result,
            }
        )
    if not failed_order_ids:
        return {
            "ok": True,
            "cancelled_order_id": cancelled_order_ids[0] if cancelled_order_ids else "",
            "cancelled_order_ids": cancelled_order_ids,
            "treated_as_closed": bool(treated_as_closed_ids),
            "treated_as_closed_ids": treated_as_closed_ids,
        }
    return {
        "ok": False,
        "cancelled_order_id": cancelled_order_ids[0] if cancelled_order_ids else (failed_order_ids[0]["order_id"] if failed_order_ids else ""),
        "cancelled_order_ids": cancelled_order_ids,
        "failed_order_ids": failed_order_ids,
        "error": f"cancel_failed:{len(failed_order_ids)}",
    }


def build_order_expiry_response(
    pb: Any,
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    config_value: ConfigValue | None,
    cancel_broker_order: CancelBrokerOrder,
    list_live_broker_order_ids: ListLiveBrokerOrderIds | None = None,
    send_interactive: SendInteractive | None = None,
    update_interactive: UpdateInteractive | None = None,
    signal_chat_id_fn: SignalChatId | None = None,
    console_base_url: str = "",
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = _request_broker_mode(request_payload, normalize_environment)
    market_data_mode = _request_market_data_mode(request_payload)
    validity_minutes = _validity_minutes(config_value, environment)
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - validity_minutes * 60 * 1000

    expired_rows = _load_expired_entry_rows(
        pb,
        environment=environment,
        cutoff_ms=cutoff_ms,
        escape_filter_string=escape_filter_string,
    )
    stale_child_rows = _load_stale_child_rows(
        pb,
        environment=environment,
        escape_filter_string=escape_filter_string,
    )
    live_broker_order_ids: set[str] | None = None
    if stale_child_rows and callable(list_live_broker_order_ids):
        try:
            live_broker_order_ids = {
                to_text(item)
                for item in (list_live_broker_order_ids(environment) or [])
                if to_text(item)
            }
        except Exception:
            live_broker_order_ids = None
    candidate_rows = dedupe_order_rows([*expired_rows, *stale_child_rows])
    if not candidate_rows:
        return (
            {
                "ok": True,
                "environment": environment,
                "broker_mode": environment,
                "market_data_mode": market_data_mode,
                "data_environment": market_data_mode,
                "processed_count": 0,
                "expired_group_count": 0,
                "cancelled_order_ids": [],
                "detail_record_ids": [],
                "updated_record_ids": [],
                "signal_results": [],
                "signal_expired_count": 0,
                "failed_groups": [],
                "validity_minutes": validity_minutes,
                "cutoff_ms": cutoff_ms,
                "source": "ibkr-api",
                "job_id": "order_expiry_check",
            },
            200,
        )

    processed_groups: set[str] = set()
    cancelled_order_ids: list[str] = []
    updated_record_ids: list[str] = []
    detail_record_ids: list[str] = []
    failed_groups: list[dict[str, Any]] = []
    signal_results: list[dict[str, Any]] = []
    processed_count = 0
    processed_group_count = 0
    signal_chat_id = _signal_chat_id(signal_chat_id_fn, environment)

    for row in candidate_rows:
        row_snapshot = normalize_order_row(row)
        is_stale_child_repair = row_snapshot.get("role") != "entry"
        trade_group_id = resolve_trade_group_id(row) or normalize_order_row(row).get("unique_id") or to_text(row.get("id"))
        if not trade_group_id or trade_group_id in processed_groups:
            continue

        related_rows = dedupe_order_rows(
            _load_related_trade_group_rows(
                pb,
                environment=environment,
                trade_group_id=trade_group_id,
                seed_rows=[row],
                escape_filter_string=escape_filter_string,
            )
            or [dict(row)]
        )
        primary_row = pick_primary_order_row(related_rows, row)
        related_rows = dedupe_order_rows([primary_row] + related_rows if primary_row else related_rows)
        related_aliases = build_related_group_aliases(related_rows, trade_group_id)
        if any(alias in processed_groups for alias in related_aliases):
            continue
        skip_broker_cancel = False
        has_canceled_unfilled_entry = False
        if is_stale_child_repair:
            has_canceled_unfilled_entry = _has_canceled_unfilled_entry(related_rows)
            child_orders_confirmed_absent = _child_orders_absent_from_live_broker(related_rows, live_broker_order_ids)
            if child_orders_confirmed_absent:
                skip_broker_cancel = True
            elif not has_canceled_unfilled_entry:
                continue
        processed_groups.update(related_aliases or [trade_group_id])
        processed_group_count += 1
        if skip_broker_cancel:
            cancel_result = {
                "ok": True,
                "skipped": True,
                "reason": "child_orders_not_live_at_broker",
                "cancelled_order_ids": [],
                "live_order_checked": True,
            }
        else:
            cancel_result = _cancel_group_broker_orders(
                related_rows,
                environment=environment,
                cancel_broker_order=cancel_broker_order,
                trade_group_id=trade_group_id,
            )
        for cancelled_order_id in cancel_result.get("cancelled_order_ids") or []:
            if cancelled_order_id and cancelled_order_id not in cancelled_order_ids:
                cancelled_order_ids.append(str(cancelled_order_id))
        if not cancel_result.get("ok"):
            failed_groups.append(
                {
                    "trade_group_id": trade_group_id,
                    "error": to_text(cancel_result.get("error")) or "cancel_failed",
                    "cancelled_order_id": to_text(cancel_result.get("cancelled_order_id")),
                    "cancelled_order_ids": list(cancel_result.get("cancelled_order_ids") or []),
                    "failed_order_ids": list(cancel_result.get("failed_order_ids") or []),
                }
            )
            continue

        source = "order_reconcile_stale_pb" if is_stale_child_repair else "order_expiry_check"
        reason = (
            "PB 残留订单自动关闭：主单已取消且 IBKR 未确认保护单仍活跃"
            if is_stale_child_repair
            else f"订单超时自动取消（有效期 {validity_minutes} 分钟）"
        )
        group_updated = 0
        group_updated_record_ids: list[str] = []
        for related_row in related_rows:
            snapshot = normalize_order_row(related_row)
            if is_closed_status(snapshot.get("status")):
                continue
            patch, event_times = build_group_status_patch(
                related_row,
                next_status="Canceled",
                source=source,
                reason=reason,
            )
            updated_row = pb.update_record("orders", to_text(related_row.get("id")), patch)
            updated_record_id = to_text((updated_row or {}).get("id") or related_row.get("id") or snapshot.get("unique_id"))
            updated_record_ids.append(updated_record_id)
            group_updated_record_ids.append(updated_record_id)
            detail_row = append_group_order_detail(
                pb,
                updated_row if isinstance(updated_row, dict) else {**related_row, **patch},
                source=source,
                reason=reason,
                event_times=event_times,
                extra_patch={
                    "previous_status": snapshot.get("status") or "",
                    "validity_minutes": validity_minutes,
                    "cutoff_ms": cutoff_ms,
                    "cancelled_broker_order_id": to_text(cancel_result.get("cancelled_order_id")),
                    "cancelled_broker_order_ids": list(cancel_result.get("cancelled_order_ids") or []),
                    "trade_group_id": trade_group_id,
                    "treated_as_closed": bool(cancel_result.get("treated_as_closed")),
                    "treated_as_closed_ids": list(cancel_result.get("treated_as_closed_ids") or []),
                    "stale_pb_repair": bool(is_stale_child_repair),
                    "live_order_checked": bool(cancel_result.get("live_order_checked")),
                    "cancel_skip_reason": to_text(cancel_result.get("reason")),
                },
            )
            detail_record_ids.append(to_text((detail_row or {}).get("id")))
            group_updated += 1
        processed_count += group_updated
        should_expire_signal = not is_stale_child_repair or has_canceled_unfilled_entry
        if group_updated > 0 and should_expire_signal:
            signal_row = _load_signal_for_order_group(
                pb,
                related_rows,
                environment=market_data_mode,
                escape_filter_string=escape_filter_string,
            )
            signal_result = _expire_signal_for_order_group(
                pb,
                signal_row,
                environment=environment,
                market_data_mode=market_data_mode,
                trade_group_id=trade_group_id,
                validity_minutes=validity_minutes,
                cutoff_ms=cutoff_ms,
                updated_order_ids=group_updated_record_ids,
                cancelled_order_id=",".join(str(item) for item in cancel_result.get("cancelled_order_ids") or []),
                send_interactive=send_interactive,
                update_interactive=update_interactive,
                signal_chat_id=signal_chat_id,
                console_base_url=console_base_url,
            )
            if signal_result:
                signal_results.append({"trade_group_id": trade_group_id, **signal_result})

    response_payload = {
        "ok": not failed_groups,
        "environment": environment,
        "broker_mode": environment,
        "market_data_mode": market_data_mode,
        "data_environment": market_data_mode,
        "processed_count": processed_count,
        "expired_group_count": processed_group_count,
        "cancelled_order_ids": [item for item in cancelled_order_ids if item],
        "updated_record_ids": [item for item in updated_record_ids if item],
        "detail_record_ids": [item for item in detail_record_ids if item],
        "signal_results": signal_results,
        "signal_expired_count": len([item for item in signal_results if item.get("status") == "expired"]),
        "failed_groups": failed_groups,
        "validity_minutes": validity_minutes,
        "cutoff_ms": cutoff_ms,
        "source": "ibkr-api",
        "job_id": "order_expiry_check",
    }
    return response_payload, (200 if response_payload["ok"] else 500)
