from __future__ import annotations

import math
from typing import Any


def to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def escape_filter(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def normalize_symbols(value: Any) -> list[str]:
    raw_items = value if isinstance(value, list) else str(value or "").split(",")
    seen: set[str] = set()
    symbols: list[str] = []
    for item in raw_items:
        symbol = to_text(item.get("symbol") if isinstance(item, dict) else item).upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def count_records(pb: Any, collection: str, filter_expr: str) -> int:
    request_fn = getattr(pb, "_request", None)
    base_url = to_text(getattr(pb, "base_url", ""))
    if callable(request_fn) and base_url:
        response = request_fn(
            "GET",
            f"{base_url.rstrip('/')}/api/collections/{collection}/records",
            params={"filter": filter_expr, "perPage": 1, "page": 1},
            timeout=15,
        )
        try:
            return int((response.json() or {}).get("totalItems") or 0)
        except Exception:
            return 0

    get_all_records = getattr(pb, "get_all_records", None)
    if callable(get_all_records):
        return len(get_all_records(collection, filter=filter_expr, max_pages=100) or [])

    get_records = getattr(pb, "get_records", None)
    if callable(get_records):
        return len(get_records(collection, filter=filter_expr, per_page=200, page=1) or [])
    return 0


def load_records(pb: Any, collection: str, *, filter_expr: str, sort: str = "", per_page: int = 200, max_pages: int = 1) -> list[dict[str, Any]]:
    get_records = getattr(pb, "get_records", None)
    if not callable(get_records):
        return []

    rows: list[dict[str, Any]] = []
    page_count = max(1, int(max_pages or 1))
    bounded_per_page = max(1, min(500, int(per_page or 200)))
    for page in range(1, page_count + 1):
        batch = get_records(collection, filter=filter_expr, sort=sort or None, per_page=bounded_per_page, page=page) or []
        rows.extend([dict(item) for item in batch if isinstance(item, dict)])
        if len(batch) < bounded_per_page:
            break
    return rows
