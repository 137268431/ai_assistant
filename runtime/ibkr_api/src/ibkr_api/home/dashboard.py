from __future__ import annotations

import time
from typing import Any, Callable

from ibkr_api.home.common import count_records, escape_filter, load_records, to_float, to_int, to_text
from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.orders.realized_pnl_stats import build_realized_pnl_stats
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms
from ibkr_compute.core.broker_mode import normalize_broker_mode


TimeStrings = Callable[[], dict[str, str]]
RequestJsonRequest = Callable[..., dict[str, Any]]


def _market_date(payload: dict[str, Any], time_strings: TimeStrings) -> tuple[str, int, int]:
    fallback = to_text((time_strings() or {}).get("date")) or time.strftime("%Y-%m-%d")
    requested = to_text(payload.get("market_date") or payload.get("marketDate") or payload.get("date")) or fallback
    try:
        start_ms, end_ms = parse_market_date_bounds_ms(requested)
        return requested, start_ms, end_ms
    except Exception:
        start_ms, end_ms = parse_market_date_bounds_ms(fallback)
        return fallback, start_ms, end_ms


def _order_field(order: dict[str, Any] | None, field_name: str) -> Any:
    if not isinstance(order, dict):
        return ""
    direct = order.get(field_name)
    if direct not in (None, ""):
        return direct
    extra = order.get("extra") if isinstance(order.get("extra"), dict) else {}
    return extra.get(field_name, "")


def _first_number(order: dict[str, Any] | None, *field_names: str) -> float | None:
    for field_name in field_names:
        number = to_float(_order_field(order, field_name))
        if number is not None:
            return number
    return None


def _first_positive_number(order: dict[str, Any] | None, *field_names: str) -> float | None:
    for field_name in field_names:
        number = to_float(_order_field(order, field_name))
        if number is not None and number > 0:
            return number
    return None


def _normalize_direction(value: Any) -> str:
    text = to_text(value).lower()
    if text in {"long", "buy"}:
        return "long"
    if text in {"short", "sell"}:
        return "short"
    return ""


def _execution_action_bucket(action_type: Any) -> str:
    text = to_text(action_type).lower()
    if text == "close":
        return "close"
    if text == "cancel":
        return "cancel"
    if text == "adjust" or text.startswith("adjust_"):
        return "adjust"
    return "other"


def _execution_action_type(row: dict[str, Any]) -> str:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    return to_text(row.get("action_type") or extra.get("action_type"))


def _summarize_pending_execution_actions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pending_by_action = {"close": 0, "cancel": 0, "adjust": 0, "other": 0}
    pending_count = 0
    for row in rows or []:
        if to_text(row.get("status")).lower() != "pending":
            continue
        pending_count += 1
        pending_by_action[_execution_action_bucket(_execution_action_type(row))] += 1
    return {
        "pending": pending_count,
        "pending_by_action": pending_by_action,
        "total": len(rows or []),
    }


def _order_role(order: dict[str, Any]) -> str:
    return to_text(_order_field(order, "role") or _order_field(order, "order_type")).lower()


def _order_type(order: dict[str, Any]) -> str:
    return to_text(_order_field(order, "order_type")).lower()


def _order_lifecycle_role(order: dict[str, Any]) -> str:
    role = _order_role(order)
    order_type = _order_type(order)
    unique_id = to_text(_order_field(order, "unique_id") or _order_field(order, "coid")).lower()
    if role in {"entry", "take_profit", "stop_loss", "repair_tp", "repair_sl", "close", "manual_close", "market_close"}:
        return role
    if order_type in {"entry", "entryorder"}:
        return "entry"
    if order_type in {"takeprofit", "take_profit", "tp"}:
        return "take_profit"
    if order_type in {"stoploss", "stop_loss", "sl"}:
        return "stop_loss"
    if unique_id.startswith("entry_") or unique_id.endswith("_entry"):
        return "entry"
    if unique_id.startswith("tp_") or unique_id.endswith("_tp") or unique_id.endswith("_takeprofit"):
        return "take_profit"
    if unique_id.startswith("sl_") or unique_id.endswith("_sl") or unique_id.endswith("_stoploss"):
        return "stop_loss"
    if unique_id.startswith("close_") or unique_id.startswith("manual_close_") or unique_id.startswith("market_close_"):
        return "close"
    return role or order_type


def _order_filled_quantity(order: dict[str, Any]) -> float:
    return _first_positive_number(order, "filled_qty", "filledQuantity", "actual_filled_qty") or 0.0


def _is_order_filled(order: dict[str, Any]) -> bool:
    status = to_text(_order_field(order, "status")).lower()
    return status in {"filled", "executed", "closed"} or _order_filled_quantity(order) > 0


def _is_entry_order(order: dict[str, Any]) -> bool:
    return _order_lifecycle_role(order) == "entry"


def _is_filled_exit_order(order: dict[str, Any]) -> bool:
    if not _is_order_filled(order):
        return False
    return _order_lifecycle_role(order) in {"take_profit", "stop_loss", "repair_tp", "repair_sl", "close", "manual_close", "market_close"}


def _unique_order_base(order: dict[str, Any]) -> str:
    unique_id = to_text(_order_field(order, "unique_id") or _order_field(order, "coid"))
    if not unique_id:
        return ""
    parts = unique_id.split("_")
    if len(parts) >= 2 and parts[0].lower() in {"entry", "tp", "sl", "close", "manual_close", "market_close"}:
        return "_".join(parts[1:])
    for index, part in enumerate(parts):
        token = to_text(part).lower()
        if token in {"entry", "tp", "sl", "takeprofit", "stoploss"} and index > 0:
            return "_".join(parts[:index])
    return unique_id


def _order_group_key(order: dict[str, Any], index: int) -> str:
    for field_name in ("trade_group_id", "linked_trade_group_id", "entry_order_unique_id", "linked_entry_order_unique_id", "parent_order_unique_id", "signal_id"):
        value = to_text(_order_field(order, field_name))
        if value:
            return value
    return _unique_order_base(order) or to_text(_order_field(order, "id")) or f"row-{index}"


def _order_sort_key(order: dict[str, Any]) -> tuple[int, str]:
    return (to_int(_order_field(order, "bar_time_ms"), 0), to_text(order.get("created") or order.get("updated")))


def _order_signal_group_key(order: dict[str, Any], index: int) -> str:
    for field_name in ("signal_id", "trade_group_id", "linked_trade_group_id", "entry_order_unique_id", "linked_entry_order_unique_id"):
        value = to_text(_order_field(order, field_name))
        if value:
            return value
    return _order_group_key(order, index)


def _summarize_order_groups(orders: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"long": 0, "short": 0, "total": 0, "entry_order_count": 0}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, order in enumerate(orders or []):
        grouped.setdefault(_order_signal_group_key(order, index), []).append(order)

    for group_orders in grouped.values():
        ordered = sorted(group_orders, key=_order_sort_key, reverse=True)
        entry_orders = [order for order in ordered if _is_entry_order(order)]
        primary = entry_orders[0] if entry_orders else None
        if not primary:
            continue
        summary["total"] += 1
        summary["entry_order_count"] += len(entry_orders)
        direction = _normalize_direction(_order_field(primary, "position_side") or _order_field(primary, "direction"))
        if direction in summary:
            summary[direction] += 1
    return summary


def _build_market_time_filter(environment_filter: str, market_date: str, start_ms: int, end_ms: int) -> str:
    start_time = f"{market_date} 00:00:00"
    end_time = f"{market_date} 23:59:59"
    return (
        f'environment = "{environment_filter}" && '
        f'((us_time >= "{escape_filter(start_time)}" && us_time <= "{escape_filter(end_time)}") || '
        f"(bar_time_ms >= {start_ms} && bar_time_ms < {end_ms}))"
    )


def _summarize_gateway_positions(
    *,
    broker_mode: str,
    request_json_request: RequestJsonRequest | None,
    runtime_base_url: str,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "long": 0,
        "short": 0,
        "total": 0,
        "available": False,
        "source": "runtime_account",
    }
    if not callable(request_json_request) or not to_text(runtime_base_url):
        summary["error"] = "runtime_account_request_unavailable"
        return summary

    try:
        result = request_json_request(
            "GET",
            runtime_base_url,
            "/ibkr/account",
            params=[("broker_mode", broker_mode), ("environment", broker_mode), ("include_pnl", "0")],
            timeout=8.0,
        )
    except Exception as exc:
        summary["error"] = str(exc) or "runtime_account_request_failed"
        return summary

    payload = result.get("payload") if isinstance(result, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    status_code = to_int(result.get("status_code") if isinstance(result, dict) else 0, 200)
    if status_code >= 400 or payload.get("ok") is False:
        summary["error"] = to_text(payload.get("error") or (result.get("error") if isinstance(result, dict) else "")) or f"runtime_account_http_{status_code}"
        return summary

    positions = payload.get("positions")
    if not isinstance(positions, list):
        summary["error"] = "runtime_account_positions_unavailable"
        return summary

    for position in positions:
        if not isinstance(position, dict):
            continue
        quantity = to_float(position.get("quantity") if position.get("quantity") not in (None, "") else position.get("position")) or 0.0
        if quantity > 0:
            summary["long"] += 1
        elif quantity < 0:
            summary["short"] += 1

    summary["total"] = summary["long"] + summary["short"]
    summary["available"] = True
    summary["account_id"] = to_text(payload.get("account_id") or payload.get("account"))
    return summary


def _calculate_pnl(orders: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, order in enumerate(orders or []):
        key = _order_group_key(order, index)
        grouped.setdefault(key, []).append(order)

    total = 0.0
    exit_count = 0
    win_count = 0
    loss_count = 0
    for group_orders in grouped.values():
        entry = next((order for order in group_orders if _is_entry_order(order)), None)
        entry_price = _first_positive_number(entry, "fill_price", "limit_price", "entry_price")
        entry_direction = _normalize_direction(_order_field(entry, "position_side") or _order_field(entry, "direction"))
        entry_quantity = _first_positive_number(entry, "filled_qty", "quantity")
        entry_commission = abs(_first_number(entry, "commission", "ibkr_commission") or 0.0)

        for exit_order in [order for order in group_orders if _is_filled_exit_order(order)]:
            stored_pnl = _first_number(exit_order, "realized_net_pnl", "realized_pnl", "realized_gross_pnl", "pnl")
            pnl = stored_pnl if stored_pnl is not None and abs(stored_pnl) > 0 else None
            can_estimate = entry is not None and entry_direction and entry_price is not None
            if pnl is None and can_estimate:
                exit_price = _first_positive_number(exit_order, "fill_price", "limit_price")
                quantity = _first_positive_number(exit_order, "filled_qty", "quantity") or entry_quantity
                if exit_price is not None and quantity is not None and quantity > 0:
                    gross = (entry_price - exit_price) * quantity if entry_direction == "short" else (exit_price - entry_price) * quantity
                    exit_commission = abs(_first_number(exit_order, "commission", "ibkr_commission") or 0.0)
                    pnl = gross - entry_commission - exit_commission
            if pnl is None:
                continue
            total += pnl
            exit_count += 1
            if pnl > 0:
                win_count += 1
            if pnl < 0:
                loss_count += 1
    return {
        "total": round(total, 4),
        "exit_count": exit_count,
        "win_count": win_count,
        "loss_count": loss_count,
    }


def _collect_order_match_ids(order: dict[str, Any]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for field_name in ("order_id", "broker_order_id", "ib_order_id", "orderId"):
        value = to_text(_order_field(order, field_name))
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def _or_equals_filter(field_name: str, values: list[str], *, limit: int = 24) -> str:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = to_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
        if len(normalized) >= limit:
            break
    if not normalized:
        return ""
    return "(" + " || ".join(f'{field_name} = "{escape_filter(value)}"' for value in normalized) + ")"


def _load_execution_fills_for_orders(
    pb: Any,
    *,
    broker_filter: str,
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    order_ids: list[str] = []
    seen: set[str] = set()
    for order in orders or []:
        for order_id in _collect_order_match_ids(order):
            if order_id and order_id not in seen:
                seen.add(order_id)
                order_ids.append(order_id)
    if not order_ids:
        return []

    fills: list[dict[str, Any]] = []
    for offset in range(0, len(order_ids), 24):
        chunk = order_ids[offset:offset + 24]
        order_filter = _or_equals_filter("order_id", chunk, limit=24)
        if not order_filter:
            continue
        try:
            fills.extend(
                load_records(
                    pb,
                    "ibkr_execution_fills",
                    filter_expr=f'environment = "{broker_filter}" && {order_filter}',
                    sort="trade_time_ms,created",
                    per_page=200,
                    max_pages=4,
                )
            )
        except Exception:
            continue
    return fills


def _load_linked_entry_orders(pb: Any, *, broker_filter: str, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values_by_field: dict[str, list[str]] = {
        "unique_id": [],
        "trade_group_id": [],
        "signal_id": [],
    }
    existing: set[str] = {
        to_text(order.get("id") or _order_field(order, "unique_id"))
        for order in orders or []
        if isinstance(order, dict)
    }
    for order in orders or []:
        if _is_entry_order(order):
            continue
        for source_field, target_field in (
            ("entry_order_unique_id", "unique_id"),
            ("linked_entry_order_unique_id", "unique_id"),
            ("parent_order_unique_id", "unique_id"),
            ("trade_group_id", "trade_group_id"),
            ("linked_trade_group_id", "trade_group_id"),
            ("signal_id", "signal_id"),
        ):
            value = to_text(_order_field(order, source_field))
            if value:
                values_by_field[target_field].append(value)

    entries: list[dict[str, Any]] = []
    seen = set(existing)
    for field_name, values in values_by_field.items():
        for offset in range(0, len(values), 24):
            value_filter = _or_equals_filter(field_name, values[offset:offset + 24], limit=24)
            if not value_filter:
                continue
            rows = load_records(
                pb,
                "orders",
                filter_expr=f'environment = "{broker_filter}" && role = "entry" && {value_filter}',
                sort="-bar_time_ms,-created",
                per_page=100,
                max_pages=3,
            )
            for row in rows:
                key = to_text(row.get("id") or _order_field(row, "unique_id"))
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                entries.append(row)
    return entries


def _activity_from_signal(signal: dict[str, Any]) -> dict[str, Any]:
    direction = to_text(signal.get("direction") or "neutral").lower() or "neutral"
    symbol = to_text(signal.get("symbol")).upper()
    return {
        "type": "signal",
        "time": signal.get("created") or signal.get("updated") or "",
        "symbol": symbol,
        "direction": direction,
    }


def _activity_from_order(order: dict[str, Any]) -> dict[str, Any]:
    direction = _normalize_direction(_order_field(order, "position_side") or _order_field(order, "direction")) or "neutral"
    symbol = to_text(order.get("symbol")).upper()
    return {
        "type": "order",
        "time": order.get("created") or order.get("updated") or "",
        "symbol": symbol,
        "direction": direction,
    }


def build_home_dashboard_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    time_strings: TimeStrings,
    request_json_request: RequestJsonRequest | None = None,
    runtime_base_url: str = "",
) -> tuple[dict[str, Any], int]:
    request_payload = payload if isinstance(payload, dict) else {}
    broker_mode = request_broker_mode(request_payload)
    data_environment = request_market_data_mode(request_payload)
    market_date, start_ms, end_ms = _market_date(request_payload, time_strings)
    broker_filter = escape_filter(broker_mode)
    data_filter = escape_filter(data_environment)
    today_data_filter = _build_market_time_filter(data_filter, market_date, start_ms, end_ms)
    today_broker_filter = f'environment = "{broker_filter}" && bar_time_ms >= {start_ms} && bar_time_ms < {end_ms}'

    signal_long_count = count_records(pb, "ibkr_signals", f'{today_data_filter} && direction = "long"')
    signal_short_count = count_records(pb, "ibkr_signals", f'{today_data_filter} && direction = "short"')
    tv_action_filter = f'{today_broker_filter} && source = "tradingview"'
    execution_action_rows = load_records(pb, "ibkr_reverse_signals", filter_expr=tv_action_filter, sort="-created", per_page=500, max_pages=2)
    today_orders = load_records(pb, "orders", filter_expr=today_broker_filter, sort="-bar_time_ms,-created", per_page=200, max_pages=40)
    linked_entry_orders = _load_linked_entry_orders(pb, broker_filter=broker_filter, orders=today_orders)
    pnl_orders = [*today_orders, *linked_entry_orders]
    execution_fills = _load_execution_fills_for_orders(pb, broker_filter=broker_filter, orders=pnl_orders)
    recent_signals = load_records(pb, "ibkr_signals", filter_expr=today_data_filter, sort="-created", per_page=4, max_pages=1)

    execution_action_summary = _summarize_pending_execution_actions(execution_action_rows)
    order_summary = _summarize_order_groups(today_orders)
    position_summary = _summarize_gateway_positions(
        broker_mode=broker_mode,
        request_json_request=request_json_request,
        runtime_base_url=runtime_base_url,
    )
    pnl_summary = build_realized_pnl_stats(pnl_orders, execution_fills, start_ms=start_ms, end_ms=end_ms)
    recent_entry_orders = [row for row in today_orders if _is_entry_order(row)][:4]
    recent_activity = sorted(
        [_activity_from_signal(row) for row in recent_signals[:4]] + [_activity_from_order(row) for row in recent_entry_orders],
        key=lambda item: to_text(item.get("time")),
        reverse=True,
    )[:6]

    summary = {
        "signals": {
            "long": signal_long_count,
            "short": signal_short_count,
            "total": signal_long_count + signal_short_count,
        },
        "execution_actions": execution_action_summary,
        "reverse_signals": execution_action_summary,
        "orders": {
            "long": order_summary["long"],
            "short": order_summary["short"],
            "total": order_summary["total"],
            "entry_order_count": order_summary["entry_order_count"],
        },
        "positions": position_summary,
        "pnl": pnl_summary,
        "activity": {"count": len(recent_activity)},
    }

    return {
        "ok": True,
        "source": "ibkr-api",
        "broker_mode": normalize_broker_mode(broker_mode, "paper"),
        "data_environment": data_environment,
        "market_data_mode": data_environment,
        "market_date": market_date,
        "market_start_ms": start_ms,
        "market_end_ms": end_ms,
        "computed_at_ms": int(time.time() * 1000),
        "summary": summary,
        "recent_activity": recent_activity,
    }, 200


__all__ = ["build_home_dashboard_response"]
