from __future__ import annotations

from typing import Any

from ibkr_api.orders.values import ensure_object, escape_filter_string, to_float, to_int, to_text
from ibkr_api.account.snapshot_shared import (
    OPEN_ORDER_FILTER_PER_PAGE,
    _clone_string_list,
    _is_closed_order_status,
    _is_exit_exposure_role,
    _is_open_like_order,
    _parse_time_ms,
    _pick_first_non_empty,
    _serialize_managed_order,
    canonical_order_status,
    normalize_order_record,
)


ORDER_LEG_SORT_WEIGHT = {
    "entry": 0,
    "take_profit": 1,
    "stop_loss": 2,
}


def _normalize_leg_role(order: dict[str, Any] | None) -> str:
    data = order or {}
    role_text = to_text(data.get("role") or data.get("leg_role") or data.get("order_type") or data.get("orderType")).lower()
    compact = role_text.replace("-", "_").replace(" ", "_")
    if compact in {"entry", "parent"} or "entry" in compact:
        return "entry"
    if compact in {"take_profit", "takeprofit", "tp", "profit_taker"} or "profit" in compact:
        return "take_profit"
    if compact in {"stop_loss", "stoploss", "sl", "stop"} or "stop" in compact:
        return "stop_loss"
    if not to_text(data.get("parent_id") or data.get("parent_order_unique_id")):
        return "entry"
    return compact or "child"


def _order_leg_sort_key(order: dict[str, Any]) -> tuple[int, int, str]:
    role = _normalize_leg_role(order)
    return (
        ORDER_LEG_SORT_WEIGHT.get(role, 9),
        -to_int(order.get("updated_ms"), 0),
        to_text(order.get("order_id") or order.get("broker_order_id") or order.get("unique_id")),
    )


def _sorted_order_legs(orders: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    result = [dict(order or {}) for order in (orders or [])]
    for order in result:
        order["leg_role"] = _normalize_leg_role(order)
    return sorted(result, key=_order_leg_sort_key)


def _fetch_execution_commissions(pb: Any, environment: str) -> dict[str, dict[str, Any]]:
    try:
        rows = pb.get_records(
            "ibkr_execution_fills",
            filter=f'environment = "{escape_filter_string(environment)}"',
            sort="-trade_time_ms,-updated",
            per_page=500,
            page=1,
        ) or []
    except Exception:
        return {}

    by_order_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = ensure_object(row)
        order_id = to_text(item.get("order_id"))
        if not order_id:
            continue
        bucket = by_order_id.setdefault(
            order_id,
            {
                "commission": 0.0,
                "commission_currency": to_text(item.get("commission_currency") or item.get("currency") or "USD").upper(),
                "commission_known": False,
                "commission_source": "ibkr_execution_fills",
                "fill_count": 0,
            },
        )
        bucket["commission"] += abs(to_float(item.get("commission")) or 0.0)
        if item.get("commission") not in (None, ""):
            bucket["commission_known"] = True
        currency = to_text(item.get("commission_currency") or item.get("currency")).upper()
        if currency:
            bucket["commission_currency"] = currency
        source = to_text(item.get("source"))
        if source:
            bucket["commission_source"] = source
        bucket["fill_count"] += 1
    return by_order_id


def _enrich_orders_with_execution_commissions(orders: list[dict[str, Any]], commission_by_order_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not commission_by_order_id:
        return orders
    enriched: list[dict[str, Any]] = []
    for order in orders:
        item = dict(order or {})
        order_id = to_text(item.get("order_id") or item.get("broker_order_id"))
        commission = commission_by_order_id.get(order_id) if order_id else None
        if commission and commission.get("commission_known"):
            item["commission"] = round(abs(to_float(commission.get("commission")) or 0.0), 6)
            item["commission_currency"] = to_text(commission.get("commission_currency") or item.get("commission_currency") or "USD").upper()
            item["commission_known"] = True
            item["commission_source"] = to_text(commission.get("commission_source") or "ibkr_execution_fills")
            item["commission_fill_count"] = to_int(commission.get("fill_count"), 0)
        elif abs(to_float(item.get("commission")) or 0.0) > 0:
            item["commission_known"] = True
            item["commission_source"] = to_text(item.get("commission_source") or "orders")
        enriched.append(item)
    return enriched


def _normalize_trade_direction(value: Any) -> str:
    text = to_text(value).lower()
    return text if text in {"long", "short"} else ""


def _direction_from_identifier(*values: Any, allow_signal_suffix: bool = False) -> str:
    for value in values:
        text = to_text(value).lower()
        if not text:
            continue
        normalized = text
        for separator in ("-", ":", "/", ".", " "):
            normalized = normalized.replace(separator, "_")
        tokens = [token for token in normalized.split("_") if token]
        direction_tokens = [(index, token) for index, token in enumerate(tokens) if token in {"long", "short"}]
        if direction_tokens:
            return direction_tokens[-1][1]
        if allow_signal_suffix and tokens:
            if tokens[-1] == "l":
                return "long"
            if tokens[-1] == "s":
                return "short"
    return ""


def _group_identifier_direction(group: dict[str, Any] | None) -> str:
    group = group or {}
    return _direction_from_identifier(
        group.get("trade_group_id"),
        group.get("entry_order_unique_id"),
        group.get("group_key"),
    ) or _direction_from_identifier(group.get("signal_id"), allow_signal_suffix=True)


def _order_identifier_direction(order: dict[str, Any] | None) -> str:
    order = order or {}
    pb_context = ensure_object(order.get("pb_context"))
    return _direction_from_identifier(
        order.get("trade_group_id"),
        order.get("entry_order_unique_id"),
        order.get("client_order_id"),
        order.get("unique_id"),
        pb_context.get("trade_group_id"),
        pb_context.get("entry_order_unique_id"),
    ) or _direction_from_identifier(order.get("signal_id"), pb_context.get("signal_id"), allow_signal_suffix=True)


def _direction_from_order_side(order: dict[str, Any] | None) -> str:
    order = order or {}
    side = to_text(order.get("side")).upper()
    if side not in {"BUY", "SELL"}:
        return ""
    role = _normalize_leg_role(order)
    has_parent = bool(to_text(order.get("parent_id") or order.get("parent_order_unique_id")))
    is_exit_leg = role in {"take_profit", "stop_loss", "child"} or has_parent
    if is_exit_leg:
        return "long" if side == "SELL" else "short"
    return "long" if side == "BUY" else "short"


def _infer_trade_direction(group: dict[str, Any] | None, orders: list[dict[str, Any]] | None) -> str:
    group = group or {}
    direction = _group_identifier_direction(group)
    if direction:
        return direction
    for key in ("trade_direction", "direction", "position_side"):
        value = _normalize_trade_direction(group.get(key))
        if value:
            return value
    sorted_orders = _sorted_order_legs(orders)
    for order in sorted_orders:
        direction = _order_identifier_direction(order)
        if direction:
            return direction
    for order in sorted_orders:
        if _normalize_leg_role(order) != "entry":
            continue
        for key in ("direction", "position_side"):
            value = _normalize_trade_direction(order.get(key))
            if value:
                return value
        direction = _direction_from_order_side(order)
        if direction:
            return direction
    for order in sorted_orders:
        direction = _direction_from_order_side(order)
        if direction:
            return direction
    for order in sorted_orders:
        for key in ("position_side", "direction"):
            value = _normalize_trade_direction(order.get(key))
            if value:
                return value
    return ""


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
    status_key = canonical_order_status(normalized.get("status_key") or status)
    total_quantity = to_float(normalized.get("total_quantity") if normalized.get("total_quantity") not in (None, "") else normalized.get("totalSize"))
    if total_quantity is None:
        total_quantity = to_float(normalized.get("quantity")) or 0.0
    filled_quantity = to_float(normalized.get("filled_quantity") if normalized.get("filled_quantity") not in (None, "") else normalized.get("filledQuantity"))
    if filled_quantity is None:
        filled_quantity = to_float(normalized.get("cum_fill")) or 0.0
    commission = abs(to_float(
        normalized.get("commission")
        if normalized.get("commission") not in (None, "")
        else normalized.get("ibkr_commission")
        if normalized.get("ibkr_commission") not in (None, "")
        else normalized.get("commissionAmount")
        if normalized.get("commissionAmount") not in (None, "")
        else normalized.get("commission_amount")
    ) or 0.0)
    commission_currency = _pick_first_non_empty(
        normalized.get("commission_currency"),
        normalized.get("commissionCurrency"),
        normalized.get("ibCommissionCurrency"),
        normalized.get("ibkr_commission_currency"),
        normalized.get("currency"),
        "USD",
    ).upper()
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
        "commission": commission,
        "commission_currency": commission_currency,
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
            "pb_commission": 0.0,
            "pb_commission_currency": "USD",
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
        "pb_commission": abs(to_float(pb_match.get("commission")) or 0.0),
        "pb_commission_currency": to_text(pb_match.get("commission_currency") or "USD").upper(),
        "diagnostic_tags": tags,
        "diagnostic_note": _build_diagnostic_note(live_order, pb_match, tags, ambiguous_match),
    }


def _finalize_group(group: dict[str, Any]) -> dict[str, Any]:
    orders = _sorted_order_legs(group["orders"])
    trade_direction = _infer_trade_direction(group, orders)
    return {
        "group_key": group["group_key"],
        "symbol": group["symbol"],
        "signal_id": group["signal_id"],
        "trade_group_id": group["trade_group_id"],
        "entry_order_unique_id": group["entry_order_unique_id"],
        "trade_direction": trade_direction,
        "direction": trade_direction,
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
        "commission": abs(to_float(group.get("commission")) or 0.0),
        "recovery_sources": [f"{key}:{group['recovery_sources'][key]}" for key in sorted(group["recovery_sources"])],
        "orders": orders,
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
    execution_commissions = _fetch_execution_commissions(pb, environment)
    normalized_order_records = _enrich_orders_with_execution_commissions(normalized_order_records, execution_commissions)
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
                "commission": 0.0,
                "has_active_order": False,
                "orders": [],
            },
        )
        group["orders"].append(normalized)
        group["commission"] += abs(to_float(normalized.get("commission")) or 0.0)
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
        if _is_exit_exposure_role(role):
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
        if (to_float(live_order.get("commission")) or 0.0) <= 0 and (to_float(pb_context.get("pb_commission")) or 0.0) > 0:
            live_order["commission"] = abs(to_float(pb_context.get("pb_commission")) or 0.0)
            live_order["commission_currency"] = to_text(pb_context.get("pb_commission_currency") or live_order.get("commission_currency") or "USD").upper()
        live_direction = _infer_trade_direction(
            {
                "signal_id": pb_context.get("signal_id"),
                "trade_group_id": pb_context.get("trade_group_id"),
                "entry_order_unique_id": pb_context.get("entry_order_unique_id"),
                "direction": pb_context.get("direction"),
                "position_side": pb_context.get("position_side"),
            },
            [live_order],
        )
        live_order["direction"] = live_direction
        live_order["position_side"] = live_direction
        live_order["leg_role"] = _normalize_leg_role(live_order)
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
                "trade_direction": "",
                "direction": "",
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
                "commission": 0.0,
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
        live_group["commission"] += abs(to_float(live_order.get("commission")) or 0.0)
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
        sorted_orders = _sorted_order_legs([_serialize_managed_order(order) for order in group["orders"]])
        trade_direction = _infer_trade_direction(group, sorted_orders)
        active_groups.append(
            {
                "symbol": group["symbol"],
                "signal_id": group["signal_id"],
                "trade_group_id": group["trade_group_id"],
                "entry_order_unique_id": group["entry_order_unique_id"],
                "trade_direction": trade_direction,
                "direction": trade_direction,
                "latest_updated": group["latest_updated"],
                "latest_updated_ms": group["latest_updated_ms"],
                "latest_order_status": group["latest_order_status"],
                "has_active_order": group["has_active_order"],
                "has_open_exposure": has_open_exposure,
                "broker_matched": matched_live_orders > 0,
                "matched_broker_orders": matched_live_orders,
                "commission": abs(to_float(group.get("commission")) or 0.0),
                "order_count": len(group["orders"]),
                "orders": sorted_orders,
            }
        )
    active_groups = [group for group in active_groups if group.get("has_active_order") or group.get("has_open_exposure")]
    active_groups.sort(
        key=lambda item: (
            -((1000 if item.get("has_open_exposure") else 0) + (100 if item.get("has_active_order") else 0) + (10 if item.get("broker_matched") else 0)),
            -to_int(item.get("latest_updated_ms"), 0),
        )
    )
    stale_pb_order_groups = [
        {
            **group,
            "authority": "pb_stale",
            "match_state": "pb_stale",
            "broker_matched": False,
            "matched_broker_orders": 0,
            "orders": [{**order, "authority": "pb_stale"} for order in group.get("orders", [])],
        }
        for group in active_groups
        if group.get("has_active_order") and not group.get("broker_matched")
    ]
    pb_only_active_groups = stale_pb_order_groups
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
        "stale_pb_order_groups": stale_pb_order_groups,
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


__all__ = ["build_managed_order_context", "normalize_live_order"]
