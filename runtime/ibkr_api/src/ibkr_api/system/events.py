from __future__ import annotations

import os
from typing import Any, Callable

DEFAULT_FEISHU_SYSTEM_CHAT_ID = "oc_b7b52fc28816d90e27ce50ca7922a9ac"
DEFAULT_FEISHU_2FA_CHAT_ID = "oc_c48c10447685e80cfea0c003864aa51f"
DEFAULT_FEISHU_ALERT_CHAT_ID = "oc_91aa4f84bc6fedb125b1a263d91d4104"

SYSTEM_EVENT_SOURCE_LABELS = {
    "qc": "IBKR Data",
    "ibkr_api": "IBKR API",
    "ibkr_compute": "IBKR Compute",
    "pb": "PocketBase",
    "manual": "Manual",
}
SYSTEM_EVENT_LEVEL_META = {
    "info": {"emoji": "ℹ️", "template": "blue"},
    "warning": {"emoji": "⚠️", "template": "orange"},
    "error": {"emoji": "🚨", "template": "red"},
}
SYSTEM_EVENT_HIDDEN_DETAIL_KEYS = {
    "今日bars",
    "indicator",
    "indicators",
    "compute_readiness",
    "backtest_readiness",
    "Backtest",
    "日筛",
    "日筛状态",
    "日筛日期",
    "日筛错误",
    "dedupe_scope",
    "broker_mode",
    "data_environment",
    "summary_window_key",
    "window_start_ms",
    "window_end_ms",
    "scan",
    "topup",
}


NormalizeEnvironment = Callable[[Any, str], str]
ConfigValue = Callable[[str, str, str], str]
ToggleCheck = Callable[[Any], bool]
AddEnvironmentToDetail = Callable[[Any, str], dict[str, Any]]
TimeStrings = Callable[..., dict[str, str]]
SystemPageUrl = Callable[[str], str]
LabelTitleWithEnvironment = Callable[[Any, str], str]
SendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
UpdateInteractive = Callable[[str, dict[str, Any], str], dict[str, Any]]


def normalized_system_event_source(source: Any) -> str:
    return str(source or "").strip().lower().replace("-", "_")


def resolved_system_chat_id() -> str:
    return str(os.environ.get("FEISHU_SYSTEM_CHAT_ID") or DEFAULT_FEISHU_SYSTEM_CHAT_ID).strip()



def resolved_2fa_chat_id() -> str:
    return str(os.environ.get("FEISHU_2FA_CHAT_ID") or DEFAULT_FEISHU_2FA_CHAT_ID).strip()



def resolved_alert_chat_id() -> str:
    return str(os.environ.get("FEISHU_ALERT_CHAT_ID") or DEFAULT_FEISHU_ALERT_CHAT_ID).strip()



def system_event_level_meta(level: str) -> dict[str, str]:
    return SYSTEM_EVENT_LEVEL_META.get(str(level or "info").strip().lower(), SYSTEM_EVENT_LEVEL_META["info"])



def build_system_event_card(
    level: str,
    source: str,
    title: str,
    detail: Any,
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    add_environment_to_detail: AddEnvironmentToDetail,
    time_strings: TimeStrings,
    system_page_url: SystemPageUrl,
    label_title_with_environment: LabelTitleWithEnvironment,
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    level_meta = system_event_level_meta(level)
    normalized_source = normalized_system_event_source(source)
    source_label = SYSTEM_EVENT_SOURCE_LABELS.get(normalized_source, str(source or "").strip() or "system")
    detail_fields = add_environment_to_detail(detail, runtime_environment)
    lines = [f"**来源**: {source_label}  |  **级别**: {level_meta['emoji']} {str(level or 'info').upper()}"]
    for key, value in detail_fields.items():
        if str(key) in SYSTEM_EVENT_HIDDEN_DETAIL_KEYS:
            continue
        lines.append(f"**{key}**: {value}")
    times = time_strings()
    lines.append(f"🕐 美东 {times['us']} | 北京 {times['cn']}")
    actions = []
    status_url = system_page_url(runtime_environment)
    if status_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📊 查看系统状态"},
                "type": "default",
                "multi_url": {"url": status_url, "pc_url": status_url, "ios_url": status_url, "android_url": status_url},
            }
        )
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{level_meta['emoji']} {label_title_with_environment(title, runtime_environment)}"},
            "template": level_meta["template"],
        },
        "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
    }
    if actions:
        card["elements"].append({"tag": "action", "actions": actions})
    return card



def likely_two_factor_alert(title: str, detail: Any) -> bool:
    text = str(title or "").strip()
    if "2FA" in text:
        return True
    for marker in ("IBKR Session 已失效", "IBKR Runtime 未认证", "IBKR Session 长时间未恢复认证"):
        if marker in text:
            return True
    if not isinstance(detail, dict):
        return False
    return "2FA状态" in detail and any(key in detail for key in ("Session认证", "验证模式", "Challenge", "响应状态"))


def likely_backtest_event(title: str, detail: Any) -> bool:
    text = str(title or "").strip().lower()
    if "backtest" in text or "回测" in text:
        return True
    if not isinstance(detail, dict):
        return False
    if any(key in detail for key in ("batch_id", "best_run_id")):
        return True
    return "run_id" in detail and any(key in detail for key in ("date_from", "date_to", "symbol_source"))



def should_notify_system_event(
    event_type: str,
    level: str,
    source: str,
    title: str,
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    config_value: ConfigValue,
    is_enabled_text: ToggleCheck,
) -> bool:
    runtime_environment = normalize_environment(environment, "live")
    normalized_event_type = str(event_type or "status_change").strip().lower()
    normalized_level = str(level or "info").strip().lower()
    normalized_source = normalized_system_event_source(source)
    normalized_title = str(title or "")
    if normalized_event_type == "daily_report":
        return is_enabled_text(config_value("daily_summary_notify_enabled", "TRUE", runtime_environment))
    if normalized_event_type == "heartbeat":
        key = "inspection_notify_enabled" if normalized_level in {"warning", "error"} else "health_check_notify_enabled"
        return is_enabled_text(config_value(key, "TRUE", runtime_environment))
    if normalized_level in {"warning", "error"} or normalized_event_type == "alert":
        return is_enabled_text(config_value("inspection_notify_enabled", "TRUE", runtime_environment))
    if normalized_source == "manual" and ("停止" in normalized_title or "急停" in normalized_title or "stop" in normalized_title.lower()):
        return is_enabled_text(config_value("manual_stop_notify_enabled", "TRUE", runtime_environment))
    return is_enabled_text(config_value("status_notify_enabled", "TRUE", runtime_environment))



def system_event_chat_id(
    event_type: str,
    level: str,
    environment: str,
    source: str,
    title: str,
    detail: Any,
    *,
    normalize_environment: NormalizeEnvironment,
    config_value: ConfigValue,
    default_2fa_chat_id: str,
    default_alert_chat_id: str,
    default_system_chat_id: str,
) -> str:
    runtime_environment = normalize_environment(environment, "live")
    normalized_event_type = str(event_type or "status_change").strip().lower()
    normalized_level = str(level or "info").strip().lower()
    if likely_two_factor_alert(title, detail):
        return config_value("system_2fa_chat_id", default_2fa_chat_id, runtime_environment)
    if normalized_level in {"warning", "error"} or normalized_event_type == "alert":
        return config_value("system_alert_chat_id", default_alert_chat_id, runtime_environment)
    if likely_backtest_event(title, detail):
        return config_value("backtest_chat_id", default_system_chat_id, runtime_environment)
    return config_value("system_status_chat_id", default_system_chat_id, runtime_environment)



def deliver_system_event_notification(
    event_type: str,
    level: str,
    source: str,
    title: str,
    detail: Any,
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    should_notify_system_event_fn: Callable[..., bool],
    build_system_event_card_fn: Callable[..., dict[str, Any]],
    system_event_chat_id_fn: Callable[..., str],
    send_interactive: SendInteractive,
    update_interactive: UpdateInteractive,
    message_id: str = "",
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    if not should_notify_system_event_fn(event_type, level, source, title, runtime_environment):
        return {"success": False, "skipped": True, "message_id": str(message_id or ""), "reason": "notify_disabled"}
    card = build_system_event_card_fn(level, source, title, detail, runtime_environment)
    if message_id:
        result = update_interactive(str(message_id or ""), card, runtime_environment)
        result["updated"] = bool(result.get("success"))
        return result
    chat_id = system_event_chat_id_fn(event_type, level, runtime_environment, source, title, detail)
    return send_interactive(card, chat_id, runtime_environment)



def write_system_event_record(
    event_type: str,
    level: str,
    source: str,
    title: str,
    detail: Any,
    environment: str,
    notified: bool,
    *,
    pb: Any,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    label_title_with_environment: LabelTitleWithEnvironment,
    add_environment_to_detail: AddEnvironmentToDetail,
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    times = time_strings()
    payload = {
        "event_type": str(event_type or "status_change").strip() or "status_change",
        "level": str(level or "info").strip() or "info",
        "source": normalized_system_event_source(source) or "ibkr_api",
        "environment": runtime_environment,
        "title": label_title_with_environment(title, runtime_environment),
        "detail": add_environment_to_detail(detail, runtime_environment),
        "us_time": times["us"],
        "cn_time": times["cn"],
        "notified": bool(notified),
    }
    try:
        return pb.create_record("system_events", payload)
    except Exception:
        return {}
