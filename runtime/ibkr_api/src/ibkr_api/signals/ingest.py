from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.values import parse_boolean, to_text
from ibkr_api.signals.ingest_active_policy import (
    BROKER_CONTROLLED_SIGNAL_STATUSES,
    MUTABLE_SIGNAL_STATUSES,
    PROTECTION_BLOCK_SIGNAL_STATUSES,
    STRONG_REVERSE_SCORE,
    build_active_signal_refresh_payload,
    build_reverse_record_payload,
    build_reverse_suppressed_patch,
    build_same_direction_followup_patch,
    build_superseded_patch,
    calculate_signal_strength,
    find_active_symbol_signal,
)
from ibkr_api.signals.ingest_dedupe import (
    annotate_signal_duplicate,
    build_signal_bar_dedupe_key,
    find_signal_duplicate_by_bar_key,
)
from ibkr_api.signals.ingest_lifecycle import prepare_signal_lifecycle
from ibkr_api.signals.ingest_payloads import build_signal_record_payload
from ibkr_api.signals.ingest_store import upsert_signal_record
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


def _apply_signal_strength(prepared: dict[str, Any]) -> dict[str, Any]:
    strength = calculate_signal_strength(prepared)
    extra = get_signal_extra(prepared)
    prepared["extra"] = {
        **extra,
        "signal_strength_score": strength["score"],
        "signal_strength_level": strength["level"],
        "signal_strength_source": strength["source"],
    }
    if strength.get("triggered_signals") and "triggered_signals" not in prepared["extra"]:
        prepared["extra"]["triggered_signals"] = list(strength.get("triggered_signals") or [])
    return prepared


def _update_signal_row(pb: Any, record: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    record_id = to_text(record.get("id"))
    if not record_id:
        return {**record, **patch}
    updated = pb.update_record("ibkr_signals", record_id, patch)
    return dict(updated) if isinstance(updated, dict) else {**record, **patch}


def _create_or_update_reverse_record(pb: Any, payload: dict[str, Any], escape_filter_string: EscapeFilterString) -> dict[str, Any]:
    from ibkr_api.reverse.repository import upsert_reverse_record

    result = upsert_reverse_record(pb, payload, escape_filter=escape_filter_string)
    record = result.get("record") if isinstance(result, dict) else None
    return dict(record) if isinstance(record, dict) else {}


def _sync_refreshed_signal_card(
    record: dict[str, Any],
    *,
    send_interactive: SendInteractive | None,
    update_interactive: UpdateInteractive | None,
    signal_chat_id: str,
    console_base_url: str,
) -> dict[str, Any]:
    extra = get_signal_extra(record)
    if not to_text(extra.get("feishu_signal_message_id")):
        return {}
    return sync_signal_status_notification(
        record,
        action="refreshed",
        message="同向新信号已合并，入场/止盈/止损已刷新",
        send_interactive=None,
        signal_chat_id=signal_chat_id,
        update_interactive=update_interactive,
        console_base_url=console_base_url,
    )


def _handle_active_symbol_policy(
    pb: Any,
    *,
    prepared: dict[str, Any],
    environment: str,
    escape_filter_string: EscapeFilterString,
    send_interactive: SendInteractive | None,
    update_interactive: UpdateInteractive | None,
    signal_chat_id: str,
    console_base_url: str,
) -> tuple[dict[str, Any] | None, int | None, dict[str, Any]]:
    active = find_active_symbol_signal(
        pb,
        prepared,
        environment,
        escape_filter_string=escape_filter_string,
    )
    if not active:
        return None, None, prepared

    incoming_direction = to_text(prepared.get("direction")).lower()
    active_direction = to_text(active.get("direction")).lower()
    active_status = to_text(active.get("status")).lower()
    incoming_signal_id = to_text(prepared.get("signal_id"))
    active_signal_id = to_text(active.get("signal_id"))

    if incoming_direction and incoming_direction == active_direction:
        if active_status in MUTABLE_SIGNAL_STATUSES:
            refresh_payload = build_active_signal_refresh_payload(active, prepared, environment)
            saved_row = _update_signal_row(pb, active, refresh_payload)
            notify_result = _sync_refreshed_signal_card(
                saved_row,
                send_interactive=send_interactive,
                update_interactive=update_interactive,
                signal_chat_id=signal_chat_id,
                console_base_url=console_base_url,
            )
            extra_patch = notify_result.get("extra_patch") if isinstance(notify_result, dict) else None
            if isinstance(extra_patch, dict) and extra_patch and to_text(saved_row.get("id")):
                saved_row = _update_signal_row(pb, saved_row, {"extra": extra_patch})
            return (
                {
                    "ok": True,
                    "signal_id": active_signal_id,
                    "merged_signal_id": incoming_signal_id,
                    "target": "ibkr_signals",
                    "id": to_text(saved_row.get("id")),
                    "action": "refreshed_active_signal",
                    "status": to_text(saved_row.get("status")),
                },
                200,
                prepared,
            )

        saved_row = _update_signal_row(pb, active, build_same_direction_followup_patch(active, prepared))
        return (
            {
                "ok": True,
                "signal_id": active_signal_id,
                "followup_signal_id": incoming_signal_id,
                "target": "ibkr_signals",
                "id": to_text(saved_row.get("id")),
                "action": "suppressed_same_direction_followup",
                "status": to_text(saved_row.get("status")),
            },
            200,
            prepared,
        )

    strength = calculate_signal_strength(prepared)
    prepared = _apply_signal_strength(prepared)
    if active_status in PROTECTION_BLOCK_SIGNAL_STATUSES:
        saved_row = _update_signal_row(
            pb,
            active,
            build_reverse_suppressed_patch(active, prepared, reason="protection_incomplete_blocks_auto_reverse"),
        )
        return (
            {
                "ok": True,
                "signal_id": active_signal_id,
                "reverse_signal_id": incoming_signal_id,
                "target": "ibkr_signals",
                "id": to_text(saved_row.get("id")),
                "action": "blocked_reverse_protection_incomplete",
                "status": to_text(saved_row.get("status")),
                "signal_strength_score": strength["score"],
                "signal_strength_level": strength["level"],
            },
            200,
            prepared,
        )

    if float(strength.get("score") or 0.0) < STRONG_REVERSE_SCORE:
        saved_row = _update_signal_row(
            pb,
            active,
            build_reverse_suppressed_patch(active, prepared, reason="reverse_signal_below_strong_threshold"),
        )
        return (
            {
                "ok": True,
                "signal_id": active_signal_id,
                "reverse_signal_id": incoming_signal_id,
                "target": "ibkr_signals",
                "id": to_text(saved_row.get("id")),
                "action": "suppressed_weak_reverse_signal",
                "status": to_text(saved_row.get("status")),
                "signal_strength_score": strength["score"],
                "signal_strength_level": strength["level"],
            },
            200,
            prepared,
        )

    if active_status in MUTABLE_SIGNAL_STATUSES:
        _update_signal_row(pb, active, build_superseded_patch(active, prepared))
        prepared["extra"] = {
            **get_signal_extra(prepared),
            "reverse_policy": "supersede_unsubmitted_signal",
            "reverse_source_signal_id": active_signal_id,
        }
        return None, None, prepared

    if active_status in BROKER_CONTROLLED_SIGNAL_STATUSES:
        reverse_payload = build_reverse_record_payload(active, prepared, environment)
        reverse_record = _create_or_update_reverse_record(pb, reverse_payload, escape_filter_string)
        active_extra = get_signal_extra(active)
        reverse_id = to_text(reverse_record.get("id"))
        saved_row = _update_signal_row(
            pb,
            active,
            {
                "extra": {
                    **active_extra,
                    "reverse_policy": "full_auto_reverse",
                    "reverse_signal_id": reverse_id,
                    "latest_reverse_source_signal_id": incoming_signal_id,
                    "latest_reverse_queued_at": reverse_payload.get("us_time") or "",
                    "signal_strength_score": strength["score"],
                    "signal_strength_level": strength["level"],
                    "suppressed_reason": "queued_full_auto_reverse_before_reentry",
                }
            },
        )
        return (
            {
                "ok": True,
                "signal_id": active_signal_id,
                "reverse_source_signal_id": incoming_signal_id,
                "reverse_id": reverse_id,
                "target": "ibkr_reverse_signals",
                "id": to_text(saved_row.get("id")),
                "action": "queued_full_auto_reverse",
                "status": to_text(saved_row.get("status")),
                "signal_strength_score": strength["score"],
                "signal_strength_level": strength["level"],
            },
            200,
            prepared,
        )

    return None, None, prepared


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
    prepared = _apply_signal_strength(prepared)

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

            policy_response, policy_status, prepared = _handle_active_symbol_policy(
                pb,
                prepared=prepared,
                environment=environment,
                escape_filter_string=escape_filter_string,
                send_interactive=send_interactive,
                update_interactive=update_interactive,
                signal_chat_id=_signal_chat_id(signal_chat_id_fn, environment),
                console_base_url=console_base_url,
            )
            if policy_response is not None and policy_status is not None:
                return policy_response, policy_status

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
            prepared = _apply_signal_strength(prepared)
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

                policy_response, _policy_status, prepared = _handle_active_symbol_policy(
                    pb,
                    prepared=prepared,
                    environment=environment,
                    escape_filter_string=escape_filter_string,
                    send_interactive=send_interactive,
                    update_interactive=update_interactive,
                    signal_chat_id=_signal_chat_id(signal_chat_id_fn, environment),
                    console_base_url=console_base_url,
                )
                if policy_response is not None:
                    action_name = to_text(policy_response.get("action"))
                    if action_name == "refreshed_active_signal":
                        updated += 1
                    else:
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
