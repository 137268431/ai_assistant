"""Close-order execution planning for regular, extended, and overnight sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Any

from ibkr_compute.core.time_utils import ET


DEFAULT_REGULAR_LIMIT_BPS = 15.0
DEFAULT_EXTENDED_LIMIT_BPS = 50.0
DEFAULT_OVERNIGHT_LIMIT_BPS = 100.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


@dataclass(frozen=True)
class CloseSession:
    name: str
    tradable: bool
    outside_rth: bool
    tif: str
    exchange: str
    include_overnight: bool
    default_bps: float
    reason: str = ""


def infer_close_session(now: datetime | None = None, *, session_override: str = "") -> CloseSession:
    """Classify the current US equity session for safe close-order routing."""
    override = _text(session_override).lower()
    if override:
        if override in {"regular", "rth", "market", "day"}:
            return CloseSession("regular", True, False, "DAY", "SMART", False, DEFAULT_REGULAR_LIMIT_BPS)
        if override in {"premarket", "pre_market", "pre"}:
            return CloseSession("premarket", True, True, "DAY", "SMART", False, DEFAULT_EXTENDED_LIMIT_BPS)
        if override in {"afterhours", "after_hours", "postmarket", "post_market", "post"}:
            return CloseSession("afterhours", True, True, "DAY", "SMART", False, DEFAULT_EXTENDED_LIMIT_BPS)
        if override in {"overnight", "overnight_smart", "overnight+smart"}:
            return CloseSession("overnight", True, True, "DAY", "OVERNIGHT", True, DEFAULT_OVERNIGHT_LIMIT_BPS)
        if override in {"closed", "halt", "halted"}:
            return CloseSession("closed", False, False, "DAY", "SMART", False, DEFAULT_EXTENDED_LIMIT_BPS, "session_override_closed")

    current = now or datetime.now(ET)
    if current.tzinfo is None:
        current = current.replace(tzinfo=ET)
    current = current.astimezone(ET)
    weekday = current.weekday()  # Monday=0, Sunday=6
    t = current.time()
    early_overnight = t < dt_time(3, 50)
    late_overnight = t >= dt_time(20, 0)
    if early_overnight:
        if weekday in {0, 1, 2, 3, 4}:
            return CloseSession("overnight", True, True, "DAY", "OVERNIGHT", True, DEFAULT_OVERNIGHT_LIMIT_BPS)
        return CloseSession("closed", False, False, "DAY", "SMART", False, DEFAULT_EXTENDED_LIMIT_BPS, "weekend_overnight_closed")
    if dt_time(3, 50) <= t < dt_time(4, 0):
        return CloseSession("closed", False, False, "DAY", "SMART", False, DEFAULT_EXTENDED_LIMIT_BPS, "overnight_premarket_break")
    if weekday not in {0, 1, 2, 3, 4}:
        if weekday == 6 and late_overnight:
            return CloseSession("overnight", True, True, "DAY", "OVERNIGHT", True, DEFAULT_OVERNIGHT_LIMIT_BPS)
        return CloseSession("closed", False, False, "DAY", "SMART", False, DEFAULT_EXTENDED_LIMIT_BPS, "weekend_closed")
    if dt_time(4, 0) <= t < dt_time(9, 30):
        return CloseSession("premarket", True, True, "DAY", "SMART", False, DEFAULT_EXTENDED_LIMIT_BPS)
    if dt_time(9, 30) <= t < dt_time(16, 0):
        return CloseSession("regular", True, False, "DAY", "SMART", False, DEFAULT_REGULAR_LIMIT_BPS)
    if dt_time(16, 0) <= t < dt_time(20, 0):
        return CloseSession("afterhours", True, True, "DAY", "SMART", False, DEFAULT_EXTENDED_LIMIT_BPS)
    if late_overnight and weekday in {0, 1, 2, 3}:
        return CloseSession("overnight", True, True, "DAY", "OVERNIGHT", True, DEFAULT_OVERNIGHT_LIMIT_BPS)
    return CloseSession("closed", False, False, "DAY", "SMART", False, DEFAULT_EXTENDED_LIMIT_BPS, "overnight_weekend_closed")


def quote_from_sources(*sources: dict[str, Any] | None) -> dict[str, Any]:
    quote: dict[str, Any] = {}
    aliases = {
        "bid": ("bid", "bid_price", "bidPrice"),
        "ask": ("ask", "ask_price", "askPrice"),
        "last_price": ("last_price", "last", "lastPrice", "market_price", "mktPrice", "close"),
        "updated_ms": ("updated_ms", "_updated", "quote_updated_ms"),
    }
    for source in sources:
        if not isinstance(source, dict):
            continue
        for target, keys in aliases.items():
            if quote.get(target) not in (None, "", 0, 0.0):
                continue
            for key in keys:
                value = source.get(key)
                parsed = _safe_float(value, 0.0)
                if parsed > 0 or (target == "updated_ms" and parsed >= 0 and value not in (None, "")):
                    quote[target] = parsed
                    break
    return quote


def build_close_execution_plan(
    *,
    symbol: str,
    direction: str,
    quote: dict[str, Any] | None = None,
    now: datetime | None = None,
    session_override: str = "",
    limit_bps: float | None = None,
    stale_quote_seconds: float = 120.0,
    explicit_limit_price: float = 0.0,
    allow_market: bool = False,
    requested_order_type: str = "",
    requested_tif: str = "",
    requested_outside_rth: Any = None,
    requested_exchange: str = "",
    requested_include_overnight: Any = None,
) -> dict[str, Any]:
    session = infer_close_session(now, session_override=session_override)
    normalized_direction = _text(direction).lower()
    close_action = "SELL" if normalized_direction == "long" else "BUY" if normalized_direction == "short" else ""
    requested_type = _text(requested_order_type).upper()
    if requested_type in {"", "AUTO", "AUTO_SESSION_LIMIT", "MARKETABLE_LIMIT", "LIMIT"}:
        requested_type = "LMT"
    if requested_type == "MKT" and not allow_market:
        requested_type = "LMT"
    if requested_type not in {"LMT", "MKT"}:
        requested_type = "LMT"
    if not session.tradable:
        return {
            "ok": False,
            "error": session.reason or "session_not_tradable",
            "symbol": _text(symbol).upper(),
            "session": session.name,
            "tradable": False,
            "order_type": requested_type,
            "close_action": close_action,
        }
    if requested_type == "MKT":
        return {
            "ok": True,
            "symbol": _text(symbol).upper(),
            "session": session.name,
            "tradable": True,
            "order_type": "MKT",
            "limit_price": 0.0,
            "outside_rth": _bool(requested_outside_rth, session.outside_rth),
            "tif": _text(requested_tif).upper() or session.tif,
            "exchange": _text(requested_exchange).upper() or session.exchange,
            "include_overnight": _bool(requested_include_overnight, session.include_overnight),
            "close_action": close_action,
            "allow_market": True,
        }

    explicit = _safe_float(explicit_limit_price, 0.0)
    selected_bps = max(0.0, float(limit_bps if limit_bps is not None else session.default_bps))
    q = quote_from_sources(quote)
    now_ms = int((now or datetime.now(ET)).astimezone(ET).timestamp() * 1000)
    updated_ms = int(_safe_float(q.get("updated_ms"), 0.0) or 0)
    if updated_ms > 0 and stale_quote_seconds > 0:
        age_s = max(0.0, (now_ms - updated_ms) / 1000.0)
        if age_s > max(1.0, float(stale_quote_seconds)):
            return {
                "ok": False,
                "error": "close_quote_stale",
                "symbol": _text(symbol).upper(),
                "session": session.name,
                "tradable": True,
                "quote": q,
                "quote_age_s": round(age_s, 3),
                "max_quote_age_s": float(stale_quote_seconds),
                "order_type": "LMT",
                "close_action": close_action,
            }
    if explicit > 0:
        price = explicit
        reference = explicit
        reference_source = "explicit_limit_price"
        offset = 0.0
    else:
        bid = _safe_float(q.get("bid"), 0.0)
        ask = _safe_float(q.get("ask"), 0.0)
        last = _safe_float(q.get("last_price"), 0.0)
        if close_action == "BUY":
            reference = ask if ask > 0 else last
            reference_source = "ask" if ask > 0 else "last_price"
            offset = max(0.01, reference * selected_bps / 10000.0) if reference > 0 else 0.0
            price = reference + offset
        elif close_action == "SELL":
            reference = bid if bid > 0 else last
            reference_source = "bid" if bid > 0 else "last_price"
            offset = max(0.01, reference * selected_bps / 10000.0) if reference > 0 else 0.0
            price = reference - offset
        else:
            reference = 0.0
            reference_source = ""
            offset = 0.0
            price = 0.0
    if price <= 0:
        return {
            "ok": False,
            "error": "close_limit_price_unavailable",
            "symbol": _text(symbol).upper(),
            "session": session.name,
            "tradable": True,
            "quote": q,
            "order_type": "LMT",
            "close_action": close_action,
        }
    return {
        "ok": True,
        "symbol": _text(symbol).upper(),
        "session": session.name,
        "tradable": True,
        "order_type": "LMT",
        "limit_price": round(price, 2),
        "outside_rth": _bool(requested_outside_rth, session.outside_rth),
        "tif": _text(requested_tif).upper() or session.tif,
        "exchange": _text(requested_exchange).upper() or session.exchange,
        "include_overnight": _bool(requested_include_overnight, session.include_overnight),
        "close_action": close_action,
        "limit_bps": selected_bps,
        "reference_price": round(reference, 4),
        "reference_source": reference_source,
        "offset": round(offset, 4),
        "quote": q,
        "allow_market": False,
    }

