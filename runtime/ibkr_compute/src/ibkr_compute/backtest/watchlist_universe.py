from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ibkr_compute.backtest.constants import (
    DEFAULT_MARKET_MONITOR_SYMBOLS,
    WATCHLIST_SYMBOL_ROLE_TRADE,
)
from ibkr_compute.market.timeframe_utils import ET


def request_excluded_symbols(request: dict) -> set[str]:
    excluded = {
        str(symbol or "").strip().upper()
        for symbol in list(request.get("exclude_symbols") or [])
        if str(symbol or "").strip()
    }
    if bool(request.get("exclude_market_monitors", True)):
        excluded.update(DEFAULT_MARKET_MONITOR_SYMBOLS)
    return excluded


def parse_pb_datetime(raw_value: Any):
    text = str(raw_value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ET)


def is_trade_watchlist_row(row: dict) -> bool:
    role = str(row.get("symbol_role") or "").strip().lower()
    return role in {"", WATCHLIST_SYMBOL_ROLE_TRADE}


def merge_trade_watchlist_rows(rows: list[dict], request: dict, *, as_of_date: str = "") -> list[dict]:
    source_environment = str(request.get("source_environment") or "live").strip().lower() or "live"
    priority = {"": 0, "global": 1, source_environment: 2}
    excluded = request_excluded_symbols(request)
    merged: dict[str, dict] = {}
    applied_rank: dict[str, int] = {}
    as_of_end = None
    if as_of_date:
        as_of_end = datetime.strptime(as_of_date, "%Y-%m-%d").replace(tzinfo=ET) + timedelta(days=1) - timedelta(milliseconds=1)

    for row in rows or []:
        symbol = str(row.get("symbol", "") or "").strip().upper()
        if not symbol or symbol in excluded or not is_trade_watchlist_row(row):
            continue
        env = str(row.get("environment", "") or "").strip().lower()
        rank = priority.get(env, -1)
        if rank < 0:
            continue
        if as_of_end is not None:
            created_at = parse_pb_datetime(row.get("created")) or parse_pb_datetime(row.get("created_us"))
            if created_at and created_at > as_of_end:
                continue
        if symbol in applied_rank and applied_rank[symbol] > rank:
            continue
        applied_rank[symbol] = rank
        merged[symbol] = row

    return [merged[symbol] for symbol in sorted(merged)]
