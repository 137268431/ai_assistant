from __future__ import annotations

from typing import Any


def pick_effective_config_rows(rows: list[dict[str, Any]], environment: str, *, normalize_environment) -> list[dict[str, Any]]:
    runtime_environment = normalize_environment(environment)
    priority = {"": 0, "global": 1, runtime_environment: 2}
    selected: dict[str, dict[str, Any]] = {}

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        row_environment = str(row.get("environment") or "").strip().lower()
        row_priority = priority.get(row_environment, -1)
        current = selected.get(key)
        current_priority = priority.get(str((current or {}).get("environment") or "").strip().lower(), -1)
        if current is None or row_priority >= current_priority:
            selected[key] = row

    return [selected[key] for key in sorted(selected)]


def serialize_config_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "key": str(row.get("key") or ""),
                "value": row.get("value") or "",
                "environment": str(row.get("environment") or ""),
                "updated": row.get("updated") or "",
            }
        )
    return items


def load_effective_config_rows(pb, environment: str, *, normalize_environment, pick_effective_config_rows_fn) -> list[dict[str, Any]]:
    runtime_environment = normalize_environment(environment, "live")
    rows: list[dict[str, Any]] = []
    try:
        rows = pb.get_runtime_config(scope="all", environment=runtime_environment)
    except Exception:
        rows = []
    if not rows:
        try:
            rows = pb.get_all_records("config", sort="sort_order,key", max_pages=20)
        except Exception:
            rows = []
    return pick_effective_config_rows_fn(rows, runtime_environment)


def load_effective_config_map(
    pb,
    environment: str,
    *,
    normalize_environment,
    load_effective_config_rows_fn,
    selected_keys: tuple[str, ...] | list[str] | set[str] | None = None,
) -> dict[str, str]:
    _ = normalize_environment  # keep signature explicit for callers mirroring api_app dependencies
    allowed = {str(key or "").strip() for key in (selected_keys or []) if str(key or "").strip()}
    config_map: dict[str, str] = {}
    for row in load_effective_config_rows_fn(pb, environment):
        key = str(row.get("key") or "").strip()
        if not key or (allowed and key not in allowed):
            continue
        config_map[key] = str(row.get("value") or "")
    return config_map


def load_recent_system_events(pb, environment: str, *, limit: int = 20, normalize_environment, escape_filter_string) -> list[dict[str, Any]]:
    runtime_environment = normalize_environment(environment, "live")
    safe_limit = max(1, min(200, int(limit or 20)))
    filter_expr = f'environment = "{escape_filter_string(runtime_environment)}"'
    rows: list[dict[str, Any]] = []
    try:
        get_records = getattr(pb, "get_records", None)
        if callable(get_records):
            rows = get_records("system_events", filter=filter_expr, sort="-created", per_page=safe_limit, page=1)
        else:
            rows = (pb.get_all_records("system_events", filter=filter_expr, sort="-created", max_pages=1) or [])[:safe_limit]
    except Exception:
        rows = []

    items: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "id": str(row.get("id") or ""),
                "event_type": str(row.get("event_type") or ""),
                "level": str(row.get("level") or ""),
                "source": str(row.get("source") or ""),
                "environment": str(row.get("environment") or runtime_environment),
                "title": str(row.get("title") or ""),
                "notified": bool(row.get("notified")),
                "us_time": row.get("us_time") or "",
                "created": row.get("created") or "",
            }
        )
    return items
