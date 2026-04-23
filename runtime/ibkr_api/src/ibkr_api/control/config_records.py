from __future__ import annotations

from typing import Any, Callable


EscapeFilterString = Callable[[Any], str]


def upsert_config_value(
    pb: Any,
    key: str,
    value: Any,
    environment: str,
    *,
    escape_filter_string: EscapeFilterString,
    display_name: str = "",
    description: str = "",
    group_name: str = "",
    default_value: Any = None,
    sort_order: int | None = None,
) -> dict[str, Any]:
    config_key = str(key or "").strip()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    filter_expr = (
        f'key = "{escape_filter_string(config_key)}" && '
        f'environment = "{escape_filter_string(runtime_environment)}"'
    )

    record: dict[str, Any] | None = None
    try:
        fetched = pb.get_first_record("config", filter=filter_expr)
        if isinstance(fetched, dict) and fetched.get("id"):
            record = fetched
    except Exception:
        record = None

    payload: dict[str, Any] = {
        "key": config_key,
        "environment": runtime_environment,
        "value": str("" if value is None else value),
    }
    if display_name:
        payload["display_name"] = str(display_name)
    if description:
        payload["description"] = str(description)
    if group_name:
        payload["group_name"] = str(group_name)
    if default_value is not None:
        payload["default_value"] = str(default_value)
    if sort_order is not None:
        payload["sort_order"] = int(sort_order)

    if record and record.get("id"):
        updated = pb.update_record("config", str(record.get("id")), payload)
        return updated if isinstance(updated, dict) else {**record, **payload}

    created = pb.create_record("config", payload)
    return created if isinstance(created, dict) else payload


__all__ = ["upsert_config_value"]
