from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, parse_boolean, to_float, to_text
from ibkr_api.signals.notifications import (
    SendInteractive,
    UpdateInteractive,
    send_signal_notification,
    sync_signal_status_notification,
)
from ibkr_api.signals.values import get_signal_extra


NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]
ConfigValue = Callable[[str, str, str], str]
SignalChatId = Callable[[str], str]

FINAL_SIGNAL_STATUSES = {"executed", "rejected", "expired", "closed"}
VOLATILE_COMPARE_KEYS = {
    "computed_at_ms": True,
    "computed_at_us": True,
    "computed_at_cn": True,
}


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_number_or_default(value: Any, default: float = 0) -> float:
    parsed = to_float(value)
    return parsed if parsed is not None else float(default)


def _to_int_or_none(value: Any) -> int | None:
    parsed = to_float(value)
    return int(parsed) if parsed is not None else None


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


def normalize_signal_source_meta(raw_value: Any) -> dict[str, str]:
    raw = to_text(raw_value).lower()
    if raw in {"tv", "tradingview", "webhook_tv", "tv_webhook", "signal"}:
        return {
            "route_source": "tradingview",
            "signal_source": "tradingview_webhook",
            "signal_source_label": "TradingView Webhook",
            "signal_source_detail": "来自 TradingView webhook 信号",
        }
    if raw in {"ibkr_compute_timeline", "timeline", "chart_timeline"}:
        return {
            "route_source": "ibkr_compute",
            "signal_source": "ibkr_compute_timeline",
            "signal_source_label": "IBKR 图表回放",
            "signal_source_detail": "来自缓存 bars 时间线重算",
        }
    if raw in {"history_repair", "recompute", "backfill_recompute"}:
        return {
            "route_source": "ibkr_compute",
            "signal_source": "ibkr_history_recompute",
            "signal_source_label": "IBKR 历史重算",
            "signal_source_detail": "来自历史回补/重算链路",
        }
    if raw in {"manual", "manual_order", "runtime_page", "account_page"}:
        return {
            "route_source": "manual",
            "signal_source": "manual_order",
            "signal_source_label": "手动触发",
            "signal_source_detail": "来自账户页/人工操作",
        }
    if raw in {"ibkr_runtime", "ibkr_compute_realtime", "ibkr_compute", "ibkr"}:
        return {
            "route_source": "ibkr_compute",
            "signal_source": "ibkr_compute_realtime",
            "signal_source_label": "IBKR 实时计算",
            "signal_source_detail": "来自 IBKR 实盘 bars 收盘计算",
        }
    return {
        "route_source": raw or "unknown",
        "signal_source": raw or "unknown",
        "signal_source_label": raw.upper() if raw else "未知来源",
        "signal_source_detail": "",
    }


def build_signal_record_payload(payload: dict[str, Any], environment: str) -> tuple[dict[str, Any] | None, str]:
    symbol = to_text(payload.get("symbol")).upper()
    signal_id = to_text(payload.get("signal_id"))
    incoming_extra = _as_object(payload.get("extra"))
    if not symbol or not signal_id:
        return None, "Missing symbol or signal_id"

    source_meta = normalize_signal_source_meta(
        first_defined(
            payload.get("signal_source"),
            incoming_extra.get("signal_source"),
            payload.get("source"),
            incoming_extra.get("source"),
            "ibkr_compute",
        )
    )
    record = {
        "symbol": symbol,
        "environment": environment,
        "direction": to_text(payload.get("direction")).lower(),
        "signal": to_text(payload.get("signal")),
        "limit_price": _to_number_or_default(payload.get("limit_price"), 0),
        "entry": _to_number_or_default(payload.get("entry"), 0),
        "stop_loss": _to_number_or_default(payload.get("stop_loss"), 0),
        "take_profit": _to_number_or_default(payload.get("take_profit"), 0),
        "rr": to_text(payload.get("rr")),
        "shares": _to_number_or_default(payload.get("shares"), 0),
        "signal_id": signal_id,
        "exchange": to_text(payload.get("exchange")).upper(),
        "interval": to_text(payload.get("interval")),
        "reason": to_text(payload.get("reason")),
        "us_time": to_text(payload.get("us_time")),
        "cn_time": to_text(payload.get("cn_time")),
        "date": to_text(payload.get("date")),
        "bar_time_ms": int(_to_number_or_default(payload.get("bar_time_ms"), 0)),
        "bar_index": _to_int_or_none(payload.get("bar_index")),
        "script_tag": to_text(payload.get("script_tag")),
        "chart_tf": to_text(payload.get("chart_tf")),
        "extra": {
            **incoming_extra,
            "source": to_text(first_defined(incoming_extra.get("source"), source_meta.get("route_source"))),
            "signal_source": to_text(
                first_defined(incoming_extra.get("signal_source"), payload.get("signal_source"), source_meta.get("signal_source"))
            ),
            "signal_source_label": to_text(
                first_defined(
                    incoming_extra.get("signal_source_label"),
                    payload.get("signal_source_label"),
                    source_meta.get("signal_source_label"),
                )
            ),
            "signal_source_detail": to_text(
                first_defined(
                    incoming_extra.get("signal_source_detail"),
                    payload.get("signal_source_detail"),
                    source_meta.get("signal_source_detail"),
                )
            ),
            "environment": environment,
        },
        "status": to_text(payload.get("status") or "pending").lower() or "pending",
        "note": to_text(payload.get("note")),
    }
    return record, ""


def first_defined(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def build_signal_bar_dedupe_key(data: dict[str, Any], environment: str) -> str:
    symbol = to_text(data.get("symbol")).upper()
    direction = to_text(data.get("direction")).lower()
    bar_time_ms = int(_to_number_or_default(data.get("bar_time_ms"), 0))
    interval = to_text(data.get("interval"))
    chart_tf = to_text(data.get("chart_tf"))
    script_tag = to_text(data.get("script_tag"))
    runtime_environment = to_text(environment or data.get("environment") or "live").lower() or "live"
    if not symbol or not direction or not bar_time_ms:
        return ""
    return "|".join(
        [
            runtime_environment,
            symbol,
            direction,
            str(bar_time_ms),
            interval or "-",
            chart_tf or "-",
            script_tag or "-",
        ]
    )


def find_signal_duplicate_by_bar_key(
    pb: Any,
    data: dict[str, Any],
    environment: str,
    *,
    exclude_signal_id: str,
    escape_filter_string: EscapeFilterString,
) -> dict[str, Any] | None:
    symbol = to_text(data.get("symbol")).upper()
    direction = to_text(data.get("direction")).lower()
    bar_time_ms = int(_to_number_or_default(data.get("bar_time_ms"), 0))
    runtime_environment = to_text(environment or data.get("environment") or "live").lower() or "live"
    if not symbol or not direction or not bar_time_ms:
        return None

    filters = [
        f'environment = "{escape_filter_string(runtime_environment)}"',
        f'symbol = "{escape_filter_string(symbol)}"',
        f'direction = "{escape_filter_string(direction)}"',
        f"bar_time_ms = {bar_time_ms}",
    ]
    interval = to_text(data.get("interval"))
    chart_tf = to_text(data.get("chart_tf"))
    script_tag = to_text(data.get("script_tag"))
    signal_id = to_text(exclude_signal_id or data.get("signal_id"))
    if interval:
        filters.append(f'interval = "{escape_filter_string(interval)}"')
    if chart_tf:
        filters.append(f'chart_tf = "{escape_filter_string(chart_tf)}"')
    if script_tag:
        filters.append(f'script_tag = "{escape_filter_string(script_tag)}"')
    if signal_id:
        filters.append(f'signal_id != "{escape_filter_string(signal_id)}"')

    rows = pb.get_records(
        "ibkr_signals",
        filter=" && ".join(filters),
        sort="-updated,-created",
        per_page=5,
        page=1,
    )
    return dict(rows[0]) if rows else None


def annotate_signal_duplicate(
    pb: Any,
    record: dict[str, Any],
    incoming_data: dict[str, Any],
    environment: str,
) -> dict[str, Any]:
    extra = get_signal_extra(record)
    duplicate_signal_ids = list(extra.get("duplicate_signal_ids") or []) if isinstance(extra.get("duplicate_signal_ids"), list) else []
    incoming_signal_id = to_text(incoming_data.get("signal_id"))
    if incoming_signal_id and incoming_signal_id not in duplicate_signal_ids:
        duplicate_signal_ids.append(incoming_signal_id)

    incoming_extra = ensure_object(incoming_data.get("extra"))
    source_meta = normalize_signal_source_meta(
        first_defined(
            incoming_data.get("signal_source"),
            incoming_extra.get("signal_source"),
            incoming_data.get("source"),
            incoming_extra.get("source"),
        )
    )
    patch_extra = {
        **extra,
        "duplicate_signal_ids": duplicate_signal_ids,
        "duplicate_signal_count": len(duplicate_signal_ids),
        "last_duplicate_signal_id": incoming_signal_id,
        "last_duplicate_signal_at": _now_iso_utc(),
        "last_duplicate_signal_source": to_text(
            first_defined(incoming_extra.get("signal_source"), incoming_data.get("signal_source"), source_meta.get("signal_source"))
        ),
        "last_duplicate_signal_source_label": to_text(
            first_defined(
                incoming_extra.get("signal_source_label"),
                incoming_data.get("signal_source_label"),
                source_meta.get("signal_source_label"),
            )
        ),
        "duplicate_bar_dedupe_key": build_signal_bar_dedupe_key(incoming_data, environment),
    }
    record_id = to_text(record.get("id"))
    if record_id:
        updated = pb.update_record("ibkr_signals", record_id, {"extra": patch_extra})
        return dict(updated) if isinstance(updated, dict) else {**record, "extra": patch_extra}
    return {**record, "extra": patch_extra}


def prepare_signal_lifecycle(
    signal_payload: dict[str, Any],
    existing_row: dict[str, Any] | None,
    *,
    manual_confirm_enabled: bool,
) -> dict[str, str]:
    existing = dict(existing_row or {})
    existing_status = to_text(existing.get("status")).lower()
    existing_note = to_text(existing.get("note"))
    existing_extra = get_signal_extra(existing)
    incoming_status = to_text(signal_payload.get("status") or "pending").lower() or "pending"
    incoming_note = to_text(signal_payload.get("note"))

    resolved_status = incoming_status
    resolved_note = incoming_note
    if existing_status in FINAL_SIGNAL_STATUSES:
        resolved_status = existing_status
        resolved_note = existing_note or incoming_note
    elif manual_confirm_enabled:
        if existing_status == "pending":
            resolved_status = "pending"
            resolved_note = existing_note or incoming_note
        elif incoming_status in {"pending", "awaiting_confirm"}:
            resolved_status = "awaiting_confirm"
            resolved_note = incoming_note or "manual_confirmation_required"
    elif incoming_status in {"pending", "awaiting_confirm"}:
        resolved_status = "pending"
        resolved_note = "" if incoming_note == "manual_confirmation_required" else incoming_note

    signal_payload["status"] = resolved_status
    signal_payload["note"] = resolved_note
    signal_payload["extra"] = {
        **existing_extra,
        **ensure_object(signal_payload.get("extra")),
        "signal_confirmation_required": bool(manual_confirm_enabled),
        "signal_confirmation_mode": "manual" if manual_confirm_enabled else "auto",
    }
    if resolved_note:
        signal_payload["extra"]["status_reason"] = resolved_note

    return {"previous_status": existing_status, "next_status": resolved_status}


def _record_needs_update(record: dict[str, Any], next_payload: dict[str, Any]) -> bool:
    return any(not _values_equal(record.get(key), next_payload.get(key)) for key in next_payload)


def upsert_signal_record(pb: Any, existing_row: dict[str, Any] | None, next_payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    existing = dict(existing_row or {})
    if existing.get("id"):
        if not _record_needs_update(existing, next_payload):
            return existing, "skipped"
        updated = pb.update_record("ibkr_signals", to_text(existing.get("id")), next_payload)
        return dict(updated) if isinstance(updated, dict) else {**existing, **next_payload}, "updated"
    created = pb.create_record("ibkr_signals", next_payload)
    return dict(created) if isinstance(created, dict) else {**next_payload}, "created"


def _sync_signal_notification_after_upsert(
    record: dict[str, Any],
    *,
    previous_status: str,
    send_interactive: SendInteractive | None,
    update_interactive: UpdateInteractive | None,
    signal_chat_id: str,
    console_base_url: str,
) -> dict[str, Any]:
    current_status = to_text(record.get("status")).lower()
    if current_status not in {"awaiting_confirm", "pending", "rejected"}:
        return {}
    extra = get_signal_extra(record)
    message_id = to_text(extra.get("feishu_signal_message_id"))
    if current_status == "rejected":
        return sync_signal_status_notification(
            record,
            action="rejected",
            message="信号已拒绝",
            send_interactive=send_interactive,
            signal_chat_id=signal_chat_id,
            update_interactive=update_interactive,
            console_base_url=console_base_url,
        )
    if not message_id:
        return send_signal_notification(
            record,
            send_interactive=send_interactive,
            signal_chat_id=signal_chat_id,
            console_base_url=console_base_url,
        )
    if previous_status != current_status:
        return sync_signal_status_notification(
            record,
            action=current_status,
            message="等待人工确认" if current_status == "awaiting_confirm" else "信号已确认，等待执行",
            send_interactive=send_interactive,
            signal_chat_id=signal_chat_id,
            update_interactive=update_interactive,
            console_base_url=console_base_url,
        )
    return {}


def _signal_chat_id(signal_chat_id_fn: SignalChatId | None, environment: str) -> str:
    if callable(signal_chat_id_fn):
        try:
            return to_text(signal_chat_id_fn(environment))
        except Exception:
            return ""
    return ""


def _manual_confirm_enabled(config_value: ConfigValue | None, environment: str) -> bool:
    if not callable(config_value):
        return True
    try:
        value = config_value("signal_manual_confirm_enabled", "true", environment)
    except Exception:
        return True
    return parse_boolean(value, True)


def build_signal_ingest_response(
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
    prepared, error = build_signal_record_payload(payload or {}, environment)
    if not prepared:
        return {"ok": False, "error": error or "invalid_signal_payload"}, 400

    try:
        existing = pb.get_first_record(
            "ibkr_signals",
            filter=(
                f'signal_id = "{escape_filter_string(prepared["signal_id"])}" && '
                f'environment = "{escape_filter_string(environment)}"'
            ),
        )
        existing_row = dict(existing) if isinstance(existing, dict) else None
        if not existing_row:
            duplicate = find_signal_duplicate_by_bar_key(
                pb,
                prepared,
                environment,
                exclude_signal_id=prepared["signal_id"],
                escape_filter_string=escape_filter_string,
            )
            if duplicate:
                duplicate_row = annotate_signal_duplicate(pb, duplicate, prepared, environment)
                return (
                    {
                        "ok": True,
                        "signal_id": to_text(duplicate_row.get("signal_id") or prepared["signal_id"]),
                        "duplicate_signal_id": prepared["signal_id"],
                        "target": "ibkr_signals",
                        "id": to_text(duplicate_row.get("id")),
                        "action": "skipped_duplicate_bar_signal",
                        "status": to_text(duplicate_row.get("status")),
                        "dedupe_key": build_signal_bar_dedupe_key(prepared, environment),
                    },
                    200,
                )

        lifecycle = prepare_signal_lifecycle(
            prepared,
            existing_row,
            manual_confirm_enabled=_manual_confirm_enabled(config_value, environment),
        )
        saved_row, action = upsert_signal_record(pb, existing_row, prepared)
        if action != "skipped":
            notify_result = _sync_signal_notification_after_upsert(
                saved_row,
                previous_status=lifecycle["previous_status"],
                send_interactive=send_interactive,
                update_interactive=update_interactive,
                signal_chat_id=_signal_chat_id(signal_chat_id_fn, environment),
                console_base_url=console_base_url,
            )
            extra_patch = notify_result.get("extra_patch") if isinstance(notify_result, dict) else None
            if isinstance(extra_patch, dict) and extra_patch and to_text(saved_row.get("id")):
                saved_row = pb.update_record("ibkr_signals", to_text(saved_row.get("id")), {"extra": extra_patch})
                saved_row = dict(saved_row) if isinstance(saved_row, dict) else {**prepared, "extra": extra_patch}
        return (
            {
                "ok": True,
                "signal_id": prepared["signal_id"],
                "target": "ibkr_signals",
                "id": to_text(saved_row.get("id")),
                "action": action,
                "status": to_text(saved_row.get("status") or prepared["status"]),
            },
            200,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "target": "ibkr_signals"}, 500


def build_signals_ingest_response(
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
    request_payload = payload or {}
    items = request_payload.get("items") if isinstance(request_payload.get("items"), list) else []
    default_environment = normalize_environment(request_payload.get("environment"), "live")
    if not items:
        return {"ok": False, "error": "Empty signals array"}, 400

    created = 0
    updated = 0
    skipped = 0
    duplicates = 0
    errors = 0
    for item in items:
        try:
            environment = normalize_environment((item or {}).get("environment"), default_environment)
            prepared, error = build_signal_record_payload(item or {}, environment)
            if not prepared:
                errors += 1
                continue
            existing = pb.get_first_record(
                "ibkr_signals",
                filter=(
                    f'signal_id = "{escape_filter_string(prepared["signal_id"])}" && '
                    f'environment = "{escape_filter_string(environment)}"'
                ),
            )
            existing_row = dict(existing) if isinstance(existing, dict) else None
            if not existing_row:
                duplicate = find_signal_duplicate_by_bar_key(
                    pb,
                    prepared,
                    environment,
                    exclude_signal_id=prepared["signal_id"],
                    escape_filter_string=escape_filter_string,
                )
                if duplicate:
                    annotate_signal_duplicate(pb, duplicate, prepared, environment)
                    duplicates += 1
                    skipped += 1
                    continue

            lifecycle = prepare_signal_lifecycle(
                prepared,
                existing_row,
                manual_confirm_enabled=_manual_confirm_enabled(config_value, environment),
            )
            saved_row, action = upsert_signal_record(pb, existing_row, prepared)
            if action == "created":
                created += 1
            elif action == "updated":
                updated += 1
            else:
                skipped += 1

            if action != "skipped":
                notify_result = _sync_signal_notification_after_upsert(
                    saved_row,
                    previous_status=lifecycle["previous_status"],
                    send_interactive=send_interactive,
                    update_interactive=update_interactive,
                    signal_chat_id=_signal_chat_id(signal_chat_id_fn, environment),
                    console_base_url=console_base_url,
                )
                extra_patch = notify_result.get("extra_patch") if isinstance(notify_result, dict) else None
                if isinstance(extra_patch, dict) and extra_patch and to_text(saved_row.get("id")):
                    pb.update_record("ibkr_signals", to_text(saved_row.get("id")), {"extra": extra_patch})
        except Exception:
            errors += 1

    return (
        {
            "ok": errors == 0,
            "received": len(items),
            "success": created + updated,
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "duplicates": duplicates,
            "errors": errors,
            "target": "ibkr_signals",
        },
        200,
    )
