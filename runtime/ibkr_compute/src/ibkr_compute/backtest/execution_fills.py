from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import xml.etree.ElementTree as XmlElementTree
from datetime import datetime, time, timezone
from typing import Any, Iterable, Sequence

from ibkr_compute.backtest import constants
from ibkr_compute.backtest.execution_cost import build_execution_cost_profile
from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite


FILL_COLLECTION = "ibkr_execution_fills"
DEFAULT_PROFILE_STATE_KEY = "backtest_execution_cost_profile"
_UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(_UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return float(default)
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else float(default)
    text = str(value or "").strip().replace(",", "")
    if not text:
        return float(default)
    text = text.replace("$", "")
    try:
        number = float(text)
        return number if math.isfinite(number) else float(default)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return int(default)


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").strip().lower())


def _get_any(payload: dict | None, *keys: str, default: Any = None) -> Any:
    if not isinstance(payload, dict):
        return default
    for key in keys:
        if key in payload and payload.get(key) not in (None, ""):
            return payload.get(key)
    index = {_normalized_key(key): key for key in payload.keys()}
    for key in keys:
        matched_key = index.get(_normalized_key(key))
        if matched_key is not None and payload.get(matched_key) not in (None, ""):
            return payload.get(matched_key)
    return default


def _normalize_environment(value: Any, default: str = "live") -> str:
    normalized = str(value or default).strip().lower() or default
    return normalized if normalized in {"live", "paper", "backtest"} else default


def _normalize_side(raw_side: Any, quantity: float = 0.0) -> str:
    text = str(raw_side or "").strip().lower()
    if text in {"buy", "bot", "b", "long"}:
        return "buy"
    if text in {"sell", "sld", "s", "short", "ss"}:
        return "sell"
    if quantity < 0:
        return "sell"
    if quantity > 0:
        return "buy"
    return ""


def _parse_datetime_text(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    compact = re.sub(r"\s+", " ", text.replace(";", " ")).strip()
    compact = compact.replace(" US/Eastern", "").replace(" America/New_York", "")
    if compact.endswith("Z"):
        compact = compact[:-1] + "+00:00"

    for parser in (
        lambda item: datetime.fromisoformat(item),
        lambda item: datetime.strptime(item, "%Y%m%d %H:%M:%S"),
        lambda item: datetime.strptime(item, "%Y%m%d %H%M%S"),
        lambda item: datetime.strptime(item, "%Y-%m-%d %H:%M:%S"),
        lambda item: datetime.strptime(item, "%Y-%m-%dT%H:%M:%S"),
        lambda item: datetime.strptime(item, "%m/%d/%Y %H:%M:%S"),
        lambda item: datetime.strptime(item, "%Y%m%d"),
        lambda item: datetime.strptime(item, "%Y-%m-%d"),
    ):
        try:
            parsed = parser(compact)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=constants.ET)
            return parsed
        except Exception:
            continue
    return None


def parse_trade_time_ms(*values: Any, default: int = 0) -> int:
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if number > 10_000_000_000:
                return int(number)
            if number > 1_000_000_000:
                return int(number * 1000)
        text = str(value or "").strip()
        if not text:
            continue
        if re.fullmatch(r"\d{13}", text):
            return int(text)
        if re.fullmatch(r"\d{10}", text):
            return int(text) * 1000
        parsed = _parse_datetime_text(text)
        if parsed is not None:
            return int(parsed.astimezone(_UTC).timestamp() * 1000)
    return int(default)


def parse_date_range_ms(date_from: Any = "", date_to: Any = "") -> tuple[int, int]:
    start_ms = 0
    end_ms = 0
    start_text = str(date_from or "").strip()
    end_text = str(date_to or "").strip()
    if start_text:
        parsed = _parse_datetime_text(start_text)
        if parsed is not None:
            if " " not in start_text and "T" not in start_text:
                parsed = datetime.combine(parsed.astimezone(constants.ET).date(), time.min, constants.ET)
            start_ms = int(parsed.astimezone(_UTC).timestamp() * 1000)
    if end_text:
        parsed = _parse_datetime_text(end_text)
        if parsed is not None:
            if " " not in end_text and "T" not in end_text:
                parsed = datetime.combine(parsed.astimezone(constants.ET).date(), time.max, constants.ET)
            end_ms = int(parsed.astimezone(_UTC).timestamp() * 1000)
    return start_ms, end_ms


def _format_trade_time(ms: int) -> str:
    if int(ms or 0) <= 0:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000.0, constants.ET).strftime("%Y-%m-%d %H:%M:%S")


def _stable_exec_id(payload: dict, *, environment: str, account: str) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
    return f"synthetic_{environment}_{account or 'global'}_{digest}"


def normalize_execution_fill(
    raw_fill: dict | None,
    *,
    environment: str = "live",
    account: str = "",
    source: str = "manual",
) -> dict | None:
    raw = dict(raw_fill or {})
    runtime_environment = _normalize_environment(
        _get_any(raw, "environment", default=environment),
        default=_normalize_environment(environment),
    )
    quantity = _safe_float(_get_any(raw, "shares", "quantity", "qty", "filledQuantity", "filled_qty"), 0.0)
    side = _normalize_side(_get_any(raw, "side", "buySell", "buy_sell", "action"), quantity)
    shares = abs(quantity)
    price = abs(_safe_float(_get_any(raw, "price", "tradePrice", "avgPrice", "avg_price", "fill_price"), 0.0))
    commission = abs(_safe_float(_get_any(raw, "commission", "ibCommission", "ib_commission"), 0.0))
    symbol = str(_get_any(raw, "symbol", "ticker", "underlyingSymbol", "contractDesc") or "").strip().upper()
    if not symbol or shares <= 0 or price <= 0:
        return None

    trade_time_ms = parse_trade_time_ms(
        _get_any(raw, "trade_time_ms", "tradeTimeMs", "bar_time_ms"),
        _get_any(raw, "dateTime", "datetime", "time", "trade_time", "fill_time", "lastExecutionTime"),
        " ".join(
            item
            for item in (
                str(_get_any(raw, "tradeDate", "date", default="") or "").strip(),
                str(_get_any(raw, "tradeTime", "executionTime", default="") or "").strip(),
            )
            if item
        ),
    )
    normalized_account = str(_get_any(raw, "account", "accountId", "acctNumber", default=account) or "").strip()
    normalized_source = str(_get_any(raw, "source", default=source) or source or "manual").strip().lower() or "manual"
    exec_id = str(_get_any(raw, "exec_id", "execId", "executionId", "executionID", default="") or "").strip()
    order_id = str(_get_any(raw, "order_id", "orderId", "orderID", "ibOrderID", default="") or "").strip()
    if not exec_id:
        exec_id = _stable_exec_id(
            {
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "shares": round(shares, 8),
                "price": round(price, 8),
                "commission": round(commission, 8),
                "trade_time_ms": trade_time_ms,
                "raw": raw,
            },
            environment=runtime_environment,
            account=normalized_account,
        )

    reference_price = abs(
        _safe_float(
            _get_any(raw, "reference_price", "referencePrice", "arrival_price", "arrivalPrice", "signal_price", "signalPrice"),
            0.0,
        )
    )
    slippage_bps = abs(_safe_float(_get_any(raw, "slippage_bps", "slippageBps"), 0.0))
    if slippage_bps <= 0 and reference_price > 0:
        slippage_bps = abs(price - reference_price) / reference_price * 10000.0

    trade_value = shares * price
    return {
        "exec_id": exec_id,
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "shares": round(shares, 8),
        "price": round(price, 8),
        "trade_value": round(trade_value, 8),
        "commission": round(commission, 8),
        "commission_currency": str(_get_any(raw, "commissionCurrency", "ibCommissionCurrency", default="") or "").strip().upper(),
        "currency": str(_get_any(raw, "currency", default="USD") or "USD").strip().upper(),
        "trade_time": str(_get_any(raw, "trade_time", "dateTime", "datetime", "time", default="") or "").strip()
        or _format_trade_time(trade_time_ms),
        "trade_time_ms": int(trade_time_ms or 0),
        "account": normalized_account,
        "source": normalized_source,
        "environment": runtime_environment,
        "asset_category": str(_get_any(raw, "assetCategory", "asset_category", "secType", default="") or "").strip().upper(),
        "exchange": str(_get_any(raw, "exchange", "listingExchange", default="") or "").strip().upper(),
        "order_type": str(_get_any(raw, "orderType", "order_type", default="") or "").strip().upper(),
        "reference_price": round(reference_price, 8) if reference_price > 0 else 0.0,
        "slippage_bps": round(slippage_bps, 8) if slippage_bps > 0 else 0.0,
        "raw": raw,
    }


def normalize_execution_fills(
    raw_fills: Iterable[dict],
    *,
    environment: str = "live",
    account: str = "",
    source: str = "manual",
) -> list[dict]:
    items: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_fill in raw_fills or []:
        if not isinstance(raw_fill, dict):
            continue
        item = normalize_execution_fill(raw_fill, environment=environment, account=account, source=source)
        if not item:
            continue
        key = (item["environment"], item.get("account") or "", item["exec_id"])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def parse_flex_xml_fills(
    xml_text: str,
    *,
    environment: str = "live",
    account: str = "",
    source: str = "flex",
) -> list[dict]:
    text = str(xml_text or "").strip()
    if not text:
        return []
    root = XmlElementTree.fromstring(text)
    raw_trades: list[dict] = []
    seen_elements: set[int] = set()
    for statement in root.iter():
        statement_tag = str(statement.tag or "").rsplit("}", 1)[-1].lower()
        if statement_tag != "flexstatement":
            continue
        statement_account = str(statement.attrib.get("accountId") or statement.attrib.get("account") or account or "").strip()
        for element in statement.iter():
            tag = str(element.tag or "").rsplit("}", 1)[-1]
            if tag.lower() != "trade":
                continue
            seen_elements.add(id(element))
            payload = dict(element.attrib or {})
            if statement_account and not (payload.get("account") or payload.get("accountId")):
                payload["accountId"] = statement_account
            if element.text and element.text.strip() and "rawText" not in payload:
                payload["rawText"] = element.text.strip()
            raw_trades.append(payload)
    for element in root.iter():
        tag = str(element.tag or "").rsplit("}", 1)[-1]
        if tag.lower() != "trade" or id(element) in seen_elements:
            continue
        payload = dict(element.attrib or {})
        if element.text and element.text.strip() and "rawText" not in payload:
            payload["rawText"] = element.text.strip()
        raw_trades.append(payload)
    return normalize_execution_fills(raw_trades, environment=environment, account=account, source=source)


def summarize_execution_fills(fills: Sequence[dict]) -> dict:
    normalized = [item for item in (fills or []) if isinstance(item, dict)]
    total_shares = sum(abs(_safe_float(item.get("shares"), 0.0)) for item in normalized)
    total_value = sum(abs(_safe_float(item.get("trade_value"), 0.0)) for item in normalized)
    total_commission = sum(abs(_safe_float(item.get("commission"), 0.0)) for item in normalized)
    symbols = sorted({str(item.get("symbol") or "").strip().upper() for item in normalized if item.get("symbol")})
    accounts = sorted({str(item.get("account") or "").strip() for item in normalized if item.get("account")})
    sources = sorted({str(item.get("source") or "").strip().lower() for item in normalized if item.get("source")})
    times = [int(_safe_int(item.get("trade_time_ms"), 0)) for item in normalized if _safe_int(item.get("trade_time_ms"), 0) > 0]
    return {
        "fill_count": len(normalized),
        "symbols": symbols,
        "accounts": accounts,
        "sources": sources,
        "total_shares": round(total_shares, 4),
        "total_trade_value": round(total_value, 4),
        "total_commission": round(total_commission, 4),
        "avg_commission_per_share": round(total_commission / total_shares, 8) if total_shares > 0 else 0.0,
        "avg_commission_bps": round(total_commission / total_value * 10000.0, 4) if total_value > 0 else 0.0,
        "first_trade_time_ms": min(times) if times else 0,
        "last_trade_time_ms": max(times) if times else 0,
        "first_trade_time": _format_trade_time(min(times)) if times else "",
        "last_trade_time": _format_trade_time(max(times)) if times else "",
    }


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values if value >= 0)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, float(percentile))) * (len(ordered) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def build_calibrated_execution_cost_profile(
    fills: Sequence[dict],
    *,
    base_request: dict | None = None,
    environment: str = "live",
    account: str = "",
    source: str = "ibkr_execution_fills",
) -> dict:
    normalized = [
        item
        for item in normalize_execution_fills(fills, environment=environment, account=account, source=source)
        if _safe_float(item.get("shares"), 0.0) > 0 and _safe_float(item.get("price"), 0.0) > 0
    ]
    commission_fills = [item for item in normalized if _safe_float(item.get("commission"), 0.0) > 0]
    summary = summarize_execution_fills(normalized)
    base = build_execution_cost_profile(base_request or {})
    generated_at = _now_iso()

    if not commission_fills:
        profile = {
            **base,
            "fee_model": "calibrated_v1",
            "execution_cost_profile": {
                **(base.get("execution_cost_profile") or {}),
                "fee_model": "calibrated_v1",
                "source": source,
                "calibrated_at": generated_at,
                "calibration_status": "insufficient_commission_sample",
                "sample": summary,
            },
        }
        return {
            "ok": True,
            "profile_available": False,
            "error": "no_commission_samples",
            "profile": profile,
            "sample": summary,
        }

    ratios = [
        _safe_float(item.get("commission"), 0.0) / _safe_float(item.get("shares"), 1.0)
        for item in commission_fills
        if _safe_float(item.get("shares"), 0.0) > 0
    ]
    total_commission = sum(_safe_float(item.get("commission"), 0.0) for item in commission_fills)
    total_shares = sum(_safe_float(item.get("shares"), 0.0) for item in commission_fills)
    weighted_per_share = total_commission / total_shares if total_shares > 0 else constants.DEFAULT_COMMISSION_PER_SHARE
    q25_per_share = _percentile(ratios, 0.25) or weighted_per_share
    per_share = min(weighted_per_share, q25_per_share) if weighted_per_share > 0 else q25_per_share

    min_item = min(commission_fills, key=lambda item: _safe_float(item.get("commission"), 0.0))
    min_commission_observed = _safe_float(min_item.get("commission"), 0.0)
    min_shares = _safe_float(min_item.get("shares"), 0.0)
    implied_min_threshold = per_share * min_shares * 1.2 if min_shares > 0 else 0.0
    inferred_min_commission = min_commission_observed if min_commission_observed > implied_min_threshold else 0.0

    slippage_samples = [
        _safe_float(item.get("slippage_bps"), 0.0)
        for item in normalized
        if _safe_float(item.get("slippage_bps"), 0.0) > 0
    ]
    calibrated_slippage_bps = _percentile(slippage_samples, 0.5) if slippage_samples else _safe_float(base.get("slippage_bps"), 0.0)

    custom_profile = {
        **(base.get("execution_cost_profile") or {}),
        "fee_model": "calibrated_v1",
        "calibrated_per_share": round(max(0.0, per_share), 8),
        "calibrated_min_commission": round(max(0.0, inferred_min_commission), 6),
        "calibrated_max_pct_trade_value": _safe_float(base.get("calibrated_max_pct_trade_value"), 0.0),
        "source": source,
        "calibrated_at": generated_at,
        "calibration_status": "ready",
        "calibration_sample": {
            **summary,
            "commission_fill_count": len(commission_fills),
            "weighted_commission_per_share": round(weighted_per_share, 8),
            "q25_commission_per_share": round(q25_per_share, 8),
            "min_commission_observed": round(min_commission_observed, 6),
            "min_commission_inferred": bool(inferred_min_commission > 0),
            "slippage_sample_count": len(slippage_samples),
        },
    }
    profile = build_execution_cost_profile(
        {
            **(base_request or {}),
            "fee_model": "calibrated_v1",
            "commission_per_share": custom_profile["calibrated_per_share"],
            "slippage_bps": calibrated_slippage_bps,
            "execution_cost_profile": custom_profile,
            "include_regulatory_fees": False,
        }
    )
    return {
        "ok": True,
        "profile_available": True,
        "profile": profile,
        "execution_cost_profile": custom_profile,
        "sample": summary,
        "backtest_payload_patch": {
            "fee_model": "calibrated_v1",
            "commission_per_share": profile["commission_per_share"],
            "slippage_bps": profile["slippage_bps"],
            "execution_cost_profile": custom_profile,
            "include_regulatory_fees": False,
        },
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _row_to_fill(row: sqlite3.Row) -> dict:
    item = {key: row[key] for key in row.keys()}
    raw = item.get("raw")
    if isinstance(raw, str) and raw.strip():
        try:
            item["raw"] = json.loads(raw)
        except Exception:
            item["raw"] = {}
    return item


def fetch_execution_fills(
    *,
    environment: str = "live",
    symbols: Sequence[str] | None = None,
    account: str = "",
    source: str = "",
    start_ms: int = 0,
    end_ms: int = 0,
    limit: int = 5000,
) -> dict:
    runtime_environment = _normalize_environment(environment)
    normalized_symbols = [
        str(symbol or "").strip().upper()
        for symbol in (symbols or [])
        if str(symbol or "").strip()
    ]
    query_limit = max(1, min(20000, int(limit or 5000)))
    with open_pb_sqlite(readonly=True, timeout=10.0) as conn:
        if not _table_exists(conn, FILL_COLLECTION):
            return {
                "ok": True,
                "available": False,
                "error": "execution_fills_collection_missing",
                "items": [],
                "summary": summarize_execution_fills([]),
            }
        where_parts = ["environment = ?"]
        params: list[Any] = [runtime_environment]
        if account:
            where_parts.append("account = ?")
            params.append(str(account or "").strip())
        if source:
            where_parts.append("source = ?")
            params.append(str(source or "").strip().lower())
        if normalized_symbols:
            placeholders = ", ".join("?" for _ in normalized_symbols)
            where_parts.append(f"symbol IN ({placeholders})")
            params.extend(normalized_symbols)
        if int(start_ms or 0) > 0:
            where_parts.append("trade_time_ms >= ?")
            params.append(int(start_ms or 0))
        if int(end_ms or 0) > 0:
            where_parts.append("trade_time_ms <= ?")
            params.append(int(end_ms or 0))
        rows = conn.execute(
            f"""
            SELECT id, exec_id, order_id, symbol, side, shares, price, trade_value,
                   commission, commission_currency, currency, trade_time,
                   trade_time_ms, account, source, environment, asset_category,
                   exchange, order_type, reference_price, slippage_bps, raw,
                   created, updated
            FROM {FILL_COLLECTION}
            WHERE {' AND '.join(where_parts)}
            ORDER BY trade_time_ms DESC, updated DESC
            LIMIT ?
            """,
            tuple([*params, query_limit]),
        ).fetchall()
    items = [_row_to_fill(row) for row in rows]
    return {
        "ok": True,
        "available": True,
        "items": items,
        "summary": summarize_execution_fills(items),
    }
