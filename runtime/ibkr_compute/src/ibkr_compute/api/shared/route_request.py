from __future__ import annotations

from flask import request


def get_json_payload() -> dict:
    return request.get_json(silent=True) or {}


def coerce_request_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    try:
        return float(text) != 0.0
    except (TypeError, ValueError):
        pass
    return default


def coerce_request_int(
    value,
    default: int = 0,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def get_query_arg_text(name: str, default: str = "", *, upper: bool = False, lower: bool = False) -> str:
    text = str(request.args.get(name) or "").strip()
    if not text:
        text = str(default or "").strip()
    if upper:
        return text.upper()
    if lower:
        return text.lower()
    return text


def get_query_arg_csv(name: str, default=None) -> list[str]:
    raw_values = request.args.getlist(name)
    if not raw_values:
        if isinstance(default, (list, tuple, set)):
            raw_values = list(default)
        elif default is None:
            raw_values = []
        else:
            raw_values = [default]

    items: list[str] = []
    for raw_value in raw_values:
        if isinstance(raw_value, str):
            parts = raw_value.replace("\n", ",").split(",")
        else:
            parts = [raw_value]
        for part in parts:
            text = str(part or "").strip()
            if text:
                items.append(text)
    return items


def get_query_arg_int(
    name: str,
    default: int = 0,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    return coerce_request_int(request.args.get(name), default, minimum=minimum, maximum=maximum)


def get_query_arg_bool(name: str, default: bool = False) -> bool:
    return coerce_request_bool(request.args.get(name), default)


def get_query_arg_page(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    return get_query_arg_int(name, default, minimum=minimum, maximum=maximum)
