from __future__ import annotations

from typing import Any

from ibkr_api.orders.values import to_float, to_int, to_text
from ibkr_api.account.snapshot_live_orders import (
    _collect_order_ids,
    _enrich_orders_with_execution_commissions,
    _fetch_execution_commissions,
)
from ibkr_api.account.snapshot_shared import (
    OPEN_ORDER_FILTER_PER_PAGE,
    canonical_order_status,
    _is_exit_exposure_role,
    normalize_order_record,
    normalize_signal_record,
)


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
        execution_commissions = _fetch_execution_commissions(pb, environment, _collect_order_ids(orders))
        orders = _enrich_orders_with_execution_commissions(orders, execution_commissions)
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
                "commission": 0.0,
                "commission_currency": "USD",
                "commission_known": False,
                "commission_source": "",
                "commission_fill_count": 0,
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
        if _is_exit_exposure_role(role):
            group["exit_filled_qty"] += abs(to_float(normalized.get("filled_qty")) or 0.0)
        commission = abs(to_float(normalized.get("commission")) or 0.0)
        group["commission"] += commission
        currency = to_text(normalized.get("commission_currency")).upper()
        if currency:
            group["commission_currency"] = currency
        if bool(normalized.get("commission_known")) or commission > 0:
            group["commission_known"] = True
        source = to_text(normalized.get("commission_source"))
        if source:
            group["commission_source"] = source
        group["commission_fill_count"] += to_int(normalized.get("commission_fill_count"), 0)
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


__all__ = ["build_relation_context"]
