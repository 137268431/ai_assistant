from __future__ import annotations

import os
from datetime import datetime
from typing import Any


def is_enabled_text(value: Any) -> bool:
    return str(value or "true").strip().lower() not in {"", "0", "false", "no", "off"}


def time_strings(*, now_ts: float | None, et_tz, cn_tz) -> dict[str, str]:
    current = float(now_ts if now_ts is not None else __import__("time").time())
    now_et = datetime.fromtimestamp(current, tz=et_tz)
    now_cn = datetime.fromtimestamp(current, tz=cn_tz)
    return {
        "us": now_et.strftime("%Y-%m-%d %H:%M:%S"),
        "cn": now_cn.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now_et.strftime("%Y-%m-%d"),
    }


def environment_tag(environment: str, *, normalize_environment, environment_labels: dict[str, str]) -> str:
    runtime_environment = normalize_environment(environment, "live")
    label = environment_labels.get(runtime_environment, runtime_environment.upper())
    if runtime_environment in {"live", "paper"}:
        return f"[Broker {label}]"
    return f"[{label}]"


def label_title_with_environment(title: Any, environment: str, *, environment_tag_fn) -> str:
    text = str(title or "").strip()
    tag = environment_tag_fn(environment)
    if not text:
        return tag
    return text if text.startswith(tag) else f"{tag} {text}"


def add_environment_to_detail(detail: Any, environment: str, *, normalize_environment) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    if isinstance(detail, dict):
        return {"environment": runtime_environment, **detail}
    if detail is None or detail == "":
        return {"environment": runtime_environment}
    return {"environment": runtime_environment, "detail": str(detail)}


def console_base_url(*, default_console_base_url: str) -> str:
    return str(
        os.environ.get("CONSOLE_BASE_URL")
        or os.environ.get("QUANT_BASE_URL")
        or os.environ.get("IBKR_CONSOLE_PUBLIC_URL")
        or default_console_base_url
    ).rstrip("/")


def runtime_page_url(environment: str, *, console_base_url_fn, normalize_environment) -> str:
    base_url = console_base_url_fn()
    if not base_url:
        return ""
    return f"{base_url}/ibkr_runtime.html?environment={normalize_environment(environment, 'live')}"


def system_page_url(environment: str, *, console_base_url_fn, normalize_environment) -> str:
    base_url = console_base_url_fn()
    if not base_url:
        return ""
    return f"{base_url}/ibkr_system.html?environment={normalize_environment(environment, 'live')}"


def config_value(config, key: str, default: str, environment: str, *, normalize_environment) -> str:
    try:
        return str(config.get_for_environment(key, normalize_environment(environment, "live"), default) or default)
    except Exception:
        return default


def signal_chat_id(environment: str, *, config_value_fn, default_chat_id: str) -> str:
    return config_value_fn("signal_chat_id", default_chat_id, environment)


def system_status_chat_id(environment: str, *, config_value_fn, default_chat_id: str) -> str:
    return config_value_fn("system_status_chat_id", default_chat_id, environment)
