from __future__ import annotations

import time
from typing import Any, Callable

from ibkr_api.home.common import count_records, escape_filter, load_records, to_float, to_int, to_text
from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms
from ibkr_compute.core.broker_mode import normalize_broker_mode


TimeStrings = Callable[[], dict[str, str]]


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


def _summarize_entry_orders(orders: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"long": 0, "short": 0}
    for order in orders:
        if not _is_entry_order(order):
            continue
        direction = _normalize_direction(_order_field(order, "position_side") or _order_field(order, "direction"))
        if direction in summary:
            summary[direction] += 1
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
) -> tuple[dict[str, Any], int]:
    request_payload = payload if isinstance(payload, dict) else {}
    broker_mode = request_broker_mode(request_payload)
    data_environment = request_market_data_mode(request_payload)
    market_date, start_ms, end_ms = _market_date(request_payload, time_strings)
    broker_filter = escape_filter(broker_mode)
    data_filter = escape_filter(data_environment)
    today_data_filter = f'environment = "{data_filter}" && bar_time_ms >= {start_ms} && bar_time_ms < {end_ms}'
    today_broker_filter = f'environment = "{broker_filter}" && bar_time_ms >= {start_ms} && bar_time_ms < {end_ms}'

    signal_long_count = count_records(pb, "ibkr_signals", f'{today_data_filter} && direction = "long"')
    signal_short_count = count_records(pb, "ibkr_signals", f'{today_data_filter} && direction = "short"')
    reverse_rows = load_records(pb, "ibkr_reverse_signals", filter_expr=today_broker_filter, sort="-created", per_page=500, max_pages=2)
    today_orders = load_records(pb, "orders", filter_expr=today_broker_filter, sort="-created", per_page=500, max_pages=2)
    recent_signals = load_records(pb, "ibkr_signals", filter_expr=today_data_filter, sort="-created", per_page=4, max_pages=1)

    position_base_filter = f'environment = "{broker_filter}" && status = "Filled" && order_type = "Entry"'
    positions_long = count_records(pb, "orders", f'{position_base_filter} && (position_side = "long" || direction = "long")')
    positions_short = count_records(pb, "orders", f'{position_base_filter} && (position_side = "short" || direction = "short")')

    pending_reverse = len([row for row in reverse_rows if not bool(row.get("processed"))])
    order_summary = _summarize_entry_orders(today_orders)
    pnl_summary = _calculate_pnl(today_orders)
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
        "reverse_signals": {
            "pending": pending_reverse,
            "total": len(reverse_rows),
        },
        "orders": {
            "long": order_summary["long"],
            "short": order_summary["short"],
            "total": order_summary["long"] + order_summary["short"],
        },
        "positions": {
            "long": positions_long,
            "short": positions_short,
            "total": positions_long + positions_short,
        },
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
