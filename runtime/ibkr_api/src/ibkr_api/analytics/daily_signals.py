from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Callable


SIGNALS_COLLECTION = "ibkr_signals"
ORDERS_COLLECTION = "orders"
RR_MISMATCH_TOLERANCE = 0.35

ENTRY_ROLES = {"entry"}
EXIT_ROLES = {"take_profit", "repair_tp", "tp", "stop_loss", "repair_sl", "sl", "close", "manual_close", "market_close"}
TAKE_PROFIT_ROLES = {"take_profit", "repair_tp", "tp"}
STOP_LOSS_ROLES = {"stop_loss", "repair_sl", "sl"}
CLOSE_ROLES = {"close", "manual_close", "market_close", "close_order", "reverse_close"}


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _positive_float(*values: Any) -> float | None:
    for value in values:
        parsed = _to_float(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _extra(row: dict[str, Any] | None) -> dict[str, Any]:
    return _as_object((row or {}).get("extra"))


def _row_value(row: dict[str, Any] | None, field: str) -> Any:
    source = row or {}
    return source.get(field) if source.get(field) not in (None, "") else _extra(source).get(field)


def _normalize_role(value: Any) -> str:
    return _text(value).lower().replace("-", "_").replace(" ", "_")


def _order_role(row: dict[str, Any]) -> str:
    role = _normalize_role(_row_value(row, "role"))
    if role in ENTRY_ROLES:
        return "entry"
    if role in TAKE_PROFIT_ROLES:
        return "take_profit"
    if role in STOP_LOSS_ROLES:
        return "stop_loss"
    if role in CLOSE_ROLES:
        return "close"
    order_type = re.sub(r"[^a-z0-9]+", "", _text(_row_value(row, "order_type")).lower())
    if order_type in {"entry", "entryorder"}:
        return "entry"
    if order_type in {"takeprofit", "takeprofitorder", "tp"}:
        return "take_profit"
    if order_type in {"stoploss", "stoplossorder", "sl", "stop"}:
        return "stop_loss"
    if order_type in {"mkt", "market", "marketclose"} and _text(_row_value(row, "unique_id")).lower().startswith("close_"):
        return "close"
    return role


def _is_filled(row: dict[str, Any]) -> bool:
    return _text(_row_value(row, "status")).lower() == "filled"


def _is_entry(row: dict[str, Any]) -> bool:
    return _order_role(row) == "entry"


def _is_exit(row: dict[str, Any]) -> bool:
    return _order_role(row) in {"take_profit", "stop_loss", "close"}


def _commission(row: dict[str, Any] | None) -> float:
    value = _to_float(_row_value(row or {}, "commission"))
    return abs(value or 0.0)


def _normalize_trade_side(value: Any, *, exit_order_direction: bool = False) -> str:
    text = _text(value).lower()
    if text in {"long", "buy_to_open"}:
        return "long"
    if text in {"short", "sell_to_open"}:
        return "short"
    if text == "buy":
        return "short" if exit_order_direction else "long"
    if text == "sell":
        return "long" if exit_order_direction else "short"
    return ""


def _trade_side(entry_order: dict[str, Any] | None, exit_order: dict[str, Any] | None, signal: dict[str, Any] | None = None) -> str:
    for value in (
        _row_value(entry_order, "position_side"),
        _row_value(entry_order, "direction"),
        _row_value(exit_order, "position_side"),
        (signal or {}).get("direction"),
    ):
        side = _normalize_trade_side(value)
        if side:
            return side
    return _normalize_trade_side(_row_value(exit_order, "direction"), exit_order_direction=True)


def _directional_pnl(direction: Any, entry_price: Any, exit_price: Any, quantity: Any) -> float | None:
    entry = _to_float(entry_price)
    exit_ = _to_float(exit_price)
    qty = _to_float(quantity)
    if entry is None or exit_ is None or qty is None or entry <= 0 or exit_ <= 0 or qty <= 0:
        return None
    side = _normalize_trade_side(direction)
    if not side:
        return None
    per_share = entry - exit_ if side == "short" else exit_ - entry
    return per_share * abs(qty)


def _parse_rr(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed > 0 else None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", _text(value))
    if not match:
        return None
    parsed = _to_float(match.group(1))
    return parsed if parsed and parsed > 0 else None


def _date_token(value: Any, fallback: str) -> str:
    text = _text(value) or fallback
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return fallback


def _next_date_token(date_token: str) -> str:
    return (datetime.strptime(date_token, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _load_records(
    pb: Any,
    collection: str,
    *,
    filter_expr: str,
    sort: str = "-created",
    per_page: int = 200,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    safe_per_page = max(1, min(int(per_page or 200), 200))
    for page in range(1, max(1, int(max_pages or 1)) + 1):
        batch = pb.get_records(collection, filter=filter_expr, sort=sort, per_page=safe_per_page, page=page) or []
        page_rows = [dict(row) for row in batch if isinstance(row, dict)]
        rows.extend(page_rows)
        if len(page_rows) < safe_per_page:
            break
    return rows


def _chunk(values: list[str], size: int) -> list[list[str]]:
    safe_size = max(1, int(size or 1))
    return [values[index : index + safe_size] for index in range(0, len(values), safe_size)]


def _signal_extra(signal: dict[str, Any]) -> dict[str, Any]:
    return _extra(signal)


def _signal_setup(signal: dict[str, Any]) -> str:
    extra = _signal_extra(signal)
    return _text(extra.get("setup") or signal.get("setup") or signal.get("signal")) or "unknown"


def _signal_setup_label(signal: dict[str, Any]) -> str:
    extra = _signal_extra(signal)
    return _text(extra.get("setup_label") or signal.get("setup_label") or _signal_setup(signal))


def _signal_family(signal: dict[str, Any]) -> str:
    extra = _signal_extra(signal)
    return _text(extra.get("setup_family") or signal.get("setup_family") or extra.get("signal_mode") or "unknown") or "unknown"


def _effective_signal_status(signal: dict[str, Any], broker_mode: str) -> str:
    extra = _signal_extra(signal)
    by_mode = extra.get("execution_by_mode")
    if isinstance(by_mode, dict):
        for key in (broker_mode, broker_mode.lower(), broker_mode.upper()):
            payload = by_mode.get(key)
            if isinstance(payload, dict):
                status = _text(payload.get("status")).lower()
                if status:
                    return status
    return _text(signal.get("status") or "pending").lower() or "pending"


def _planned_signal_risk(signal: dict[str, Any]) -> dict[str, Any]:
    direction = _text(signal.get("direction")).lower()
    entry = _positive_float(signal.get("entry"), signal.get("limit_price"), _signal_extra(signal).get("entry"))
    quantity = _positive_float(signal.get("shares"), _signal_extra(signal).get("shares"), _signal_extra(signal).get("quantity"))
    take_profit = _positive_float(signal.get("take_profit"), _signal_extra(signal).get("take_profit"), _signal_extra(signal).get("tp_price"))
    stop_loss = _positive_float(signal.get("stop_loss"), _signal_extra(signal).get("stop_loss"), _signal_extra(signal).get("sl_price"))
    expected_profit = _directional_pnl(direction, entry, take_profit, quantity)
    expected_loss = _directional_pnl(direction, entry, stop_loss, quantity)
    parsed_rr = _parse_rr(signal.get("rr") or _signal_extra(signal).get("rr"))
    valid = expected_profit is not None and expected_profit > 0 and expected_loss is not None and expected_loss < 0
    calculated_rr = (expected_profit / abs(expected_loss)) if valid and expected_loss else None
    mismatch = bool(parsed_rr is not None and calculated_rr is not None and abs(parsed_rr - calculated_rr) > RR_MISMATCH_TOLERANCE)
    return {
        "parsed_rr": parsed_rr,
        "calculated_rr": calculated_rr,
        "expected_profit": expected_profit if expected_profit is not None else 0.0,
        "expected_loss": expected_loss if expected_loss is not None else 0.0,
        "planned_risk": abs(expected_loss) if valid and expected_loss is not None else 0.0,
        "valid": valid,
        "mismatch": mismatch,
    }


def _order_key_values(row: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for field in fields:
        value = _text(_row_value(row, field))
        if value and value not in values:
            values.append(value)
    return values


def _build_entry_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not _is_entry(row):
            continue
        for key in _order_key_values(row, ("unique_id", "entry_order_unique_id", "trade_group_id", "signal_id", "broker_order_id", "order_id")):
            index.setdefault(key, row)
    return index


def _find_entry_for_exit(exit_order: dict[str, Any], entry_index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for key in _order_key_values(exit_order, ("entry_order_unique_id", "parent_order_unique_id", "trade_group_id", "signal_id")):
        entry = entry_index.get(key)
        if entry:
            return entry
    return None


def _order_price(row: dict[str, Any] | None, *fields: str) -> float | None:
    for field in fields:
        value = _positive_float(_row_value(row or {}, field))
        if value is not None:
            return value
    return None


def _order_quantity(row: dict[str, Any] | None, *fields: str) -> float | None:
    return _order_price(row, *fields)


def _exit_role_label(role: str) -> str:
    if role == "take_profit":
        return "take_profit"
    if role == "stop_loss":
        return "stop_loss"
    if role == "close":
        return "close"
    return role or "exit"


def _compute_realized_trade(
    exit_order: dict[str, Any],
    entry_order: dict[str, Any] | None,
    signal: dict[str, Any] | None,
) -> dict[str, Any] | None:
    role = _order_role(exit_order)
    side = _trade_side(entry_order, exit_order, signal)
    entry_price = _order_price(entry_order, "fill_price", "filled_price", "avgPrice", "avg_fill_price", "limit_price", "entry_price")
    if entry_price is None and signal:
        entry_price = _positive_float(signal.get("executed_price"), signal.get("entry"), signal.get("limit_price"))
    exit_price = _order_price(exit_order, "fill_price", "filled_price", "avgPrice", "avg_fill_price", "limit_price", "exit_price", "tp_price", "sl_price")
    quantity = _order_quantity(exit_order, "filled_qty", "quantity")
    if quantity is None:
        quantity = _order_quantity(entry_order, "filled_qty", "quantity")
    stored_pnl = _to_float(_row_value(exit_order, "pnl"))
    gross_pnl = stored_pnl if stored_pnl is not None and abs(stored_pnl) > 0.0000001 else None
    if gross_pnl is None:
        gross_pnl = _directional_pnl(side, entry_price, exit_price, quantity)
    if gross_pnl is None:
        return None
    entry_qty = _order_quantity(entry_order, "filled_qty", "quantity") or quantity or 0.0
    entry_commission = _commission(entry_order)
    if entry_qty and quantity and quantity < entry_qty:
        entry_commission = entry_commission * (quantity / entry_qty)
    commission = entry_commission + _commission(exit_order)
    net_pnl = gross_pnl - commission
    return {
        "signal_id": _text(exit_order.get("signal_id") or (signal or {}).get("signal_id")),
        "symbol": _upper(exit_order.get("symbol") or (signal or {}).get("symbol")),
        "direction": side,
        "role": _exit_role_label(role),
        "entry_price": round(float(entry_price or 0.0), 6),
        "exit_price": round(float(exit_price or 0.0), 6),
        "quantity": round(float(quantity or 0.0), 6),
        "gross_pnl": round(float(gross_pnl), 6),
        "commission": round(float(commission), 6),
        "net_pnl": round(float(net_pnl), 6),
        "us_time": _text(exit_order.get("us_time") or exit_order.get("fill_time")),
        "trade_group_id": _text(exit_order.get("trade_group_id") or (entry_order or {}).get("trade_group_id")),
    }


def _empty_money_stats() -> dict[str, Any]:
    return {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "flat": 0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "net_pnl": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "profit_factor": None,
        "profit_loss_ratio": None,
        "profit_loss_ratio_reason": "no_closed_trades",
    }


def _finalize_money_stats(values: list[float]) -> dict[str, Any]:
    stats = _empty_money_stats()
    stats["count"] = len(values)
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    flats = [value for value in values if abs(value) <= 0.0000001]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    stats.update(
        {
            "wins": len(wins),
            "losses": len(losses),
            "flat": len(flats),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "net_pnl": round(sum(values), 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(gross_profit / abs(gross_loss), 4) if gross_profit > 0 and gross_loss < 0 else None,
            "profit_loss_ratio": round(avg_win / abs(avg_loss), 4) if avg_win > 0 and avg_loss < 0 else None,
        }
    )
    if not values:
        stats["profit_loss_ratio_reason"] = "no_closed_trades"
    elif not wins:
        stats["profit_loss_ratio_reason"] = "no_winning_trades"
    elif not losses:
        stats["profit_loss_ratio_reason"] = "no_losing_trades"
    else:
        stats["profit_loss_ratio_reason"] = ""
    return stats


def _distribution(counter: Counter[str], total: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, count in counter.most_common():
        rows.append({"key": key, "count": count, "pct": round((count / total) * 100, 2) if total else 0.0})
    return rows


def _safe_avg(values: list[float]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 4) if clean else None


def _safe_median(values: list[float]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(float(median(clean)), 4) if clean else None


def build_daily_signal_analytics_response(
    pb: Any,
    *,
    params: dict[str, Any] | None = None,
    normalize_environment: Callable[[Any, str], str],
    escape_filter_string: Callable[[Any], str],
    time_strings: Callable[[], dict[str, str]],
) -> tuple[dict[str, Any], int]:
    request_params = params if isinstance(params, dict) else {}
    fallback_date = _text((time_strings() or {}).get("date")) or datetime.utcnow().strftime("%Y-%m-%d")
    date_token = _date_token(request_params.get("date") or request_params.get("market_date"), fallback_date)
    next_date = _next_date_token(date_token)
    # Console URLs often carry environment=live for market-data context; do not
    # let that override the intended default paper execution account.
    raw_broker_mode = request_params.get("broker_mode")
    if not _text(raw_broker_mode) and _text(request_params.get("environment")).lower() == "paper":
        raw_broker_mode = "paper"
    broker_mode = normalize_environment(raw_broker_mode, "paper")
    data_environment = normalize_environment(
        request_params.get("data_environment") or request_params.get("market_data_mode"),
        "live",
    )
    broker_escaped = escape_filter_string(broker_mode)
    data_escaped = escape_filter_string(data_environment)
    date_escaped = escape_filter_string(date_token)
    next_date_escaped = escape_filter_string(next_date)
    signal_filter = (
        f'environment = "{data_escaped}" && '
        f'(date = "{date_escaped}" || (us_time >= "{date_escaped} 00:00:00" && us_time < "{next_date_escaped} 00:00:00"))'
    )
    signals = _load_records(pb, SIGNALS_COLLECTION, filter_expr=signal_filter, sort="us_time", max_pages=30)
    signal_ids = [_text(row.get("signal_id")) for row in signals if _text(row.get("signal_id"))]
    unique_signal_ids = list(dict.fromkeys(signal_ids))

    orders: list[dict[str, Any]] = []
    for id_chunk in _chunk(unique_signal_ids, 25):
        id_filter = " || ".join(f'signal_id = "{escape_filter_string(signal_id)}"' for signal_id in id_chunk)
        order_filter = f'environment = "{broker_escaped}" && ({id_filter})'
        orders.extend(_load_records(pb, ORDERS_COLLECTION, filter_expr=order_filter, sort="us_time", max_pages=20))

    signals_by_id = {_text(signal.get("signal_id")): signal for signal in signals if _text(signal.get("signal_id"))}
    planned_by_signal: dict[str, dict[str, Any]] = {}
    rr_values: list[float] = []
    calculated_rr_values: list[float] = []
    planned_profit_sum = 0.0
    planned_risk_sum = 0.0
    planned_valid_count = 0
    rr_mismatch_count = 0
    rr_invalid_count = 0

    direction_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    setup_counts: Counter[str] = Counter()
    setup_label_by_key: dict[str, str] = {}
    family_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    time_counts: Counter[str] = Counter()
    setup_summary: dict[str, dict[str, Any]] = {}

    for signal in signals:
        signal_id = _text(signal.get("signal_id"))
        direction = _text(signal.get("direction")).lower() or "unknown"
        status = _effective_signal_status(signal, broker_mode)
        setup = _signal_setup(signal)
        family = _signal_family(signal)
        symbol = _upper(signal.get("symbol"))
        planned = _planned_signal_risk(signal)
        planned_by_signal[signal_id] = planned
        direction_counts[direction] += 1
        status_counts[status] += 1
        setup_counts[setup] += 1
        setup_label_by_key.setdefault(setup, _signal_setup_label(signal))
        family_counts[family] += 1
        if symbol:
            symbol_counts[symbol] += 1
        us_time = _text(signal.get("us_time"))
        if len(us_time) >= 13:
            time_counts[f"{us_time[11:13]}:00"] += 1
        if planned["parsed_rr"] is not None:
            rr_values.append(float(planned["parsed_rr"]))
        if planned["calculated_rr"] is not None:
            calculated_rr_values.append(float(planned["calculated_rr"]))
        if planned["valid"]:
            planned_valid_count += 1
            planned_profit_sum += float(planned["expected_profit"] or 0.0)
            planned_risk_sum += float(planned["planned_risk"] or 0.0)
        else:
            rr_invalid_count += 1
        if planned["mismatch"]:
            rr_mismatch_count += 1
        row = setup_summary.setdefault(
            setup,
            {
                "key": setup,
                "label": setup_label_by_key.get(setup, setup),
                "family": family,
                "count": 0,
                "long": 0,
                "short": 0,
                "status_counts": {},
                "rr_values": [],
                "calculated_rr_values": [],
                "planned_profit": 0.0,
                "planned_risk": 0.0,
                "planned_valid_count": 0,
                "rr_mismatch_count": 0,
                "entry_filled": 0,
                "closed_trades": 0,
                "wins": 0,
                "losses": 0,
                "flat": 0,
                "net_pnl": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
            },
        )
        row["count"] += 1
        if direction == "long":
            row["long"] += 1
        elif direction == "short":
            row["short"] += 1
        row["status_counts"][status] = int(row["status_counts"].get(status, 0)) + 1
        if planned["parsed_rr"] is not None:
            row["rr_values"].append(float(planned["parsed_rr"]))
        if planned["calculated_rr"] is not None:
            row["calculated_rr_values"].append(float(planned["calculated_rr"]))
        if planned["valid"]:
            row["planned_valid_count"] += 1
            row["planned_profit"] += float(planned["expected_profit"] or 0.0)
            row["planned_risk"] += float(planned["planned_risk"] or 0.0)
        if planned["mismatch"]:
            row["rr_mismatch_count"] += 1

    entry_index = _build_entry_index(orders)
    entries_by_signal = defaultdict(list)
    realized_trades: list[dict[str, Any]] = []
    exit_role_counts: Counter[str] = Counter()
    for order in orders:
        if _is_entry(order):
            entries_by_signal[_text(order.get("signal_id"))].append(order)
    entry_filled_signal_ids = {
        signal_id for signal_id, rows in entries_by_signal.items() if any(_is_filled(row) for row in rows)
    }
    for signal_id in entry_filled_signal_ids:
        signal = signals_by_id.get(signal_id)
        if not signal:
            continue
        setup = _signal_setup(signal)
        if setup in setup_summary:
            setup_summary[setup]["entry_filled"] += 1

    for order in orders:
        if not _is_exit(order) or not _is_filled(order):
            continue
        signal_id = _text(order.get("signal_id"))
        trade = _compute_realized_trade(order, _find_entry_for_exit(order, entry_index), signals_by_id.get(signal_id))
        if not trade:
            continue
        realized_trades.append(trade)
        exit_role_counts[trade["role"]] += 1
        signal = signals_by_id.get(signal_id)
        if signal:
            setup = _signal_setup(signal)
            row = setup_summary.get(setup)
            if row:
                pnl = float(trade["net_pnl"])
                row["closed_trades"] += 1
                row["net_pnl"] += pnl
                if pnl > 0:
                    row["wins"] += 1
                    row["gross_profit"] += pnl
                elif pnl < 0:
                    row["losses"] += 1
                    row["gross_loss"] += pnl
                else:
                    row["flat"] += 1

    realized_values = [float(trade["net_pnl"]) for trade in realized_trades]
    realized_stats = _finalize_money_stats(realized_values)
    setup_rows: list[dict[str, Any]] = []
    for setup, row in setup_summary.items():
        avg_win = (row["gross_profit"] / row["wins"]) if row["wins"] else 0.0
        avg_loss = (row["gross_loss"] / row["losses"]) if row["losses"] else 0.0
        setup_rows.append(
            {
                "key": setup,
                "label": row["label"],
                "family": row["family"],
                "count": row["count"],
                "long": row["long"],
                "short": row["short"],
                "status_counts": row["status_counts"],
                "avg_rr": _safe_avg(row["rr_values"]),
                "avg_calculated_rr": _safe_avg(row["calculated_rr_values"]),
                "planned_profit": round(float(row["planned_profit"]), 2),
                "planned_risk": round(float(row["planned_risk"]), 2),
                "planned_portfolio_rr": round(float(row["planned_profit"]) / float(row["planned_risk"]), 4)
                if row["planned_risk"]
                else None,
                "planned_valid_count": row["planned_valid_count"],
                "rr_mismatch_count": row["rr_mismatch_count"],
                "entry_filled": row["entry_filled"],
                "closed_trades": row["closed_trades"],
                "wins": row["wins"],
                "losses": row["losses"],
                "flat": row["flat"],
                "win_rate": round((row["wins"] / row["closed_trades"]) * 100, 2) if row["closed_trades"] else 0.0,
                "net_pnl": round(float(row["net_pnl"]), 2),
                "gross_profit": round(float(row["gross_profit"]), 2),
                "gross_loss": round(float(row["gross_loss"]), 2),
                "profit_factor": round(float(row["gross_profit"]) / abs(float(row["gross_loss"])), 4)
                if row["gross_profit"] > 0 and row["gross_loss"] < 0
                else None,
                "profit_loss_ratio": round(avg_win / abs(avg_loss), 4) if avg_win > 0 and avg_loss < 0 else None,
            }
        )
    setup_rows.sort(key=lambda item: (-int(item["count"]), str(item["key"])))

    total_signals = len(signals)
    order_status_counts = Counter(_text(order.get("status") or "unknown") for order in orders)
    order_role_counts = Counter(_order_role(order) or "unknown" for order in orders)
    rr_distribution = Counter(str(_parse_rr(signal.get("rr") or _signal_extra(signal).get("rr")) or "unknown") for signal in signals)
    top_rule = setup_rows[0] if setup_rows else None
    payload = {
        "ok": True,
        "source": "ibkr-api",
        "date": date_token,
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "signal_filter": signal_filter,
        "order_signal_ids": len(unique_signal_ids),
        "summary": {
            "total_signals": total_signals,
            "unique_symbols": len(symbol_counts),
            "multi_signal_symbols": sum(1 for count in symbol_counts.values() if count > 1),
            "top_rule": top_rule,
            "avg_rr": _safe_avg(rr_values),
            "median_rr": _safe_median(rr_values),
            "avg_calculated_rr": _safe_avg(calculated_rr_values),
            "planned_valid_count": planned_valid_count,
            "planned_invalid_count": rr_invalid_count,
            "rr_mismatch_count": rr_mismatch_count,
            "planned_profit": round(planned_profit_sum, 2),
            "planned_risk": round(planned_risk_sum, 2),
            "planned_portfolio_rr": round(planned_profit_sum / planned_risk_sum, 4) if planned_risk_sum else None,
            "orders": len(orders),
            "ordered_signals": len({order.get("signal_id") for order in orders if _text(order.get("signal_id"))}),
            "entry_filled_signals": len(entry_filled_signal_ids),
            "closed_trades": realized_stats["count"],
            "realized": realized_stats,
        },
        "distributions": {
            "direction": _distribution(direction_counts, total_signals),
            "status": _distribution(status_counts, total_signals),
            "setup": _distribution(setup_counts, total_signals),
            "family": _distribution(family_counts, total_signals),
            "time_hour": _distribution(time_counts, total_signals),
            "rr": _distribution(rr_distribution, total_signals),
            "order_status": _distribution(order_status_counts, len(orders)),
            "order_role": _distribution(order_role_counts, len(orders)),
            "exit_role": _distribution(exit_role_counts, max(1, sum(exit_role_counts.values()))),
        },
        "setup_rows": setup_rows,
        "realized_trades": sorted(realized_trades, key=lambda item: item.get("us_time") or "")[:100],
    }
    return payload, 200


__all__ = ["build_daily_signal_analytics_response"]
