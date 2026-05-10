from __future__ import annotations

from datetime import datetime
from typing import Any

try:
    from ibkr_compute.market.bar_coverage_daily import is_nyse_trading_day as _is_nyse_trading_day
except Exception:  # pragma: no cover - fail open if the compute calendar is unavailable.
    _is_nyse_trading_day = None


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _parse_market_date(value: Any):
    text = _to_text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def is_nyse_non_trading_day(market_date: Any) -> bool:
    day = _parse_market_date(market_date)
    if day is None or _is_nyse_trading_day is None:
        return False
    try:
        return not bool(_is_nyse_trading_day(day))
    except Exception:
        return False


__all__ = ["is_nyse_non_trading_day"]
