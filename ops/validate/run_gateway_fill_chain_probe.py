#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_paper_commission_probe as fee_probe  # noqa: E402


ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")

CONFIRM_TEXT = "PAPER_GATEWAY_FILL_CHAIN_PROBE"
DEFAULT_ARTIFACT_ROOT = Path("artifacts/validation/gateway_fill_chain_probe")
DEFAULT_SYMBOLS = os.environ.get("IBKR_GATEWAY_FILL_CHAIN_SYMBOLS", "TSLA")


class FillChainProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeIds:
    signal_id: str
    trade_group_id: str
    client_order_id: str


@dataclass(frozen=True)
class PriceContext:
    symbol: str
    bid: float = 0.0
    ask: float = 0.0
    last_price: float = 0.0
    reference_price: float = 0.0
    reference_source: str = ""
    entry_reference_price: float = 0.0
    entry_reference_source: str = ""
    entry_price: float = 0.0
    take_profit_price: float = 0.0
    stop_loss_price: float = 0.0
    conid: int = 0
    quote_source: str = ""
    fallback_without_bid_ask: bool = False
    raw_quote: dict[str, Any] | None = None
    reference_row: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "last_price": self.last_price,
            "reference_price": self.reference_price,
            "reference_source": self.reference_source,
            "entry_reference_price": self.entry_reference_price,
            "entry_reference_source": self.entry_reference_source,
            "entry_price": self.entry_price,
            "take_profit_price": self.take_profit_price,
            "stop_loss_price": self.stop_loss_price,
            "conid": self.conid,
            "quote_source": self.quote_source,
            "fallback_without_bid_ask": self.fallback_without_bid_ask,
            "raw_quote": self.raw_quote or {},
            "reference_row": self.reference_row or {},
        }


@dataclass(frozen=True)
class FillChainPlan:
    symbol: str
    direction: str
    quantity: int
    target_notional: float
    requested_exposure: float
    price: PriceContext
    ids: ProbeIds
    outside_rth: bool
    tif: str

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "quantity": self.quantity,
            "target_notional": self.target_notional,
            "requested_exposure": self.requested_exposure,
            "signal_id": self.ids.signal_id,
            "trade_group_id": self.ids.trade_group_id,
            "client_order_id": self.ids.client_order_id,
            "outside_rth": self.outside_rth,
            "tif": self.tif,
            "price": self.price.summary(),
        }


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def safe_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:
        return float(default)
    return float(number)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def round_price(value: float) -> float:
    return max(0.01, round(float(value or 0.0), 2))


def pct_to_ratio(value: Any) -> float:
    return max(0.0, safe_float(value, 0.0)) / 100.0


def split_symbols(raw: Any) -> list[str]:
    symbols: list[str] = []
    if isinstance(raw, (list, tuple, set)):
        pieces: list[Any] = list(raw)
    else:
        pieces = safe_text(raw).replace(";", ",").split(",")
    for piece in pieces:
        symbol = fee_probe.normalize_symbol(piece)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def normalize_direction(value: Any) -> str:
    direction = safe_text(value).lower()
    if direction not in {"long", "short"}:
        raise FillChainProbeError("direction must be long or short")
    return direction


def _artifact_dir(args: argparse.Namespace) -> Path:
    return Path(args.artifact_root) / safe_text(args.run_id)


def finalize_artifact(args: argparse.Namespace, payload: dict[str, Any]) -> Path:
    root = _artifact_dir(args)
    root.mkdir(parents=True, exist_ok=True)
    summary_path = root / "summary.json"
    tmp_path = summary_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp_path.replace(summary_path)
    return summary_path


def account_snapshot_client(args: argparse.Namespace, *, timeout_sec: float | None = None) -> tuple[fee_probe.ApiClient, str]:
    base_url = fee_probe.to_text(getattr(args, "account_base_url", "")) or args.api_base_url
    snapshot_args = argparse.Namespace(
        account_base_url=fee_probe.to_text(getattr(args, "account_base_url", "")),
        account_snapshot_path=fee_probe.to_text(getattr(args, "account_snapshot_path", "")),
    )
    timeout = float(timeout_sec if timeout_sec is not None else getattr(args, "http_timeout_sec", 30.0))
    return fee_probe.ApiClient(base_url, timeout=timeout), fee_probe.account_snapshot_path(snapshot_args)


def account_snapshot_params(*, orders_fast: bool = False) -> dict[str, Any]:
    params = dict(fee_probe.account_params())
    params["cache"] = "0"
    if orders_fast:
        params.update(
            {
                "include_pnl": "0",
                "orders_fast": "1",
                "snapshot_profile": "orders_fast",
                "open_orders_only": "1",
            }
        )
    return params


def get_snapshot(
    args: argparse.Namespace,
    *,
    timeout_sec: float | None = None,
    orders_fast: bool = False,
) -> dict[str, Any]:
    client, path = account_snapshot_client(args, timeout_sec=timeout_sec)
    return client.get(path or fee_probe.DEFAULT_ACCOUNT_SNAPSHOT_PATH, account_snapshot_params(orders_fast=orders_fast))


def get_json_api(args: argparse.Namespace, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(getattr(args, "http_timeout_sec", 30.0)))
    return client.get(path, params or {})


def fetch_quotes(args: argparse.Namespace, symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    payload = get_json_api(
        args,
        "/api/custom/ibkr/quotes",
        {
            "symbols": ",".join(symbols),
            "environment": "paper",
            "broker_mode": "paper",
            "market_data_mode": safe_text(getattr(args, "market_data_mode", "")) or "live",
            "snapshot": "1" if bool(getattr(args, "quote_snapshot", True)) else "",
            "snapshot_timeout": float(getattr(args, "quote_snapshot_timeout_sec", 3.0) or 3.0),
        },
    )
    if int(payload.get("_http_status") or 0) >= 400 or payload.get("ok") is False:
        raise FillChainProbeError(f"quotes_failed:{compact_json(payload)[:1000]}")
    out: dict[str, dict[str, Any]] = {}
    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = fee_probe.normalize_symbol(item.get("symbol"))
            if symbol:
                out[symbol] = dict(item)
    elif isinstance(payload.get("quotes"), dict):
        for symbol, quote in payload["quotes"].items():
            if isinstance(quote, dict):
                out[fee_probe.normalize_symbol(symbol)] = dict(quote)
    return out


def quote_price(quote: dict[str, Any] | None, *keys: str) -> float:
    if not isinstance(quote, dict):
        return 0.0
    for key in keys:
        value = safe_float(quote.get(key), 0.0)
        if value > 0:
            return value
    return 0.0


def fetch_latest_reference_context(args: argparse.Namespace, symbol: str) -> dict[str, Any]:
    explicit = safe_float(getattr(args, "reference_price", 0.0), 0.0)
    if explicit > 0:
        return {
            "symbol": fee_probe.normalize_symbol(symbol),
            "close": explicit,
            "source": "cli_reference_price",
            "environment": "paper",
        }
    script = Path(safe_text(getattr(args, "sqlite_script", "")))
    if not script.is_file():
        raise FillChainProbeError(f"reference_price_unavailable_no_sqlite_script:{script}")
    safe_symbol = fee_probe.sqlite_literal(fee_probe.normalize_symbol(symbol))
    rows = fee_probe.run_remote_sql(
        args,
        f"""
        select symbol, close, environment, interval, us_time, bar_time_ms, extra
        from ibkr_bars
        where symbol = {safe_symbol}
          and close > 0
          and environment in ('paper', 'live')
        order by bar_time_ms desc
        limit 1;
        """,
    )
    if not rows:
        raise FillChainProbeError(f"reference_price_unavailable:{symbol}")
    row = dict(rows[0])
    close = safe_float(row.get("close"), 0.0)
    if close <= 0:
        raise FillChainProbeError(f"reference_price_invalid:{compact_json(row)[:500]}")
    extra = row.get("extra")
    if isinstance(extra, str) and extra.strip().startswith("{"):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    if isinstance(extra, dict):
        for key in ("conid", "conidEx"):
            if row.get(key) in (None, "") and extra.get(key) not in (None, ""):
                row[key] = extra.get(key)
    row.setdefault("source", "ibkr_bars_close")
    return row


def compute_protection_prices(fill_price: float, direction: str, *, tp_pct: float, sl_pct: float) -> tuple[float, float]:
    price = safe_float(fill_price, 0.0)
    if price <= 0:
        raise FillChainProbeError("fill_price_required")
    tp_ratio = pct_to_ratio(tp_pct)
    sl_ratio = pct_to_ratio(sl_pct)
    if tp_ratio <= 0 or sl_ratio <= 0:
        raise FillChainProbeError("tp_pct and sl_pct must be positive")
    direction = normalize_direction(direction)
    if direction == "long":
        take_profit = round_price(price * (1.0 + tp_ratio))
        stop_loss = round_price(price * (1.0 - sl_ratio))
        if stop_loss >= price:
            stop_loss = round_price(price - 0.01)
        if take_profit <= price:
            take_profit = round_price(price + 0.01)
        return take_profit, stop_loss
    take_profit = round_price(price * (1.0 - tp_ratio))
    stop_loss = round_price(price * (1.0 + sl_ratio))
    if take_profit >= price:
        take_profit = round_price(price - 0.01)
    if stop_loss <= price:
        stop_loss = round_price(price + 0.01)
    return take_profit, stop_loss


def build_price_context(
    *,
    symbol: str,
    direction: str,
    quote: dict[str, Any] | None,
    reference: dict[str, Any] | None,
    entry_buffer_pct: float,
    tp_pct: float,
    sl_pct: float,
) -> PriceContext:
    direction = normalize_direction(direction)
    quote = dict(quote or {})
    reference = dict(reference or {})
    bid = quote_price(quote, "bid", "bid_price", "84")
    ask = quote_price(quote, "ask", "ask_price", "86")
    last = quote_price(quote, "last_price", "last", "close", "mark", "market_price")
    reference_close = quote_price(reference, "close", "last_price", "last", "market_price")
    if reference_close <= 0:
        reference_close = last
    conid = safe_int(quote.get("conid") or quote.get("conidEx") or reference.get("conid") or reference.get("conidEx"), 0)
    buffer_ratio = pct_to_ratio(entry_buffer_pct)
    if direction == "long" and ask > 0:
        entry_reference = ask
        entry_source = "ask"
        entry = round_price(ask * (1.0 + buffer_ratio))
        fallback = False
    elif direction == "short" and bid > 0:
        entry_reference = bid
        entry_source = "bid"
        entry = round_price(bid * (1.0 - buffer_ratio))
        fallback = False
    elif reference_close > 0:
        entry_reference = reference_close
        entry_source = "reference_close_fallback_no_bid_ask"
        if direction == "long":
            entry = round_price(reference_close * (1.0 + buffer_ratio))
        else:
            entry = round_price(reference_close * (1.0 - buffer_ratio))
        fallback = True
    else:
        raise FillChainProbeError(f"entry_reference_unavailable:{symbol}")

    take_profit, stop_loss = compute_protection_prices(entry, direction, tp_pct=tp_pct, sl_pct=sl_pct)
    return PriceContext(
        symbol=fee_probe.normalize_symbol(symbol),
        bid=round_price(bid) if bid > 0 else 0.0,
        ask=round_price(ask) if ask > 0 else 0.0,
        last_price=round_price(last) if last > 0 else 0.0,
        reference_price=round_price(reference_close) if reference_close > 0 else 0.0,
        reference_source=safe_text(reference.get("source")) or ("quote_last" if last > 0 else ""),
        entry_reference_price=round_price(entry_reference),
        entry_reference_source=entry_source,
        entry_price=entry,
        take_profit_price=take_profit,
        stop_loss_price=stop_loss,
        conid=conid,
        quote_source=safe_text(quote.get("source")) or ("ibkr_quotes" if quote else "none"),
        fallback_without_bid_ask=fallback,
        raw_quote=quote,
        reference_row=reference,
    )


def resolve_price_context(args: argparse.Namespace, symbol: str) -> PriceContext:
    quotes: dict[str, dict[str, Any]] = {}
    quote_error = ""
    try:
        quotes = fetch_quotes(args, [symbol])
    except Exception as exc:
        quote_error = str(exc)
    quote = dict(quotes.get(fee_probe.normalize_symbol(symbol)) or {})
    reference: dict[str, Any] = {}
    try:
        reference = fetch_latest_reference_context(args, symbol)
    except Exception as exc:
        if not quote:
            raise
        reference = {"source": "unavailable", "error": str(exc)}
    if quote_error:
        reference.setdefault("quote_error", quote_error)
    price = build_price_context(
        symbol=symbol,
        direction=args.direction,
        quote=quote,
        reference=reference,
        entry_buffer_pct=float(args.entry_buffer_pct),
        tp_pct=float(args.tp_pct),
        sl_pct=float(args.sl_pct),
    )
    max_age = float(getattr(args, "max_quote_age_sec", 0.0) or 0.0)
    quote_age = safe_float((price.raw_quote or {}).get("quote_age_s"), -1.0)
    quote_fallback = bool((price.raw_quote or {}).get("quote_fallback"))
    fresh_age = quote_age < 0 or max_age <= 0 or quote_age <= max_age
    has_bid_ask = price.bid > 0 and price.ask > 0 and price.ask >= price.bid
    if (
        not bool(getattr(args, "dry_run", True))
        and not bool(getattr(args, "allow_reference_fallback", False))
        and (price.fallback_without_bid_ask or quote_fallback or not has_bid_ask or not fresh_age)
    ):
        raise FillChainProbeError(
            "fresh_bid_ask_required_for_execute:"
            f"symbol={symbol}:bid={price.bid}:ask={price.ask}:quote_age_s={quote_age}:quote_fallback={quote_fallback}"
        )
    return price


def quantity_for_target(entry_price: float, *, quantity: int, target_notional: float) -> tuple[int, float]:
    target = safe_float(target_notional, 0.0)
    if target > 0:
        if entry_price <= 0:
            raise FillChainProbeError("entry_price_required_for_target_notional")
        qty = max(1, int(math.ceil(target / float(entry_price))))
    else:
        qty = max(1, int(quantity or 1))
    return qty, round(float(entry_price) * qty, 4)


def _sanitize_id_part(value: Any, *, max_len: int = 48) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", safe_text(value)).strip("_")
    if not text:
        text = "run"
    return text[-max_len:]


def build_probe_ids(run_id: str, symbol: str, index: int) -> ProbeIds:
    token = uuid.uuid4().hex[:8]
    base = f"GWFC_{_sanitize_id_part(run_id)}_{index:02d}_{fee_probe.normalize_symbol(symbol)}_{token}"
    base = base[:96]
    return ProbeIds(
        signal_id=f"{base}_SIG",
        trade_group_id=f"{base}_GRP",
        client_order_id=f"{base}_COID",
    )


def build_place_payload(plan: FillChainPlan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": plan.symbol,
        "direction": plan.direction,
        "quantity": int(plan.quantity),
        "order_type": "LMT",
        "entry_order_type": "LMT",
        "entry_price": plan.price.entry_price,
        "take_profit_price": plan.price.take_profit_price,
        "stop_loss_price": plan.price.stop_loss_price,
        "signal_id": plan.ids.signal_id,
        "trade_group_id": plan.ids.trade_group_id,
        "client_order_id": plan.ids.client_order_id,
        "order_ref": plan.ids.client_order_id,
        "outside_rth": bool(plan.outside_rth),
        "outsideRth": bool(plan.outside_rth),
        "tif": plan.tif,
        "include_snapshot": False,
        "fast_ack": True,
        "extra": {
            "source": "gateway_fill_chain_probe",
            "signal_id": plan.ids.signal_id,
            "trade_group_id": plan.ids.trade_group_id,
            "client_order_id": plan.ids.client_order_id,
            "outside_rth": bool(plan.outside_rth),
            "tif": plan.tif,
            "entry_reference_source": plan.price.entry_reference_source,
            "fallback_without_bid_ask": bool(plan.price.fallback_without_bid_ask),
        },
        "order_extra": {
            "source": "gateway_fill_chain_probe",
            "signal_id": plan.ids.signal_id,
            "trade_group_id": plan.ids.trade_group_id,
            "client_order_id": plan.ids.client_order_id,
            "outside_rth": bool(plan.outside_rth),
            "tif": plan.tif,
            "entry_reference_source": plan.price.entry_reference_source,
            "fallback_without_bid_ask": bool(plan.price.fallback_without_bid_ask),
        },
    }
    if int(plan.price.conid or 0) > 0:
        payload["conid"] = int(plan.price.conid)
    return payload


def response_action_ok(response: dict[str, Any] | None) -> bool:
    if not isinstance(response, dict) or response.get("ok") is False:
        return False
    result = response.get("result")
    if isinstance(result, dict) and result.get("ok") is False:
        return False
    return bool(response.get("ok") or (isinstance(result, dict) and result.get("ok")))


def nested_result(response: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    result = response.get("result")
    return result if isinstance(result, dict) else {}


def place_bracket_order(args: argparse.Namespace, plan: FillChainPlan) -> dict[str, Any]:
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))
    payload = build_place_payload(plan)
    started = time.perf_counter()
    try:
        response = client.post("/api/custom/ibkr/orders/place", payload, fee_probe.action_params())
        ok = response_action_ok(response)
        error = response.get("error") or nested_result(response).get("error") or ""
    except Exception as exc:
        response = {}
        ok = False
        error = str(exc)
    return {
        "ok": ok,
        "symbol": plan.symbol,
        "payload": payload,
        "response": response,
        "order_ids": fee_probe.extract_order_ids(response),
        "elapsed_s": round(time.perf_counter() - started, 3),
        "error": error,
    }


def iter_snapshot_orders(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("live_open_orders", "orders", "order_history"):
        for item in snapshot.get(key) or []:
            if not isinstance(item, dict):
                continue
            identity = fee_probe.order_id(item) or compact_json(item)[:200]
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(dict(item))
    return rows


def _deep_values(row: dict[str, Any], *keys: str) -> list[Any]:
    values: list[Any] = []
    for key in keys:
        if key in row:
            values.append(row.get(key))
    for container_key in ("raw", "extra", "order_extra", "pb_context", "result"):
        nested = row.get(container_key)
        if isinstance(nested, str) and nested.strip().startswith("{"):
            try:
                nested = json.loads(nested)
            except Exception:
                nested = {}
        if isinstance(nested, dict):
            for key in keys:
                if key in nested:
                    values.append(nested.get(key))
    return values


def _row_has_identifier(row: dict[str, Any], identifiers: set[str]) -> bool:
    if not identifiers:
        return False
    for value in _deep_values(
        row,
        "order_id",
        "orderId",
        "id",
        "broker_order_id",
        "client_order_id",
        "cOID",
        "coid",
        "order_ref",
        "orderRef",
        "trade_group_id",
        "bracket_group",
        "signal_id",
        "entry_order_unique_id",
        "parent_order_unique_id",
        "oca_group",
        "ocaGroup",
    ):
        text = safe_text(value)
        if not text:
            continue
        for identifier in identifiers:
            if text == identifier or identifier in text or text in identifier:
                return True
    return False


def _order_matches_probe(
    row: dict[str, Any],
    *,
    symbol: str,
    order_ids: list[str],
    ids: ProbeIds,
) -> bool:
    row_symbol = fee_probe.order_symbol(row)
    normalized_symbol = fee_probe.normalize_symbol(symbol)
    symbol_matches = not row_symbol or row_symbol == normalized_symbol
    identifiers = {safe_text(item) for item in order_ids if safe_text(item)}
    identifiers.update({ids.trade_group_id, ids.signal_id, ids.client_order_id})
    return symbol_matches and _row_has_identifier(row, identifiers)


def _first_numeric(row: dict[str, Any], *keys: str) -> float:
    for value in _deep_values(row, *keys):
        number = safe_float(value, 0.0)
        if number > 0:
            return number
    return 0.0


def _filled_qty_from_order(row: dict[str, Any], requested_quantity: int = 0) -> float:
    filled = _first_numeric(
        row,
        "filled_qty",
        "filledQuantity",
        "filled_quantity",
        "filled",
        "cumQty",
        "cum_qty",
        "shares",
    )
    if filled > 0:
        return filled
    total = _first_numeric(row, "quantity", "totalSize", "total_size", "qty")
    remaining = _first_numeric(row, "remaining", "remainingQuantity", "remaining_qty")
    if total > 0 and remaining >= 0 and total > remaining:
        return total - remaining
    status = fee_probe.order_status(row)
    if status in {"FILLED", "EXECUTED"} and requested_quantity > 0:
        return float(requested_quantity)
    return 0.0


def _avg_fill_price_from_order(row: dict[str, Any]) -> float:
    return _first_numeric(
        row,
        "avg_fill_price",
        "avgFillPrice",
        "average_fill_price",
        "avg_price",
        "avgPrice",
        "averagePrice",
    )


def _position_symbol(position: dict[str, Any]) -> str:
    return fee_probe.normalize_symbol(
        position.get("symbol")
        or position.get("ticker")
        or position.get("contractDesc")
        or position.get("contract_description")
        or ""
    )


def _position_quantity(position: dict[str, Any]) -> float:
    return safe_float(
        position.get("quantity")
        or position.get("position")
        or position.get("qty")
        or position.get("position_qty"),
        0.0,
    )


def _position_avg_price(position: dict[str, Any]) -> float:
    return _first_numeric(
        position,
        "avg_fill_price",
        "avgFillPrice",
        "avg_price",
        "avgPrice",
        "avg_cost",
        "avgCost",
        "average_cost",
        "averageCost",
    )


def fill_status_from_snapshot(
    snapshot: dict[str, Any],
    *,
    symbol: str,
    entry_order_ids: list[str],
    ids: ProbeIds,
    requested_quantity: int,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for row in iter_snapshot_orders(snapshot):
        if not _order_matches_probe(row, symbol=symbol, order_ids=entry_order_ids, ids=ids):
            continue
        qty = _filled_qty_from_order(row, requested_quantity=requested_quantity)
        avg = _avg_fill_price_from_order(row)
        if qty > 0 or fee_probe.order_status(row) in {"FILLED", "EXECUTED"}:
            matches.append(
                {
                    "order_id": fee_probe.order_id(row),
                    "status": fee_probe.order_status(row),
                    "filled_qty": qty,
                    "avg_fill_price": avg,
                    "row": row,
                }
            )
    best = next((item for item in matches if item["filled_qty"] > 0 and item["avg_fill_price"] > 0), None)
    if best:
        return {
            "ok": True,
            "filled_qty": best["filled_qty"],
            "avg_fill_price": best["avg_fill_price"],
            "source": "account_snapshot_order",
            "order": best,
        }

    normalized_symbol = fee_probe.normalize_symbol(symbol)
    for position in snapshot.get("positions") or []:
        if not isinstance(position, dict) or _position_symbol(position) != normalized_symbol:
            continue
        qty = abs(_position_quantity(position))
        avg = _position_avg_price(position)
        if qty > 0 and avg > 0:
            return {
                "ok": True,
                "filled_qty": qty,
                "avg_fill_price": avg,
                "source": "account_snapshot_position",
                "position": dict(position),
                "orders": matches,
            }
    return {"ok": False, "filled_qty": 0.0, "avg_fill_price": 0.0, "source": "account_snapshot", "orders": matches}


def fill_status_from_execution_fills(
    args: argparse.Namespace,
    *,
    symbol: str,
    started_ms: int,
    entry_order_ids: list[str],
) -> dict[str, Any]:
    script = Path(safe_text(getattr(args, "sqlite_script", "")))
    if not script.is_file():
        return {"ok": False, "source": "execution_fills", "error": f"sqlite_script_missing:{script}"}
    try:
        fills = fee_probe.fetch_execution_fills(args, symbol, started_ms, entry_order_ids)
    except Exception as exc:
        return {"ok": False, "source": "execution_fills", "error": str(exc)}
    total_qty = 0.0
    total_value = 0.0
    used: list[dict[str, Any]] = []
    entry_id_set = {safe_text(item) for item in entry_order_ids if safe_text(item)}
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        if entry_id_set and safe_text(fill.get("order_id")) not in entry_id_set:
            continue
        qty = abs(safe_float(fill.get("shares"), 0.0))
        price = safe_float(fill.get("price"), 0.0)
        if qty <= 0 or price <= 0:
            continue
        total_qty += qty
        total_value += qty * price
        used.append(dict(fill))
    if total_qty <= 0 or total_value <= 0:
        return {"ok": False, "source": "execution_fills", "fills": fills}
    return {
        "ok": True,
        "filled_qty": total_qty,
        "avg_fill_price": round(total_value / total_qty, 6),
        "source": "execution_fills",
        "fills": used,
    }


def wait_for_entry_fill(
    args: argparse.Namespace,
    plan: FillChainPlan,
    place_result: dict[str, Any],
    *,
    started_ms: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    timeout = max(0.0, float(args.timeout_sec))
    poll = max(0.05, float(args.poll_interval_sec))
    deadline = time.monotonic() + timeout
    order_ids = [safe_text(item) for item in (place_result.get("order_ids") or []) if safe_text(item)]
    entry_order_ids = order_ids[:1] or [safe_text(nested_result(place_result.get("response")).get("entry_order_id"))]
    entry_order_ids = [item for item in entry_order_ids if item]
    attempts = 0
    last_snapshot: dict[str, Any] = {}
    last_fill: dict[str, Any] = {}
    while True:
        attempts += 1
        try:
            last_snapshot = get_snapshot(args, timeout_sec=float(args.http_timeout_sec), orders_fast=False)
            fee_probe.assert_paper_snapshot(last_snapshot)
            last_fill = fill_status_from_snapshot(
                last_snapshot,
                symbol=plan.symbol,
                entry_order_ids=entry_order_ids,
                ids=plan.ids,
                requested_quantity=plan.quantity,
            )
            if last_fill.get("ok"):
                return {
                    "ok": True,
                    "attempts": attempts,
                    "elapsed_s": round(time.perf_counter() - started, 3),
                    "entry_latency_s": round(time.perf_counter() - started, 3),
                    "entry_order_ids": entry_order_ids,
                    "fill": last_fill,
                    "snapshot": last_snapshot,
                }
        except Exception as exc:
            last_fill = {"ok": False, "error": str(exc), "source": "account_snapshot"}

        sql_fill = fill_status_from_execution_fills(
            args,
            symbol=plan.symbol,
            started_ms=started_ms,
            entry_order_ids=entry_order_ids,
        )
        if sql_fill.get("ok"):
            return {
                "ok": True,
                "attempts": attempts,
                "elapsed_s": round(time.perf_counter() - started, 3),
                "entry_latency_s": round(time.perf_counter() - started, 3),
                "entry_order_ids": entry_order_ids,
                "fill": sql_fill,
                "snapshot": last_snapshot,
            }
        if time.monotonic() >= deadline:
            break
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
    return {
        "ok": False,
        "error": "entry_fill_timeout",
        "attempts": attempts,
        "elapsed_s": round(time.perf_counter() - started, 3),
        "entry_latency_s": None,
        "entry_order_ids": entry_order_ids,
        "last_fill": last_fill,
        "last_snapshot": last_snapshot,
    }


def protection_ids_from_response(place_response: dict[str, Any]) -> dict[str, str]:
    order_ids = fee_probe.extract_order_ids(place_response)
    result = nested_result(place_response)
    tp = ""
    sl = ""
    for key in ("take_profit_order_id", "take_profit_id", "tp_order_id"):
        tp = safe_text(result.get(key) or place_response.get(key))
        if tp:
            break
    for key in ("stop_loss_order_id", "stop_order_id", "sl_order_id"):
        sl = safe_text(result.get(key) or place_response.get(key))
        if sl:
            break
    if not tp and len(order_ids) >= 2:
        tp = order_ids[1]
    if not sl and len(order_ids) >= 3:
        sl = order_ids[2]
    return {"take_profit": tp, "stop_loss": sl}


def _order_role(row: dict[str, Any]) -> str:
    role = safe_text(
        row.get("role")
        or row.get("leg_role")
        or row.get("order_family_type")
        or row.get("family")
        or row.get("order_role")
    ).lower()
    if role in {"tp", "takeprofit"}:
        return "take_profit"
    if role in {"sl", "stop", "stoploss"}:
        return "stop_loss"
    if role:
        return role
    order_type = safe_text(row.get("order_type") or row.get("orderType") or row.get("orderDesc")).upper()
    parent = safe_text(row.get("parent_id") or row.get("parentId") or row.get("parent_order_id"))
    if parent and ("STP" in order_type or "STOP" in order_type):
        return "stop_loss"
    if parent and ("LMT" in order_type or "LIMIT" in order_type):
        return "take_profit"
    return "entry" if not parent else "child"


def protection_ids_from_snapshot(snapshot: dict[str, Any], plan: FillChainPlan, place_response: dict[str, Any]) -> dict[str, str]:
    resolved = protection_ids_from_response(place_response)
    if resolved.get("take_profit") and resolved.get("stop_loss"):
        return resolved
    all_order_ids = fee_probe.extract_order_ids(place_response)
    for row in iter_snapshot_orders(snapshot):
        if not _order_matches_probe(row, symbol=plan.symbol, order_ids=all_order_ids, ids=plan.ids):
            continue
        role = _order_role(row)
        oid = fee_probe.order_id(row)
        if role == "take_profit" and oid and not resolved.get("take_profit"):
            resolved["take_profit"] = oid
        if role == "stop_loss" and oid and not resolved.get("stop_loss"):
            resolved["stop_loss"] = oid
    return resolved


def modify_one_protection(
    args: argparse.Namespace,
    *,
    symbol: str,
    order_id: str,
    family: str,
    price: float,
    plan: FillChainPlan,
) -> dict[str, Any]:
    payload = {
        "order_id": order_id,
        "symbol": symbol,
        "price": price,
        "order_family_type": family,
        "source": f"gateway_fill_chain_probe_reprice_{family}",
        "include_snapshot": False,
        "tif": plan.tif,
        "trade_group_id": plan.ids.trade_group_id,
        "signal_id": plan.ids.signal_id,
    }
    started = time.perf_counter()
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))
    try:
        response = client.post("/api/custom/ibkr/orders/modify", payload, fee_probe.action_params())
        ok = response_action_ok(response)
        error = response.get("error") or nested_result(response).get("error") or ""
    except Exception as exc:
        response = {}
        ok = False
        error = str(exc)
    return {
        "ok": ok,
        "family": family,
        "order_id": order_id,
        "expected_price": price,
        "payload": payload,
        "response": response,
        "elapsed_s": round(time.perf_counter() - started, 3),
        "error": error,
    }


def _order_price_for_family(row: dict[str, Any], family: str) -> float:
    if family == "stop_loss":
        price = _first_numeric(row, "auxPrice", "aux_price", "stop_price", "trigger_price", "price")
        if price > 0:
            return price
    return _first_numeric(row, "price", "lmtPrice", "lmt_price", "limit_price", "auxPrice", "aux_price")


def verify_protection_prices(
    snapshot: dict[str, Any],
    *,
    tp_order_id: str,
    sl_order_id: str,
    expected_tp: float,
    expected_sl: float,
    tolerance: float = 0.011,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    wanted = {
        "take_profit": (safe_text(tp_order_id), float(expected_tp)),
        "stop_loss": (safe_text(sl_order_id), float(expected_sl)),
    }
    for family, (order_id, expected) in wanted.items():
        row = next((item for item in iter_snapshot_orders(snapshot) if fee_probe.order_id(item) == order_id), {})
        observed = _order_price_for_family(row, family) if row else 0.0
        checks.append(
            {
                "family": family,
                "order_id": order_id,
                "expected_price": round_price(expected),
                "observed_price": round_price(observed) if observed > 0 else 0.0,
                "ok": bool(row) and observed > 0 and abs(observed - expected) <= tolerance,
                "row_found": bool(row),
            }
        )
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


def modify_protection_orders(
    args: argparse.Namespace,
    plan: FillChainPlan,
    place_result: dict[str, Any],
    *,
    actual_fill_price: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    expected_tp, expected_sl = compute_protection_prices(
        actual_fill_price,
        plan.direction,
        tp_pct=float(args.tp_pct),
        sl_pct=float(args.sl_pct),
    )
    try:
        snapshot_before = get_snapshot(args, timeout_sec=float(args.http_timeout_sec), orders_fast=True)
    except Exception:
        snapshot_before = {}
    protection_ids = protection_ids_from_snapshot(snapshot_before, plan, place_result)
    tp_order_id = safe_text(protection_ids.get("take_profit"))
    sl_order_id = safe_text(protection_ids.get("stop_loss"))
    if not tp_order_id or not sl_order_id:
        return {
            "ok": False,
            "error": "protection_order_ids_unavailable",
            "elapsed_s": round(time.perf_counter() - started, 3),
            "modify_latency_s": None,
            "expected_new_tp": expected_tp,
            "expected_new_sl": expected_sl,
            "protection_order_ids": protection_ids,
            "snapshot_before": snapshot_before,
            "results": [],
        }
    results = [
        modify_one_protection(
            args,
            symbol=plan.symbol,
            order_id=tp_order_id,
            family="take_profit",
            price=expected_tp,
            plan=plan,
        ),
        modify_one_protection(
            args,
            symbol=plan.symbol,
            order_id=sl_order_id,
            family="stop_loss",
            price=expected_sl,
            plan=plan,
        ),
    ]
    try:
        snapshot_after = get_snapshot(args, timeout_sec=float(args.http_timeout_sec), orders_fast=False)
    except Exception as exc:
        snapshot_after = {"ok": False, "error": str(exc)}
    verification = verify_protection_prices(
        snapshot_after,
        tp_order_id=tp_order_id,
        sl_order_id=sl_order_id,
        expected_tp=expected_tp,
        expected_sl=expected_sl,
    )
    elapsed = round(time.perf_counter() - started, 3)
    return {
        "ok": all(item.get("ok") for item in results) and bool(verification.get("ok")),
        "elapsed_s": elapsed,
        "modify_latency_s": elapsed,
        "actual_fill_price": actual_fill_price,
        "expected_new_tp": expected_tp,
        "expected_new_sl": expected_sl,
        "protection_order_ids": protection_ids,
        "results": results,
        "verification": verification,
        "snapshot_before": snapshot_before,
        "snapshot_after": snapshot_after,
    }


def cleanup_symbol(
    args: argparse.Namespace,
    plan: FillChainPlan,
    place_response: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    client = fee_probe.ApiClient(args.api_base_url, timeout=float(args.http_timeout_sec))
    snapshot_client, snapshot_path = account_snapshot_client(args, timeout_sec=float(args.http_timeout_sec))
    snapshot_fn = lambda: fee_probe.get_account_snapshot(snapshot_client, snapshot_path)
    cleanup: dict[str, Any] = {
        "attempted": True,
        "reason": reason,
        "close_order_type": "marketable_limit",
        "flat": False,
        "close": {},
        "cancel_results": [],
    }
    try:
        snapshot = snapshot_fn()
        if fee_probe.has_nonzero_position(snapshot, plan.symbol):
            close_context = build_close_limit_context(args, plan)
            close_payload = build_marketable_limit_close_payload(
                args,
                plan,
                snapshot=snapshot,
                place_response=place_response,
                close_context=close_context,
                reason=reason,
            )
            cleanup["close_limit_context"] = close_context
            cleanup["close_payload"] = close_payload
            cleanup["close"] = client.post("/api/custom/ibkr/positions/close", close_payload, fee_probe.action_params())
            try:
                cleanup["final_snapshot"] = fee_probe.wait_for(
                    f"{plan.symbol}_flat_cleanup",
                    snapshot_fn,
                    lambda value: fee_probe.is_flat_for_symbol(value, plan.symbol),
                    timeout_s=float(args.cleanup_timeout_sec),
                    interval_s=float(args.poll_interval_sec),
                )
            except fee_probe.ProbeError:
                cleanup["final_snapshot"] = snapshot_fn()
        else:
            cleanup["close"] = {"skipped": True, "reason": "already_flat"}
            cleanup["final_snapshot"] = snapshot

        cleanup["flat"] = fee_probe.is_flat_for_symbol(cleanup.get("final_snapshot") or {}, plan.symbol)
        if cleanup["flat"]:
            cleanup["cancel_results"] = fee_probe.cancel_residual_orders(client, cleanup.get("final_snapshot") or {}, plan.symbol)
            if cleanup["cancel_results"]:
                try:
                    cleanup["final_snapshot"] = fee_probe.wait_for(
                        f"{plan.symbol}_flat_after_residual_cancel",
                        snapshot_fn,
                        lambda value: fee_probe.is_flat_for_symbol(value, plan.symbol),
                        timeout_s=float(args.cleanup_timeout_sec),
                        interval_s=float(args.poll_interval_sec),
                    )
                except fee_probe.ProbeError:
                    cleanup["final_snapshot"] = snapshot_fn()
                cleanup["flat"] = fee_probe.is_flat_for_symbol(cleanup.get("final_snapshot") or {}, plan.symbol)
        else:
            close_order_ids = fee_probe.extract_order_ids(cleanup.get("close") or {})
            cleanup["close_cancel_results"] = cancel_order_ids(client, close_order_ids)
            cleanup["residual_cancel_skipped"] = "position_not_flat_keep_existing_protection"
        return cleanup
    except Exception as exc:
        cleanup["error"] = str(exc)
        try:
            cleanup["final_snapshot"] = snapshot_fn()
            cleanup["flat"] = fee_probe.is_flat_for_symbol(cleanup["final_snapshot"], plan.symbol)
        except Exception:
            pass
        return cleanup
    finally:
        cleanup["elapsed_s"] = round(time.perf_counter() - started, 3)


def build_close_limit_context(args: argparse.Namespace, plan: FillChainPlan) -> dict[str, Any]:
    quotes = fetch_quotes(args, [plan.symbol])
    quote = dict(quotes.get(plan.symbol) or {})
    bid = quote_price(quote, "bid", "bid_price", "84")
    ask = quote_price(quote, "ask", "ask_price", "86")
    max_age = float(getattr(args, "max_quote_age_sec", 0.0) or 0.0)
    quote_age = safe_float(quote.get("quote_age_s"), -1.0)
    quote_fallback = bool(quote.get("quote_fallback"))
    fresh_age = quote_age < 0 or max_age <= 0 or quote_age <= max_age
    buffer_ratio = pct_to_ratio(getattr(args, "cleanup_buffer_pct", getattr(args, "entry_buffer_pct", 0.05)))
    direction = normalize_direction(plan.direction)
    if direction == "long" and bid > 0:
        reference = bid
        reference_source = "bid"
        limit_price = round_price(bid * (1.0 - buffer_ratio))
    elif direction == "short" and ask > 0:
        reference = ask
        reference_source = "ask"
        limit_price = round_price(ask * (1.0 + buffer_ratio))
    else:
        raise FillChainProbeError(
            f"cleanup_fresh_bid_ask_required:{plan.symbol}:bid={bid}:ask={ask}:quote_fallback={quote_fallback}"
        )
    has_bid_ask = bid > 0 and ask > 0 and ask >= bid
    if quote_fallback or not has_bid_ask or not fresh_age:
        raise FillChainProbeError(
            "cleanup_fresh_bid_ask_required:"
            f"{plan.symbol}:bid={bid}:ask={ask}:quote_age_s={quote_age}:quote_fallback={quote_fallback}"
        )
    return {
        "symbol": plan.symbol,
        "direction": direction,
        "bid": round_price(bid),
        "ask": round_price(ask),
        "quote_age_s": quote_age,
        "quote_fallback": quote_fallback,
        "reference_price": round_price(reference),
        "reference_source": reference_source,
        "buffer_pct": float(getattr(args, "cleanup_buffer_pct", getattr(args, "entry_buffer_pct", 0.05)) or 0.0),
        "limit_price": limit_price,
        "quote": quote,
    }


def build_marketable_limit_close_payload(
    args: argparse.Namespace,
    plan: FillChainPlan,
    *,
    snapshot: dict[str, Any],
    place_response: dict[str, Any],
    close_context: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    payload = fee_probe.close_payload_from_snapshot(
        snapshot,
        symbol=plan.symbol,
        direction=plan.direction,
        quantity=plan.quantity,
        place_result=place_response,
    )
    payload.update(
        {
            "source": "gateway_fill_chain_probe_cleanup",
            "close_reason": f"gateway_fill_chain_probe_{reason}",
            "close_reason_human": "Gateway fill-chain probe cleanup",
            "order_type": "marketable_limit",
            "limit_price": float(close_context["limit_price"]),
            "wait_for_fill": True,
            "fill_timeout": float(getattr(args, "cleanup_fill_timeout_sec", 20.0) or 20.0),
            "outside_rth": bool(getattr(args, "outside_rth", True)),
            "outsideRth": bool(getattr(args, "outside_rth", True)),
            "tif": safe_text(getattr(args, "tif", "DAY")).upper() or "DAY",
            "trade_group_id": plan.ids.trade_group_id,
            "signal_id": plan.ids.signal_id,
            "entry_order_unique_id": plan.ids.client_order_id,
        }
    )
    return payload


def cancel_order_ids(client: fee_probe.ApiClient, order_ids: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for order_id in order_ids:
        oid = safe_text(order_id)
        if not oid or oid in seen:
            continue
        seen.add(oid)
        results.append(client.post("/api/custom/ibkr/orders/cancel", {"order_id": oid}, fee_probe.action_params()))
    return results


def is_flat_for_symbols(snapshot: dict[str, Any], symbols: list[str]) -> bool:
    if not fee_probe.supports_symbol_flat_check(snapshot):
        return False
    return all(fee_probe.is_flat_for_symbol(snapshot, symbol) for symbol in symbols)


def check_clean_preflight(snapshot: dict[str, Any], symbols: list[str]) -> dict[str, Any]:
    fee_probe.assert_paper_snapshot(snapshot)
    dirty: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        positions = [
            item
            for item in fee_probe.positions_for_symbol(snapshot, symbol)
            if abs(safe_float(item.get("quantity"), 0.0)) > 1e-9
        ]
        orders = fee_probe.open_orders_for_symbol(snapshot, symbol)
        if positions or orders:
            dirty[symbol] = {"positions": positions, "open_orders": orders}
    return {"ok": not dirty, "dirty": dirty}


def build_plan(args: argparse.Namespace, symbol: str, index: int) -> FillChainPlan:
    price = resolve_price_context(args, symbol)
    quantity, exposure = quantity_for_target(
        price.entry_price,
        quantity=int(args.quantity),
        target_notional=float(args.target_notional),
    )
    return FillChainPlan(
        symbol=fee_probe.normalize_symbol(symbol),
        direction=normalize_direction(args.direction),
        quantity=quantity,
        target_notional=float(args.target_notional),
        requested_exposure=exposure,
        price=price,
        ids=build_probe_ids(args.run_id, symbol, index),
        outside_rth=bool(args.outside_rth),
        tif=safe_text(args.tif).upper() or "DAY",
    )


def run_symbol_probe(args: argparse.Namespace, symbol: str, index: int) -> dict[str, Any]:
    symbol_started = time.perf_counter()
    plan = build_plan(args, symbol, index)
    phase_durations: dict[str, float] = {"plan_s": round(time.perf_counter() - symbol_started, 3)}
    summary: dict[str, Any] = {
        "ok": False,
        "symbol": plan.symbol,
        "dry_run": bool(args.dry_run),
        "plan": plan.summary(),
        "phase_durations_s": phase_durations,
        "entry_latency_s": None,
        "modify_latency_s": None,
        "actual_fill_price": None,
        "expected_new_tp": None,
        "expected_new_sl": None,
        "cleanup": {},
    }
    if bool(args.dry_run):
        phase_durations["total_s"] = round(time.perf_counter() - symbol_started, 3)
        summary.update({"ok": True, "skipped": True, "reason": "dry_run"})
        return summary

    submit_started = time.perf_counter()
    started_ms = int(time.time() * 1000)
    place_result = place_bracket_order(args, plan)
    phase_durations["submit_s"] = round(time.perf_counter() - submit_started, 3)
    summary["place"] = place_result
    if not place_result.get("ok"):
        cleanup = cleanup_symbol(args, plan, place_result.get("response") or {}, reason="place_failed")
        summary["cleanup"] = cleanup
        summary["error"] = place_result.get("error") or "place_failed"
        phase_durations["total_s"] = round(time.perf_counter() - symbol_started, 3)
        return summary

    wait_started = time.perf_counter()
    fill_result = wait_for_entry_fill(args, plan, place_result, started_ms=started_ms)
    phase_durations["wait_entry_fill_s"] = round(time.perf_counter() - wait_started, 3)
    summary["entry_fill"] = fill_result
    if not fill_result.get("ok"):
        cleanup = cleanup_symbol(args, plan, place_result.get("response") or place_result, reason="entry_fill_timeout")
        summary["cleanup"] = cleanup
        summary["error"] = fill_result.get("error") or "entry_fill_failed"
        summary["ok"] = False
        phase_durations["total_s"] = round(time.perf_counter() - symbol_started, 3)
        return summary

    fill = fill_result.get("fill") if isinstance(fill_result.get("fill"), dict) else {}
    actual_fill_price = safe_float(fill.get("avg_fill_price"), 0.0)
    summary["entry_latency_s"] = fill_result.get("entry_latency_s")
    summary["actual_fill_price"] = actual_fill_price
    if actual_fill_price <= 0:
        cleanup = cleanup_symbol(args, plan, place_result.get("response") or place_result, reason="avg_fill_price_missing")
        summary["cleanup"] = cleanup
        summary["error"] = "avg_fill_price_missing"
        phase_durations["total_s"] = round(time.perf_counter() - symbol_started, 3)
        return summary

    modify_started = time.perf_counter()
    modify_result = modify_protection_orders(args, plan, place_result.get("response") or place_result, actual_fill_price=actual_fill_price)
    phase_durations["modify_protection_s"] = round(time.perf_counter() - modify_started, 3)
    summary["modify"] = modify_result
    summary["modify_latency_s"] = modify_result.get("modify_latency_s")
    summary["expected_new_tp"] = modify_result.get("expected_new_tp")
    summary["expected_new_sl"] = modify_result.get("expected_new_sl")

    cleanup_started = time.perf_counter()
    cleanup = cleanup_symbol(args, plan, place_result.get("response") or place_result, reason="post_modify_flatten")
    phase_durations["cleanup_s"] = round(time.perf_counter() - cleanup_started, 3)
    summary["cleanup"] = cleanup
    summary["ok"] = bool(modify_result.get("ok")) and bool(cleanup.get("flat"))
    if not summary["ok"]:
        summary["error"] = "modify_or_cleanup_failed"
    phase_durations["total_s"] = round(time.perf_counter() - symbol_started, 3)
    return summary


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    run_started = time.perf_counter()
    symbols = split_symbols(args.symbols)
    payload: dict[str, Any] = {
        "ok": False,
        "run_id": args.run_id,
        "dry_run": bool(args.dry_run),
        "paper_only": True,
        "confirm_text": CONFIRM_TEXT,
        "created_at_et": datetime.now(ET).isoformat(),
        "created_at_cn": datetime.now(CN).isoformat(),
        "symbols": symbols,
        "parameters": {
            "direction": args.direction,
            "quantity": args.quantity,
            "target_notional": args.target_notional,
            "entry_buffer_pct": args.entry_buffer_pct,
            "tp_pct": args.tp_pct,
            "sl_pct": args.sl_pct,
            "cleanup_buffer_pct": args.cleanup_buffer_pct,
            "cleanup_fill_timeout_sec": args.cleanup_fill_timeout_sec,
            "timeout_sec": args.timeout_sec,
            "poll_interval_sec": args.poll_interval_sec,
            "api_base_url": args.api_base_url,
            "account_base_url": args.account_base_url,
            "market_data_mode": args.market_data_mode,
            "quote_snapshot": args.quote_snapshot,
            "quote_snapshot_timeout_sec": args.quote_snapshot_timeout_sec,
            "max_quote_age_sec": args.max_quote_age_sec,
            "allow_reference_fallback": args.allow_reference_fallback,
            "outside_rth": args.outside_rth,
            "tif": args.tif,
        },
        "phase_durations_s": {},
        "preflight": {},
        "results": [],
        "final_flat": {},
    }
    try:
        if not symbols:
            raise FillChainProbeError("symbols_required")
        if not bool(args.dry_run) and args.confirm != CONFIRM_TEXT:
            raise FillChainProbeError(f"confirmation_required: pass --confirm {CONFIRM_TEXT}")
        preflight_started = time.perf_counter()
        before_snapshot = get_snapshot(args, timeout_sec=float(args.http_timeout_sec), orders_fast=False)
        preflight = check_clean_preflight(before_snapshot, symbols)
        payload["preflight"] = {"ok": preflight.get("ok"), "dirty": preflight.get("dirty"), "snapshot": before_snapshot}
        payload["phase_durations_s"]["preflight_s"] = round(time.perf_counter() - preflight_started, 3)
        if not preflight.get("ok"):
            payload["error"] = "preflight_not_clean"
            return payload

        for index, symbol in enumerate(symbols, start=1):
            result = run_symbol_probe(args, symbol, index)
            payload["results"].append(result)
            if not result.get("ok") and not bool((result.get("cleanup") or {}).get("flat", True)):
                break

        final_started = time.perf_counter()
        final_snapshot = get_snapshot(args, timeout_sec=float(args.http_timeout_sec), orders_fast=False)
        fee_probe.assert_paper_snapshot(final_snapshot)
        final_flat = is_flat_for_symbols(final_snapshot, symbols)
        payload["final_flat"] = {"ok": final_flat, "snapshot": final_snapshot}
        payload["phase_durations_s"]["final_flat_check_s"] = round(time.perf_counter() - final_started, 3)
        payload["ok"] = all(item.get("ok") for item in payload["results"]) and final_flat
        if not payload["ok"] and not payload.get("error"):
            payload["error"] = "one_or_more_symbol_probes_failed"
        return payload
    except Exception as exc:
        payload["ok"] = False
        payload["error"] = str(exc)
        return payload
    finally:
        payload["phase_durations_s"]["total_s"] = round(time.perf_counter() - run_started, 3)
        try:
            payload["artifact_path"] = str(finalize_artifact(args, payload))
        except Exception as artifact_exc:
            payload["artifact_error"] = str(artifact_exc)


def build_parser() -> argparse.ArgumentParser:
    default_run_id = "GWFILL_" + datetime.now(ET).strftime("%Y%m%d_%H%M%S_ET")
    parser = argparse.ArgumentParser(
        description="Paper-only marketable-limit fill-chain probe for IBKR Gateway entry fill, TP/SL reprice, and cleanup."
    )
    parser.add_argument("--api-base-url", "--runtime-url", dest="api_base_url", default=fee_probe.DEFAULT_API_BASE_URL)
    parser.add_argument(
        "--account-base-url",
        default=os.environ.get("IBKR_ACCOUNT_BASE_URL") or os.environ.get("IBKR_RUNTIME_BASE_URL") or "",
    )
    parser.add_argument("--account-snapshot-path", default="")
    parser.add_argument("--host", default=fee_probe.DEFAULT_HOST)
    parser.add_argument("--db-path", default=fee_probe.DEFAULT_DB_PATH)
    parser.add_argument("--sqlite-script", default=str(fee_probe.DEFAULT_SQLITE_SCRIPT))
    parser.add_argument("--symbols", "--symbol", dest="symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--target-notional", type=float, default=0.0)
    parser.add_argument("--direction", choices=("long", "short"), default="long")
    parser.add_argument("--entry-buffer-pct", type=float, default=0.05, help="Percent points; 0.05 means 0.05%%.")
    parser.add_argument("--tp-pct", type=float, default=1.0, help="Percent points from actual fill price.")
    parser.add_argument("--sl-pct", type=float, default=1.0, help="Percent points from actual fill price.")
    parser.add_argument("--cleanup-buffer-pct", type=float, default=0.05, help="Percent points for cleanup marketable limit.")
    parser.add_argument("--cleanup-fill-timeout-sec", type=float, default=20.0)
    parser.add_argument("--reference-price", type=float, default=0.0)
    parser.add_argument("--market-data-mode", default="live")
    parser.set_defaults(quote_snapshot=True)
    parser.add_argument("--quote-snapshot", action="store_true", dest="quote_snapshot")
    parser.add_argument("--no-quote-snapshot", action="store_false", dest="quote_snapshot")
    parser.add_argument("--quote-snapshot-timeout-sec", type=float, default=3.0)
    parser.add_argument("--max-quote-age-sec", type=float, default=10.0)
    parser.add_argument(
        "--allow-reference-fallback",
        action="store_true",
        help="Allow execute without fresh bid/ask; unsafe for fill-chain validation and disabled by default.",
    )
    parser.add_argument("--timeout-sec", "--timeout", dest="timeout_sec", type=float, default=60.0)
    parser.add_argument("--poll-interval-sec", "--poll", dest="poll_interval_sec", type=float, default=2.0)
    parser.add_argument("--cleanup-timeout-sec", type=float, default=60.0)
    parser.add_argument("--http-timeout-sec", type=float, default=30.0)
    parser.add_argument("--sqlite-timeout-sec", type=float, default=45.0)
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument("--run-id", default=default_run_id)
    parser.set_defaults(outside_rth=True)
    parser.add_argument("--outside-rth", action="store_true", dest="outside_rth", help="Allow fills outside regular trading hours.")
    parser.add_argument("--no-outside-rth", action="store_false", dest="outside_rth")
    parser.add_argument("--tif", default="DAY")
    parser.set_defaults(dry_run=True)
    parser.add_argument("--dry-run", action="store_true", dest="dry_run", help="Build plan and artifacts without submitting orders.")
    parser.add_argument("--execute", action="store_false", dest="dry_run", help="Submit paper orders; requires --confirm.")
    parser.add_argument("--confirm", default="", help=f"Required with --execute: {CONFIRM_TEXT}")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    symbols = split_symbols(args.symbols)
    if not symbols:
        raise FillChainProbeError("symbols_required")
    if int(args.quantity or 0) <= 0:
        raise FillChainProbeError("quantity must be a positive integer")
    if float(args.target_notional or 0.0) < 0:
        raise FillChainProbeError("target_notional must be non-negative")
    if float(args.entry_buffer_pct or 0.0) < 0:
        raise FillChainProbeError("entry_buffer_pct must be non-negative")
    if float(args.tp_pct or 0.0) <= 0 or float(args.sl_pct or 0.0) <= 0:
        raise FillChainProbeError("tp_pct and sl_pct must be positive")
    if float(args.cleanup_buffer_pct or 0.0) < 0:
        raise FillChainProbeError("cleanup_buffer_pct must be non-negative")
    if float(args.cleanup_fill_timeout_sec or 0.0) <= 0:
        raise FillChainProbeError("cleanup_fill_timeout_sec must be positive")
    if float(args.quote_snapshot_timeout_sec or 0.0) <= 0:
        raise FillChainProbeError("quote_snapshot_timeout_sec must be positive")
    if float(args.max_quote_age_sec or 0.0) < 0:
        raise FillChainProbeError("max_quote_age_sec must be non-negative")
    if not bool(args.dry_run) and args.confirm != CONFIRM_TEXT:
        raise FillChainProbeError(f"confirmation_required: pass --confirm {CONFIRM_TEXT}")
    args.symbols = ",".join(symbols)
    args.tif = safe_text(args.tif).upper() or "DAY"


def print_text(payload: dict[str, Any]) -> None:
    print(
        "ok={ok} dry_run={dry_run} run_id={run_id} symbols={symbols} error={error}".format(
            ok=payload.get("ok"),
            dry_run=payload.get("dry_run"),
            run_id=payload.get("run_id"),
            symbols=",".join(payload.get("symbols") or []),
            error=payload.get("error") or "",
        )
    )
    for result in payload.get("results") or []:
        print(
            "- {symbol}: ok={ok} entry_latency={entry_latency} modify_latency={modify_latency} "
            "fill={fill} tp={tp} sl={sl} cleanup_flat={flat} error={error}".format(
                symbol=result.get("symbol"),
                ok=result.get("ok"),
                entry_latency=result.get("entry_latency_s"),
                modify_latency=result.get("modify_latency_s"),
                fill=result.get("actual_fill_price"),
                tp=result.get("expected_new_tp"),
                sl=result.get("expected_new_sl"),
                flat=(result.get("cleanup") or {}).get("flat"),
                error=result.get("error") or "",
            )
        )
    artifact_path = payload.get("artifact_path")
    if artifact_path:
        print(f"summary={artifact_path}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
    except FillChainProbeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    payload = run_probe(args)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    else:
        print_text(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
