from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.details import build_order_detail_payload
from ibkr_api.orders.relationships import get_order_status_transition_text, normalize_order_role, resolve_order_relationship
from ibkr_api.orders.timestamps import resolve_order_status_event_times
from ibkr_api.orders.values import ensure_object, first_defined, to_float, to_text


ORDER_ACTION_SORT = "-created,-updated,-bar_time_ms"
ORDER_ACTION_LIMIT = 200
CANCEL_GROUP_ACTION = "cancel_group"
CLOSE_GROUP_ACTION = "close_group"


def build_action_identifiers(payload: dict[str, Any] | None) -> dict[str, str]:
    data = payload or {}
    target_id = to_text(
        first_defined(
            data.get("id"),
            data.get("unique_id"),
            data.get("entry_order_unique_id"),
            data.get("trade_group_id"),
        )
    )
    broker_order_id = to_text(first_defined(data.get("broker_order_id"), data.get("order_id")))
    return {
        "target_id": target_id,
        "broker_order_id": broker_order_id,
        "broker_lookup_id": broker_order_id or (target_id if target_id.isdigit() else ""),
    }


def normalize_order_row(order_row: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(order_row or {})
    extra = ensure_object(row.get("extra"))
    status = to_text(first_defined(row.get("status"), extra.get("status"), extra.get("current_status")))
    role = normalize_order_role(first_defined(row.get("role"), extra.get("role")), row.get("order_type"))
    unique_id = to_text(first_defined(row.get("unique_id"), extra.get("unique_id"), row.get("id")))
    entry_order_unique_id = to_text(
        first_defined(row.get("entry_order_unique_id"), extra.get("entry_order_unique_id"), unique_id)
    )
    trade_group_id = to_text(
        first_defined(row.get("trade_group_id"), extra.get("trade_group_id"), entry_order_unique_id, unique_id)
    )
    broker_order_id = to_text(
        first_defined(row.get("broker_order_id"), extra.get("broker_order_id"), row.get("order_id"), extra.get("order_id"))
    )
    quantity = to_float(first_defined(row.get("quantity"), extra.get("quantity"))) or 0.0
    filled_qty = to_float(first_defined(row.get("filled_qty"), extra.get("filled_qty")))
    if filled_qty is None:
        filled_qty = quantity if status == "Filled" else 0.0
    return {
        "id": to_text(first_defined(row.get("id"), unique_id)),
        "unique_id": unique_id,
        "trade_group_id": trade_group_id,
        "entry_order_unique_id": entry_order_unique_id,
        "broker_order_id": broker_order_id,
        "status": status,
        "role": role,
        "filled_qty": float(filled_qty),
        "symbol": to_text(first_defined(row.get("symbol"), extra.get("symbol"))),
        "environment": to_text(first_defined(row.get("environment"), extra.get("environment"), "live")) or "live",
    }


def is_closed_status(status: Any) -> bool:
    return to_text(status) in {"Filled", "Canceled", "Closed"}


def broker_cancel_response_looks_closed(payload: dict[str, Any] | None) -> bool:
    data = ensure_object(payload)
    nested_payload = ensure_object(data.get("payload"))
    error_text = to_text(
        first_defined(
            data.get("error"),
            data.get("message"),
            data.get("raw"),
            nested_payload.get("error"),
            nested_payload.get("message"),
            nested_payload.get("raw"),
        )
    ).lower()
    if not error_text:
        return False
    return any(
        marker in error_text
        for marker in (
            "already canceled",
            "already cancelled",
            "already inactive",
            "not active",
            "inactive",
            "not found",
            "cannot be cancelled",
            "cannot be canceled",
            "cannot cancel",
            "filled",
        )
    )


def resolve_trade_group_id(order_row: dict[str, Any] | None) -> str:
    return normalize_order_row(order_row).get("trade_group_id", "")


def resolve_cancelable_broker_order_id(order_row: dict[str, Any] | None) -> str:
    normalized = normalize_order_row(order_row)
    broker_order_id = normalized.get("broker_order_id", "")
    return broker_order_id if broker_order_id.isdigit() else ""


def dedupe_order_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows or []):
        normalized = normalize_order_row(row)
        key = normalized["id"] or normalized["unique_id"] or f"row-{index}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dict(row))
    return deduped


def pick_primary_order_row(rows: list[dict[str, Any]] | None, fallback_row: dict[str, Any] | None = None) -> dict[str, Any] | None:
    candidates = dedupe_order_rows(rows)
    for row in candidates:
        if normalize_order_row(row)["role"] == "entry":
            return row
    for row in candidates:
        normalized = normalize_order_row(row)
        if normalized["unique_id"] and normalized["unique_id"] == normalized["entry_order_unique_id"]:
            return row
    if fallback_row:
        return dict(fallback_row)
    return dict(candidates[0]) if candidates else None


def _query_order_rows(pb: Any, filter_value: str) -> list[dict[str, Any]]:
    rows = pb.get_records(
        "orders",
        filter=filter_value,
        sort=ORDER_ACTION_SORT,
        per_page=ORDER_ACTION_LIMIT,
        page=1,
    )
    return [dict(row) for row in rows or []]


def _add_lookup_alias(aliases: list[str], value: Any) -> None:
    text = to_text(value)
    if text and text not in aliases:
        aliases.append(text)


def build_group_lookup_aliases(*values: Any) -> list[str]:
    aliases: list[str] = []
    for value in values:
        text = to_text(value)
        if not text:
            continue
        _add_lookup_alias(aliases, text)
        if text.startswith("entry_"):
            _add_lookup_alias(aliases, text.removeprefix("entry_"))
        else:
            _add_lookup_alias(aliases, f"entry_{text}")
    return aliases


def build_lookup_filter_for_values(
    fields: tuple[str, ...],
    values: list[str] | tuple[str, ...],
    environment: str,
    escape_filter_string: Callable[[Any], str],
) -> str:
    escaped_environment = escape_filter_string(environment)
    or_terms = []
    for value in values:
        escaped_value = escape_filter_string(value)
        or_terms.extend(f'{field} = "{escaped_value}"' for field in fields)
    return f"({' || '.join(or_terms)}) && environment = \"{escaped_environment}\""


def build_lookup_filter(fields: tuple[str, ...], value: str, environment: str, escape_filter_string: Callable[[Any], str]) -> str:
    return build_lookup_filter_for_values(fields, [value], environment, escape_filter_string)


def query_order_rows_by_aliases(
    pb: Any,
    *,
    fields: tuple[str, ...],
    aliases: list[str] | tuple[str, ...],
    environment: str,
    escape_filter_string: Callable[[Any], str],
) -> list[dict[str, Any]]:
    lookup_values = [value for value in aliases if to_text(value)]
    if not lookup_values:
        return []
    return _query_order_rows(
        pb,
        build_lookup_filter_for_values(fields, lookup_values, environment, escape_filter_string),
    )


def build_related_group_aliases(rows: list[dict[str, Any]] | None, *seed_values: Any) -> list[str]:
    aliases = build_group_lookup_aliases(*seed_values)
    for row in rows or []:
        normalized = normalize_order_row(row)
        aliases = build_group_lookup_aliases(
            *aliases,
            normalized.get("trade_group_id"),
            normalized.get("entry_order_unique_id"),
            normalized.get("unique_id"),
        )
    return aliases


def load_order_action_context(
    pb: Any,
    *,
    payload: dict[str, Any],
    environment: str,
    escape_filter_string: Callable[[Any], str],
) -> dict[str, Any]:
    identifiers = build_action_identifiers(payload)
    target_id = identifiers["target_id"]
    broker_lookup_id = identifiers["broker_lookup_id"]

    matched_rows: list[dict[str, Any]] = []
    if target_id:
        matched_rows = query_order_rows_by_aliases(
            pb,
            fields=("unique_id", "entry_order_unique_id", "trade_group_id"),
            aliases=build_group_lookup_aliases(target_id),
            environment=environment,
            escape_filter_string=escape_filter_string,
        )
    if not matched_rows and broker_lookup_id:
        matched_rows = _query_order_rows(
            pb,
            build_lookup_filter(
                ("broker_order_id", "order_id"),
                broker_lookup_id,
                environment,
                escape_filter_string,
            ),
        )

    deduped_rows = dedupe_order_rows(matched_rows)
    if not deduped_rows:
        return {
            "action_row": None,
            "primary_row": None,
            "related_rows": [],
            "trade_group_id": "",
            **identifiers,
        }

    exact_broker_row = None
    if broker_lookup_id:
        exact_broker_row = next(
            (row for row in deduped_rows if resolve_cancelable_broker_order_id(row) == broker_lookup_id),
            None,
        )
    exact_unique_row = None
    if target_id:
        exact_unique_row = next(
            (row for row in deduped_rows if normalize_order_row(row)["unique_id"] == target_id),
            None,
        )
    primary_from_matches = pick_primary_order_row(deduped_rows, deduped_rows[0])
    action_row = exact_broker_row or exact_unique_row or primary_from_matches
    initial_trade_group_id = resolve_trade_group_id(primary_from_matches or action_row)

    related_rows = []
    related_aliases = build_related_group_aliases(deduped_rows, initial_trade_group_id, target_id)
    if related_aliases:
        related_rows = query_order_rows_by_aliases(
            pb,
            fields=("trade_group_id", "entry_order_unique_id", "unique_id"),
            aliases=related_aliases,
            environment=environment,
            escape_filter_string=escape_filter_string,
        )
    resolved_related_rows = dedupe_order_rows(related_rows or deduped_rows)
    primary_row = pick_primary_order_row(resolved_related_rows, primary_from_matches or action_row)

    return {
        "action_row": action_row,
        "primary_row": primary_row,
        "related_rows": resolved_related_rows,
        "trade_group_id": resolve_trade_group_id(primary_row or action_row),
        **identifiers,
    }


def build_group_status_patch(
    order_row: dict[str, Any],
    *,
    next_status: str,
    source: str,
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    existing = dict(order_row)
    normalized = normalize_order_row(existing)
    existing_extra = ensure_object(existing.get("extra"))
    previous_status = normalized["status"]
    relation = resolve_order_relationship({"status": next_status, "relation_status": "closed"}, existing)
    event_times = resolve_order_status_event_times(
        existing,
        {
            "status": next_status,
            "previous_status": previous_status,
        },
    )
    order_time = to_text(first_defined(existing.get("order_time"), existing_extra.get("order_time"), event_times["us_time"]))
    patch_extra = {
        **existing_extra,
        "environment": normalized["environment"],
        "order_time": order_time,
        "us_time": event_times["us_time"],
        "cn_time": event_times["cn_time"],
        "bar_time_ms": event_times["bar_time_ms"],
        "trade_group_id": relation["trade_group_id"],
        "entry_order_unique_id": relation["entry_order_unique_id"],
        "parent_order_unique_id": relation["parent_order_unique_id"],
        "sibling_order_unique_id": relation["sibling_order_unique_id"],
        "role": relation["role"],
        "relation_status": relation["relation_status"],
        "position_side": relation["position_side"],
        "previous_status": previous_status,
        "current_status": next_status,
        "status_transition_text": get_order_status_transition_text(previous_status, next_status),
        "status_updated_us_time": event_times["us_time"],
        "status_updated_cn_time": event_times["cn_time"],
        "status_updated_bar_time_ms": event_times["bar_time_ms"],
        "last_status_source": source,
        "last_status_reason": reason,
    }
    if not existing_extra.get("created_us_time") and not previous_status:
        patch_extra["created_us_time"] = event_times["us_time"]
        patch_extra["created_cn_time"] = event_times["cn_time"]
        patch_extra["created_bar_time_ms"] = event_times["bar_time_ms"]
    fill_time = to_text(first_defined(existing.get("fill_time"), existing_extra.get("fill_time")))
    if fill_time:
        patch_extra["fill_time"] = fill_time
    if existing_extra.get("filled_us_time"):
        patch_extra["filled_us_time"] = existing_extra.get("filled_us_time")
        patch_extra["filled_cn_time"] = existing_extra.get("filled_cn_time") or ""
        patch_extra["filled_bar_time_ms"] = existing_extra.get("filled_bar_time_ms") or 0

    patch = {
        "status": next_status,
        "trade_group_id": relation["trade_group_id"],
        "entry_order_unique_id": relation["entry_order_unique_id"],
        "parent_order_unique_id": relation["parent_order_unique_id"],
        "sibling_order_unique_id": relation["sibling_order_unique_id"],
        "role": relation["role"],
        "relation_status": relation["relation_status"],
        "broker_order_id": relation["broker_order_id"],
        "position_side": relation["position_side"],
        "us_time": event_times["us_time"],
        "cn_time": event_times["cn_time"],
        "bar_time_ms": event_times["bar_time_ms"],
        "order_time": order_time,
        "extra": patch_extra,
    }
    if fill_time:
        patch["fill_time"] = fill_time
    return patch, event_times


def append_group_order_detail(
    pb: Any,
    updated_order_row: dict[str, Any],
    *,
    source: str,
    reason: str,
    event_times: dict[str, Any],
    extra_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail_payload = build_order_detail_payload(pb, updated_order_row, source=source, reason=reason)
    detail_payload["status"] = updated_order_row.get("status") or detail_payload.get("status") or ""
    detail_payload["us_time"] = event_times.get("us_time") or detail_payload.get("us_time") or ""
    detail_payload["cn_time"] = event_times.get("cn_time") or detail_payload.get("cn_time") or ""
    detail_payload["bar_time_ms"] = event_times.get("bar_time_ms") or detail_payload.get("bar_time_ms") or 0
    detail_payload["extra"] = {
        **ensure_object(detail_payload.get("extra")),
        **ensure_object(extra_patch),
    }
    return pb.create_record("ibkr_order_details", detail_payload)
