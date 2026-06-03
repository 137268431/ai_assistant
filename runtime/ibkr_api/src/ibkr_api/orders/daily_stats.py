from __future__ import annotations

import re
from typing import Any

from ibkr_api.orders.values import ensure_object, first_defined, to_float, to_text


TAKE_PROFIT_ROLES = {"take_profit", "repair_tp", "tp"}
STOP_LOSS_ROLES = {"stop_loss", "repair_sl", "sl"}
CLOSE_ROLES = {"close", "manual_close", "market_close", "close_order", "reverse_close"}
ENTRY_ROLES = {"entry"}
ENTRY_ORDER_TYPES = {"entry", "entryorder"}
TAKE_PROFIT_ORDER_TYPES = {"takeprofit", "takeprofitorder", "tp"}
STOP_LOSS_ORDER_TYPES = {"stoploss", "stoplossorder", "sl", "stop"}
CLOSE_ORDER_TYPES = {"mkt", "market", "marketclose"}
EPSILON = 0.0000001


def empty_daily_order_stats() -> dict[str, Any]:
    return {
        "take_profit_filled": 0,
        "stop_loss_filled": 0,
        "protective_take_profit_filled": 0,
        "protective_stop_loss_filled": 0,
        "close_take_profit_filled": 0,
        "close_stop_loss_filled": 0,
        "close_flat_filled": 0,
        "close_unclassified_filled": 0,
        "close_filled": 0,
        "manual_close_filled": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "flat_trades": 0,
        "pnl_missing_count": 0,
        "realized_gross_pnl": 0.0,
        "realized_net_pnl": 0.0,
        "profit_amount": 0.0,
        "loss_amount": 0.0,
        "commission": 0.0,
        "actual_exit_count": 0,
        "commission_missing_count": 0,
        "entry_missing_count": 0,
        "fill_missing_count": 0,
        "currency_mismatch_count": 0,
        "unsupported_asset_count": 0,
        "ibkr_realized_pnl_mismatch_count": 0,
    }


def _extra(row: dict[str, Any] | None) -> dict[str, Any]:
    return ensure_object((row or {}).get("extra"))


def _row_value(row: dict[str, Any] | None, field: str) -> Any:
    source = row or {}
    return first_defined(source.get(field), _extra(source).get(field))


def _extra_has_value(row: dict[str, Any] | None, field: str) -> bool:
    extra = _extra(row)
    return field in extra and extra.get(field) not in (None, "")


def _normalized_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", to_text(value).lower())


def _normalized_role(value: Any) -> str:
    return to_text(value).lower().replace("-", "_").replace(" ", "_")


def _exit_role(row: dict[str, Any]) -> str:
    role = _normalized_role(_row_value(row, "role"))
    if role in TAKE_PROFIT_ROLES:
        return "take_profit"
    if role in STOP_LOSS_ROLES:
        return "stop_loss"
    if role in CLOSE_ROLES:
        return "close"
    order_type = _normalized_token(_row_value(row, "order_type"))
    if order_type in TAKE_PROFIT_ORDER_TYPES:
        return "take_profit"
    if order_type in STOP_LOSS_ORDER_TYPES:
        return "stop_loss"
    if order_type in CLOSE_ORDER_TYPES and to_text(_row_value(row, "unique_id")).lower().startswith("close_"):
        return "close"
    return ""


def _is_entry_order(row: dict[str, Any]) -> bool:
    role = _normalized_role(_row_value(row, "role"))
    order_type = _normalized_token(_row_value(row, "order_type"))
    return role in ENTRY_ROLES or order_type in ENTRY_ORDER_TYPES


def _is_filled(row: dict[str, Any]) -> bool:
    status = to_text(_row_value(row, "status")).lower()
    filled_qty = _positive_number_from(row, "filled_qty", "actual_filled_qty")
    return status in {"filled", "executed", "closed"} or filled_qty is not None


def _nonempty_values(row: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for field in fields:
        value = to_text(_row_value(row, field))
        if value and value not in values:
            values.append(value)
    return values


def _build_entry_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not _is_entry_order(row):
            continue
        for key in _nonempty_values(
            row,
            (
                "unique_id",
                "entry_order_unique_id",
                "trade_group_id",
                "signal_id",
                "broker_order_id",
                "order_id",
            ),
        ):
            index.setdefault(key, row)
    return index


def _find_entry(exit_order: dict[str, Any], entry_index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    for key in _nonempty_values(
        exit_order,
        (
            "entry_order_unique_id",
            "parent_order_unique_id",
            "trade_group_id",
            "signal_id",
        ),
    ):
        entry = entry_index.get(key)
        if entry:
            return entry
    return {}


def _number_from(row: dict[str, Any] | None, *fields: str) -> float | None:
    for field in fields:
        value = to_float(_row_value(row, field))
        if value is not None:
            return value
    return None


def _positive_number_from(row: dict[str, Any] | None, *fields: str) -> float | None:
    for field in fields:
        value = _number_from(row, field)
        if value is not None and value > 0:
            return value
    return None


def _normalize_trade_side(value: Any, *, exit_order_direction: bool = False) -> str:
    text = to_text(value).lower()
    if text in {"long", "buy_to_open"}:
        return "long"
    if text in {"short", "sell_to_open"}:
        return "short"
    if text == "buy":
        return "short" if exit_order_direction else "long"
    if text == "sell":
        return "long" if exit_order_direction else "short"
    return ""


def _trade_side(entry_order: dict[str, Any], exit_order: dict[str, Any]) -> str:
    for value in (
        _row_value(entry_order, "position_side"),
        _row_value(entry_order, "direction"),
        _row_value(exit_order, "position_side"),
    ):
        side = _normalize_trade_side(value)
        if side:
            return side
    return _normalize_trade_side(_row_value(exit_order, "direction"), exit_order_direction=True)


def _computed_pnl(exit_order: dict[str, Any], entry_order: dict[str, Any]) -> float | None:
    entry_price = first_defined(
        _positive_number_from(entry_order, "fill_price", "limit_price", "entry_price"),
        _positive_number_from(exit_order, "entry_price"),
    )
    exit_price = _positive_number_from(exit_order, "fill_price", "limit_price", "exit_price", "tp_price", "sl_price")
    quantity = first_defined(
        _positive_number_from(exit_order, "filled_qty", "quantity"),
        _positive_number_from(entry_order, "filled_qty", "quantity"),
    )
    side = _trade_side(entry_order, exit_order)
    if entry_price is None or exit_price is None or quantity is None or not side:
        return None
    per_share = entry_price - exit_price if side == "short" else exit_price - entry_price
    return per_share * quantity


def _stored_or_computed_pnl(exit_order: dict[str, Any], entry_order: dict[str, Any]) -> tuple[float | None, bool]:
    raw = _row_value(exit_order, "pnl")
    parsed = to_float(raw)
    if parsed is not None and abs(parsed) > EPSILON:
        return parsed, False
    stored_gross = _number_from(exit_order, "realized_gross_pnl")
    if stored_gross is not None and (abs(stored_gross) > EPSILON or _extra_has_value(exit_order, "realized_gross_pnl")):
        return stored_gross, False
    computed = _computed_pnl(exit_order, entry_order)
    if computed is not None:
        return computed, False
    if parsed is not None and abs(parsed) <= EPSILON:
        return None, True
    if raw is not None and raw != "":
        return parsed if parsed is not None else None, parsed is None
    return None, True


def _trade_commission(exit_order: dict[str, Any], entry_order: dict[str, Any]) -> float:
    total = 0.0
    seen: set[str] = set()
    for row in (entry_order, exit_order):
        if not row:
            continue
        row_id = to_text(row.get("id") or row.get("unique_id") or id(row))
        if row_id in seen:
            continue
        seen.add(row_id)
        commission = _number_from(row, "commission")
        if commission is not None:
            total += commission
    return total


def build_daily_order_stats(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    stats = empty_daily_order_stats()
    order_rows = [row for row in rows or [] if isinstance(row, dict)]
    entry_index = _build_entry_index(order_rows)

    for row in order_rows:
        exit_role = _exit_role(row)
        if not exit_role or not _is_filled(row):
            continue
        if exit_role == "take_profit":
            stats["protective_take_profit_filled"] += 1
            stats["take_profit_filled"] += 1
        elif exit_role == "stop_loss":
            stats["protective_stop_loss_filled"] += 1
            stats["stop_loss_filled"] += 1
        elif exit_role == "close":
            stats["close_filled"] += 1
            stats["manual_close_filled"] += 1

        entry_order = _find_entry(row, entry_index)
        gross_pnl, missing = _stored_or_computed_pnl(row, entry_order)
        if missing or gross_pnl is None:
            if exit_role == "close":
                stats["close_unclassified_filled"] += 1
            stats["pnl_missing_count"] += 1
            continue

        commission = _trade_commission(row, entry_order)
        net_pnl = gross_pnl - commission
        if exit_role == "close":
            if net_pnl > 0:
                stats["close_take_profit_filled"] += 1
                stats["take_profit_filled"] += 1
            elif net_pnl < 0:
                stats["close_stop_loss_filled"] += 1
                stats["stop_loss_filled"] += 1
            else:
                stats["close_flat_filled"] += 1
        stats["realized_gross_pnl"] += gross_pnl
        stats["realized_net_pnl"] += net_pnl
        stats["commission"] += commission
        if net_pnl > 0:
            stats["winning_trades"] += 1
            stats["profit_amount"] += net_pnl
        elif net_pnl < 0:
            stats["losing_trades"] += 1
            stats["loss_amount"] += net_pnl
        else:
            stats["flat_trades"] += 1

    for key in ("realized_gross_pnl", "realized_net_pnl", "profit_amount", "loss_amount", "commission"):
        stats[key] = round(float(stats[key]), 2)
    return stats


__all__ = ["build_daily_order_stats", "empty_daily_order_stats"]
