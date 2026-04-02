"""
Symbol → conid 映射解析 & 缓存
- 首次查询 IBKR secdef/search API
- 缓存到 PocketBase ibkr_conid_cache collection
"""

import os
import logging
import requests
from typing import Optional, Dict

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000")


class ConidResolver:
    def __init__(self, gateway_url: str = None, pb_client=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.pb_client = pb_client
        self._cache: Dict[str, int] = {}
        self._session = requests.Session()
        self._session.verify = False

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def load_cache_from_pb(self):
        if not self.pb_client:
            return
        try:
            records = self.pb_client.get_all_records("ibkr_conid_cache")
            for r in records:
                symbol = r.get("symbol", "").upper()
                conid = r.get("conid")
                if symbol and conid:
                    self._cache[symbol] = int(conid)
            logger.info("Loaded %d conid mappings from PB cache", len(self._cache))
        except Exception as e:
            logger.warning("Failed to load conid cache from PB: %s", e)

    def resolve(self, symbol: str) -> Optional[int]:
        symbol = symbol.upper()
        if symbol in self._cache:
            return self._cache[symbol]

        conid = self._search_ibkr(symbol)
        if conid:
            self._cache[symbol] = conid
            self._save_to_pb(symbol, conid)
            return conid

        logger.warning("Could not resolve conid for symbol: %s", symbol)
        return None

    def _search_ibkr(self, symbol: str) -> Optional[int]:
        try:
            resp = self._session.get(
                self._api_url("/iserver/secdef/search"),
                params={"symbol": symbol},
                timeout=15,
            )
            resp.raise_for_status()
            results = resp.json()

            if not results:
                return None

            for item in results:
                if item.get("description", "").upper() == symbol.upper():
                    sections = item.get("sections", [])
                    for sec in sections:
                        if sec.get("secType") == "STK":
                            return int(item.get("conid", 0)) or None
                    if item.get("conid"):
                        return int(item["conid"])

            if results and results[0].get("conid"):
                return int(results[0]["conid"])

            return None

        except Exception as e:
            logger.error("IBKR secdef search failed for %s: %s", symbol, e)
            return None

    def _save_to_pb(self, symbol: str, conid: int):
        if not self.pb_client:
            return
        try:
            existing = self.pb_client.get_records(
                "ibkr_conid_cache",
                filter=f'symbol = "{symbol}"',
                per_page=1,
            )
            if existing:
                self.pb_client.update_record("ibkr_conid_cache", existing[0]["id"], {
                    "conid": conid,
                })
            else:
                self.pb_client.create_record("ibkr_conid_cache", {
                    "symbol": symbol,
                    "conid": conid,
                })
        except Exception as e:
            logger.debug("Failed to save conid cache to PB: %s", e)

    def resolve_bulk(self, symbols: list) -> Dict[str, int]:
        result = {}
        for symbol in symbols:
            conid = self.resolve(symbol)
            if conid:
                result[symbol.upper()] = conid
        return result

    def get_reverse(self, conid: int) -> Optional[str]:
        for symbol, cid in self._cache.items():
            if cid == conid:
                return symbol
        return None
