from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, to_float, to_int, to_text

NormalizeEnvironment = Callable[[Any, str], str]
RequestJsonRequest = Callable[..., dict[str, Any]]


OPEN_ORDER_FILTER_PER_PAGE = 800


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            import json

            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _parse_time_ms(value: Any) -> int:
    text = to_text(value)
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).timestamp() * 1000)
        except Exception:
            continue
    return 0


def _pick_first_non_empty(*values: Any) -> str:
    for value in values:
        text = to_text(value)
        if text:
            return text
    return ""


def _clone_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    items: list[str] = []
    for value in values:
        text = to_text(value)
        if text:
            items.append(text)
    return items


def canonical_order_status(status: Any) -> str:
    key = to_text(status).upper()
    if key in {"PENDING", "PRESUBMITTED", "SUBMITTED", "PENDINGSUBMIT", "INPROGRESS", "INIT", "APIPENDING", "API_PENDING"}:
        return "SUBMITTED"
    if key in {"FILLED", "EXECUTED"}:
        return "FILLED"
    if key in {"CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}:
        return "CANCELED"
    return key or "UNKNOWN"


def _order_status_weight(status: Any) -> int:
    key = canonical_order_status(status)
    if key == "FILLED":
        return 90
    if key == "SUBMITTED":
        return 70
    if key == "CANCELED":
        return 10
    return 20


def _signal_status_weight(status: Any) -> int:
    key = to_text(status).lower()
    if key == "executed":
        return 90
    if key == "confirmed":
        return 80
    if key == "awaiting_confirm":
        return 70
    if key == "pending":
        return 60
    if key in {"rejected", "expired"}:
        return 20
    return 30


def normalize_order_record(record: dict[str, Any]) -> dict[str, Any]:
    extra = _parse_json_object(record.get("extra"))
    quantity = to_float(record.get("quantity")) or 0.0
    filled_qty_raw = to_float(record.get("filled_qty")) or 0.0
    status = _pick_first_non_empty(record.get("status"), extra.get("status"))
    role = _pick_first_non_empty(record.get("role"), extra.get("role")) or "entry"
    trade_group_id = _pick_first_non_empty(
        record.get("trade_group_id"),
        extra.get("trade_group_id"),
        record.get("entry_order_unique_id"),
        extra.get("entry_order_unique_id"),
        record.get("unique_id"),
    )
    entry_order_unique_id = _pick_first_non_empty(
        record.get("entry_order_unique_id"),
        extra.get("entry_order_unique_id"),
        record.get("unique_id"),
    )
    signal_id = _pick_first_non_empty(record.get("signal_id"), extra.get("signal_id"))
    updated = _pick_first_non_empty(record.get("updated"), record.get("us_time"), record.get("created"))
    relation_status = _pick_first_non_empty(record.get("relation_status"), extra.get("relation_status"))
    broker_order_id = _pick_first_non_empty(record.get("broker_order_id"), extra.get("broker_order_id"), record.get("order_id"))
    order_id = _pick_first_non_empty(record.get("order_id"), extra.get("order_id"), broker_order_id)
    unique_id = _pick_first_non_empty(record.get("unique_id"), extra.get("unique_id"))
    group_key = _pick_first_non_empty(trade_group_id, entry_order_unique_id, unique_id, broker_order_id, order_id)
    filled_qty = filled_qty_raw if filled_qty_raw > 0 else (quantity if canonical_order_status(status) == "FILLED" else 0.0)
    return {
        "record_id": to_text(record.get("id")),
        "symbol": _pick_first_non_empty(record.get("symbol"), extra.get("symbol")).upper(),
        "status": status,
        "status_key": canonical_order_status(status),
        "signal_id": signal_id,
        "trade_group_id": trade_group_id,
        "entry_order_unique_id": entry_order_unique_id,
        "parent_order_unique_id": _pick_first_non_empty(record.get("parent_order_unique_id"), extra.get("parent_order_unique_id")),
        "sibling_order_unique_id": _pick_first_non_empty(record.get("sibling_order_unique_id"), extra.get("sibling_order_unique_id")),
        "unique_id": unique_id,
        "broker_order_id": broker_order_id,
        "order_id": order_id,
        "role": role,
        "relation_status": relation_status,
        "direction": _pick_first_non_empty(record.get("direction"), extra.get("direction")),
        "position_side": _pick_first_non_empty(record.get("position_side"), extra.get("position_side")),
        "quantity": quantity,
        "filled_qty": filled_qty,
        "limit_price": to_float(record.get("limit_price") if record.get("limit_price") not in (None, "") else extra.get("limit_price")) or 0.0,
        "fill_price": to_float(record.get("fill_price") if record.get("fill_price") not in (None, "") else extra.get("fill_price")) or 0.0,
        "updated": updated,
        "updated_ms": _parse_time_ms(updated),
        "status_weight": _order_status_weight(status),
        "group_key": group_key,
        "extra": extra,
    }


def normalize_signal_record(record: dict[str, Any]) -> dict[str, Any]:
    extra = _parse_json_object(record.get("extra"))
    updated = _pick_first_non_empty(record.get("updated"), record.get("us_time"), record.get("created"))
    status = _pick_first_non_empty(record.get("status"), extra.get("status"))
    return {
        "signal_id": _pick_first_non_empty(record.get("signal_id"), extra.get("signal_id")),
        "symbol": _pick_first_non_empty(record.get("symbol"), extra.get("symbol")).upper(),
        "status": status,
        "note": _pick_first_non_empty(record.get("note"), extra.get("status_reason"), extra.get("note")),
        "updated": updated,
        "updated_ms": _parse_time_ms(updated),
        "status_weight": _signal_status_weight(status),
    }


def _is_closed_order_status(status: Any) -> bool:
    return canonical_order_status(status) in {"FILLED", "CANCELED", "CLOSED", "REJECTED", "INACTIVE", "EXPIRED"}


def _is_open_like_order(order: dict[str, Any]) -> bool:
    if not order or _is_closed_order_status(order.get("status")):
        return False
    relation_status = to_text(order.get("relation_status")).lower()
    if relation_status in {"active", "planned"}:
        return True
    return to_int(order.get("status_weight"), 0) >= 40


def _serialize_managed_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": to_text(order.get("record_id")),
        "symbol": to_text(order.get("symbol")),
        "unique_id": to_text(order.get("unique_id")),
        "order_id": to_text(order.get("order_id")),
        "broker_order_id": to_text(order.get("broker_order_id")),
        "signal_id": to_text(order.get("signal_id")),
        "trade_group_id": to_text(order.get("trade_group_id")),
        "entry_order_unique_id": to_text(order.get("entry_order_unique_id")),
        "parent_order_unique_id": to_text(order.get("parent_order_unique_id")),
        "sibling_order_unique_id": to_text(order.get("sibling_order_unique_id")),
        "role": to_text(order.get("role")),
        "relation_status": to_text(order.get("relation_status")),
        "direction": to_text(order.get("direction")),
        "position_side": to_text(order.get("position_side")),
        "status": to_text(order.get("status")),
        "quantity": to_float(order.get("quantity")) or 0.0,
        "filled_qty": to_float(order.get("filled_qty")) or 0.0,
        "limit_price": to_float(order.get("limit_price")) or 0.0,
        "fill_price": to_float(order.get("fill_price")) or 0.0,
        "updated": to_text(order.get("updated")),
    }


def _build_pb_order_lookup(normalized_orders: list[dict[str, Any]]) -> dict[str, Any]:
    lookup = {
        "by_broker_order_id": {},
        "by_order_id": {},
        "by_unique_id": {},
        "ambiguous_broker_order_ids": {},
        "ambiguous_order_ids": {},
    }

    def track_match(bucket: dict[str, Any], ambiguous: dict[str, bool], key: str, order: dict[str, Any]) -> None:
        if not key:
            return
        existing = bucket.get(key)
        if not existing:
            bucket[key] = order
            return
        if existing.get("record_id") == order.get("record_id"):
            return
        ambiguous[key] = True
        bucket[key] = None

    for order in normalized_orders:
        if _is_open_like_order(order):
            track_match(lookup["by_broker_order_id"], lookup["ambiguous_broker_order_ids"], to_text(order.get("broker_order_id")), order)
            track_match(lookup["by_order_id"], lookup["ambiguous_order_ids"], to_text(order.get("order_id")), order)
        unique_id = to_text(order.get("unique_id"))
        if unique_id and unique_id not in lookup["by_unique_id"]:
            lookup["by_unique_id"][unique_id] = order
    return lookup


def normalize_live_order(order: dict[str, Any]) -> dict[str, Any]:
    normalized = ensure_object(order)
    submitted_time = to_text(normalized.get("submitted_time"))
    last_execution_time = to_text(normalized.get("last_execution_time"))
    good_till_date = to_text(normalized.get("good_till_date"))
    client_order_id = _pick_first_non_empty(
        normalized.get("client_order_id"),
        normalized.get("cOID"),
        normalized.get("coid"),
        normalized.get("order_ref"),
        normalized.get("orderRef"),
    )
    parent_id = _pick_first_non_empty(normalized.get("parent_id"), normalized.get("parentId"))
    status = _pick_first_non_empty(normalized.get("status"), normalized.get("status_key"))
    status_key = canonical_order_status(first := (normalized.get("status_key") or status))
    total_quantity = to_float(normalized.get("total_quantity") if normalized.get("total_quantity") not in (None, "") else normalized.get("totalSize"))
    if total_quantity is None:
        total_quantity = to_float(normalized.get("quantity")) or 0.0
    filled_quantity = to_float(normalized.get("filled_quantity") if normalized.get("filled_quantity") not in (None, "") else normalized.get("filledQuantity"))
    if filled_quantity is None:
        filled_quantity = to_float(normalized.get("cum_fill")) or 0.0
    remaining_quantity = normalized.get("remaining_quantity")
    if remaining_quantity in (None, ""):
        remaining_quantity = normalized.get("remainingQuantity")
    if remaining_quantity in (None, ""):
        remaining_quantity = normalized.get("remainingSize")
    remaining = to_float(remaining_quantity) if remaining_quantity not in (None, "") else max(total_quantity - filled_quantity, 0.0)
    is_open = bool(normalized.get("is_open")) if normalized.get("is_open") is not None else not _is_closed_order_status(status_key)
    seed_sources = _clone_string_list(normalized.get("seed_sources"))
    diagnostic_tags = _clone_string_list(normalized.get("diagnostic_tags"))
    updated_ms = max(
        to_int(normalized.get("last_execution_time_ms"), 0),
        to_int(normalized.get("submitted_time_ms"), 0),
        _parse_time_ms(last_execution_time or submitted_time),
    )
    return {
        "order_id": _pick_first_non_empty(normalized.get("order_id"), normalized.get("orderId"), normalized.get("id")),
        "parent_id": parent_id,
        "client_order_id": client_order_id,
        "symbol": to_text(normalized.get("symbol")).upper(),
        "conid": to_int(normalized.get("conid"), 0),
        "side": to_text(normalized.get("side")).upper(),
        "status": status,
        "status_key": status_key,
        "role": _pick_first_non_empty(normalized.get("role")),
        "order_type": to_text(normalized.get("order_type") or normalized.get("orderType")).upper(),
        "order_description": to_text(normalized.get("order_description")),
        "price": to_float(normalized.get("price")) or 0.0,
        "trigger_price": to_float(normalized.get("trigger_price")) or 0.0,
        "avg_price": to_float(normalized.get("avg_price")) or 0.0,
        "total_quantity": total_quantity,
        "filled_quantity": filled_quantity,
        "remaining_quantity": remaining if remaining is not None else max(total_quantity - filled_quantity, 0.0),
        "time_in_force": to_text(normalized.get("time_in_force")).upper(),
        "account": to_text(normalized.get("account")),
        "currency": to_text(normalized.get("currency") or "USD").upper(),
        "asset_class": to_text(normalized.get("asset_class")).upper(),
        "listing_exchange": to_text(normalized.get("listing_exchange")),
        "submitted_time": submitted_time,
        "submitted_time_ms": to_int(normalized.get("submitted_time_ms"), _parse_time_ms(submitted_time)),
        "last_execution_time": last_execution_time,
        "last_execution_time_ms": to_int(normalized.get("last_execution_time_ms"), _parse_time_ms(last_execution_time)),
        "good_till_date": good_till_date,
        "good_till_date_ms": to_int(normalized.get("good_till_date_ms"), _parse_time_ms(good_till_date)),
        "outside_rth": bool(normalized.get("outside_rth")),
        "can_cancel": bool(normalized.get("can_cancel")),
        "can_modify": bool(normalized.get("can_modify")),
        "is_open": is_open,
        "is_child": bool(normalized.get("is_child")) if normalized.get("is_child") is not None else bool(parent_id),
        "recovery_source": _pick_first_non_empty(normalized.get("recovery_source"), normalized.get("_recovery_source")) or "bulk",
        "seed_sources": seed_sources,
        "diagnostic_tags": diagnostic_tags,
        "diagnostic_note": to_text(normalized.get("diagnostic_note")),
        "updated_ms": updated_ms,
        "raw": ensure_object(normalized.get("raw")) or normalized,
    }


def _resolve_pb_match(live_order: dict[str, Any], lookup: dict[str, Any]) -> dict[str, Any] | None:
    client_order_id = to_text(live_order.get("client_order_id"))
    if client_order_id and lookup["by_unique_id"].get(client_order_id):
        return lookup["by_unique_id"][client_order_id]
    order_id = to_text(live_order.get("order_id"))
    if order_id and lookup["by_broker_order_id"].get(order_id):
        return lookup["by_broker_order_id"][order_id]
    if order_id and lookup["by_order_id"].get(order_id):
        return lookup["by_order_id"][order_id]
    return None


def _is_ambiguous_pb_order_match(live_order: dict[str, Any], lookup: dict[str, Any]) -> bool:
    order_id = to_text(live_order.get("order_id"))
    if not order_id:
        return False
    return bool(lookup["ambiguous_broker_order_ids"].get(order_id) or lookup["ambiguous_order_ids"].get(order_id))


def _build_live_group_key(live_order: dict[str, Any], pb_match: dict[str, Any] | None) -> str:
    return _pick_first_non_empty(
        (pb_match or {}).get("trade_group_id"),
        (pb_match or {}).get("entry_order_unique_id"),
        live_order.get("parent_id"),
        live_order.get("order_id"),
        live_order.get("client_order_id"),
        f"{to_text(live_order.get('symbol')).upper()}:{to_text(live_order.get('side')).upper()}:{to_text(live_order.get('order_type')).upper()}",
    )


def _build_diagnostic_note(live_order: dict[str, Any], pb_match: dict[str, Any] | None, tags: list[str], ambiguous_match: bool) -> str:
    notes: list[str] = []
    if not pb_match:
        notes.append("broker_order_id 命中多个 PB 订单记录，已跳过自动关联" if ambiguous_match else "broker 实时挂单未匹配到 PB 订单记录")
    if "status_recovered" in tags:
        notes.append("该订单由单笔状态接口补回")
    mismatch_tags = [tag for tag in tags if tag in {"status_mismatch", "quantity_mismatch", "filled_qty_mismatch"}]
    if pb_match and mismatch_tags:
        notes.append(f"PB 对账差异: {', '.join(mismatch_tags)}")
    if "missing_client_order_id" in tags:
        notes.append("client_order_id 缺失")
    return "；".join(notes)


def _build_pb_context(live_order: dict[str, Any], pb_match: dict[str, Any] | None, ambiguous_match: bool) -> dict[str, Any]:
    tags = _clone_string_list(live_order.get("diagnostic_tags"))
    live_total_quantity = to_float(live_order.get("total_quantity")) or 0.0
    live_filled_quantity = to_float(live_order.get("filled_quantity")) or 0.0
    live_has_explicit_quantity = live_total_quantity > 0 or live_filled_quantity > 0
    if live_order.get("recovery_source") == "status_recovered" and "status_recovered" not in tags:
        tags.append("status_recovered")
    if not to_text(live_order.get("client_order_id")) and "missing_client_order_id" not in tags:
        tags.append("missing_client_order_id")
    if ambiguous_match and "ambiguous_broker_order_id" not in tags:
        tags.append("ambiguous_broker_order_id")
    if not pb_match:
        return {
            "match_state": "broker_only",
            "record_id": "",
            "signal_id": "",
            "trade_group_id": "",
            "entry_order_unique_id": "",
            "parent_order_unique_id": "",
            "sibling_order_unique_id": "",
            "role": "",
            "relation_status": "",
            "direction": "",
            "position_side": "",
            "pb_status": "",
            "pb_quantity": 0.0,
            "pb_filled_qty": 0.0,
            "pb_limit_price": 0.0,
            "pb_fill_price": 0.0,
            "diagnostic_tags": tags,
            "diagnostic_note": _build_diagnostic_note(live_order, None, tags, ambiguous_match),
        }
    if canonical_order_status(live_order.get("status_key") or live_order.get("status")) != pb_match.get("status_key") and "status_mismatch" not in tags:
        tags.append("status_mismatch")
    if live_has_explicit_quantity and abs(live_total_quantity - (to_float(pb_match.get("quantity")) or 0.0)) > 1e-9 and "quantity_mismatch" not in tags:
        tags.append("quantity_mismatch")
    if live_filled_quantity > 0 and abs(live_filled_quantity - (to_float(pb_match.get("filled_qty")) or 0.0)) > 1e-9 and "filled_qty_mismatch" not in tags:
        tags.append("filled_qty_mismatch")
    return {
        "match_state": "matched",
        "record_id": to_text(pb_match.get("record_id")),
        "signal_id": to_text(pb_match.get("signal_id")),
        "trade_group_id": to_text(pb_match.get("trade_group_id") or pb_match.get("group_key")),
        "entry_order_unique_id": to_text(pb_match.get("entry_order_unique_id") or pb_match.get("unique_id")),
        "parent_order_unique_id": to_text(pb_match.get("parent_order_unique_id")),
        "sibling_order_unique_id": to_text(pb_match.get("sibling_order_unique_id")),
        "role": to_text(pb_match.get("role")),
        "relation_status": to_text(pb_match.get("relation_status")),
        "direction": to_text(pb_match.get("direction")),
        "position_side": to_text(pb_match.get("position_side")),
        "pb_status": to_text(pb_match.get("status")),
        "pb_quantity": to_float(pb_match.get("quantity")) or 0.0,
        "pb_filled_qty": to_float(pb_match.get("filled_qty")) or 0.0,
        "pb_limit_price": to_float(pb_match.get("limit_price")) or 0.0,
        "pb_fill_price": to_float(pb_match.get("fill_price")) or 0.0,
        "diagnostic_tags": tags,
        "diagnostic_note": _build_diagnostic_note(live_order, pb_match, tags, ambiguous_match),
    }


def _finalize_group(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_key": group["group_key"],
        "symbol": group["symbol"],
        "signal_id": group["signal_id"],
        "trade_group_id": group["trade_group_id"],
        "entry_order_unique_id": group["entry_order_unique_id"],
        "latest_updated": group["latest_updated"],
        "latest_updated_ms": group["latest_updated_ms"],
        "latest_order_status": group["latest_order_status"],
        "match_state": "matched" if group["matched_live_orders"] > 0 else "broker_only",
        "live_order_count": group["live_order_count"],
        "matched_live_orders": group["matched_live_orders"],
        "broker_only_live_orders": group["broker_only_live_orders"],
        "cancelable_orders": group["cancelable_orders"],
        "editable_orders": group["editable_orders"],
        "outside_rth_orders": group["outside_rth_orders"],
        "total_quantity": group["total_quantity"],
        "filled_quantity": group["filled_quantity"],
        "remaining_quantity": group["remaining_quantity"],
        "recovery_sources": [f"{key}:{group['recovery_sources'][key]}" for key in sorted(group["recovery_sources"])],
        "orders": group["orders"],
    }


def _sort_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        groups or [],
        key=lambda item: (
            -((100 if to_int(item.get("matched_live_orders"), 0) > 0 else 0) + to_int(item.get("cancelable_orders"), 0)),
            -to_int(item.get("latest_updated_ms"), 0),
        ),
    )


def build_managed_order_context(pb: Any, environment: str, live_orders: list[dict[str, Any]]) -> dict[str, Any]:
    order_rows = pb.get_records("orders", filter=f'environment = "{environment}"', sort="-updated", per_page=OPEN_ORDER_FILTER_PER_PAGE, page=1) or []
    normalized_order_records = [normalize_order_record(dict(row)) for row in order_rows if isinstance(row, dict)]
    normalized_order_records = [row for row in normalized_order_records if row.get("symbol")]
    pb_lookup = _build_pb_order_lookup(normalized_order_records)
    active_pb_groups_by_key: dict[str, dict[str, Any]] = {}
    active_order_count = 0
    for normalized in normalized_order_records:
        group_key = to_text(normalized.get("group_key"))
        if not group_key:
            continue
        group = active_pb_groups_by_key.setdefault(
            group_key,
            {
                "symbol": normalized.get("symbol"),
                "signal_id": to_text(normalized.get("signal_id")),
                "trade_group_id": to_text(normalized.get("trade_group_id") or group_key),
                "entry_order_unique_id": to_text(normalized.get("entry_order_unique_id") or normalized.get("unique_id") or group_key),
                "latest_updated_ms": 0,
                "latest_updated": "",
                "latest_order_status": "",
                "best_status_weight": -1,
                "entry_filled_qty": 0.0,
                "exit_filled_qty": 0.0,
                "has_active_order": False,
                "orders": [],
            },
        )
        group["orders"].append(normalized)
        if not group["signal_id"] and normalized.get("signal_id"):
            group["signal_id"] = normalized.get("signal_id")
        if to_int(normalized.get("updated_ms"), 0) >= group["latest_updated_ms"]:
            group["latest_updated_ms"] = to_int(normalized.get("updated_ms"), 0)
            group["latest_updated"] = to_text(normalized.get("updated"))
        if to_int(normalized.get("status_weight"), 0) >= group["best_status_weight"]:
            group["best_status_weight"] = to_int(normalized.get("status_weight"), 0)
            group["latest_order_status"] = to_text(normalized.get("status"))
        role = to_text(normalized.get("role"))
        if role == "entry":
            group["entry_filled_qty"] += abs(to_float(normalized.get("filled_qty")) or 0.0)
        if role in {"take_profit", "stop_loss"}:
            group["exit_filled_qty"] += abs(to_float(normalized.get("filled_qty")) or 0.0)
        if _is_open_like_order(normalized):
            group["has_active_order"] = True
            active_order_count += 1

    normalized_live_orders: list[dict[str, Any]] = []
    live_groups_by_key: dict[str, dict[str, Any]] = {}
    matched_pb_group_counts: dict[str, int] = {}
    broker_only_orders: list[dict[str, Any]] = []
    matched_live_order_count = 0
    broker_only_live_order_count = 0
    editable_order_count = 0
    cancelable_order_count = 0
    outside_rth_order_count = 0
    missing_client_order_id_count = 0
    status_mismatch_count = 0
    quantity_mismatch_count = 0
    filled_qty_mismatch_count = 0

    for live_order_raw in live_orders or []:
        live_order = normalize_live_order(live_order_raw)
        if not to_text(live_order.get("order_id")) or not live_order.get("is_open"):
            continue
        pb_match = _resolve_pb_match(live_order, pb_lookup)
        ambiguous_pb_match = not pb_match and _is_ambiguous_pb_order_match(live_order, pb_lookup)
        pb_context = _build_pb_context(live_order, pb_match, ambiguous_pb_match)
        live_order["pb_context"] = pb_context
        live_order["diagnostic_tags"] = _clone_string_list(pb_context.get("diagnostic_tags"))
        live_order["diagnostic_note"] = to_text(pb_context.get("diagnostic_note"))
        live_order["signal_id"] = to_text(pb_context.get("signal_id"))
        live_order["trade_group_id"] = to_text(pb_context.get("trade_group_id"))
        live_order["entry_order_unique_id"] = to_text(pb_context.get("entry_order_unique_id"))
        live_order["relation_status"] = to_text(pb_context.get("relation_status"))
        live_order["match_state"] = to_text(pb_context.get("match_state"))
        if not to_text(live_order.get("symbol")) and pb_match and pb_match.get("symbol"):
            live_order["symbol"] = pb_match.get("symbol")
        if not to_text(live_order.get("role")) and pb_context.get("role"):
            live_order["role"] = pb_context.get("role")
        if (to_float(live_order.get("total_quantity")) or 0.0) <= 0 and (to_float(pb_context.get("pb_quantity")) or 0.0) > 0:
            live_order["total_quantity"] = to_float(pb_context.get("pb_quantity")) or 0.0
        if (to_float(live_order.get("price")) or 0.0) <= 0 and (to_float(pb_context.get("pb_limit_price")) or 0.0) > 0:
            live_order["price"] = to_float(pb_context.get("pb_limit_price")) or 0.0
        live_order["direction"] = to_text(pb_context.get("direction"))
        live_order["position_side"] = to_text(pb_context.get("position_side"))
        normalized_live_orders.append(live_order)
        if pb_context.get("match_state") == "matched":
            matched_live_order_count += 1
            pb_group_key = _pick_first_non_empty((pb_match or {}).get("group_key"), pb_context.get("trade_group_id"), pb_context.get("entry_order_unique_id"))
            if pb_group_key:
                matched_pb_group_counts[pb_group_key] = matched_pb_group_counts.get(pb_group_key, 0) + 1
        else:
            broker_only_live_order_count += 1
            broker_only_orders.append(
                {
                    "symbol": to_text(live_order.get("symbol")),
                    "order_id": to_text(live_order.get("order_id")),
                    "client_order_id": to_text(live_order.get("client_order_id")),
                    "parent_id": to_text(live_order.get("parent_id")),
                    "status": to_text(live_order.get("status")),
                }
            )
        if live_order.get("can_modify"):
            editable_order_count += 1
        if live_order.get("can_cancel"):
            cancelable_order_count += 1
        if live_order.get("outside_rth"):
            outside_rth_order_count += 1
        if not to_text(live_order.get("client_order_id")):
            missing_client_order_id_count += 1
        tags = set(_clone_string_list(live_order.get("diagnostic_tags")))
        if "status_mismatch" in tags:
            status_mismatch_count += 1
        if "quantity_mismatch" in tags:
            quantity_mismatch_count += 1
        if "filled_qty_mismatch" in tags:
            filled_qty_mismatch_count += 1
        live_group_key = _build_live_group_key(live_order, pb_match)
        live_group = live_groups_by_key.setdefault(
            live_group_key,
            {
                "group_key": live_group_key,
                "symbol": to_text(live_order.get("symbol")),
                "signal_id": to_text(pb_context.get("signal_id")),
                "trade_group_id": to_text(pb_context.get("trade_group_id") or live_group_key),
                "entry_order_unique_id": to_text(pb_context.get("entry_order_unique_id") or live_order.get("client_order_id") or live_order.get("order_id")),
                "latest_updated": "",
                "latest_updated_ms": 0,
                "latest_order_status": "",
                "live_order_count": 0,
                "matched_live_orders": 0,
                "broker_only_live_orders": 0,
                "cancelable_orders": 0,
                "editable_orders": 0,
                "outside_rth_orders": 0,
                "total_quantity": 0.0,
                "filled_quantity": 0.0,
                "remaining_quantity": 0.0,
                "recovery_sources": {},
                "orders": [],
            },
        )
        live_group["live_order_count"] += 1
        if not live_group["signal_id"] and pb_context.get("signal_id"):
            live_group["signal_id"] = pb_context.get("signal_id")
        if not live_group["trade_group_id"] and pb_context.get("trade_group_id"):
            live_group["trade_group_id"] = pb_context.get("trade_group_id")
        if not live_group["entry_order_unique_id"] and pb_context.get("entry_order_unique_id"):
            live_group["entry_order_unique_id"] = pb_context.get("entry_order_unique_id")
        if pb_context.get("match_state") == "matched":
            live_group["matched_live_orders"] += 1
        else:
            live_group["broker_only_live_orders"] += 1
        if live_order.get("can_cancel"):
            live_group["cancelable_orders"] += 1
        if live_order.get("can_modify"):
            live_group["editable_orders"] += 1
        if live_order.get("outside_rth"):
            live_group["outside_rth_orders"] += 1
        live_group["total_quantity"] += abs(to_float(live_order.get("total_quantity")) or 0.0)
        live_group["filled_quantity"] += abs(to_float(live_order.get("filled_quantity")) or 0.0)
        live_group["remaining_quantity"] += abs(to_float(live_order.get("remaining_quantity")) or 0.0)
        live_group["orders"].append(live_order)
        updated_ms = max(to_int(live_order.get("last_execution_time_ms"), 0), to_int(live_order.get("submitted_time_ms"), 0), to_int(live_order.get("updated_ms"), 0))
        if updated_ms >= live_group["latest_updated_ms"]:
            live_group["latest_updated_ms"] = updated_ms
            live_group["latest_updated"] = to_text(live_order.get("last_execution_time") or live_order.get("submitted_time"))
            live_group["latest_order_status"] = to_text(live_order.get("status"))
        recovery_source = to_text(live_order.get("recovery_source")) or "bulk"
        live_group["recovery_sources"][recovery_source] = live_group["recovery_sources"].get(recovery_source, 0) + 1

    active_groups = []
    for key, group in active_pb_groups_by_key.items():
        has_open_exposure = group["entry_filled_qty"] > group["exit_filled_qty"]
        matched_live_orders = matched_pb_group_counts.get(key, 0)
        active_groups.append(
            {
                "symbol": group["symbol"],
                "signal_id": group["signal_id"],
                "trade_group_id": group["trade_group_id"],
                "entry_order_unique_id": group["entry_order_unique_id"],
                "latest_updated": group["latest_updated"],
                "latest_updated_ms": group["latest_updated_ms"],
                "latest_order_status": group["latest_order_status"],
                "has_active_order": group["has_active_order"],
                "has_open_exposure": has_open_exposure,
                "broker_matched": matched_live_orders > 0,
                "matched_broker_orders": matched_live_orders,
                "order_count": len(group["orders"]),
                "orders": [_serialize_managed_order(order) for order in group["orders"]],
            }
        )
    active_groups = [group for group in active_groups if group.get("has_active_order") or group.get("has_open_exposure")]
    active_groups.sort(
        key=lambda item: (
            -((1000 if item.get("has_open_exposure") else 0) + (100 if item.get("has_active_order") else 0) + (10 if item.get("broker_matched") else 0)),
            -to_int(item.get("latest_updated_ms"), 0),
        )
    )
    pb_only_active_groups = [group for group in active_groups if group.get("has_active_order") and not group.get("broker_matched")]
    live_order_groups = _sort_groups([_finalize_group(group) for group in live_groups_by_key.values()])
    matched_order_groups = [group for group in live_order_groups if to_int(group.get("matched_live_orders"), 0) > 0]
    broker_only_order_groups = [group for group in live_order_groups if to_int(group.get("matched_live_orders"), 0) == 0]
    normalized_live_orders.sort(
        key=lambda item: (
            0 if item.get("can_cancel") else 1,
            -to_int(item.get("updated_ms"), 0),
            to_text(item.get("order_id")),
        )
    )
    return {
        "order_records": normalized_order_records,
        "active_groups": active_groups,
        "pb_only_active_groups": pb_only_active_groups,
        "active_order_count": active_order_count,
        "broker_only_orders": broker_only_orders,
        "live_orders": normalized_live_orders,
        "live_order_groups": live_order_groups,
        "matched_order_groups": matched_order_groups,
        "broker_only_order_groups": broker_only_order_groups,
        "matched_live_order_count": matched_live_order_count,
        "broker_only_live_order_count": broker_only_live_order_count,
        "editable_order_count": editable_order_count,
        "cancelable_order_count": cancelable_order_count,
        "outside_rth_order_count": outside_rth_order_count,
        "missing_client_order_id_count": missing_client_order_id_count,
        "status_mismatch_count": status_mismatch_count,
        "quantity_mismatch_count": quantity_mismatch_count,
        "filled_qty_mismatch_count": filled_qty_mismatch_count,
    }


def build_relation_context(pb: Any, environment: str, symbols: list[str], normalized_order_records: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_symbols = []
    seen: set[str] = set()
    for value in symbols or []:
        symbol = to_text(value).upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            normalized_symbols.append(symbol)
    if not normalized_symbols:
        return {"activeGroupBySymbol": {}, "signalMap": {}}
    symbol_set = set(normalized_symbols)
    orders = list(normalized_order_records or [])
    if not orders:
        order_rows = pb.get_records("orders", filter=f'environment = "{environment}"', sort="-updated", per_page=OPEN_ORDER_FILTER_PER_PAGE, page=1) or []
        orders = [normalize_order_record(dict(row)) for row in order_rows if isinstance(row, dict)]
    filtered_orders = [order for order in orders if to_text(order.get("symbol")).upper() in symbol_set]
    groups_by_symbol: dict[str, dict[str, dict[str, Any]]] = {}
    signal_ids: set[str] = set()
    for normalized in filtered_orders:
        group_key = to_text(normalized.get("group_key"))
        symbol = to_text(normalized.get("symbol")).upper()
        if not symbol or not group_key:
            continue
        symbol_groups = groups_by_symbol.setdefault(symbol, {})
        group = symbol_groups.setdefault(
            group_key,
            {
                "trade_group_id": to_text(normalized.get("trade_group_id") or group_key),
                "entry_order_unique_id": to_text(normalized.get("entry_order_unique_id") or normalized.get("unique_id") or group_key),
                "signal_id": to_text(normalized.get("signal_id")),
                "latest_updated_ms": 0,
                "latest_updated": "",
                "latest_order_status": "",
                "best_status_weight": -1,
                "entry_filled_qty": 0.0,
                "exit_filled_qty": 0.0,
                "has_active_order": False,
                "has_open_exposure": False,
                "orders": [],
            },
        )
        group["orders"].append(normalized)
        if normalized.get("signal_id") and not group["signal_id"]:
            group["signal_id"] = normalized.get("signal_id")
        if to_int(normalized.get("updated_ms"), 0) >= group["latest_updated_ms"]:
            group["latest_updated_ms"] = to_int(normalized.get("updated_ms"), 0)
            group["latest_updated"] = to_text(normalized.get("updated"))
        if to_int(normalized.get("status_weight"), 0) >= group["best_status_weight"]:
            group["best_status_weight"] = to_int(normalized.get("status_weight"), 0)
            group["latest_order_status"] = to_text(normalized.get("status"))
        role = to_text(normalized.get("role"))
        if role == "entry":
            group["entry_filled_qty"] += abs(to_float(normalized.get("filled_qty")) or 0.0)
        if role in {"take_profit", "stop_loss"}:
            group["exit_filled_qty"] += abs(to_float(normalized.get("filled_qty")) or 0.0)
        if to_text(normalized.get("relation_status")).lower() in {"active", "planned"} or (to_int(normalized.get("status_weight"), 0) >= 70 and canonical_order_status(normalized.get("status")) != "FILLED"):
            group["has_active_order"] = True
    active_group_by_symbol: dict[str, dict[str, Any]] = {}
    signal_id_list: list[str] = []
    for symbol, groups in groups_by_symbol.items():
        group_list = []
        for group in groups.values():
            group["has_open_exposure"] = group["entry_filled_qty"] > group["exit_filled_qty"]
            signal_id = to_text(group.get("signal_id"))
            if signal_id and signal_id not in signal_ids:
                signal_ids.add(signal_id)
                signal_id_list.append(signal_id)
            group_list.append(group)
        group_list.sort(
            key=lambda item: (
                -((1000 if item.get("has_open_exposure") else 0) + (100 if item.get("has_active_order") else 0) + to_int(item.get("best_status_weight"), 0)),
                -to_int(item.get("latest_updated_ms"), 0),
            )
        )
        if group_list:
            active_group_by_symbol[symbol] = group_list[0]
    signal_map: dict[str, dict[str, Any]] = {}
    if signal_id_list:
        clauses = [f'signal_id = "{signal_id}"' for signal_id in signal_id_list]
        signal_rows = pb.get_records(
            "ibkr_signals",
            filter=f'environment = "{environment}" && ({" || ".join(clauses)})',
            sort="-updated",
            per_page=400,
            page=1,
        ) or []
        for row in signal_rows:
            if not isinstance(row, dict):
                continue
            normalized_signal = normalize_signal_record(dict(row))
            signal_id = to_text(normalized_signal.get("signal_id"))
            if not signal_id:
                continue
            existing = signal_map.get(signal_id)
            if existing is None or to_int(normalized_signal.get("status_weight"), 0) >= to_int(existing.get("status_weight"), 0) or to_int(normalized_signal.get("updated_ms"), 0) >= to_int(existing.get("updated_ms"), 0):
                signal_map[signal_id] = normalized_signal
    return {"activeGroupBySymbol": active_group_by_symbol, "signalMap": signal_map}


def enrich_account_snapshot(pb: Any, payload: dict[str, Any], environment: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    positions = list(payload.get("positions") or [])
    broker_orders = list(payload.get("orders") or [])
    live_open_orders = list(payload.get("live_open_orders") or []) or [item for item in broker_orders if not _is_closed_order_status((item or {}).get("status_key") or (item or {}).get("status"))]
    managed_context = build_managed_order_context(pb, environment, live_open_orders)
    symbols = [to_text((item or {}).get("symbol")).upper() for item in positions]
    symbols.extend(to_text((group or {}).get("symbol")).upper() for group in managed_context.get("active_groups", []))
    symbols.extend(to_text((group or {}).get("symbol")).upper() for group in managed_context.get("live_order_groups", []))
    symbols = [symbol for symbol in symbols if symbol]
    relation_context = build_relation_context(pb, environment, symbols, managed_context.get("order_records", []))
    system_managed_count = 0
    external_count = 0
    flat_count = 0
    next_positions = []
    for position in positions:
        item = ensure_object(position)
        symbol = to_text(item.get("symbol")).upper()
        quantity = to_float(item.get("quantity")) or 0.0
        active_group = ensure_object(relation_context["activeGroupBySymbol"].get(symbol))
        related_signal = ensure_object(relation_context["signalMap"].get(to_text(active_group.get("signal_id")))) if active_group.get("signal_id") else {}
        if quantity == 0:
            flat_count += 1
            relation = {
                "status": "flat_legacy",
                "reason": "gateway_flat_position_record",
                "signal_id": to_text(active_group.get("signal_id")),
                "signal_status": to_text(related_signal.get("status")),
                "trade_group_id": to_text(active_group.get("trade_group_id")),
                "entry_order_unique_id": to_text(active_group.get("entry_order_unique_id")),
                "last_order_status": to_text(active_group.get("latest_order_status")),
                "order_updated": to_text(active_group.get("latest_updated")),
            }
        elif active_group and active_group.get("has_open_exposure"):
            system_managed_count += 1
            relation = {
                "status": "system_managed",
                "reason": "matched_open_trade_group",
                "signal_id": to_text(active_group.get("signal_id")),
                "signal_status": to_text(related_signal.get("status")),
                "signal_note": to_text(related_signal.get("note")),
                "trade_group_id": to_text(active_group.get("trade_group_id")),
                "entry_order_unique_id": to_text(active_group.get("entry_order_unique_id")),
                "last_order_status": to_text(active_group.get("latest_order_status")),
                "order_updated": to_text(active_group.get("latest_updated")),
                "order_count": len(active_group.get("orders") or []),
            }
        else:
            external_count += 1
            relation = {
                "status": "external_position",
                "reason": "no_system_order_link",
                "signal_id": "",
                "signal_status": "",
                "trade_group_id": "",
                "entry_order_unique_id": "",
                "last_order_status": "",
                "order_updated": "",
            }
        next_positions.append({**item, "relation": relation})
    payload["positions"] = next_positions
    coverage = ensure_object(payload.get("live_order_coverage"))
    recovered_open_orders = to_int(ensure_object(payload.get("counts")).get("recovered_open_orders"), 0) or len([item for item in managed_context.get("live_orders", []) if to_text(item.get("recovery_source")) == "status_recovered"])
    payload["live_open_orders"] = managed_context.get("live_orders", [])
    payload["live_order_groups"] = managed_context.get("live_order_groups", [])
    payload["matched_order_groups"] = managed_context.get("matched_order_groups", [])
    payload["broker_only_order_groups"] = managed_context.get("broker_only_order_groups", [])
    payload["managed_order_groups"] = managed_context.get("active_groups", [])
    payload["pb_only_order_groups"] = managed_context.get("pb_only_active_groups", [])
    counts = ensure_object(payload.get("counts"))
    payload["counts"] = {
        **counts,
        "open_orders": len(payload.get("live_open_orders") or []),
        "cancelable_orders": to_int(managed_context.get("cancelable_order_count"), 0),
        "editable_orders": to_int(managed_context.get("editable_order_count"), 0),
        "outside_rth_orders": to_int(managed_context.get("outside_rth_order_count"), 0),
        "recovered_open_orders": recovered_open_orders,
        "broker_matched_orders": to_int(managed_context.get("matched_live_order_count"), 0),
        "broker_only_open_orders": to_int(managed_context.get("broker_only_live_order_count"), 0),
        "system_managed_positions": system_managed_count,
        "external_positions": external_count,
        "flat_positions": flat_count,
        "pb_active_order_groups": len(managed_context.get("active_groups", [])),
        "pb_active_orders": to_int(managed_context.get("active_order_count"), 0),
        "pb_only_active_order_groups": len(managed_context.get("pb_only_active_groups", [])),
        "pb_shadow_groups": len(managed_context.get("pb_only_active_groups", [])),
        "missing_client_order_id_orders": to_int(managed_context.get("missing_client_order_id_count"), 0),
        "status_mismatch_orders": to_int(managed_context.get("status_mismatch_count"), 0),
        "quantity_mismatch_orders": to_int(managed_context.get("quantity_mismatch_count"), 0),
        "filled_qty_mismatch_orders": to_int(managed_context.get("filled_qty_mismatch_count"), 0),
    }
    payload["order_reconciliation"] = {
        "broker_total_orders": len(broker_orders),
        "broker_open_orders": len(payload.get("live_open_orders") or []),
        "broker_matched_orders": to_int(managed_context.get("matched_live_order_count"), 0),
        "broker_only_open_orders": to_int(managed_context.get("broker_only_live_order_count"), 0),
        "broker_matched_groups": len(managed_context.get("matched_order_groups", [])),
        "broker_only_groups": len(managed_context.get("broker_only_order_groups", [])),
        "pb_active_order_groups": len(managed_context.get("active_groups", [])),
        "pb_active_orders": to_int(managed_context.get("active_order_count"), 0),
        "pb_only_active_order_groups": len(managed_context.get("pb_only_active_groups", [])),
        "pb_shadow_groups": len(managed_context.get("pb_only_active_groups", [])),
        "broker_only_orders": managed_context.get("broker_only_orders", []),
        "coverage_state": to_text(coverage.get("coverage_state")) or "complete",
        "bulk_open_count": to_int(coverage.get("bulk_open_count"), 0),
        "recovered_open_orders": recovered_open_orders,
        "unresolved_seed_count": to_int(coverage.get("unresolved_seed_count"), 0),
        "unresolved_order_ids": _clone_string_list(coverage.get("unresolved_order_ids")),
        "cancelable_orders": to_int(managed_context.get("cancelable_order_count"), 0),
        "editable_orders": to_int(managed_context.get("editable_order_count"), 0),
        "outside_rth_orders": to_int(managed_context.get("outside_rth_order_count"), 0),
        "missing_client_order_id_orders": to_int(managed_context.get("missing_client_order_id_count"), 0),
        "status_mismatch_orders": to_int(managed_context.get("status_mismatch_count"), 0),
        "quantity_mismatch_orders": to_int(managed_context.get("quantity_mismatch_count"), 0),
        "filled_qty_mismatch_orders": to_int(managed_context.get("filled_qty_mismatch_count"), 0),
    }
    return payload


def build_account_snapshot_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    result = request_json_request(
        "GET",
        runtime_base_url,
        "/ibkr/account",
        params=[("environment", environment)],
        timeout=20.0,
    )
    status_code = int(result.get("status_code") or 200)
    upstream_payload = ensure_object(result.get("payload"))
    selected_upstream = to_text(result.get("target_url")) or f"{runtime_base_url.rstrip('/')}/ibkr/account"
    if not upstream_payload or (status_code >= 400 and not upstream_payload.get("ok")):
        return {
            "ok": False,
            "status": "offline",
            "environment": environment,
            "error": result.get("error") or upstream_payload.get("error") or "account_snapshot_upstream_unavailable",
            "proxy_source": "ibkr-api",
            "proxy_route": "/api/custom/ibkr/account_snapshot",
            "proxy_upstream": selected_upstream,
            "source": "ibkr-api",
        }, 502 if status_code < 400 else status_code
    enriched = enrich_account_snapshot(pb, dict(upstream_payload), environment)
    enriched["proxy_source"] = "ibkr-api"
    enriched["proxy_route"] = "/api/custom/ibkr/account_snapshot"
    enriched["proxy_upstream"] = selected_upstream
    enriched["source"] = "ibkr-api"
    return enriched, status_code if status_code >= 400 else 200


__all__ = [
    "build_account_snapshot_response",
    "build_managed_order_context",
    "build_relation_context",
    "canonical_order_status",
    "enrich_account_snapshot",
    "normalize_live_order",
    "normalize_order_record",
]
