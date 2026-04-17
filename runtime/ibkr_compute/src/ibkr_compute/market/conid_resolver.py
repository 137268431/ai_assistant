from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional

from ibkr_compute.broker import BrokerAdapter

logger = logging.getLogger(__name__)

CONTRACT_SEARCH_LIMIT = 12


class ConidResolver:
    def __init__(self, gateway_url: str = None, pb_client=None, broker: BrokerAdapter | None = None):
        self.pb_client = pb_client
        self.broker = broker or BrokerAdapter()
        self._cache: Dict[str, int] = {}
        self._validated_symbols: set[str] = set()

    def load_cache_from_pb(self):
        if not self.pb_client:
            return
        try:
            items = self.pb_client.get_records("ibkr_conid_cache", per_page=500)
        except Exception as exc:
            logger.debug("Failed to load conid cache from PB: %s", exc)
            return
        for item in items or []:
            symbol = str(item.get("symbol") or "").strip().upper()
            conid = int(item.get("conid") or 0)
            if symbol and conid > 0:
                self._cache[symbol] = conid

    def _save_to_pb(self, symbol: str, conid: int):
        if not self.pb_client or not symbol or int(conid or 0) <= 0:
            return
        try:
            existing = self.pb_client.get_records(
                "ibkr_conid_cache",
                filter=f'symbol = "{symbol}"',
                per_page=1,
            )
            payload = {"symbol": symbol, "conid": int(conid)}
            if existing:
                self.pb_client.update_record("ibkr_conid_cache", existing[0]["id"], payload)
            else:
                self.pb_client.create_record("ibkr_conid_cache", payload)
        except Exception as exc:
            logger.debug("Failed to save conid cache %s=%s: %s", symbol, conid, exc)

    def resolve(self, symbol: str) -> Optional[int]:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return None
        cached = self._cache.get(normalized)
        if cached:
            if normalized in self._validated_symbols:
                return int(cached)
            try:
                contract = self.broker.resolve_contract(symbol=normalized, conid=int(cached))
            except Exception as exc:
                logger.warning("Cached conid validation failed for %s=%s: %s", normalized, cached, exc)
                contract = None
            resolved_conid = int((contract or {}).get("conid") or 0)
            if resolved_conid > 0:
                self._validated_symbols.add(normalized)
                if resolved_conid != int(cached):
                    logger.warning(
                        "Corrected cached conid for %s: %s -> %s",
                        normalized,
                        cached,
                        resolved_conid,
                    )
                    self._cache[normalized] = resolved_conid
                    self._save_to_pb(normalized, resolved_conid)
                return resolved_conid
            self._cache.pop(normalized, None)
        try:
            contract = self.broker.resolve_contract(symbol=normalized)
        except Exception as exc:
            logger.warning("Conid resolve failed for %s: %s", normalized, exc)
            return None
        conid = int((contract or {}).get("conid") or 0)
        if conid > 0:
            self._cache[normalized] = conid
            self._validated_symbols.add(normalized)
            self._save_to_pb(normalized, conid)
            return conid
        return None

    def resolve_bulk(self, symbols: Iterable[str]) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for symbol in symbols or []:
            normalized = str(symbol or "").strip().upper()
            if not normalized or normalized in result:
                continue
            conid = self.resolve(normalized)
            if conid:
                result[normalized] = int(conid)
        return result

    def search_contracts(self, query: str, limit: int = CONTRACT_SEARCH_LIMIT) -> List[dict]:
        try:
            items = self.broker.search_contracts(query, limit=limit)
        except Exception as exc:
            logger.warning("Contract search failed for %s: %s", query, exc)
            return []

        normalized_query = str(query or "").strip().upper()
        for item in items or []:
            symbol = str(item.get("symbol") or "").strip().upper()
            conid = int(item.get("conid") or 0)
            if symbol and conid > 0 and symbol == normalized_query:
                self._cache[symbol] = conid
                self._save_to_pb(symbol, conid)
        return list(items or [])

    def get_reverse(self, conid: int) -> Optional[str]:
        target = int(conid or 0)
        if target <= 0:
            return None
        for symbol, cached_conid in self._cache.items():
            if int(cached_conid) == target:
                return symbol
        try:
            contract = self.broker.resolve_contract(conid=target)
        except Exception:
            contract = None
        symbol = str((contract or {}).get("symbol") or "").strip().upper()
        if symbol:
            self._cache[symbol] = target
            self._save_to_pb(symbol, target)
            return symbol
        return None
