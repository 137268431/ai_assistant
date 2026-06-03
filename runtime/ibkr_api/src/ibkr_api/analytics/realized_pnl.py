from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


FILLS_COLLECTION = "ibkr_execution_fills"
ORDERS_COLLECTION = "orders"
EPSILON = 0.0000001

NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]
TimeStrings = Callable[[], dict[str, str]]


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _row_value(row: dict[str, Any] | None, field: str) -> Any:
    source = row or {}
    extra = _as_object(source.get("extra"))
    value = source.get(field)
    return value if value not in (None, "") else extra.get(field)


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _to_bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _lower(value)
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _positive_float(*values: Any) -> float | None:
    for value in values:
        parsed = _to_float(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _has_field(row: dict[str, Any] | None, *fields: str) -> bool:
    source = row or {}
    extra = _as_object(source.get("extra"))
    for field in fields:
        if source.get(field) not in (None, "") or extra.get(field) not in (None, ""):
            return True
    return False


def _date_token(value: Any, fallback: str) -> str:
    text = _text(value) or fallback
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return fallback


def _next_date_token(date_token: str) -> str:
    return (datetime.strptime(date_token, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _parse_time_text_ms(value: Any) -> int:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    text = _text(value)
    if not text:
        return 0
    if text.isdigit():
        return _to_int(text, 0)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except Exception:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[: len(pattern)], pattern).replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except Exception:
            continue
    return 0


def _date_from_ms(ms: int, fallback: str) -> str:
    if int(ms or 0) <= 0:
        return fallback
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return fallback


def _fill_time_ms(fill: dict[str, Any]) -> int:
    for field in ("trade_time_ms", "fill_time_ms", "time_ms", "created_ms"):
        value = _to_int(fill.get(field), 0)
        if value > 0:
            return value
    for field in ("trade_time", "fill_time", "us_time", "created", "updated"):
        value = _parse_time_text_ms(fill.get(field))
        if value > 0:
            return value
    return 0


def _fill_date(fill: dict[str, Any], fallback: str = "") -> str:
    for field in ("trade_time", "fill_time", "us_time", "created", "updated"):
        text = _text(fill.get(field))
        if len(text) >= 10:
            try:
                return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
            except Exception:
                continue
    return _date_from_ms(_fill_time_ms(fill), fallback)


def _order_time_ms(row: dict[str, Any]) -> int:
    for field in ("bar_time_ms", "filled_bar_time_ms", "trade_time_ms", "filled_at_ms", "created_ms", "updated_ms"):
        value = _to_int(_row_value(row, field), 0)
        if value > 0:
            return value
    for field in ("us_time", "fill_time", "trade_time", "created", "updated"):
        value = _parse_time_text_ms(_row_value(row, field))
        if value > 0:
            return value
    return 0


def _order_date(row: dict[str, Any], fallback: str = "") -> str:
    for field in ("us_time", "fill_time", "trade_time", "created", "updated"):
        text = _text(_row_value(row, field))
        if len(text) >= 10:
            try:
                return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
            except Exception:
                continue
    return _date_from_ms(_order_time_ms(row), fallback)


def _date_in_range(day: str, start_date: str, end_date: str) -> bool:
    return bool(day and start_date <= day <= end_date)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    parsed = _to_int(value, default)
    return max(int(minimum), min(int(maximum), parsed))


def _round_money(value: Any) -> float:
    return round(float(value or 0.0), 2)


def _round_float(value: Any, places: int = 6) -> float:
    return round(float(value or 0.0), places)


def _load_records(
    pb: Any,
    collection: str,
    *,
    filter_expr: str,
    sort: str,
    per_page: int = 200,
    max_pages: int = 100,
) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    try:
        getter = getattr(pb, "get_records", None)
        if not callable(getter):
            return [], f"{collection}:missing_get_records"
        safe_per_page = max(1, min(int(per_page or 200), 200))
        for page in range(1, max(1, int(max_pages or 1)) + 1):
            batch = getter(collection, filter=filter_expr, sort=sort, per_page=safe_per_page, page=page) or []
            page_rows = [dict(row) for row in batch if isinstance(row, dict)]
            rows.extend(page_rows)
            if len(page_rows) < safe_per_page:
                break
        return rows, ""
    except Exception as exc:
        return [], f"{collection}:{exc}"


def _fill_number(fill: dict[str, Any], *fields: str) -> float | None:
    raw = _as_object(fill.get("raw"))
    for field in fields:
        value = _to_float(fill.get(field))
        if value is not None:
            return value
        value = _to_float(raw.get(field))
        if value is not None:
            return value
    return None


def _fill_shares(fill: dict[str, Any]) -> float:
    return abs(_fill_number(fill, "shares", "quantity", "filled_qty", "filledQuantity") or 0.0)


def _fill_price(fill: dict[str, Any]) -> float:
    return abs(_fill_number(fill, "price", "fill_price", "avgPrice", "avg_price") or 0.0)


def _fill_commission(fill: dict[str, Any]) -> float:
    return abs(_fill_number(fill, "commission", "ibCommission", "ibkr_commission", "commission_amount") or 0.0)


def _raw_has_commission(raw: dict[str, Any]) -> bool:
    normalized = {re.sub(r"[^a-z0-9]+", "", str(key or "").lower()) for key in raw.keys()}
    return bool(normalized & {"commission", "ibcommission", "ibkrcommission", "commissionamount", "commissionreport"})


def _commission_known(fill: dict[str, Any]) -> bool:
    if fill.get("commission_known") in (True, False):
        return bool(fill.get("commission_known"))
    if fill.get("commissionKnown") in (True, False):
        return bool(fill.get("commissionKnown"))
    if fill.get("commission") not in (None, ""):
        return True
    return _raw_has_commission(_as_object(fill.get("raw")))


def _commission_verified(fill: dict[str, Any]) -> bool:
    raw = _as_object(fill.get("raw"))
    if "commission_known" in fill:
        return _to_bool(fill.get("commission_known"))
    if "commissionKnown" in fill:
        return _to_bool(fill.get("commissionKnown"))
    if "commission_known" in raw:
        return _to_bool(raw.get("commission_known"))
    if "commissionKnown" in raw:
        return _to_bool(raw.get("commissionKnown"))
    return False


def _fill_currency(fill: dict[str, Any]) -> str:
    raw = _as_object(fill.get("raw"))
    return _upper(fill.get("currency") or fill.get("commission_currency") or raw.get("currency"))


def _fill_multiplier(fill: dict[str, Any]) -> tuple[float, bool]:
    raw = _as_object(fill.get("raw"))
    multiplier = _fill_number(fill, "contract_multiplier", "multiplier")
    if multiplier is None or multiplier <= 0:
        multiplier = _fill_number(raw, "contract_multiplier", "multiplier")
    asset = _upper(fill.get("asset_category") or raw.get("assetCategory") or raw.get("secType"))
    if multiplier is not None and multiplier > 0:
        return multiplier, True
    if asset in {"", "STK", "ETF"}:
        return 1.0, True
    return 0.0, False


def _fill_side(fill: dict[str, Any]) -> str:
    raw = _as_object(fill.get("raw"))
    text = _lower(fill.get("side") or fill.get("action") or raw.get("side") or raw.get("action"))
    if text in {"buy", "bot", "b"}:
        return "buy"
    if text in {"sell", "sld", "s", "ss"}:
        return "sell"
    return ""


def _exec_id(fill: dict[str, Any]) -> str:
    return _text(fill.get("exec_id") or fill.get("execId") or fill.get("id"))


def _dedupe_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int]] = set()
    for index, fill in enumerate(fills):
        exec_id = _exec_id(fill)
        key = (_upper(fill.get("symbol")), exec_id, _text(fill.get("order_id") or fill.get("orderId")), _fill_time_ms(fill))
        if exec_id and key in seen:
            continue
        if exec_id:
            seen.add(key)
        deduped.append({**fill, "_source_index": index})
    return deduped


def _compute_fill_trades(
    fills: list[dict[str, Any]],
    *,
    start_date: str,
    end_date: str,
    require_verified_commission: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    missing = Counter()
    trades: list[dict[str, Any]] = []
    lots_by_symbol: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    sorted_fills = sorted(_dedupe_fills(fills), key=lambda row: (_fill_time_ms(row), int(row.get("_source_index") or 0)))

    for fill in sorted_fills:
        symbol = _upper(fill.get("symbol"))
        side = _fill_side(fill)
        qty = _fill_shares(fill)
        price = _fill_price(fill)
        if not symbol or side not in {"buy", "sell"} or qty <= 0 or price <= 0:
            missing["fill_missing_required_count"] += 1
            continue
        multiplier, supported = _fill_multiplier(fill)
        if not supported:
            missing["unsupported_asset_count"] += 1
            continue
        if require_verified_commission and not _commission_verified(fill):
            missing["commission_missing_count"] += 1
            continue
        if not _commission_known(fill):
            missing["commission_missing_count"] += 1
        commission = _fill_commission(fill)
        fill_currency = _fill_currency(fill)
        fill_date = _fill_date(fill, start_date)
        fill_ms = _fill_time_ms(fill)
        direction_sign = 1 if side == "buy" else -1
        remaining_qty = qty
        total_fill_qty = qty
        remaining_commission = commission
        lots = lots_by_symbol[symbol]

        while remaining_qty > EPSILON and lots and int(lots[0]["sign"]) != direction_sign:
            lot = lots[0]
            lot_qty = float(lot["qty"])
            close_qty = min(remaining_qty, lot_qty)
            if close_qty <= EPSILON:
                lots.popleft()
                continue
            lot_ratio = close_qty / lot_qty if lot_qty > 0 else 0.0
            fill_ratio = close_qty / total_fill_qty if total_fill_qty > 0 else 0.0
            entry_commission = float(lot.get("commission") or 0.0) * lot_ratio
            exit_commission = commission * fill_ratio
            if lot.get("currency") and fill_currency and lot.get("currency") != fill_currency:
                missing["currency_mismatch_count"] += 1
            trade_side = "long" if int(lot["sign"]) > 0 else "short"
            entry_price = float(lot["price"])
            gross_pnl = (entry_price - price) * close_qty * float(lot.get("multiplier") or multiplier)
            if trade_side == "long":
                gross_pnl = (price - entry_price) * close_qty * float(lot.get("multiplier") or multiplier)
            total_commission = entry_commission + exit_commission
            net_pnl = gross_pnl - total_commission
            if _date_in_range(fill_date, start_date, end_date):
                trades.append(
                    {
                        "date": fill_date,
                        "symbol": symbol,
                        "direction": trade_side,
                        "quantity": _round_float(close_qty),
                        "entry_price": _round_float(entry_price),
                        "exit_price": _round_float(price),
                        "realized_gross_pnl": _round_float(gross_pnl),
                        "commission": _round_float(total_commission),
                        "realized_net_pnl": _round_float(net_pnl),
                        "source": FILLS_COLLECTION,
                        "entry_exec_id": _text(lot.get("exec_id")),
                        "exit_exec_id": _exec_id(fill),
                        "exit_time_ms": fill_ms,
                    }
                )
            lot["qty"] = lot_qty - close_qty
            lot["commission"] = max(0.0, float(lot.get("commission") or 0.0) - entry_commission)
            remaining_qty -= close_qty
            remaining_commission = max(0.0, remaining_commission - exit_commission)
            if float(lot["qty"]) <= EPSILON:
                lots.popleft()

        if remaining_qty > EPSILON:
            lots.append(
                {
                    "sign": direction_sign,
                    "qty": remaining_qty,
                    "price": price,
                    "commission": remaining_commission,
                    "currency": fill_currency,
                    "multiplier": multiplier,
                    "exec_id": _exec_id(fill),
                    "opened_date": fill_date,
                    "opened_time_ms": fill_ms,
                }
            )

    return trades, dict(missing)


def _normalize_role(value: Any) -> str:
    return _lower(value).replace("-", "_").replace(" ", "_")


def _normalized_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _lower(value))


def _order_role(row: dict[str, Any]) -> str:
    role = _normalize_role(_row_value(row, "role"))
    order_type = _normalized_token(_row_value(row, "order_type"))
    unique_id = _lower(_row_value(row, "unique_id") or _row_value(row, "coid"))
    if role in {"entry"} or order_type in {"entry", "entryorder"} or unique_id.startswith("entry_"):
        return "entry"
    if role in {"take_profit", "repair_tp", "tp"} or order_type in {"takeprofit", "takeprofitorder", "take_profit", "tp"}:
        return "take_profit"
    if role in {"stop_loss", "repair_sl", "sl"} or order_type in {"stoploss", "stoplossorder", "stop_loss", "sl", "stop"}:
        return "stop_loss"
    if role in {"close", "manual_close", "market_close", "close_order", "reverse_close"}:
        return "close"
    if order_type in {"mkt", "market", "marketclose", "close", "market_close"} and (
        unique_id.startswith("close_") or unique_id.startswith("manual_close_")
    ):
        return "close"
    if unique_id.startswith(("close_", "manual_close_", "market_close_")):
        return "close"
    return role or order_type


def _order_filled_qty(row: dict[str, Any]) -> float:
    return _positive_float(_row_value(row, "filled_qty"), _row_value(row, "filledQuantity"), _row_value(row, "actual_filled_qty")) or 0.0


def _is_filled(row: dict[str, Any]) -> bool:
    status = _lower(_row_value(row, "status"))
    return status in {"filled", "executed", "closed", "complete", "completed"} or _order_filled_qty(row) > 0


def _is_entry(row: dict[str, Any]) -> bool:
    return _order_role(row) == "entry"


def _is_exit(row: dict[str, Any]) -> bool:
    return _is_filled(row) and _order_role(row) in {"take_profit", "stop_loss", "close"}


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
        for key in _order_key_values(
            row,
            ("unique_id", "entry_order_unique_id", "trade_group_id", "signal_id", "broker_order_id", "order_id", "ib_order_id"),
        ):
            index.setdefault(key, row)
    return index


def _find_entry_for_exit(exit_order: dict[str, Any], entry_index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for key in _order_key_values(
        exit_order,
        ("entry_order_unique_id", "linked_entry_order_unique_id", "parent_order_unique_id", "trade_group_id", "signal_id"),
    ):
        entry = entry_index.get(key)
        if entry:
            return entry
    return None


def _order_number(row: dict[str, Any] | None, *fields: str) -> float | None:
    for field in fields:
        value = _to_float(_row_value(row, field))
        if value is not None:
            return value
    return None


def _order_side(entry_order: dict[str, Any] | None, exit_order: dict[str, Any]) -> str:
    for value in (
        _row_value(entry_order, "position_side"),
        _row_value(entry_order, "direction"),
        _row_value(exit_order, "position_side"),
        _row_value(exit_order, "direction"),
    ):
        text = _lower(value)
        if text in {"long", "buy_to_open"}:
            return "long"
        if text in {"short", "sell_to_open"}:
            return "short"
    exit_side = _lower(_row_value(exit_order, "side") or _row_value(exit_order, "action"))
    if exit_side in {"sell", "sld", "s"}:
        return "long"
    if exit_side in {"buy", "bot", "b"}:
        return "short"
    return ""


def _compute_order_trade(exit_order: dict[str, Any], entry_order: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    symbol = _upper(_row_value(exit_order, "symbol") or _row_value(entry_order, "symbol"))
    direction = _order_side(entry_order, exit_order)
    quantity = _positive_float(_row_value(exit_order, "filled_qty"), _row_value(exit_order, "quantity"))
    if quantity is None:
        quantity = _positive_float(_row_value(entry_order, "filled_qty"), _row_value(entry_order, "quantity"))
    stored_net = _order_number(exit_order, "realized_net_pnl", "realized_pnl", "net_pnl")
    stored_gross = _order_number(exit_order, "realized_gross_pnl", "pnl", "gross_pnl")
    entry_qty = _positive_float(_row_value(entry_order, "filled_qty"), _row_value(entry_order, "quantity")) or quantity or 0.0
    entry_commission = abs(_order_number(entry_order, "commission", "ibkr_commission") or 0.0)
    if entry_qty and quantity and quantity < entry_qty:
        entry_commission *= quantity / entry_qty
    exit_commission = abs(_order_number(exit_order, "commission", "ibkr_commission") or 0.0)
    commission = entry_commission + exit_commission
    if stored_net is not None:
        gross = stored_gross if stored_gross is not None else stored_net + commission
        return (
            {
                "symbol": symbol,
                "direction": direction or "unknown",
                "quantity": _round_float(quantity or 0.0),
                "entry_price": _round_float(
                    _positive_float(_row_value(entry_order, "fill_price"), _row_value(entry_order, "limit_price"), _row_value(entry_order, "entry_price"))
                    or 0.0
                ),
                "exit_price": _round_float(
                    _positive_float(
                        _row_value(exit_order, "fill_price"),
                        _row_value(exit_order, "limit_price"),
                        _row_value(exit_order, "exit_price"),
                        _row_value(exit_order, "tp_price"),
                        _row_value(exit_order, "sl_price"),
                    )
                    or 0.0
                ),
                "realized_gross_pnl": _round_float(gross),
                "commission": _round_float(commission),
                "realized_net_pnl": _round_float(stored_net),
                "source": "orders",
                "order_id": _text(_row_value(exit_order, "id") or _row_value(exit_order, "order_id") or _row_value(exit_order, "unique_id")),
            },
            "",
        )
    entry_price = _positive_float(_row_value(entry_order, "fill_price"), _row_value(entry_order, "filled_price"), _row_value(entry_order, "limit_price"))
    exit_price = _positive_float(
        _row_value(exit_order, "fill_price"),
        _row_value(exit_order, "filled_price"),
        _row_value(exit_order, "limit_price"),
        _row_value(exit_order, "exit_price"),
        _row_value(exit_order, "tp_price"),
        _row_value(exit_order, "sl_price"),
    )
    if not symbol or entry_price is None or exit_price is None or quantity is None or direction not in {"long", "short"}:
        return None, "order_missing_required_count"
    gross = (entry_price - exit_price) * abs(quantity) if direction == "short" else (exit_price - entry_price) * abs(quantity)
    net = gross - commission
    reason = ""
    if not _has_field(entry_order, "commission", "ibkr_commission") or not _has_field(exit_order, "commission", "ibkr_commission"):
        reason = "order_commission_missing_count"
    return (
        {
            "symbol": symbol,
            "direction": direction,
            "quantity": _round_float(quantity),
            "entry_price": _round_float(entry_price),
            "exit_price": _round_float(exit_price),
            "realized_gross_pnl": _round_float(gross),
            "commission": _round_float(commission),
            "realized_net_pnl": _round_float(net),
            "source": "orders",
            "order_id": _text(_row_value(exit_order, "id") or _row_value(exit_order, "order_id") or _row_value(exit_order, "unique_id")),
        },
        reason,
    )


def _compute_order_trades(
    orders: list[dict[str, Any]],
    *,
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    missing = Counter()
    trades: list[dict[str, Any]] = []
    order_rows = sorted([dict(row) for row in orders if isinstance(row, dict)], key=lambda row: _order_time_ms(row))
    entry_index = _build_entry_index(order_rows)
    for row in order_rows:
        if not _is_exit(row):
            continue
        day = _order_date(row, start_date)
        if not _date_in_range(day, start_date, end_date):
            continue
        entry = _find_entry_for_exit(row, entry_index)
        if entry is None:
            missing["order_entry_missing_count"] += 1
        trade, reason = _compute_order_trade(row, entry)
        if reason:
            missing[reason] += 1
        if not trade:
            missing["order_missing_count"] += 1
            continue
        trades.append({**trade, "date": day, "exit_time_ms": _order_time_ms(row)})
    return trades, dict(missing)


def _empty_bucket() -> dict[str, Any]:
    return {
        "trade_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "flat_count": 0,
        "long_count": 0,
        "short_count": 0,
        "quantity": 0.0,
        "realized_net_pnl": 0.0,
        "net_pnl": 0.0,
        "realized_gross_pnl": 0.0,
        "commission": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
    }


def _accumulate(bucket: dict[str, Any], trade: dict[str, Any]) -> None:
    net = float(trade.get("realized_net_pnl") or 0.0)
    bucket["trade_count"] += 1
    bucket["quantity"] += float(trade.get("quantity") or 0.0)
    bucket["realized_net_pnl"] += net
    bucket["net_pnl"] += net
    bucket["realized_gross_pnl"] += float(trade.get("realized_gross_pnl") or 0.0)
    bucket["commission"] += float(trade.get("commission") or 0.0)
    direction = _lower(trade.get("direction"))
    if direction == "long":
        bucket["long_count"] += 1
    elif direction == "short":
        bucket["short_count"] += 1
    if net > EPSILON:
        bucket["win_count"] += 1
        bucket["gross_profit"] += net
    elif net < -EPSILON:
        bucket["loss_count"] += 1
        bucket["gross_loss"] += net
    else:
        bucket["flat_count"] += 1


def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    row = dict(bucket)
    for key in ("realized_net_pnl", "net_pnl", "realized_gross_pnl", "commission", "gross_profit", "gross_loss"):
        row[key] = _round_money(row.get(key) or 0.0)
    row["quantity"] = _round_float(row.get("quantity") or 0.0)
    wins = int(row.get("win_count") or 0)
    losses = int(row.get("loss_count") or 0)
    count = int(row.get("trade_count") or 0)
    row["win_rate"] = round((wins / count) * 100.0, 2) if count else 0.0
    row["profit_factor"] = (
        round(float(row["gross_profit"]) / abs(float(row["gross_loss"])), 4)
        if float(row["gross_profit"]) > 0 and float(row["gross_loss"]) < 0
        else None
    )
    row["profit_loss_ratio"] = None
    if wins and losses:
        avg_win = float(row["gross_profit"]) / wins
        avg_loss = float(row["gross_loss"]) / losses
        row["profit_loss_ratio"] = round(avg_win / abs(avg_loss), 4) if avg_win > 0 and avg_loss < 0 else None
    return row


def _aggregate_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _empty_bucket()
    by_day: dict[str, dict[str, Any]] = defaultdict(_empty_bucket)
    by_symbol: dict[str, dict[str, Any]] = defaultdict(_empty_bucket)
    by_day_symbol: dict[tuple[str, str], dict[str, Any]] = defaultdict(_empty_bucket)
    for trade in trades:
        day = _text(trade.get("date"))
        symbol = _upper(trade.get("symbol")) or "UNKNOWN"
        _accumulate(summary, trade)
        _accumulate(by_day[day], trade)
        _accumulate(by_symbol[symbol], trade)
        _accumulate(by_day_symbol[(day, symbol)], trade)

    day_symbol_rows = []
    for (day, symbol), bucket in sorted(by_day_symbol.items()):
        day_symbol_rows.append({"date": day, "symbol": symbol, **_finalize_bucket(bucket)})
    day_rows = [{"date": day, **_finalize_bucket(bucket)} for day, bucket in sorted(by_day.items())]
    symbol_rows = [{"symbol": symbol, **_finalize_bucket(bucket)} for symbol, bucket in sorted(by_symbol.items())]
    finalized_summary = _finalize_bucket(summary)
    finalized_summary.update({"days": len(by_day), "symbols": len(by_symbol)})
    return {
        "summary": finalized_summary,
        "rows": day_symbol_rows,
        "by_day": day_rows,
        "by_symbol": symbol_rows,
    }


def _warning(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _nonzero_counts(counts: dict[str, int]) -> dict[str, int]:
    return {key: int(value) for key, value in sorted((counts or {}).items()) if int(value or 0) != 0}


def _request_bool(params: dict[str, Any], *keys: str, default: bool = False) -> bool:
    for key in keys:
        if key in params and params.get(key) not in (None, ""):
            return _to_bool(params.get(key), default)
    return bool(default)


def _strict_insufficient_response(
    *,
    start_date: str,
    end_date: str,
    broker_mode: str,
    data_environment: str,
    fill_filter: str,
    order_filter: str,
    max_pages: int,
    lookback_days: int,
    fills: list[dict[str, Any]],
    fill_trades: list[dict[str, Any]],
    missing_counts: dict[str, int],
    warnings: list[dict[str, Any]],
    reason: str,
) -> tuple[dict[str, Any], int]:
    aggregate = _aggregate_trades([])
    return {
        "ok": True,
        "source": "ibkr-api",
        "pnl_source": FILLS_COLLECTION,
        "source_collection": FILLS_COLLECTION,
        "fallback_used": False,
        "strict": True,
        "data_status": "insufficient",
        "data_insufficient": True,
        "insufficient_reason": reason,
        "start_date": start_date,
        "end_date": end_date,
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "filters": {
            "fill_filter": fill_filter,
            "order_filter": order_filter,
            "max_pages": max_pages,
            "lookback_days": lookback_days,
        },
        "summary": aggregate["summary"],
        "rows": [],
        "by_day": [],
        "by_symbol": [],
        "daily_rows": [],
        "symbol_rows": [],
        "day_symbol_rows": [],
        "missing_counts": _nonzero_counts(missing_counts),
        "warnings": warnings,
        "diagnostics": {
            "fill_rows": len(fills),
            "order_rows": 0,
            "realized_trade_rows": len(fill_trades),
        },
        "trades": [],
    }, 200


def build_realized_pnl_summary_response(
    pb: Any,
    *,
    params: dict[str, Any] | None = None,
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
    time_strings: TimeStrings,
) -> tuple[dict[str, Any], int]:
    request_params = params if isinstance(params, dict) else {}
    fallback_date = _text((time_strings() or {}).get("date")) or datetime.utcnow().strftime("%Y-%m-%d")
    start_date = _date_token(
        request_params.get("start_date") or request_params.get("date") or request_params.get("market_date"),
        fallback_date,
    )
    end_date = _date_token(
        request_params.get("end_date") or request_params.get("date") or request_params.get("market_date") or start_date,
        start_date,
    )
    if end_date < start_date:
        return {
            "ok": False,
            "source": "ibkr-api",
            "error": "invalid_date_range",
            "message": "end_date must be on or after start_date",
            "start_date": start_date,
            "end_date": end_date,
        }, 400

    raw_broker_mode = request_params.get("broker_mode")
    if not _text(raw_broker_mode) and _lower(request_params.get("environment")) == "paper":
        raw_broker_mode = "paper"
    broker_mode = normalize_environment(raw_broker_mode, "paper")
    data_environment = normalize_environment(
        request_params.get("data_environment") or request_params.get("market_data_mode"),
        "live",
    )
    max_pages = _bounded_int(request_params.get("max_pages"), 100, 1, 500)
    lookback_days = _bounded_int(request_params.get("lookback_days"), 30, 0, 3650)
    strict = _request_bool(request_params, "strict", default=False) or not _request_bool(
        request_params,
        "allow_fallback",
        default=True,
    )
    end_exclusive_date = _next_date_token(end_date)
    start_ms = _parse_time_text_ms(f"{start_date} 00:00:00")
    end_ms = _parse_time_text_ms(f"{end_exclusive_date} 00:00:00")
    lookback_start = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=lookback_days)
    lookback_date = lookback_start.strftime("%Y-%m-%d")
    lookback_ms = max(0, start_ms - (lookback_days * 24 * 60 * 60 * 1000))
    broker_escaped = escape_filter_string(broker_mode)
    fill_filter = f'environment = "{broker_escaped}" && trade_time_ms >= {lookback_ms} && trade_time_ms < {end_ms}'
    order_filter = (
        f'environment = "{broker_escaped}" && '
        f'(us_time >= "{escape_filter_string(lookback_date)} 00:00:00" && '
        f'us_time < "{escape_filter_string(end_exclusive_date)} 00:00:00")'
    )
    warnings: list[dict[str, Any]] = []

    fills, fill_error = _load_records(
        pb,
        FILLS_COLLECTION,
        filter_expr=fill_filter,
        sort="trade_time_ms",
        max_pages=max_pages,
    )
    fill_trades: list[dict[str, Any]] = []
    fill_missing_counts: dict[str, int] = {}
    if fill_error:
        warnings.append(
            _warning(
                "execution_fills_unavailable",
                "ibkr_execution_fills could not be loaded; using computable orders fallback",
                error=fill_error,
            )
        )
        if strict:
            return _strict_insufficient_response(
                start_date=start_date,
                end_date=end_date,
                broker_mode=broker_mode,
                data_environment=data_environment,
                fill_filter=fill_filter,
                order_filter=order_filter,
                max_pages=max_pages,
                lookback_days=lookback_days,
                fills=[],
                fill_trades=[],
                missing_counts={"execution_fills_unavailable_count": 1},
                warnings=warnings,
                reason="execution_fills_unavailable",
            )
    elif fills:
        fill_trades, fill_missing_counts = _compute_fill_trades(
            fills,
            start_date=start_date,
            end_date=end_date,
            require_verified_commission=strict,
        )
        if fill_trades:
            aggregate = _aggregate_trades(fill_trades)
            missing_counts = _nonzero_counts(fill_missing_counts)
            if missing_counts:
                warnings.append(
                    _warning(
                        "execution_fills_missing_inputs",
                        "some execution fill rows had missing or partial realized-PnL inputs",
                        missing_counts=missing_counts,
                    )
                )
            return {
                "ok": True,
                "source": "ibkr-api",
                "pnl_source": FILLS_COLLECTION,
                "source_collection": FILLS_COLLECTION,
                "fallback_used": False,
                "strict": bool(strict),
                "data_status": "complete",
                "data_insufficient": False,
                "start_date": start_date,
                "end_date": end_date,
                "broker_mode": broker_mode,
                "data_environment": data_environment,
                "filters": {
                    "fill_filter": fill_filter,
                    "order_filter": order_filter,
                    "max_pages": max_pages,
                    "lookback_days": lookback_days,
                },
                "summary": aggregate["summary"],
                "rows": aggregate["rows"],
                "by_day": aggregate["by_day"],
                "by_symbol": aggregate["by_symbol"],
                "daily_rows": aggregate["by_day"],
                "symbol_rows": aggregate["by_symbol"],
                "day_symbol_rows": aggregate["rows"],
                "missing_counts": missing_counts,
                "warnings": warnings,
                "diagnostics": {
                    "fill_rows": len(fills),
                    "order_rows": 0,
                    "realized_trade_rows": len(fill_trades),
                },
                "trades": sorted(fill_trades, key=lambda row: (row.get("date") or "", row.get("symbol") or "", row.get("exit_time_ms") or 0))[:200],
            }, 200
        warnings.append(
            _warning(
                "execution_fills_no_complete_realized_trades" if strict else "execution_fills_no_realized_trades_using_orders",
                (
                    "ibkr_execution_fills rows were present but did not produce complete strict realized trades"
                    if strict
                    else "ibkr_execution_fills rows were present but did not produce realized trades; using computable orders fallback"
                ),
                missing_counts=_nonzero_counts(fill_missing_counts),
            )
        )
        if strict:
            return _strict_insufficient_response(
                start_date=start_date,
                end_date=end_date,
                broker_mode=broker_mode,
                data_environment=data_environment,
                fill_filter=fill_filter,
                order_filter=order_filter,
                max_pages=max_pages,
                lookback_days=lookback_days,
                fills=fills,
                fill_trades=fill_trades,
                missing_counts=fill_missing_counts,
                warnings=warnings,
                reason="execution_fills_no_complete_realized_trades",
            )
    else:
        warnings.append(
            _warning(
                "execution_fills_empty" if strict else "execution_fills_empty_using_orders",
                (
                    "ibkr_execution_fills had no rows for the broker mode; strict realized PnL is unavailable"
                    if strict
                    else "ibkr_execution_fills had no rows for the broker mode; using computable orders fallback"
                ),
            )
        )
        if strict:
            return _strict_insufficient_response(
                start_date=start_date,
                end_date=end_date,
                broker_mode=broker_mode,
                data_environment=data_environment,
                fill_filter=fill_filter,
                order_filter=order_filter,
                max_pages=max_pages,
                lookback_days=lookback_days,
                fills=[],
                fill_trades=[],
                missing_counts={"execution_fills_empty_count": 1},
                warnings=warnings,
                reason="execution_fills_empty",
            )

    orders, order_error = _load_records(
        pb,
        ORDERS_COLLECTION,
        filter_expr=order_filter,
        sort="us_time",
        max_pages=max_pages,
    )
    if order_error:
        warnings.append(
            _warning(
                "orders_unavailable",
                "orders could not be loaded for realized-PnL fallback",
                error=order_error,
            )
        )
        orders = []
    order_trades, order_missing_counts = _compute_order_trades(orders, start_date=start_date, end_date=end_date)
    aggregate = _aggregate_trades(order_trades)
    missing_counts = _nonzero_counts({**fill_missing_counts, **Counter(order_missing_counts)})
    if order_error or not order_trades:
        warnings.append(
            _warning(
                "orders_fallback_empty",
                "computable orders fallback produced no realized trades",
            )
        )
    if _nonzero_counts(order_missing_counts):
        warnings.append(
            _warning(
                "orders_missing_inputs",
                "some order rows had missing or partial realized-PnL inputs",
                missing_counts=_nonzero_counts(order_missing_counts),
            )
        )

    return {
        "ok": True,
        "source": "ibkr-api",
        "pnl_source": "orders",
        "source_collection": ORDERS_COLLECTION,
        "fallback_used": True,
        "strict": False,
        "data_status": "fallback",
        "data_insufficient": False,
        "start_date": start_date,
        "end_date": end_date,
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "filters": {
            "fill_filter": fill_filter,
            "order_filter": order_filter,
            "max_pages": max_pages,
            "lookback_days": lookback_days,
        },
        "summary": aggregate["summary"],
        "rows": aggregate["rows"],
        "by_day": aggregate["by_day"],
        "by_symbol": aggregate["by_symbol"],
        "daily_rows": aggregate["by_day"],
        "symbol_rows": aggregate["by_symbol"],
        "day_symbol_rows": aggregate["rows"],
        "missing_counts": missing_counts,
        "warnings": warnings,
        "diagnostics": {
            "fill_rows": len(fills),
            "order_rows": len(orders),
            "realized_trade_rows": len(order_trades),
        },
        "trades": sorted(order_trades, key=lambda row: (row.get("date") or "", row.get("symbol") or "", row.get("exit_time_ms") or 0))[:200],
    }, 200


__all__ = ["FILLS_COLLECTION", "ORDERS_COLLECTION", "build_realized_pnl_summary_response"]
