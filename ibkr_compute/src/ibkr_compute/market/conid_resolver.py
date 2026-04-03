"""
Symbol → conid 映射解析 & 缓存
- 首次查询 IBKR secdef/search API
- 缓存到 PocketBase ibkr_conid_cache collection
"""

import os
import logging
import requests
from typing import Optional, Dict, Iterable, List

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
STOCK_SEARCH_BATCH_SIZE = max(1, int(os.environ.get("IBKR_CONID_BATCH_SIZE", "25")))


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
        conid = self._search_stocks(symbol)
        if conid:
            return conid
        return self._search_secdef(symbol)

    def _search_stocks(self, symbol: str) -> Optional[int]:
        try:
            resp = self._session.get(
                self._api_url("/trsrv/stocks"),
                params={"symbols": symbol},
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                return None

            items = payload.get(symbol) or payload.get(symbol.upper()) or []
            return self._pick_best_stock_conid(items)
        except Exception as e:
            logger.error("IBKR stocks search failed for %s: %s", symbol, e)
            return None

    def _score_contract(self, contract: dict) -> int:
        exchange = str(contract.get("exchange") or "").upper()
        is_us = bool(contract.get("isUS"))
        score = 0
        if is_us:
            score += 100
        if exchange in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "SMART"}:
            score += 50
        if exchange in {"NASDAQ", "NYSE"}:
            score += 10
        return score

    def _pick_best_stock_conid(self, items) -> Optional[int]:
        best_conid = None
        best_score = -1
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if str(item.get("assetClass") or "").upper() != "STK":
                continue

            contracts = item.get("contracts")
            for contract in contracts if isinstance(contracts, list) else []:
                if not isinstance(contract, dict):
                    continue
                conid = contract.get("conid")
                if not conid:
                    continue

                score = self._score_contract(contract)
                if score > best_score:
                    best_score = score
                    best_conid = int(conid)
        return best_conid

    def _batched(self, symbols: Iterable[str], batch_size: int) -> List[List[str]]:
        batch = []
        output = []
        for symbol in symbols:
            normalized = str(symbol or "").strip().upper()
            if not normalized:
                continue
            batch.append(normalized)
            if len(batch) >= batch_size:
                output.append(batch)
                batch = []
        if batch:
            output.append(batch)
        return output

    def _search_stocks_bulk(self, symbols: Iterable[str]) -> Dict[str, int]:
        resolved = {}
        for batch in self._batched(symbols, STOCK_SEARCH_BATCH_SIZE):
            try:
                resp = self._session.get(
                    self._api_url("/trsrv/stocks"),
                    params={"symbols": ",".join(batch)},
                    timeout=20,
                )
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict):
                    continue

                for symbol in batch:
                    best_conid = self._pick_best_stock_conid(payload.get(symbol) or payload.get(symbol.upper()) or [])
                    if best_conid:
                        resolved[symbol] = best_conid
            except Exception as e:
                logger.warning("IBKR bulk stocks search failed for %s: %s", ",".join(batch), e)
        return resolved

    def _search_secdef(self, symbol: str) -> Optional[int]:
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

            normalized_symbol = str(symbol or "").strip().upper()
            best_stk_exact = None
            best_stk = None
            best_any_exact = None

            for item in results:
                if not isinstance(item, dict):
                    continue

                description = str(item.get("description") or "").strip().upper()
                item_symbol = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
                company_name = str(item.get("companyName") or item.get("companyHeader") or "").strip().upper()
                sections = item.get("sections", [])
                sections = sections if isinstance(sections, list) else []
                has_stk = any(str(sec.get("secType") or "").upper() == "STK" for sec in sections if isinstance(sec, dict))
                exact_match = item_symbol == normalized_symbol or description == normalized_symbol or company_name == normalized_symbol
                conid = int(item.get("conid", 0)) or None
                if not conid:
                    continue

                if has_stk and exact_match:
                    best_stk_exact = conid
                    break
                if has_stk and best_stk is None:
                    best_stk = conid
                if exact_match and best_any_exact is None:
                    best_any_exact = conid

            if best_stk_exact:
                return best_stk_exact
            if best_stk:
                return best_stk
            if best_any_exact:
                return best_any_exact
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
        normalized_symbols = []
        seen = set()
        for symbol in symbols or []:
            normalized = str(symbol or "").strip().upper()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            normalized_symbols.append(normalized)

        result = {}
        bulk_matches = self._search_stocks_bulk(normalized_symbols)
        for symbol in normalized_symbols:
            matched_conid = bulk_matches.get(symbol)
            cached_conid = self._cache.get(symbol)

            if matched_conid:
                if cached_conid != matched_conid:
                    if cached_conid:
                        logger.warning(
                            "Conid cache mismatch for %s: cached=%s live=%s; refreshing cache",
                            symbol,
                            cached_conid,
                            matched_conid,
                        )
                    self._cache[symbol] = matched_conid
                    self._save_to_pb(symbol, matched_conid)
                result[symbol] = matched_conid
                continue

            conid = cached_conid or self.resolve(symbol)
            if conid:
                result[symbol] = int(conid)
        return result

    def get_reverse(self, conid: int) -> Optional[str]:
        for symbol, cid in self._cache.items():
            if cid == conid:
                return symbol
        return None
