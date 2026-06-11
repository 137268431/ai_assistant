from __future__ import annotations

import math
import re
from typing import Any

from ibkr_api.orders.values import ensure_object, first_defined, to_float, to_int, to_text


ENTRY_ROLES = {"entry"}
ENTRY_ORDER_TYPES = {"entry", "entryorder"}
TAKE_PROFIT_ROLES = {"take_profit", "repair_tp", "tp"}
STOP_LOSS_ROLES = {"stop_loss", "repair_sl", "sl"}
CLOSE_ROLES = {"close", "manual_close", "market_close", "close_order", "reverse_close"}
TAKE_PROFIT_ORDER_TYPES = {"takeprofit", "takeprofitorder", "take_profit", "tp"}
STOP_LOSS_ORDER_TYPES = {"stoploss", "stoplossorder", "stop_loss", "sl", "stop"}
CLOSE_ORDER_TYPES = {"mkt", "market", "marketclose", "close", "market_close"}
CLOSE_REFERENCE_PREFIXES = ("close_", "manual_close_", "market_close_")
EPSILON = 0.0000001


def empty_realized_pnl_stats() -> dict[str, Any]:
    return {
        "source": "gateway_execution_fills",
        "total": 0.0,
        "realized_net_pnl": 0.0,
        "realized_gross_pnl": 0.0,
        "commission": 0.0,
        "entry_commission": 0.0,
        "exit_commission": 0.0,
        "commission_fill_count": 0,
        "commission_per_exit": 0.0,
        "exit_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "flat_count": 0,
        "profit_amount": 0.0,
        "loss_amount": 0.0,
        "missing_count": 0,
        "entry_missing_count": 0,
        "fill_missing_count": 0,
        "commission_missing_count": 0,
        "currency_mismatch_count": 0,
        "unsupported_asset_count": 0,
        "ibkr_realized_pnl_mismatch_count": 0,
        "estimated_total": 0.0,
        "estimated_exit_count": 0,
        "estimated_missing_count": 0,
        "estimated_source": "orders_estimated_entry_exit_fields",
    }


def _extra(row: dict[str, Any] | None) -> dict[str, Any]:
    return ensure_object((row or {}).get("extra"))


def _row_value(row: dict[str, Any] | None, field: str) -> Any:
    source = row or {}
    return first_defined(source.get(field), _extra(source).get(field))


def _normalized_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", to_text(value).lower())


def _normalized_role(value: Any) -> str:
    return to_text(value).lower().replace("-", "_").replace(" ", "_")


def _lifecycle_role(row: dict[str, Any]) -> str:
    role = _normalized_role(_row_value(row, "role"))
    order_type = _normalized_token(_row_value(row, "order_type"))
    unique_id = to_text(_row_value(row, "unique_id") or _row_value(row, "coid")).lower()
    if role in ENTRY_ROLES:
        return "entry"
    if role in TAKE_PROFIT_ROLES:
        return "take_profit"
    if role in STOP_LOSS_ROLES:
        return "stop_loss"
    if role in CLOSE_ROLES:
        return "close"
    if order_type in ENTRY_ORDER_TYPES:
        return "entry"
    if order_type in TAKE_PROFIT_ORDER_TYPES:
        return "take_profit"
    if order_type in STOP_LOSS_ORDER_TYPES:
        return "stop_loss"
    if order_type in CLOSE_ORDER_TYPES and (unique_id.startswith("close_") or unique_id.startswith("manual_close_")):
        return "close"
    if unique_id.startswith("entry_") or unique_id.endswith("_entry"):
        return "entry"
    if unique_id.startswith("tp_") or unique_id.endswith("_tp") or unique_id.endswith("_takeprofit"):
        return "take_profit"
    if unique_id.startswith("sl_") or unique_id.endswith("_sl") or unique_id.endswith("_stoploss"):
        return "stop_loss"
    if unique_id.startswith("close_") or unique_id.startswith("manual_close_") or unique_id.startswith("market_close_"):
        return "close"
    return role or order_type


def _filled_quantity(row: dict[str, Any]) -> float:
    return _positive_number(row, "filled_qty", "filledQuantity", "actual_filled_qty") or 0.0


def _is_filled(row: dict[str, Any]) -> bool:
    status = to_text(_row_value(row, "status")).lower()
    return status in {"filled", "executed", "closed"} or _filled_quantity(row) > 0


def _is_entry(row: dict[str, Any]) -> bool:
    return _lifecycle_role(row) == "entry"


def _is_exit(row: dict[str, Any]) -> bool:
    return _is_filled(row) and _lifecycle_role(row) in {"take_profit", "stop_loss", "close"}


def _number(row: dict[str, Any] | None, *fields: str) -> float | None:
    for field in fields:
        value = to_float(_row_value(row, field))
        if value is not None and math.isfinite(value):
            return float(value)
    return None


def _positive_number(row: dict[str, Any] | None, *fields: str) -> float | None:
    for field in fields:
        value = _number(row, field)
        if value is not None and value > 0:
            return value
    return None


def _order_group_key(row: dict[str, Any], index: int) -> str:
    for field in (
        "trade_group_id",
        "linked_trade_group_id",
        "entry_order_unique_id",
        "linked_entry_order_unique_id",
        "parent_order_unique_id",
        "signal_id",
    ):
        value = to_text(_row_value(row, field))
        if value:
            return value
    return to_text(_row_value(row, "id") or _row_value(row, "unique_id")) or f"row-{index}"


def _row_identity(row: dict[str, Any]) -> str:
    return to_text(_row_value(row, "id") or _row_value(row, "unique_id") or id(row))


def _append_unique(values: list[str], value: Any) -> None:
    text = to_text(value)
    if text and text not in values:
        values.append(text)


def _looks_like_close_reference(value: Any) -> bool:
    return to_text(value).lower().startswith(CLOSE_REFERENCE_PREFIXES)


def _is_close_self_reference(row: dict[str, Any], value: Any) -> bool:
    text = to_text(value)
    if not text or _lifecycle_role(row) != "close":
        return False
    unique_id = to_text(_row_value(row, "unique_id") or _row_value(row, "coid"))
    return text == unique_id or _looks_like_close_reference(text)


def _entry_aliases(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for field in (
        "unique_id",
        "entry_order_unique_id",
        "trade_group_id",
        "linked_trade_group_id",
        "signal_id",
        "broker_order_id",
        "order_id",
        "ib_order_id",
        "orderId",
    ):
        _append_unique(values, _row_value(row, field))
    return values


def _build_entry_index(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped_entries: dict[str, list[dict[str, Any]]] = {}
    group_aliases: dict[str, list[str]] = {}
    group_seen: dict[str, set[str]] = {}

    for index, row in enumerate(rows):
        if not _is_entry(row):
            continue
        group_key = (
            to_text(_row_value(row, "trade_group_id"))
            or to_text(_row_value(row, "entry_order_unique_id"))
            or to_text(_row_value(row, "unique_id"))
            or f"entry-{index}"
        )
        row_id = _row_identity(row)
        if row_id not in group_seen.setdefault(group_key, set()):
            group_seen[group_key].add(row_id)
            grouped_entries.setdefault(group_key, []).append(row)
        aliases = group_aliases.setdefault(group_key, [])
        _append_unique(aliases, group_key)
        for alias in _entry_aliases(row):
            _append_unique(aliases, alias)

    index: dict[str, list[dict[str, Any]]] = {}
    alias_seen: dict[str, set[str]] = {}
    for group_key, entries in grouped_entries.items():
        for alias in group_aliases.get(group_key, []):
            bucket = index.setdefault(alias, [])
            seen = alias_seen.setdefault(alias, set())
            for entry in entries:
                row_id = _row_identity(entry)
                if row_id in seen:
                    continue
                seen.add(row_id)
                bucket.append(entry)
    return index


def _exit_entry_aliases(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    role = _lifecycle_role(row)
    fields = (
        (
            "parent_order_unique_id",
            "linked_entry_order_unique_id",
            "entry_order_unique_id",
            "linked_trade_group_id",
            "trade_group_id",
            "signal_id",
            "parent_order_id",
            "parentId",
        )
        if role == "close"
        else (
            "entry_order_unique_id",
            "parent_order_unique_id",
            "linked_entry_order_unique_id",
            "trade_group_id",
            "linked_trade_group_id",
            "signal_id",
            "parent_order_id",
            "parentId",
        )
    )
    for field in fields:
        value = _row_value(row, field)
        if _is_close_self_reference(row, value):
            continue
        _append_unique(values, value)
    return values


def _entry_orders_for_exit(row: dict[str, Any], entry_index: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    for alias in _exit_entry_aliases(row):
        entries = entry_index.get(alias)
        if entries:
            return list(entries)
    return []


def _order_ids(row: dict[str, Any] | None) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for field in ("order_id", "broker_order_id", "ib_order_id", "orderId"):
        value = to_text(_row_value(row, field))
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def _fill_order_id(fill: dict[str, Any]) -> str:
    return to_text(fill.get("order_id") or fill.get("orderId") or fill.get("ibOrderID"))


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    text = to_text(value).lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _raw_has_commission(raw: dict[str, Any]) -> bool:
    normalized = {re.sub(r"[^a-z0-9]+", "", str(key or "").lower()) for key in raw.keys()}
    return bool(
        normalized
        & {
            "commission",
            "ibcommission",
            "ibkrcommission",
            "commissionamount",
            "commission_amount",
            "commissionandfees",
            "commissionreport",
        }
    )


def _commission_known(fill: dict[str, Any]) -> bool:
    explicit = _bool_value(fill.get("commission_known") if "commission_known" in fill else fill.get("commissionKnown"))
    if explicit is not None:
        return explicit
    raw = ensure_object(fill.get("raw"))
    if _raw_has_commission(raw):
        return True
    source = to_text(fill.get("source")).lower()
    return source in {"flex", "flex_payload"} and fill.get("commission") not in (None, "")


def _fill_number(fill: dict[str, Any], *fields: str) -> float:
    for field in fields:
        value = to_float(fill.get(field))
        if value is not None and math.isfinite(value):
            return float(value)
    return 0.0


def _fill_shares(fill: dict[str, Any]) -> float:
    return abs(_fill_number(fill, "shares", "quantity", "filled_qty", "filledQuantity"))


def _fill_price(fill: dict[str, Any]) -> float:
    return abs(_fill_number(fill, "price", "fill_price", "avgPrice", "avg_price"))


def _fill_commission(fill: dict[str, Any]) -> float:
    return abs(_fill_number(fill, "commission", "ibCommission", "ibkr_commission", "commission_amount"))


def _fill_currency(fill: dict[str, Any]) -> str:
    return to_text(fill.get("currency") or fill.get("commission_currency") or ensure_object(fill.get("raw")).get("currency")).upper()


def _fill_multiplier(fill: dict[str, Any]) -> tuple[float, bool]:
    raw = ensure_object(fill.get("raw"))
    multiplier = _fill_number(fill, "contract_multiplier", "multiplier")
    if multiplier <= 0:
        multiplier = _fill_number(raw, "contract_multiplier", "multiplier")
    asset = to_text(fill.get("asset_category") or raw.get("assetCategory") or raw.get("secType")).upper()
    if multiplier > 0:
        return multiplier, True
    if asset in {"", "STK", "ETF"}:
        return 1.0, True
    return 0.0, False


def _fill_side(fill: dict[str, Any]) -> str:
    text = to_text(fill.get("side") or ensure_object(fill.get("raw")).get("side")).lower()
    if text in {"buy", "bot", "b"}:
        return "buy"
    if text in {"sell", "sld", "s", "ss"}:
        return "sell"
    return text if text in {"long", "short"} else ""


def _trade_side(entry_orders: list[dict[str, Any]], exit_fills: list[dict[str, Any]], entry_fills: list[dict[str, Any]]) -> str:
    for row in entry_orders:
        text = to_text(_row_value(row, "position_side") or _row_value(row, "direction")).lower()
        if text in {"long", "short"}:
            return text
    for fill in entry_fills:
        side = _fill_side(fill)
        if side == "buy":
            return "long"
        if side == "sell":
            return "short"
    for fill in exit_fills:
        side = _fill_side(fill)
        if side == "sell":
            return "long"
        if side == "buy":
            return "short"
    return ""


def _stored_or_computed_estimated_pnl(exit_order: dict[str, Any], entry_order: dict[str, Any] | None) -> float | None:
    stored_net = _number(exit_order, "realized_net_pnl", "realized_pnl")
    if stored_net is not None and abs(stored_net) > EPSILON:
        return stored_net
    stored_gross = _number(exit_order, "realized_gross_pnl", "pnl")
    if stored_gross is not None and abs(stored_gross) > EPSILON:
        return stored_gross
    if not entry_order:
        return None
    entry_price = _positive_number(entry_order, "fill_price", "limit_price", "entry_price")
    exit_price = _positive_number(exit_order, "fill_price", "limit_price", "exit_price", "tp_price", "sl_price")
    quantity = first_defined(
        _positive_number(exit_order, "filled_qty", "quantity"),
        _positive_number(entry_order, "filled_qty", "quantity"),
    )
    side = to_text(_row_value(entry_order, "position_side") or _row_value(entry_order, "direction")).lower()
    if entry_price is None or exit_price is None or quantity is None or side not in {"long", "short"}:
        return None
    gross = (entry_price - exit_price) * quantity if side == "short" else (exit_price - entry_price) * quantity
    entry_commission = abs(_number(entry_order, "commission", "ibkr_commission") or 0.0)
    exit_commission = abs(_number(exit_order, "commission", "ibkr_commission") or 0.0)
    return gross - entry_commission - exit_commission


def _round_money(value: float) -> float:
    return round(float(value or 0.0), 2)


def _build_fill_index(fills: list[dict[str, Any]] | None) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for fill in fills or []:
        if not isinstance(fill, dict):
            continue
        order_id = _fill_order_id(fill)
        exec_id = to_text(fill.get("exec_id") or fill.get("execId"))
        key = (order_id, exec_id)
        if not order_id or key in seen:
            continue
        seen.add(key)
        index.setdefault(order_id, []).append(dict(fill))
    return index


def _fills_for_orders(fill_index: dict[str, list[dict[str, Any]]], orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    seen_execs: set[str] = set()
    for row in orders:
        for order_id in _order_ids(row):
            for fill in fill_index.get(order_id, []):
                exec_id = to_text(fill.get("exec_id") or fill.get("execId") or id(fill))
                if exec_id in seen_execs:
                    continue
                seen_execs.add(exec_id)
                fills.append(fill)
    return fills


def _order_time_ms(row: dict[str, Any]) -> int:
    return to_int(_row_value(row, "bar_time_ms") or _row_value(row, "filled_bar_time_ms"), 0)


def build_realized_pnl_stats(
    orders: list[dict[str, Any]] | None,
    fills: list[dict[str, Any]] | None,
    *,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict[str, Any]:
    stats = empty_realized_pnl_stats()
    order_rows = [dict(row) for row in (orders or []) if isinstance(row, dict)]
    fill_index = _build_fill_index(fills)
    entry_index = _build_entry_index(order_rows)

    for exit_order in order_rows:
        if not _is_exit(exit_order):
            continue
        order_ms = _order_time_ms(exit_order)
        if int(start_ms or 0) > 0 and order_ms > 0 and order_ms < int(start_ms):
            continue
        if int(end_ms or 0) > 0 and order_ms > 0 and order_ms >= int(end_ms):
            continue

        entry_orders = _entry_orders_for_exit(exit_order, entry_index)
        primary_entry = entry_orders[0] if entry_orders else None
        estimated = _stored_or_computed_estimated_pnl(exit_order, primary_entry)
        if estimated is not None:
            stats["estimated_total"] += estimated
            stats["estimated_exit_count"] += 1
        else:
            stats["estimated_missing_count"] += 1

        if not entry_orders:
            stats["entry_missing_count"] += 1
            stats["missing_count"] += 1
            continue

        entry_fills = _fills_for_orders(fill_index, entry_orders)
        exit_fills = _fills_for_orders(fill_index, [exit_order])
        if not entry_fills or not exit_fills:
            stats["fill_missing_count"] += 1
            stats["missing_count"] += 1
            continue

        if any(_fill_shares(fill) <= 0 or _fill_price(fill) <= 0 for fill in [*entry_fills, *exit_fills]):
            stats["fill_missing_count"] += 1
            stats["missing_count"] += 1
            continue

        if not all(_commission_known(fill) for fill in [*entry_fills, *exit_fills]):
            stats["commission_missing_count"] += 1
            stats["missing_count"] += 1
            continue

        currencies = {_fill_currency(fill) for fill in [*entry_fills, *exit_fills] if _fill_currency(fill)}
        if len(currencies) > 1:
            stats["currency_mismatch_count"] += 1
            stats["missing_count"] += 1
            continue

        multipliers: list[float] = []
        unsupported = False
        for fill in [*entry_fills, *exit_fills]:
            multiplier, supported = _fill_multiplier(fill)
            if not supported:
                unsupported = True
                break
            multipliers.append(multiplier)
        if unsupported or not multipliers:
            stats["unsupported_asset_count"] += 1
            stats["missing_count"] += 1
            continue
        multiplier = multipliers[0]

        trade_side = _trade_side(entry_orders, exit_fills, entry_fills)
        if trade_side not in {"long", "short"}:
            stats["fill_missing_count"] += 1
            stats["missing_count"] += 1
            continue

        entry_qty = sum(_fill_shares(fill) for fill in entry_fills)
        exit_qty = sum(_fill_shares(fill) for fill in exit_fills)
        if entry_qty <= 0 or exit_qty <= 0:
            stats["fill_missing_count"] += 1
            stats["missing_count"] += 1
            continue

        entry_value = sum(_fill_shares(fill) * _fill_price(fill) * multiplier for fill in entry_fills)
        exit_value = sum(_fill_shares(fill) * _fill_price(fill) * multiplier for fill in exit_fills)
        entry_value_for_exit = (entry_value / entry_qty) * exit_qty
        gross_pnl = entry_value_for_exit - exit_value if trade_side == "short" else exit_value - entry_value_for_exit
        entry_commission = sum(_fill_commission(fill) for fill in entry_fills) * min(exit_qty / entry_qty, 1.0)
        exit_commission = sum(_fill_commission(fill) for fill in exit_fills)
        commission = entry_commission + exit_commission
        net_pnl = gross_pnl - commission

        ibkr_realized = sum(
            _fill_number(fill, "realized_pnl", "realizedPNL", "realizedPnl")
            for fill in exit_fills
            if _fill_number(fill, "realized_pnl", "realizedPNL", "realizedPnl") != 0
        )
        if ibkr_realized and abs(ibkr_realized - net_pnl) > max(0.05, abs(net_pnl) * 0.005):
            stats["ibkr_realized_pnl_mismatch_count"] += 1

        stats["exit_count"] += 1
        stats["realized_gross_pnl"] += gross_pnl
        stats["realized_net_pnl"] += net_pnl
        stats["commission"] += commission
        stats["entry_commission"] += entry_commission
        stats["exit_commission"] += exit_commission
        stats["commission_fill_count"] += len(entry_fills) + len(exit_fills)
        if net_pnl > EPSILON:
            stats["win_count"] += 1
            stats["profit_amount"] += net_pnl
        elif net_pnl < -EPSILON:
            stats["loss_count"] += 1
            stats["loss_amount"] += net_pnl
        else:
            stats["flat_count"] += 1

    for key in (
        "realized_net_pnl",
        "realized_gross_pnl",
        "commission",
        "entry_commission",
        "exit_commission",
        "profit_amount",
        "loss_amount",
        "estimated_total",
    ):
        stats[key] = _round_money(stats[key])
    stats["commission_per_exit"] = _round_money(
        stats["commission"] / stats["exit_count"] if stats["exit_count"] else 0.0
    )
    stats["total"] = stats["realized_net_pnl"]
    return stats


__all__ = ["build_realized_pnl_stats", "empty_realized_pnl_stats"]
