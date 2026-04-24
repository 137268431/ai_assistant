from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def normalize_environment(value: Any, default: str = "live") -> str:
    text = str(value or "").strip().lower()
    return text or default


def parse_boolean(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    return bool(default)


def escape_filter_string(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def as_dict(value: Any) -> dict[str, Any]:
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


def normalize_symbol_list(values: Any) -> list[str]:
    source = values if isinstance(values, list) else [values]
    items: list[str] = []
    seen: set[str] = set()

    def append_symbol(raw: Any) -> None:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen:
            return
        seen.add(symbol)
        items.append(symbol)

    for value in source:
        if isinstance(value, list):
            for nested in value:
                append_symbol(nested)
            continue
        append_symbol(value)
    return items


def trim_array(values: Any, limit: int) -> list[Any]:
    if not isinstance(values, list):
        return []
    max_items = max(0, int(limit or 0))
    return list(values[:max_items]) if max_items > 0 else []


def trim_object_entries(value: Any, limit: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    max_items = max(0, int(limit or 0))
    if max_items <= 0 or len(value) <= max_items:
        return dict(value)
    trimmed: dict[str, Any] = {}
    for key, item in list(value.items())[:max_items]:
        trimmed[str(key)] = item
    return trimmed


def parse_time_ms(value: Any, *, default_tz) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=default_tz)
        return int(parsed.timestamp() * 1000)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=default_tz)
            return int(parsed.timestamp() * 1000)
        except Exception:
            continue
    return 0
