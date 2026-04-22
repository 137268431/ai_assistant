from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.details import build_order_detail_payload
from ibkr_api.orders.values import parse_boolean, parse_integer, to_text


def normalize_statuses(values: Any) -> list[str]:
    source = values if isinstance(values, list) else [values]
    items: list[str] = []
    for value in source:
        text = to_text(value)
        if text:
            items.append(text)
    return items


def build_order_filter(
    payload: dict[str, Any],
    *,
    environment: str,
    escape_filter_string: Callable[[Any], str],
) -> str:
    filters = [f'environment = "{escape_filter_string(environment)}"']

    signal_id = to_text(payload.get("signal_id"))
    if signal_id:
        filters.append(f'signal_id = "{escape_filter_string(signal_id)}"')

    unique_id = to_text(payload.get("unique_id") or payload.get("id"))
    if unique_id:
        filters.append(f'unique_id = "{escape_filter_string(unique_id)}"')

    trade_group_id = to_text(payload.get("trade_group_id"))
    if trade_group_id:
        escaped_trade_group = escape_filter_string(trade_group_id)
        filters.append(
            f'(trade_group_id = "{escaped_trade_group}" || entry_order_unique_id = "{escaped_trade_group}")'
        )

    updated_after = to_text(payload.get("updated_after"))
    if updated_after:
        filters.append(f'updated >= "{escape_filter_string(updated_after)}"')

    statuses = normalize_statuses(payload.get("statuses"))
    if statuses:
        clauses = [f'status = "{escape_filter_string(status)}"' for status in statuses]
        filters.append(f"({' || '.join(clauses)})")

    return " && ".join(filters)


def load_order_records(
    pb: Any,
    *,
    payload: dict[str, Any],
    environment: str,
    escape_filter_string: Callable[[Any], str],
) -> list[dict[str, Any]]:
    filter_expr = build_order_filter(
        payload,
        environment=environment,
        escape_filter_string=escape_filter_string,
    )
    sort = to_text(payload.get("sort")) or "-updated,-created"
    limit = parse_integer(payload.get("limit"), 20, 1, 500)
    offset = parse_integer(payload.get("offset"), 0, 0)
    scan_all = parse_boolean(payload.get("scan_all"), False)
    if not scan_all:
        page = offset // limit + 1
        return list(pb.get_records("orders", filter=filter_expr, sort=sort, per_page=limit, page=page) or [])

    page_size = parse_integer(payload.get("page_size"), limit, 1, 200)
    max_scan = parse_integer(payload.get("max_scan"), page_size, page_size, 2000)
    records: list[dict[str, Any]] = []
    page = offset // page_size + 1
    skip_in_page = offset % page_size
    while len(records) < max_scan:
        batch = list(pb.get_records("orders", filter=filter_expr, sort=sort, per_page=page_size, page=page) or [])
        if skip_in_page > 0:
            batch = batch[skip_in_page:]
            skip_in_page = 0
        if not batch:
            break
        records.extend(batch[: max_scan - len(records)])
        if len(batch) < page_size:
            break
        page += 1
    return records


def load_detail_records(
    pb: Any,
    *,
    unique_id: str,
    environment: str,
    detail_limit: int,
    escape_filter_string: Callable[[Any], str],
) -> list[dict[str, Any]]:
    return list(
        pb.get_records(
            "ibkr_order_details",
            filter=(
                f'order_id = "{escape_filter_string(unique_id)}" && '
                f'environment = "{escape_filter_string(environment)}"'
            ),
            sort="-created,-bar_time_ms",
            per_page=detail_limit,
            page=1,
        )
        or []
    )


def build_repair_context(
    order_row: dict[str, Any],
    detail_rows: list[dict[str, Any]],
    *,
    environment: str,
    suppress_notification: bool,
    only_missing: bool,
) -> dict[str, Any]:
    latest_detail = detail_rows[0] if detail_rows else {}
    status = to_text(order_row.get("status")) or "Submitted"
    return {
        "environment": environment,
        "signal_id": to_text(order_row.get("signal_id")),
        "unique_id": to_text(order_row.get("unique_id")),
        "symbol": to_text(order_row.get("symbol")),
        "status": status,
        "order_type": to_text(order_row.get("order_type")),
        "order_id": to_text(order_row.get("order_id")),
        "broker_order_id": to_text(order_row.get("broker_order_id")),
        "us_time": to_text(order_row.get("us_time")),
        "cn_time": to_text(order_row.get("cn_time")),
        "bar_time_ms": int(order_row.get("bar_time_ms") or 0),
        "suppress_notification": suppress_notification,
        "latest_detail": latest_detail,
        "latest_detail_status": to_text(latest_detail.get("status")),
        "latest_detail_id": to_text(latest_detail.get("id")),
        "repair_reason": (
            "reconciled_missing_current_status_detail"
            if only_missing and detail_rows
            else "reconciled_missing_ibkr_order_details"
            if only_missing
            else "reconciled_order_snapshot"
        ),
        "repair_mode": (
            "missing_current_status_detail"
            if only_missing and detail_rows
            else "missing_ibkr_order_details"
            if only_missing
            else "snapshot_replay"
        ),
    }


def append_reconciled_detail(
    pb: Any,
    *,
    order_row: dict[str, Any],
    context: dict[str, Any],
    source: str,
    repair_source: str,
) -> dict[str, Any]:
    detail_payload = build_order_detail_payload(
        pb,
        {
            **dict(order_row),
            "environment": context["environment"],
        },
        source=source,
        reason=context["repair_reason"],
    )
    detail_extra = dict(detail_payload.get("extra") or {})
    detail_extra.update(
        {
            "repair_source": repair_source,
            "repair_mode": context["repair_mode"],
            "previous_detail_status": context["latest_detail_status"],
            "suppress_notification": context["suppress_notification"],
        }
    )
    detail_payload["extra"] = detail_extra
    return pb.create_record("ibkr_order_details", detail_payload)
