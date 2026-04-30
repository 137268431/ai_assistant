from __future__ import annotations

from typing import Any


def compact_json_payload(
    value: Any,
    *,
    max_list_items: int = 40,
    max_dict_items: int = 200,
    max_string_length: int = 1200,
    max_depth: int = 8,
) -> Any:
    """Return a JSON-safe payload with bounded nested collection sizes."""

    if max_depth <= 0:
        return _compact_leaf(value, max_string_length=max_string_length)

    if isinstance(value, dict):
        items = list(value.items())
        limited = items[: max(0, max_dict_items)]
        compacted = {
            str(key): compact_json_payload(
                item,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
                max_string_length=max_string_length,
                max_depth=max_depth - 1,
            )
            for key, item in limited
        }
        if len(items) > len(limited):
            compacted["_truncated_key_count"] = len(items) - len(limited)
        return compacted

    if isinstance(value, (list, tuple, set)):
        values = list(value)
        limited = values[: max(0, max_list_items)]
        compacted_list = [
            compact_json_payload(
                item,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
                max_string_length=max_string_length,
                max_depth=max_depth - 1,
            )
            for item in limited
        ]
        if len(values) > len(limited):
            compacted_list.append({"_truncated_item_count": len(values) - len(limited)})
        return compacted_list

    return _compact_leaf(value, max_string_length=max_string_length)


def _compact_leaf(value: Any, *, max_string_length: int) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > max_string_length:
            return value[:max_string_length] + "...[truncated]"
        return value
    return str(value)
