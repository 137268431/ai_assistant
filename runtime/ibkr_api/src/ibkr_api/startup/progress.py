from __future__ import annotations

import time
import uuid
from typing import Any, Callable

STARTUP_STEP_ORDER = (
    "service_boot",
    "card_ready",
    "manual_trigger",
    "manual_confirm",
    "runtime_resume",
    "health_check",
)
STARTUP_STEP_LABELS = {
    "service_boot": "服务拉起",
    "card_ready": "准备 2FA 卡片",
    "manual_trigger": "在飞书手动触发 2FA",
    "manual_confirm": "完成当前 2FA 验证",
    "runtime_resume": "恢复 Runtime 运行态",
    "health_check": "启动后健康检查",
}
STARTUP_STEP_STATUS_META = {
    "pending": {"icon": "⬜", "text": "待开始"},
    "running": {"icon": "⏳", "text": "进行中"},
    "waiting": {"icon": "⏳", "text": "等待中"},
    "done": {"icon": "✅", "text": "已完成"},
    "failed": {"icon": "❌", "text": "失败"},
    "skipped": {"icon": "➖", "text": "非阻塞"},
}
STARTUP_STEP_ALIAS_MAP = {
    "gateway": "service_boot",
    "subscriptions": "runtime_resume",
    "core_threads": "runtime_resume",
    "warmup": "runtime_resume",
    "trading_gate": "health_check",
}

NormalizeEnvironment = Callable[[Any, str], str]
ConfigValue = Callable[[str, str, str], str]
TimeStrings = Callable[..., dict[str, str]]
EnvironmentTag = Callable[[str], str]
LabelTitleWithEnvironment = Callable[[Any, str], str]
RuntimePageUrl = Callable[[str], str]
SystemPageUrl = Callable[[str], str]
ConsoleBaseUrl = Callable[[], str]
SendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
UpdateInteractive = Callable[[str, dict[str, Any], str], dict[str, Any]]



def normalize_startup_step_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "pending"
    if text in STARTUP_STEP_STATUS_META:
        return text
    if text in {"complete", "completed", "success", "ok"}:
        return "done"
    if text in {"in_progress", "active"}:
        return "running"
    if text in {"blocked", "manual", "manual_action"}:
        return "waiting"
    if text == "error":
        return "failed"
    if text == "background":
        return "skipped"
    return "pending"



def default_startup_steps() -> dict[str, dict[str, Any]]:
    return {key: {"key": key, "label": STARTUP_STEP_LABELS[key], "status": "pending", "detail": ""} for key in STARTUP_STEP_ORDER}



def apply_startup_step_patch(steps: dict[str, dict[str, Any]], key: str, patch: dict[str, Any]) -> None:
    if key not in steps or not isinstance(patch, dict):
        return
    current = steps.get(key) or {}
    steps[key] = {
        "key": key,
        "label": str(patch.get("label") or current.get("label") or STARTUP_STEP_LABELS.get(key) or key),
        "status": normalize_startup_step_status(patch.get("status") or current.get("status")),
        "detail": str(patch.get("detail") if patch.get("detail") is not None else current.get("detail") or ""),
    }



def apply_auth_step_patch(steps: dict[str, dict[str, Any]], patch: dict[str, Any], trigger_login: bool) -> None:
    if not isinstance(patch, dict):
        return
    status = normalize_startup_step_status(patch.get("status"))
    detail = str(patch.get("detail") or "")
    apply_startup_step_patch(
        steps,
        "service_boot",
        {"status": "done", "detail": (steps.get("service_boot") or {}).get("detail") or "Gateway 已启动并可访问。"},
    )
    if status == "waiting":
        apply_startup_step_patch(steps, "card_ready", {"status": "done", "detail": detail or "已把 2FA 卡片准备好，等待人工点击开始验证。"})
        apply_startup_step_patch(steps, "manual_trigger", {"status": "waiting", "detail": "点击当前启动卡片下方“开始 2FA 验证”。"})
        return
    if status == "running":
        if trigger_login:
            apply_startup_step_patch(steps, "card_ready", {"status": "done", "detail": "当前轮次已使用现有卡片或同一入口继续推进。"})
            apply_startup_step_patch(steps, "manual_trigger", {"status": "done", "detail": "当前轮次已手动触发，不会自动补发新的 Push。"})
            apply_startup_step_patch(steps, "manual_confirm", {"status": "waiting", "detail": detail or "等待手机确认或完成当前响应码验证。"})
            return
        apply_startup_step_patch(steps, "card_ready", {"status": "running", "detail": detail or "正在确认是否需要进入手动 2FA。"})
        return
    if status == "done":
        if trigger_login:
            apply_startup_step_patch(steps, "card_ready", {"status": "done", "detail": "2FA 卡片阶段已完成。"})
            apply_startup_step_patch(steps, "manual_trigger", {"status": "done", "detail": "人工触发步骤已完成。"})
            apply_startup_step_patch(steps, "manual_confirm", {"status": "done", "detail": detail or "当前 2FA 验证已完成。"})
            return
        apply_startup_step_patch(steps, "card_ready", {"status": "skipped", "detail": "已复用现有认证会话，本轮无需准备新的 2FA 卡片。"})
        apply_startup_step_patch(steps, "manual_trigger", {"status": "skipped", "detail": "Session 已认证，本轮无需在飞书手动触发 2FA。"})
        apply_startup_step_patch(steps, "manual_confirm", {"status": "skipped", "detail": detail or "Session 已认证，本轮无需完成新的 2FA 验证。"})
        return
    if status == "failed":
        target_key = "manual_confirm" if trigger_login else "manual_trigger"
        apply_startup_step_patch(steps, target_key, {"status": "failed", "detail": detail or "当前轮次未完成，需要人工重新发起。"})



def merge_startup_steps(existing_steps: Any, patch_steps: Any, trigger_login: bool) -> dict[str, dict[str, Any]]:
    merged = default_startup_steps()
    source_steps = existing_steps if isinstance(existing_steps, dict) else {}
    for raw_key, raw_patch in source_steps.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(raw_patch, dict):
            continue
        if key in merged:
            apply_startup_step_patch(merged, key, raw_patch)
            continue
        mapped_key = STARTUP_STEP_ALIAS_MAP.get(key)
        if mapped_key:
            apply_startup_step_patch(merged, mapped_key, raw_patch)
    if not isinstance(patch_steps, dict):
        return merged
    for raw_key, raw_patch in patch_steps.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(raw_patch, dict):
            continue
        if key in merged:
            apply_startup_step_patch(merged, key, raw_patch)
            continue
        if key == "auth":
            apply_auth_step_patch(merged, raw_patch, trigger_login)
            continue
        mapped_key = STARTUP_STEP_ALIAS_MAP.get(key)
        if not mapped_key:
            continue
        normalized_status = normalize_startup_step_status(raw_patch.get("status"))
        if mapped_key == "runtime_resume" and normalized_status == "done":
            apply_startup_step_patch(
                merged,
                "runtime_resume",
                {"status": "running", "detail": str(raw_patch.get("detail") or "认证已恢复，正在恢复订阅、线程与 Warmup。")},
            )
            continue
        apply_startup_step_patch(merged, mapped_key, raw_patch)
    return merged



def normalize_startup_fields(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): val for key, val in value.items() if str(key or "").strip()}



def format_startup_label_timestamp(timestamp_text: str) -> str:
    compact = "".join(ch for ch in str(timestamp_text or "").strip() if ch.isdigit())
    return compact[:14] if len(compact) >= 14 else ""



def build_startup_label(
    environment: str,
    sequence: int,
    started_at: str,
    *,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
) -> str:
    runtime_environment = normalize_environment(environment, "live").upper()
    compact_ts = format_startup_label_timestamp(started_at) or format_startup_label_timestamp(time_strings()["us"]) or str(int(time.time()))
    return f"{runtime_environment}-{compact_ts[:8]}-{compact_ts[8:14]}-{max(0, int(sequence or 0)):03d}"



def build_startup_cycle_id(environment: str, *, normalize_environment: NormalizeEnvironment) -> str:
    return f"{normalize_environment(environment, 'live')}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"



def startup_chat_id(environment: str, *, config_value: ConfigValue, default_chat_id: str) -> str:
    return config_value("system_startup_chat_id", default_chat_id, environment)



def normalize_startup_state(
    state: Any,
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    startup_chat_id_fn: Callable[[str], str],
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    source = state if isinstance(state, dict) else {}
    return {
        "cycle_id": str(source.get("cycle_id") or ""),
        "startup_seq": max(0, int(source.get("startup_seq") or 0)),
        "startup_label": str(source.get("startup_label") or ""),
        "active": bool(source.get("active")),
        "status": str(source.get("status") or "idle").strip().lower() or "idle",
        "title": str(source.get("title") or "IBKR Runtime 启动中"),
        "summary": str(source.get("summary") or ""),
        "current_step": str(source.get("current_step") or ""),
        "current_blocker": str(source.get("current_blocker") or ""),
        "operator_action": str(source.get("operator_action") or ""),
        "started_at": str(source.get("started_at") or ""),
        "finished_at": str(source.get("finished_at") or ""),
        "last_update_at": str(source.get("last_update_at") or ""),
        "reason": str(source.get("reason") or ""),
        "source": str(source.get("source") or ""),
        "trigger_login": bool(source.get("trigger_login")),
        "runtime_phase": str(source.get("runtime_phase") or ""),
        "runtime_url": str(source.get("runtime_url") or ""),
        "startup_chat_id": str(source.get("startup_chat_id") or startup_chat_id_fn(runtime_environment)),
        "message_id": str(source.get("message_id") or ""),
        "last_delivery_mode": str(source.get("last_delivery_mode") or ""),
        "last_delivery_at": str(source.get("last_delivery_at") or ""),
        "last_delivery_error": str(source.get("last_delivery_error") or ""),
        "fields": normalize_startup_fields(source.get("fields")),
        "steps": merge_startup_steps(source.get("steps"), {}, bool(source.get("trigger_login"))),
    }



def resolve_startup_step_label(state: dict[str, Any]) -> str:
    key = str((state or {}).get("current_step") or "").strip()
    mapped = STARTUP_STEP_ALIAS_MAP.get(key, key)
    if mapped in STARTUP_STEP_LABELS:
        return STARTUP_STEP_LABELS[mapped]
    return mapped or "-"



def build_startup_checklist_markdown(steps: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in STARTUP_STEP_ORDER:
        step = steps.get(key) if isinstance(steps, dict) else {}
        if not isinstance(step, dict):
            step = {}
        status_meta = STARTUP_STEP_STATUS_META.get(normalize_startup_step_status(step.get("status")), STARTUP_STEP_STATUS_META["pending"])
        label = str(step.get("label") or STARTUP_STEP_LABELS.get(key) or key)
        detail = str(step.get("detail") or "")
        line = f"{status_meta['icon']} {label}"
        if detail:
            line += f"  \n{detail}"
        lines.append(line)
    return "\n".join(lines)


def _append_action_rows(
    elements: list[dict[str, Any]],
    primary_actions: list[dict[str, Any]],
    navigation_actions: list[dict[str, Any]],
) -> None:
    if not primary_actions and not navigation_actions:
        return
    elements.append({"tag": "hr"})
    elements.extend({"tag": "action", "actions": [action]} for action in primary_actions)
    if navigation_actions:
        elements.append({"tag": "action", "actions": navigation_actions})


def build_startup_card(
    state: dict[str, Any],
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    normalize_startup_state_fn: Callable[[Any, str], dict[str, Any]],
    environment_tag: EnvironmentTag,
    label_title_with_environment: LabelTitleWithEnvironment,
    runtime_page_url: RuntimePageUrl,
    system_page_url: SystemPageUrl,
    console_base_url: ConsoleBaseUrl,
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    normalized = normalize_startup_state_fn(state, runtime_environment)
    current_step = resolve_startup_step_label(normalized)
    manual_trigger = normalized["steps"].get("manual_trigger") if isinstance(normalized.get("steps"), dict) else {}
    show_trigger = bool(normalized.get("active")) and (
        STARTUP_STEP_ALIAS_MAP.get(str(normalized.get("current_step") or ""), str(normalized.get("current_step") or "")) == "manual_trigger"
        or normalize_startup_step_status((manual_trigger or {}).get("status")) in {"waiting", "failed"}
    )
    header_template = "green" if normalized.get("status") == "completed" else ("red" if normalized.get("status") == "failed" else "blue")
    current_runtime_url = normalized.get("runtime_url") or runtime_page_url(runtime_environment)
    current_system_url = system_page_url(runtime_environment)
    summary_lines = [
        f"**启动编号**: {normalized.get('startup_label') or '-'}",
        f"**当前阶段**: {current_step or '-'}",
        f"**当前卡点**: {normalized.get('current_blocker') or normalized.get('summary') or '-'}",
        f"**下一步**: {normalized.get('operator_action') or '等待系统继续推进'}",
    ]
    context_lines = [
        f"**环境**: {environment_tag(runtime_environment)}",
        f"**最近更新时间**: {normalized.get('last_update_at') or '-'}",
    ]
    if normalized.get("reason"):
        context_lines.append(f"**启动原因**: {normalized['reason']}")
    for key, value in normalized.get("fields", {}).items():
        context_lines.append(f"**{key}**: {value}")
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": "\n".join(summary_lines)},
        {"tag": "hr"},
        {"tag": "markdown", "content": build_startup_checklist_markdown(normalized.get("steps", {}))},
        {"tag": "hr"},
        {"tag": "markdown", "content": "\n".join(context_lines)},
    ]
    primary_actions: list[dict[str, Any]] = []
    if show_trigger:
        callback_url = f"{console_base_url()}/webhook/feishu/callback" if console_base_url() else ""
        if callback_url:
            primary_actions.append(
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "开始 2FA 验证"},
                    "action_type": "request",
                    "url": callback_url,
                    "value": {"action": "ibkr_2fa_start", "environment": runtime_environment, "force_restart": False},
                }
            )
    navigation_actions: list[dict[str, Any]] = []
    runtime_button_type = "default" if primary_actions else "primary"
    if current_runtime_url:
        navigation_actions.append(
            {
                "tag": "button",
                "type": runtime_button_type,
                "text": {"tag": "plain_text", "content": "查看 Runtime"},
                "multi_url": {"url": current_runtime_url, "pc_url": current_runtime_url, "ios_url": current_runtime_url, "android_url": current_runtime_url},
            }
        )
    if current_system_url:
        navigation_actions.append(
            {
                "tag": "button",
                "type": "default",
                "text": {"tag": "plain_text", "content": "查看 System"},
                "multi_url": {"url": current_system_url, "pc_url": current_system_url, "ios_url": current_system_url, "android_url": current_system_url},
            }
        )
    _append_action_rows(elements, primary_actions, navigation_actions)
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"⏳ {label_title_with_environment(normalized.get('title') or 'IBKR Runtime 启动中', runtime_environment)}"},
            "template": header_template,
        },
        "elements": elements,
    }



def deliver_startup_progress_card(
    state: dict[str, Any],
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    normalize_startup_state_fn: Callable[[Any, str], dict[str, Any]],
    build_startup_card_fn: Callable[[dict[str, Any], str], dict[str, Any]],
    send_interactive: SendInteractive,
    update_interactive: UpdateInteractive,
    startup_chat_id_fn: Callable[[str], str],
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    normalized = normalize_startup_state_fn(state, runtime_environment)
    card = build_startup_card_fn(normalized, runtime_environment)
    message_id = str(normalized.get("message_id") or "")
    if message_id:
        result = update_interactive(message_id, card, runtime_environment)
        result["updated"] = bool(result.get("success"))
        result["message_id"] = str(result.get("message_id") or message_id)
        return result
    return send_interactive(card, startup_chat_id_fn(runtime_environment), runtime_environment)
