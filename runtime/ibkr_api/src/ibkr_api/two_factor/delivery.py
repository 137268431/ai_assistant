from __future__ import annotations

import json
from typing import Any, Callable

from ibkr_api.two_factor.messages import (
    build_card_summary,
    build_confirm_deadline_note,
    build_waiting_response_prompt,
    build_weekly_reminder_deadline_note,
    card_meta_for_status,
    get_active_cycle_primary_label,
    manual_auth_reason_label,
)
from ibkr_api.two_factor.state import is_active_status, is_current_cycle_active_status


CARD_UPDATE_COOLDOWN_MS = 15000
DEFAULT_TWO_FA_CHAT_ID = "oc_c48c10447685e80cfea0c003864aa51f"
NormalizeEnvironment = Callable[[Any, str], str]
ConfigValue = Callable[[str, str, str], str]
SendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
UpdateInteractive = Callable[[str, dict[str, Any], str], dict[str, Any]]


def resolve_two_factor_chat_id(environment: str, *, config_value: ConfigValue) -> str:
    return str(config_value("system_2fa_chat_id", DEFAULT_TWO_FA_CHAT_ID, environment) or DEFAULT_TWO_FA_CHAT_ID).strip()


def build_delivery_fingerprint(state_data: dict[str, Any]) -> str:
    fields = {
        key: state_data.get(key)
        for key in (
            "status",
            "reason",
            "message",
            "last_result",
            "last_error",
            "mode",
            "challenge_code",
            "response_status",
            "challenge_feedback",
            "operator_action",
            "reset_recommended",
            "reset_reason",
            "recovery_phase",
            "manual_takeover_active",
            "probe_result",
            "requested_at",
            "triggered_at",
            "result_at",
            "business_deadline_at",
            "business_deadline_cn",
            "business_deadline_overdue",
            "confirm_deadline_at",
            "confirm_deadline_cn",
            "confirm_deadline_overdue",
            "confirm_window_seconds",
            "restarted_from_active_cycle",
            "previous_cycle",
            "detail",
        )
    }
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)


def _open_button(label: str, url: str, button_type: str = "default") -> dict[str, Any]:
    return {
        "tag": "button",
        "type": button_type,
        "width": "fill",
        "text": {"tag": "plain_text", "content": label},
        "multi_url": {"url": url, "pc_url": url, "ios_url": url, "android_url": url},
    }


def _request_button(label: str, callback_url: str, environment: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "type": "primary",
        "width": "fill",
        "text": {"tag": "plain_text", "content": label},
        "action_type": "request",
        "url": callback_url,
        "value": {"action": "ibkr_2fa_start", "environment": environment, "force_restart": False},
    }


def build_two_factor_card(
    state_data: dict[str, Any],
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    console_base_url: str,
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    state = dict(state_data or {})
    meta = card_meta_for_status(state.get("status"))
    summary = build_card_summary(state, meta["summary"])
    reason_label = manual_auth_reason_label(state.get("reason") or state.get("recovery_reason"))
    runtime_url = f"{console_base_url}/ibkr_runtime.html?environment={runtime_environment}" if console_base_url else ""
    system_url = f"{console_base_url}/ibkr_system.html?environment={runtime_environment}" if console_base_url else ""
    callback_url = f"{console_base_url}/webhook/feishu/callback" if console_base_url else ""
    broker_badge = f"Broker {runtime_environment.upper()}" if runtime_environment in {"live", "paper"} else runtime_environment.upper()

    meta_lines = [
        f"**Broker**: {broker_badge}",
        f"**当前用途**: {reason_label}",
        f"**状态**: {meta['emoji']} {state.get('status') or 'requested'}",
        f"**请求时间**: {state.get('requested_at') or '-'}",
    ]
    if state.get("triggered_at"):
        meta_lines.append(f"**触发时间**: {state['triggered_at']}")
    if state.get("result_at"):
        meta_lines.append(f"**结果时间**: {state['result_at']}")
    if state.get("challenge_code"):
        meta_lines.append(f"**Challenge**: {state['challenge_code']}")
    if state.get("response_status"):
        meta_lines.append(f"**响应状态**: {state['response_status']}")
    if state.get("challenge_feedback"):
        meta_lines.append(f"**Gateway反馈**: {state['challenge_feedback']}")
    if state.get("recovery_phase"):
        meta_lines.append(f"**恢复阶段**: {state['recovery_phase']}")
    if state.get("confirm_deadline_cn"):
        meta_lines.append(f"**本轮截止**: {state['confirm_deadline_cn']} CN / {state.get('confirm_deadline_at') or '-'} US")
    if state.get("business_deadline_cn"):
        meta_lines.append(f"**周验证截止**: {state['business_deadline_cn']} CN / {state.get('business_deadline_at') or '-'} US")
    if state.get("last_result"):
        meta_lines.append(f"**反馈**: {state['last_result']}")
    if state.get("last_error"):
        meta_lines.append(f"**异常**: {state['last_error']}")

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**说明**: {summary}"},
        {"tag": "hr"},
        {"tag": "markdown", "content": "\n".join(meta_lines)},
    ]
    detail = state.get("detail") if isinstance(state.get("detail"), dict) else {}
    if detail:
        detail_lines = [f"**{key}**: {value}" for key, value in detail.items()]
        elements.extend([
            {"tag": "hr"},
            {"tag": "markdown", "content": "\n".join(detail_lines)},
        ])
    if state.get("status") == "waiting_response":
        elements.extend([
            {"tag": "hr"},
            {"tag": "markdown", "content": build_waiting_response_prompt(state)},
        ])
    weekly_note = build_weekly_reminder_deadline_note(state)
    if weekly_note:
        elements.extend([
            {"tag": "hr"},
            {"tag": "markdown", "content": weekly_note},
        ])
    confirm_note = build_confirm_deadline_note(state)
    if confirm_note:
        elements.extend([
            {"tag": "hr"},
            {"tag": "markdown", "content": confirm_note},
        ])

    actions: list[dict[str, Any]] = []
    if str(state.get("status") or "") != "success":
        if is_current_cycle_active_status(state.get("status")) and runtime_url:
            actions.append(_open_button(get_active_cycle_primary_label(state), runtime_url, "primary"))
        elif callback_url:
            actions.append(_request_button(meta["button"], callback_url, runtime_environment))
    if runtime_url:
        actions.append(_open_button("查看 Runtime", runtime_url))
    if system_url:
        actions.append(_open_button("查看 System", system_url))
    if actions:
        elements.extend([
            {"tag": "hr"},
            {"tag": "action", "actions": actions},
        ])
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{meta['emoji']} [{broker_badge}] {meta['title']} · {reason_label}"},
            "template": meta["template"],
        },
        "elements": elements,
    }


def _persist_delivery_state(pb: Any, environment: str, date: str, state_data: dict[str, Any]) -> dict[str, Any]:
    try:
        return pb.upsert_state("ibkr_2fa", environment, state_data, date=date)
    except Exception:
        return {"data": dict(state_data)}


def deliver_two_factor_card(
    saved_state: dict[str, Any],
    *,
    pb: Any,
    normalize_environment: NormalizeEnvironment,
    console_base_url: str,
    config_value: ConfigValue,
    send_interactive: SendInteractive,
    update_interactive: UpdateInteractive,
    force_new: bool = False,
    bypass_throttle: bool = False,
) -> dict[str, Any]:
    environment = normalize_environment(saved_state.get("environment"), "live")
    date = str(saved_state.get("date") or "global")
    state_data = dict(saved_state.get("data") or {})
    card = build_two_factor_card(
        state_data,
        environment,
        normalize_environment=normalize_environment,
        console_base_url=str(console_base_url or "").rstrip("/"),
    )
    message_id = str(state_data.get("message_id") or "")
    fingerprint = build_delivery_fingerprint(state_data)
    last_delivered_ms = int(state_data.get("last_delivered_ms") or 0)
    same_payload = (
        str(state_data.get("last_delivered_hash") or "") == fingerprint
        and str(state_data.get("last_delivered_status") or "") == str(state_data.get("status") or "")
    )
    if (
        message_id
        and not force_new
        and not bypass_throttle
        and is_active_status(state_data.get("status"))
        and same_payload
        and last_delivered_ms > 0
    ):
        from time import time

        if int(time() * 1000) - last_delivered_ms < CARD_UPDATE_COOLDOWN_MS:
            return {
                "ok": True,
                "skipped": True,
                "skipped_reason": "cooldown",
                "environment": environment,
                "date": date,
                "card": card,
                "message_id": message_id,
                "data": state_data,
                "result": {"success": True, "skipped": True, "reason": "cooldown"},
            }

    result: dict[str, Any]
    delivery_mode = "send"
    chat_id = resolve_two_factor_chat_id(environment, config_value=config_value)
    if message_id and not force_new:
        result = dict(update_interactive(message_id, card, environment) or {})
        delivery_mode = "update"
        if not bool(result.get("success")):
            result = dict(send_interactive(card, chat_id, environment) or {})
            delivery_mode = "replace"
    else:
        result = dict(send_interactive(card, chat_id, environment) or {})
    delivered_message_id = str(result.get("message_id") or message_id)
    success = bool(result.get("success"))
    patch = {
        "message_id": delivered_message_id,
        "last_delivered_hash": fingerprint,
        "last_delivered_status": str(state_data.get("status") or ""),
        "last_delivery_mode": delivery_mode,
        "last_delivery_error": "" if success else str(result.get("error") or "send_failed"),
    }
    if success:
        from time import time

        now_ms = int(time() * 1000)
        patch.update({
            "last_delivered_ms": now_ms,
            "last_delivered_at": state_data.get("updated_at") or "",
        })
    next_state = {**state_data, **patch}
    persisted = _persist_delivery_state(pb, environment, date, next_state)
    persisted_data = dict((persisted or {}).get("data") or next_state)
    return {
        "ok": success,
        "environment": environment,
        "date": date,
        "card": card,
        "message_id": delivered_message_id,
        "data": persisted_data,
        "result": result,
        "skipped": bool(result.get("skipped")),
        "skipped_reason": str(result.get("reason") or ""),
    }


__all__ = [
    "build_delivery_fingerprint",
    "build_two_factor_card",
    "deliver_two_factor_card",
    "resolve_two_factor_chat_id",
]
