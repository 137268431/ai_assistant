from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.orders.values import to_text
from ibkr_api.signals.notifications import SendInteractive, UpdateInteractive, sync_signal_status_notification
from ibkr_api.signals.values import get_signal_extra
from ibkr_compute.core.broker_mode import resolve_data_environment


NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]
ConfigValue = Callable[[str, str, str], str]
SignalChatId = Callable[[str], str]

BROKER_FINAL_STATUSES = {
    "submitted",
    "protected_active",
    "protection_incomplete",
    "executed",
    "rejected",
    "expired",
    "closed",
}


def _parse_timestamp_ms(text: Any) -> int:
    value = to_text(text)
    if not value:
        return 0
    try:
        if value.endswith("Z"):
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except Exception:
        return 0


def _signal_reference_time(record: dict[str, Any]) -> tuple[str, int]:
    extra = get_signal_extra(record)
    bar_time_ms = int(record.get("bar_time_ms") or extra.get("bar_time_ms") or 0)
    if bar_time_ms > 0:
        return "bar_time_ms", bar_time_ms
    created_ms = _parse_timestamp_ms(record.get("created"))
    if created_ms > 0:
        return "created_or_updated", created_ms
    updated_ms = _parse_timestamp_ms(record.get("updated"))
    if updated_ms > 0:
        return "created_or_updated", updated_ms
    return "", 0


def _signal_chat_id(signal_chat_id_fn: SignalChatId | None, environment: str) -> str:
    if not callable(signal_chat_id_fn):
        return ""
    try:
        return to_text(signal_chat_id_fn(environment))
    except Exception:
        return ""


def _validity_minutes(config_value: ConfigValue | None, environment: str) -> int:
    if not callable(config_value):
        return 30
    try:
        raw = int(str(config_value("signal_validity_minutes", "30", environment) or "30").strip())
    except Exception:
        raw = 30
    return raw if raw > 0 else 30


def _order_status(row: dict[str, Any]) -> str:
    return to_text((row or {}).get("status"))


def _order_role(row: dict[str, Any]) -> str:
    return to_text((row or {}).get("role")).lower()


def _has_filled_entry(orders: list[dict[str, Any]]) -> bool:
    for row in orders:
        if _order_role(row) == "entry" and _order_status(row) == "Filled":
            return True
    return False


def _has_submitted_order(orders: list[dict[str, Any]]) -> bool:
    return any(_order_status(row) in {"Submitted", "Filled"} for row in orders)


def _has_submitted_protection(orders: list[dict[str, Any]]) -> bool:
    roles = {
        _order_role(row)
        for row in orders
        if _order_status(row) == "Submitted"
    }
    return "take_profit" in roles and "stop_loss" in roles


def _repair_status_from_orders(orders: list[dict[str, Any]]) -> tuple[str, str]:
    if _has_filled_entry(orders):
        if _has_submitted_protection(orders):
            return "protected_active", "entry_filled_and_protection_submitted"
        return "submitted", "entry_filled_but_protection_not_confirmed"
    if _has_submitted_order(orders):
        return "submitted", "orders_detected_before_expiry"
    return "", ""


def _broker_execution_status(extra: dict[str, Any], broker_mode: str) -> str:
    execution_by_mode = extra.get("execution_by_mode") if isinstance(extra, dict) else {}
    if not isinstance(execution_by_mode, dict):
        return ""
    broker_payload = execution_by_mode.get(broker_mode)
    if not isinstance(broker_payload, dict):
        return ""
    return to_text(broker_payload.get("status")).lower()


def _with_broker_execution(
    extra: dict[str, Any],
    *,
    broker_mode: str,
    data_environment: str,
    status: str,
    note: str,
) -> dict[str, Any]:
    merged = dict(extra if isinstance(extra, dict) else {})
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
        "data_environment": data_environment,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "signal_expiry_check",
    }
    merged["execution_by_mode"] = execution_by_mode
    merged["broker_mode"] = broker_mode
    merged["data_environment"] = data_environment
    return merged


def _apply_notification_patch(pb: Any, row: dict[str, Any], notify_result: dict[str, Any]) -> dict[str, Any]:
    extra_patch = notify_result.get("extra_patch") if isinstance(notify_result, dict) else None
    record_id = to_text((row or {}).get("id"))
    if not record_id or not isinstance(extra_patch, dict) or not extra_patch:
        return dict(row or {})
    updated = pb.update_record("ibkr_signals", record_id, {"extra": extra_patch})
    return dict(updated) if isinstance(updated, dict) else {**dict(row or {}), "extra": extra_patch}


def build_signal_expiry_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    config_value: ConfigValue | None = None,
    send_interactive: SendInteractive | None = None,
    update_interactive: UpdateInteractive | None = None,
    signal_chat_id_fn: SignalChatId | None = None,
    console_base_url: str = "",
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment((payload or {}).get("environment"), "live")
    data_environment = resolve_data_environment(environment)
    validity_minutes = _validity_minutes(config_value, environment)
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - validity_minutes * 60 * 1000
    candidate_limit = max(1, int((payload or {}).get("limit") or 100))

    try:
        candidates = list(
            pb.get_records(
                "ibkr_signals",
                filter=(
                    '(status = "pending" || status = "awaiting_confirm") && '
                    f'environment = "{escape_filter_string(data_environment)}"'
                ),
                sort="-created",
                per_page=candidate_limit,
                page=1,
            )
            or []
        )
        expired_rows = []
        for row in candidates:
            record = dict(row) if isinstance(row, dict) else {}
            extra = get_signal_extra(record)
            if _broker_execution_status(extra, environment) in BROKER_FINAL_STATUSES:
                continue
            reference_kind, reference_ms = _signal_reference_time(record)
            if reference_ms > 0 and reference_ms <= cutoff_ms:
                expired_rows.append((record, reference_kind))

        expired_count = 0
        repaired_count = 0
        results: list[dict[str, Any]] = []
        signal_chat_id = _signal_chat_id(signal_chat_id_fn, environment)

        for row, reference_kind in expired_rows:
            signal_id = to_text(row.get("signal_id"))
            related_orders = []
            if signal_id:
                related_orders = list(
                    pb.get_records(
                        "orders",
                        filter=(
                            f'signal_id = "{escape_filter_string(signal_id)}" && '
                            f'environment = "{escape_filter_string(environment)}"'
                        ),
                        sort="-created",
                        per_page=20,
                        page=1,
                    )
                    or []
                )
            order_refs = [
                to_text(order_row.get("unique_id") or order_row.get("order_id"))
                for order_row in related_orders
                if to_text(order_row.get("unique_id") or order_row.get("order_id"))
            ]
            repair_status, repair_reason = _repair_status_from_orders(
                [dict(order_row) for order_row in related_orders if isinstance(order_row, dict)]
            )
            if repair_status:
                existing_extra = get_signal_extra(row)
                repaired_extra = _with_broker_execution(
                    {
                        **existing_extra,
                        "status_repaired_by": "signal_expiry_check",
                        "status_repaired_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "status_repair_reason": repair_reason,
                        "linked_order_unique_ids": order_refs,
                    },
                    broker_mode=environment,
                    data_environment=data_environment,
                    status=repair_status,
                    note=repair_reason,
                )
                update_payload = {
                    "extra": repaired_extra,
                    "note": repair_reason if environment == data_environment == "live" else f"{environment}:{repair_reason}",
                }
                if environment == data_environment == "live":
                    update_payload["status"] = repair_status
                updated = pb.update_record(
                    "ibkr_signals",
                    to_text(row.get("id")),
                    update_payload,
                )
                updated_row = dict(updated) if isinstance(updated, dict) else {**row, **update_payload}
                updated_row["status"] = repair_status
                updated_row["note"] = repair_reason
                notify_result = sync_signal_status_notification(
                    updated_row,
                    action=repair_status,
                    message=f"检测到关联订单，已自动修正信号状态（{len(order_refs)} 条订单）",
                    send_interactive=send_interactive,
                    signal_chat_id=signal_chat_id,
                    update_interactive=update_interactive,
                    console_base_url=console_base_url,
                )
                updated_row = _apply_notification_patch(pb, updated_row, notify_result)
                repaired_count += 1
                results.append(
                    {
                        "signal_id": signal_id,
                        "status": repair_status,
                        "action": "repaired_from_orders",
                        "repair_reason": repair_reason,
                        "linked_order_unique_ids": order_refs,
                    }
                )
                continue

            existing_extra = get_signal_extra(row)
            expired_extra = _with_broker_execution(
                {
                    **existing_extra,
                    "expired_by": "signal_expiry_check",
                    "expired_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "expiry_reference": reference_kind or "created_or_updated",
                },
                broker_mode=environment,
                data_environment=data_environment,
                status="expired",
                note="signal_expired",
            )
            update_payload = {
                "extra": expired_extra,
                "note": "signal_expired" if environment == data_environment == "live" else f"{environment}:signal_expired",
            }
            if environment == data_environment == "live":
                update_payload["status"] = "expired"
            updated = pb.update_record(
                "ibkr_signals",
                to_text(row.get("id")),
                update_payload,
            )
            updated_row = dict(updated) if isinstance(updated, dict) else {**row, **update_payload}
            updated_row["status"] = "expired"
            updated_row["note"] = "signal_expired"
            notify_result = sync_signal_status_notification(
                updated_row,
                action="expired",
                message=f"信号超时自动失效（有效期 {validity_minutes} 分钟）",
                send_interactive=send_interactive,
                signal_chat_id=signal_chat_id,
                update_interactive=update_interactive,
                console_base_url=console_base_url,
            )
            _apply_notification_patch(pb, updated_row, notify_result)
            expired_count += 1
            results.append(
                {
                    "signal_id": signal_id,
                    "status": "expired",
                    "action": "expired",
                    "expiry_reference": reference_kind or "created_or_updated",
                }
            )

        return (
            {
                "ok": True,
                "environment": environment,
                "broker_mode": environment,
                "data_environment": data_environment,
                "validity_minutes": validity_minutes,
                "candidate_count": len(candidates),
                "expired_candidate_count": len(expired_rows),
                "expired_count": expired_count,
                "repaired_to_order_status_count": repaired_count,
                "repaired_to_executed_count": 0,
                "results": results,
                "source": "ibkr-api",
            },
            200,
        )
    except Exception as exc:
        return {"ok": False, "environment": environment, "error": str(exc), "source": "ibkr-api"}, 500
