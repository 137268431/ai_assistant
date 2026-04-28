"""
In-memory realtime quote snapshots fed by IBKR websocket market data.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from ibkr_compute.core.time_utils import ET
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)



class RealtimeQuoteBook:
    def __init__(
        self,
        conid_to_symbol: Dict[int, str] | None = None,
        prev_close_provider: Optional[Callable[[str], Optional[float]]] = None,
    ):
        self.conid_to_symbol = dict(conid_to_symbol or {})
        self.prev_close_provider = prev_close_provider
        self._quotes: Dict[str, dict] = {}
        self._lock = threading.RLock()
        self._tick_count = 0
        self._update_count = 0

    def set_symbol_map(self, conid_to_symbol: Dict[int, str]):
        with self._lock:
            self.conid_to_symbol = dict(conid_to_symbol or {})

    def remove_conids(self, conids):
        remove_symbols = set()
        with self._lock:
            reverse = {int(conid): symbol for conid, symbol in self.conid_to_symbol.items()}
            for conid in list(conids or []):
                try:
                    normalized = int(conid)
                except (TypeError, ValueError):
                    continue
                symbol = reverse.get(normalized)
                if symbol:
                    remove_symbols.add(str(symbol).upper())
            for symbol in remove_symbols:
                self._quotes.pop(symbol, None)

    def reset(self):
        with self._lock:
            self._quotes.clear()

    def on_tick(self, tick_data: dict):
        conid = tick_data.get("conid") or tick_data.get("conidEx")
        if not conid:
            return

        try:
            conid = int(conid)
        except (TypeError, ValueError):
            return

        symbol = self.conid_to_symbol.get(conid)
        if not symbol:
            return
        normalized_symbol = str(symbol).upper()

        last_price = self._extract_first_numeric(tick_data, ("31", "last_price", "last"))
        bid_price = self._extract_first_numeric(tick_data, ("84", "bid", "bid_price"))
        ask_price = self._extract_first_numeric(tick_data, ("86", "ask", "ask_price"))
        last_size = self._extract_first_numeric(tick_data, ("7059", "last_size", "lastSize", "size"))
        volume = self._extract_first_numeric(tick_data, ("87", "volume", "vol"))
        updated_at = self._extract_updated_at(tick_data)

        with self._lock:
            self._tick_count += 1
            quote = self._quotes.get(normalized_symbol)
            if quote is None:
                quote = {
                    "symbol": normalized_symbol,
                    "conid": conid,
                    "last_price": None,
                    "bid": None,
                    "ask": None,
                    "prev_close": None,
                    "day_change": None,
                    "day_change_pct": None,
                    "last_size": None,
                    "volume": None,
                    "last_update_ts": 0.0,
                    "last_update": "",
                }
                self._quotes[normalized_symbol] = quote

            quote["conid"] = conid
            if last_price is not None and last_price > 0:
                quote["last_price"] = last_price
            if bid_price is not None and bid_price > 0:
                quote["bid"] = bid_price
            if ask_price is not None and ask_price > 0:
                quote["ask"] = ask_price
            if last_size is not None and last_size >= 0:
                quote["last_size"] = last_size
            if volume is not None and volume >= 0:
                quote["volume"] = volume

            prev_close = quote.get("prev_close")
            if (prev_close is None or prev_close <= 0) and self.prev_close_provider:
                try:
                    resolved = self.prev_close_provider(normalized_symbol)
                except Exception as exc:
                    logger.debug("Realtime prev_close resolve failed for %s: %s", normalized_symbol, exc)
                    resolved = None
                if resolved is not None and resolved > 0:
                    prev_close = float(resolved)
                    quote["prev_close"] = prev_close

            current_last = quote.get("last_price")
            if current_last is not None and current_last > 0 and prev_close is not None and prev_close > 0:
                day_change = round(float(current_last) - float(prev_close), 4)
                quote["day_change"] = day_change
                quote["day_change_pct"] = round((day_change / float(prev_close)) * 100.0, 4)
            else:
                quote["day_change"] = None
                quote["day_change_pct"] = None

            quote["last_update_ts"] = updated_at
            quote["last_update"] = datetime.fromtimestamp(updated_at, ET).isoformat()
            self._update_count += 1

    def get_quote(self, symbol: str) -> Optional[dict]:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return None
        with self._lock:
            quote = dict(self._quotes.get(normalized_symbol) or {})
        if not quote:
            return None
        return self._build_public_quote(quote)

    def get_quotes(self, symbols=None) -> list[dict]:
        normalized_symbols = {
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        }
        with self._lock:
            items = [
                dict(quote)
                for symbol, quote in sorted(self._quotes.items())
                if not normalized_symbols or symbol in normalized_symbols
            ]
        return [self._build_public_quote(item) for item in items]

    def status(self) -> dict:
        now_ts = time.time()
        with self._lock:
            items = [dict(item) for item in self._quotes.values()]
            tick_count = self._tick_count
            update_count = self._update_count
        stale_quotes = 0
        active_quotes = {}
        for quote in items:
            public_quote = self._build_public_quote(quote, now_ts=now_ts)
            age_s = public_quote.get("quote_age_s")
            if age_s is not None and age_s > 600:
                stale_quotes += 1
            active_quotes[str(public_quote.get("symbol") or "").upper()] = public_quote
        return {
            "total_quotes": len(items),
            "stale_quotes": stale_quotes,
            "tick_count": tick_count,
            "update_count": update_count,
            "quotes": active_quotes,
        }

    def _build_public_quote(self, quote: dict, now_ts: Optional[float] = None) -> dict:
        current_ts = float(now_ts or time.time())
        last_update_ts = float(quote.get("last_update_ts", 0.0) or 0.0)
        quote_age_s = round(max(0.0, current_ts - last_update_ts), 1) if last_update_ts > 0 else None
        return {
            "symbol": str(quote.get("symbol") or "").upper(),
            "conid": int(quote.get("conid", 0) or 0),
            "last_price": self._round_or_none(quote.get("last_price")),
            "bid": self._round_or_none(quote.get("bid")),
            "ask": self._round_or_none(quote.get("ask")),
            "prev_close": self._round_or_none(quote.get("prev_close")),
            "day_change": self._round_or_none(quote.get("day_change")),
            "day_change_pct": self._round_or_none(quote.get("day_change_pct")),
            "last_size": self._round_or_none(quote.get("last_size")),
            "volume": self._round_or_none(quote.get("volume")),
            "quote_age_s": quote_age_s,
            "last_update": quote.get("last_update") or "",
        }

    def _extract_updated_at(self, tick_data: dict) -> float:
        updated_ms = tick_data.get("_updated")
        if updated_ms is not None:
            try:
                updated_ts = int(updated_ms) / 1000.0
                if updated_ts > 0:
                    return updated_ts
            except (TypeError, ValueError, OSError):
                pass
        return time.time()

    def _extract_first_numeric(self, payload: dict, keys) -> Optional[float]:
        for key in keys:
            value = self._coerce_numeric(payload.get(key))
            if value is not None:
                return value
        return None

    def _coerce_numeric(self, value) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip().replace(",", "")
        if not text:
            return None

        multiplier = 1.0
        upper = text.upper()
        if upper.endswith("K"):
            multiplier = 1_000.0
            text = text[:-1]
        elif upper.endswith("M"):
            multiplier = 1_000_000.0
            text = text[:-1]
        elif upper.endswith("B"):
            multiplier = 1_000_000_000.0
            text = text[:-1]

        text = text.replace("C", "").replace("H", "")
        try:
            return float(text) * multiplier
        except (TypeError, ValueError):
            return None

    def _round_or_none(self, value, digits: int = 4):
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:
            return None
        return round(number, digits)
