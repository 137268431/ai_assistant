from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.orders.group_common import (
    append_group_order_detail,
    build_group_status_patch,
    dedupe_order_rows,
    is_closed_status,
    normalize_order_row,
    pick_primary_order_row,
    resolve_cancelable_broker_order_id,
    resolve_trade_group_id,
)
from ibkr_api.orders.values import ensure_object, to_text
from ibkr_api.signals.notifications import SendInteractive, UpdateInteractive, sync_signal_status_notification
from ibkr_api.signals.values import get_signal_extra, merge_signal_extra


ConfigValue = Callable[[str, str, str], str]
EscapeFilterString = Callable[[Any], str]
NormalizeEnvironment = Callable[[Any, str], str]
CancelBrokerOrder = Callable[[str, str, dict[str, Any]], dict[str, Any]]
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


def _cancel_failure_looks_closed(payload: dict[str, Any] | None) -> bool:
    error_text = to_text(
        (payload or {}).get("error")
        or (payload or {}).get("message")
        or (payload or {}).get("raw")
    ).lower()
    if not error_text:
        return False
    return any(
        marker in error_text
        for marker in (
            "already canceled",
            "already cancelled",
            "already inactive",
            "not active",
            "inactive",
            "not found",
            "cannot be cancelled",
            "cannot be canceled",
            "filled",
        )
    )


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


def _expire_signal_for_order_group(
    pb: Any,
    signal_row: dict[str, Any] | None,
    *,
    environment: str,
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
    extra_patch = merge_signal_extra(
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
    )
    updated = pb.update_record(
        "ibkr_signals",
        to_text(signal_row.get("id")),
        {
            "status": "expired",
            "note": "order_expired",
            "extra": extra_patch,
        },
    )
    updated_row = dict(updated) if isinstance(updated, dict) else {**signal_row, "status": "expired", "note": "order_expired", "extra": extra_patch}
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


def _load_related_trade_group_rows(
    pb: Any,
    *,
    environment: str,
    trade_group_id: str,
    escape_filter_string: EscapeFilterString,
) -> list[dict[str, Any]]:
    escaped_group = escape_filter_string(trade_group_id)
    escaped_environment = escape_filter_string(environment)
    return _query_order_rows(
        pb,
        (
            f'(trade_group_id = "{escaped_group}" || '
            f'entry_order_unique_id = "{escaped_group}" || '
            f'unique_id = "{escaped_group}") && '
            f'environment = "{escaped_environment}"'
        ),
    )


def _cancel_group_broker_order(
    primary_row: dict[str, Any] | None,
    *,
    environment: str,
    cancel_broker_order: CancelBrokerOrder,
    trade_group_id: str,
) -> dict[str, Any]:
    cancel_order_id = resolve_cancelable_broker_order_id(primary_row)
    if not cancel_order_id:
        return {"ok": True, "skipped": True, "reason": "no_cancelable_broker_order_id"}
    result = dict(cancel_broker_order(environment, cancel_order_id, {"trade_group_id": trade_group_id}) or {})
    if result.get("ok"):
        return {"ok": True, "cancelled_order_id": cancel_order_id, "payload": result}
    payload = ensure_object(result.get("payload"))
    if _cancel_failure_looks_closed({**payload, "error": result.get("error"), "message": result.get("message")}):
        return {
            "ok": True,
            "cancelled_order_id": cancel_order_id,
            "payload": result,
            "treated_as_closed": True,
        }
    return {
        "ok": False,
        "cancelled_order_id": cancel_order_id,
        "error": to_text(result.get("error") or result.get("message") or payload.get("error") or payload.get("message")) or "cancel_failed",
        "payload": result,
    }


def build_order_expiry_response(
    pb: Any,
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    config_value: ConfigValue | None,
    cancel_broker_order: CancelBrokerOrder,
    send_interactive: SendInteractive | None = None,
    update_interactive: UpdateInteractive | None = None,
    signal_chat_id_fn: SignalChatId | None = None,
    console_base_url: str = "",
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    environment = normalize_environment(request_payload.get("environment"), "live")
    validity_minutes = _validity_minutes(config_value, environment)
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - validity_minutes * 60 * 1000

    expired_rows = _load_expired_entry_rows(
        pb,
        environment=environment,
        cutoff_ms=cutoff_ms,
        escape_filter_string=escape_filter_string,
    )
    if not expired_rows:
        return (
            {
                "ok": True,
                "environment": environment,
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
    signal_chat_id = _signal_chat_id(signal_chat_id_fn, environment)

    for row in expired_rows:
        trade_group_id = resolve_trade_group_id(row) or normalize_order_row(row).get("unique_id") or to_text(row.get("id"))
        if not trade_group_id or trade_group_id in processed_groups:
            continue
        processed_groups.add(trade_group_id)

        related_rows = dedupe_order_rows(
            _load_related_trade_group_rows(
                pb,
                environment=environment,
                trade_group_id=trade_group_id,
                escape_filter_string=escape_filter_string,
            )
            or [dict(row)]
        )
        primary_row = pick_primary_order_row(related_rows, row)
        cancel_result = _cancel_group_broker_order(
            primary_row,
            environment=environment,
            cancel_broker_order=cancel_broker_order,
            trade_group_id=trade_group_id,
        )
        if cancel_result.get("cancelled_order_id"):
            cancelled_order_ids.append(str(cancel_result.get("cancelled_order_id") or ""))
        if not cancel_result.get("ok"):
            failed_groups.append(
                {
                    "trade_group_id": trade_group_id,
                    "error": to_text(cancel_result.get("error")) or "cancel_failed",
                    "cancelled_order_id": to_text(cancel_result.get("cancelled_order_id")),
                }
            )
            continue

        reason = f"订单超时自动取消（有效期 {validity_minutes} 分钟）"
        group_updated = 0
        group_updated_record_ids: list[str] = []
        for related_row in related_rows:
            snapshot = normalize_order_row(related_row)
            if is_closed_status(snapshot.get("status")):
                continue
            patch, event_times = build_group_status_patch(
                related_row,
                next_status="Canceled",
                source="order_expiry_check",
                reason=reason,
            )
            updated_row = pb.update_record("orders", to_text(related_row.get("id")), patch)
            updated_record_id = to_text((updated_row or {}).get("id") or related_row.get("id") or snapshot.get("unique_id"))
            updated_record_ids.append(updated_record_id)
            group_updated_record_ids.append(updated_record_id)
            detail_row = append_group_order_detail(
                pb,
                updated_row if isinstance(updated_row, dict) else {**related_row, **patch},
                source="order_expiry_check",
                reason=reason,
                event_times=event_times,
                extra_patch={
                    "previous_status": snapshot.get("status") or "",
                    "validity_minutes": validity_minutes,
                    "cutoff_ms": cutoff_ms,
                    "cancelled_broker_order_id": to_text(cancel_result.get("cancelled_order_id")),
                    "trade_group_id": trade_group_id,
                    "treated_as_closed": bool(cancel_result.get("treated_as_closed")),
                },
            )
            detail_record_ids.append(to_text((detail_row or {}).get("id")))
            group_updated += 1
        processed_count += group_updated
        if group_updated > 0:
            signal_row = _load_signal_for_order_group(
                pb,
                related_rows,
                environment=environment,
                escape_filter_string=escape_filter_string,
            )
            signal_result = _expire_signal_for_order_group(
                pb,
                signal_row,
                environment=environment,
                trade_group_id=trade_group_id,
                validity_minutes=validity_minutes,
                cutoff_ms=cutoff_ms,
                updated_order_ids=group_updated_record_ids,
                cancelled_order_id=to_text(cancel_result.get("cancelled_order_id")),
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
        "processed_count": processed_count,
        "expired_group_count": len(processed_groups),
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
