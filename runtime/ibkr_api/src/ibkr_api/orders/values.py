from __future__ import annotations

from typing import Any


ORDER_INTERNAL_EXTRA_KEYS = {
    "feishu_order_message_id": True,
    "feishu_order_card_version": True,
    "feishu_order_notify_key": True,
    "feishu_order_notify_last_action": True,
    "feishu_order_notify_last_status": True,
    "feishu_order_notify_last_result": True,
    "feishu_order_notify_last_at_ms": True,
    "feishu_order_notify_error": True,
    "feishu_order_notify_http_status": True,
    "feishu_order_notify_api_code": True,
    "feishu_order_notify_api_message": True,
    "feishu_order_notify_response_body": True,
    "feishu_trade_ledger_notify_key": True,
    "feishu_trade_ledger_message_id": True,
    "feishu_trade_ledger_last_result": True,
    "feishu_trade_ledger_last_at_ms": True,
    "feishu_trade_ledger_last_reason": True,
    "feishu_trade_ledger_error": True,
    "feishu_trade_ledger_http_status": True,
    "feishu_trade_ledger_api_code": True,
    "feishu_trade_ledger_api_message": True,
    "feishu_trade_ledger_response_body": True,
}

ORDER_STATUS_TEXT_MAP = {
    "Init": "初始化",
    "Submitted": "待成交",
    "Filled": "已成交",
    "Canceled": "已取消",
    "Closed": "已平仓",
}


def to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def first_defined(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def escape_filter_string(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def ensure_object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def strip_internal_extra(extra: Any) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    source = ensure_object(extra)
    for key, value in source.items():
        if not ORDER_INTERNAL_EXTRA_KEYS.get(str(key), False):
            safe[str(key)] = value
    return safe


def parse_boolean(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    normalized = to_text(value).lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def parse_integer(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    parsed = to_int(value, default)
    if minimum is not None:
        parsed = max(int(minimum), parsed)
    if maximum is not None:
        parsed = min(int(maximum), parsed)
    return parsed
