"""
Symbol → conid 映射解析 & 缓存
- 首次查询 IBKR secdef/search API
- 缓存到 PocketBase ibkr_conid_cache collection
"""

import os
import logging
import requests
from typing import Optional, Dict, Iterable, List

from ibkr_compute.gateway.cookie_store import load_cookies, save_cookies

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
STOCK_SEARCH_BATCH_SIZE = max(1, int(os.environ.get("IBKR_CONID_BATCH_SIZE", "25")))
CONTRACT_SEARCH_LIMIT = max(1, int(os.environ.get("IBKR_CONTRACT_SEARCH_LIMIT", "12")))
INDEX_HINTS = {
    "VIX": {
        "preferred_sec_types": {"IND", "INDEX"},
        "preferred_exchanges": {"CBOE", "CFE"},
    },
}


class ConidResolver:
    def __init__(self, gateway_url: str = None, pb_client=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.pb_client = pb_client
        self._cache: Dict[str, int] = {}
        self._session = requests.Session()
        self._session.verify = False
        load_cookies(self._session)

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
        hint = INDEX_HINTS.get(symbol)
        if hint:
            conid = self._search_secdef(symbol, hint=hint)
            if conid:
                return conid
        conid = self._search_stocks(symbol)
        if conid:
            return conid
        return self._search_secdef(symbol, hint=hint)

    def _search_stocks(self, symbol: str) -> Optional[int]:
        try:
            load_cookies(self._session)
            resp = self._session.get(
                self._api_url("/trsrv/stocks"),
                params={"symbols": symbol},
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
            save_cookies(self._session)
            if not isinstance(payload, dict):
                return None

            items = payload.get(symbol) or payload.get(symbol.upper()) or []
            return self._pick_best_stock_conid(items)
        except Exception as e:
            logger.error("IBKR stocks search failed for %s: %s", symbol, e)
            return None

    @staticmethod
    def _coerce_conid(value) -> Optional[int]:
        try:
            conid = int(value)
        except (TypeError, ValueError):
            return None
        return conid if conid > 0 else None

    @staticmethod
    def _first_text(*values) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _normalize_sec_type(value: str) -> str:
        sec_type = str(value or "").strip().upper()
        if sec_type == "INDEX":
            return "IND"
        return sec_type

    def _collect_sec_types(self, item: dict, *fallbacks: str) -> List[str]:
        sec_types: List[str] = []

        def add(value):
            normalized = self._normalize_sec_type(value)
            if normalized and normalized not in sec_types:
                sec_types.append(normalized)

        for fallback in fallbacks:
            add(fallback)

        add(item.get("assetClass"))
        sections = item.get("sections")
        for section in sections if isinstance(sections, list) else []:
            if not isinstance(section, dict):
                continue
            add(section.get("secType"))
            add(section.get("assetClass"))

        return sec_types

    def _asset_class_from_sec_types(self, sec_types: List[str], fallback: str = "") -> str:
        if sec_types:
            return str(sec_types[0]).upper()
        return self._normalize_sec_type(fallback)

    def _build_stock_candidate(self, symbol: str, item: dict, contract: dict) -> Optional[dict]:
        conid = self._coerce_conid(contract.get("conid"))
        if not conid:
            return None

        sec_types = self._collect_sec_types(item, "STK")
        exchange = self._first_text(
            contract.get("exchange"),
            item.get("listingExchange"),
            item.get("exchange"),
            item.get("exchangeName"),
        ).upper()
        company_name = self._first_text(
            contract.get("name"),
            item.get("name"),
            item.get("companyName"),
            item.get("companyHeader"),
            item.get("chineseName"),
        )
        description = self._first_text(
            item.get("name"),
            item.get("companyName"),
            contract.get("description"),
            company_name,
        )
        normalized_symbol = self._first_text(
            contract.get("symbol"),
            item.get("symbol"),
            item.get("ticker"),
            symbol,
        ).upper()
        is_us = bool(contract.get("isUS"))
        score = self._score_contract(contract)
        if normalized_symbol == str(symbol or "").strip().upper():
            score += 180

        return {
            "symbol": normalized_symbol,
            "conid": conid,
            "exchange": exchange,
            "asset_class": self._asset_class_from_sec_types(sec_types, item.get("assetClass")),
            "sec_types": sec_types,
            "company_name": company_name,
            "description": description,
            "is_us": is_us,
            "score": score,
            "sources": ["trsrv/stocks"],
        }

    def _build_secdef_candidate(self, query: str, item: dict, hint: Optional[dict] = None) -> Optional[dict]:
        score, conid = self._score_secdef_item(query, item, hint=hint)
        if not conid:
            return None

        sec_types = self._collect_sec_types(item)
        exchange = self._first_text(
            item.get("listingExchange"),
            item.get("exchange"),
            item.get("exchangeName"),
        ).upper()
        symbol = self._first_text(item.get("symbol"), item.get("ticker"), query).upper()
        company_name = self._first_text(
            item.get("companyName"),
            item.get("companyHeader"),
            item.get("name"),
        )
        description = self._first_text(
            item.get("description"),
            item.get("name"),
            company_name,
            exchange,
        )
        is_us = bool(item.get("isUS"))
        if not is_us and exchange in {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "SMART"}:
            is_us = True

        return {
            "symbol": symbol,
            "conid": conid,
            "exchange": exchange,
            "asset_class": self._asset_class_from_sec_types(sec_types, item.get("assetClass")),
            "sec_types": sec_types,
            "company_name": company_name,
            "description": description,
            "is_us": is_us,
            "score": int(score),
            "sources": ["iserver/secdef/search"],
        }

    def _search_stocks_candidates(self, symbol: str) -> List[dict]:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return []

        try:
            load_cookies(self._session)
            resp = self._session.get(
                self._api_url("/trsrv/stocks"),
                params={"symbols": normalized_symbol},
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
            save_cookies(self._session)
            if not isinstance(payload, dict):
                return []

            items = payload.get(normalized_symbol) or payload.get(normalized_symbol.upper()) or []
            candidates = []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                contracts = item.get("contracts")
                for contract in contracts if isinstance(contracts, list) else []:
                    if not isinstance(contract, dict):
                        continue
                    candidate = self._build_stock_candidate(normalized_symbol, item, contract)
                    if candidate:
                        candidates.append(candidate)
            return candidates
        except Exception as e:
            logger.warning("IBKR stocks candidate search failed for %s: %s", normalized_symbol, e)
            return []

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

    def _score_sort_key(self, query: str, candidate: dict):
        normalized_query = str(query or "").strip().upper()
        symbol = str(candidate.get("symbol") or "").strip().upper()
        sec_types = {
            self._normalize_sec_type(value)
            for value in (candidate.get("sec_types") or [])
            if str(value or "").strip()
        }
        equity_like = 1 if sec_types & {"STK", "ETF"} else 0
        return (
            1 if symbol == normalized_query else 0,
            equity_like,
            1 if candidate.get("is_us") else 0,
            float(candidate.get("score") or 0.0),
            1 if str(candidate.get("exchange") or "").strip().upper() in {"NASDAQ", "NYSE"} else 0,
            symbol,
        )

    def _merge_candidates(self, query: str, candidates: List[dict], limit: int) -> List[dict]:
        merged: Dict[int, dict] = {}
        for item in candidates:
            if not isinstance(item, dict):
                continue
            conid = self._coerce_conid(item.get("conid"))
            if not conid:
                continue

            sec_types = []
            for sec_type in item.get("sec_types") or []:
                normalized = self._normalize_sec_type(sec_type)
                if normalized and normalized not in sec_types:
                    sec_types.append(normalized)

            prepared = {
                "symbol": str(item.get("symbol") or "").strip().upper(),
                "conid": conid,
                "exchange": str(item.get("exchange") or "").strip().upper(),
                "asset_class": self._normalize_sec_type(item.get("asset_class") or ""),
                "sec_types": sec_types,
                "company_name": str(item.get("company_name") or "").strip(),
                "description": str(item.get("description") or "").strip(),
                "is_us": bool(item.get("is_us")),
                "score": int(item.get("score") or 0),
                "sources": list(dict.fromkeys(
                    str(source).strip() for source in (item.get("sources") or []) if str(source).strip()
                )),
            }

            existing = merged.get(conid)
            if not existing:
                merged[conid] = prepared
                continue

            if self._score_sort_key(query, prepared) > self._score_sort_key(query, existing):
                existing["symbol"] = prepared["symbol"] or existing["symbol"]
                existing["exchange"] = prepared["exchange"] or existing["exchange"]
                existing["company_name"] = prepared["company_name"] or existing["company_name"]
                existing["description"] = prepared["description"] or existing["description"]
                existing["asset_class"] = prepared["asset_class"] or existing["asset_class"]

            existing["is_us"] = bool(existing["is_us"] or prepared["is_us"])
            existing["score"] = max(int(existing.get("score") or 0), int(prepared.get("score") or 0))
            for sec_type in prepared["sec_types"]:
                if sec_type not in existing["sec_types"]:
                    existing["sec_types"].append(sec_type)
            if not existing.get("asset_class"):
                existing["asset_class"] = self._asset_class_from_sec_types(existing["sec_types"])
            for source in prepared["sources"]:
                if source not in existing["sources"]:
                    existing["sources"].append(source)

        items = list(merged.values())
        items.sort(key=lambda candidate: self._score_sort_key(query, candidate), reverse=True)
        return items[: max(1, int(limit or CONTRACT_SEARCH_LIMIT))]

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
                load_cookies(self._session)
                resp = self._session.get(
                    self._api_url("/trsrv/stocks"),
                    params={"symbols": ",".join(batch)},
                    timeout=20,
                )
                resp.raise_for_status()
                payload = resp.json()
                save_cookies(self._session)
                if not isinstance(payload, dict):
                    continue

                for symbol in batch:
                    best_conid = self._pick_best_stock_conid(payload.get(symbol) or payload.get(symbol.upper()) or [])
                    if best_conid:
                        resolved[symbol] = best_conid
            except Exception as e:
                logger.warning("IBKR bulk stocks search failed for %s: %s", ",".join(batch), e)
        return resolved

    def _score_secdef_item(self, symbol: str, item: dict, hint: Optional[dict] = None) -> tuple[int, Optional[int]]:
        normalized_symbol = str(symbol or "").strip().upper()
        item_symbol = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
        description = str(item.get("description") or "").strip().upper()
        company_name = str(item.get("companyName") or item.get("companyHeader") or "").strip().upper()
        exchange = str(
            item.get("listingExchange")
            or item.get("exchange")
            or item.get("exchangeName")
            or ""
        ).strip().upper()
        sections = item.get("sections", [])
        sections = sections if isinstance(sections, list) else []
        sec_types = {
            str(sec.get("secType") or "").strip().upper()
            for sec in sections
            if isinstance(sec, dict) and str(sec.get("secType") or "").strip()
        }
        conid = int(item.get("conid", 0)) or None
        if not conid:
            return -1, None

        score = 0
        exact_match = item_symbol == normalized_symbol or description == normalized_symbol or company_name == normalized_symbol
        starts_match = description.startswith(normalized_symbol) or company_name.startswith(normalized_symbol)
        if exact_match:
            score += 500
        elif starts_match:
            score += 250

        preferred_sec_types = set((hint or {}).get("preferred_sec_types") or [])
        preferred_exchanges = set((hint or {}).get("preferred_exchanges") or [])
        if preferred_sec_types:
            if sec_types & preferred_sec_types:
                score += 300
            elif sec_types:
                score -= 40
        elif "STK" in sec_types:
            score += 200
        elif "ETF" in sec_types:
            score += 120

        if preferred_exchanges:
            exchange_matches = any(
                exchange == preferred_exchange
                or exchange.startswith(preferred_exchange)
                or preferred_exchange in exchange
                for preferred_exchange in preferred_exchanges
            )
            if exchange_matches:
                score += 120
            elif exchange:
                score -= 10

        if hint and "VOLATILITY INDEX" in description:
            score += 80

        if "OPT" in sec_types or "FOP" in sec_types or "WAR" in sec_types:
            score -= 80
        if "FUT" in sec_types and preferred_sec_types and "FUT" not in preferred_sec_types:
            score -= 60
        return score, conid

    def _search_secdef(self, symbol: str, hint: Optional[dict] = None) -> Optional[int]:
        preferred_sec_types = []
        for sec_type in list((hint or {}).get("preferred_sec_types") or []):
            normalized = str(sec_type or "").strip().upper()
            if not normalized:
                continue
            if normalized == "INDEX":
                normalized = "IND"
            if normalized not in preferred_sec_types:
                preferred_sec_types.append(normalized)
        if not preferred_sec_types:
            preferred_sec_types = ["STK"]

        attempts = [
            {"symbol": symbol, "name": False, "secType": sec_type}
            for sec_type in preferred_sec_types
        ]
        attempts.append({"symbol": symbol, "name": False})

        last_error = None
        for payload in attempts:
            try:
                load_cookies(self._session)
                resp = self._session.post(
                    self._api_url("/iserver/secdef/search"),
                    json=payload,
                    timeout=15,
                )
                resp.raise_for_status()
                results = resp.json()
                save_cookies(self._session)
            except Exception as e:
                last_error = e
                logger.debug("IBKR secdef POST search failed for %s payload=%s: %s", symbol, payload, e)
                continue

            if not results:
                continue

            best_score = -1
            best_conid = None
            for item in results:
                if not isinstance(item, dict):
                    continue
                score, conid = self._score_secdef_item(symbol, item, hint=hint)
                if conid and score > best_score:
                    best_score = score
                    best_conid = conid
            if best_conid:
                return best_conid
            if results and isinstance(results, list) and results[0].get("conid"):
                return int(results[0]["conid"])

        try:
            load_cookies(self._session)
            resp = self._session.get(
                self._api_url("/iserver/secdef/search"),
                params={"symbol": symbol},
                timeout=15,
            )
            resp.raise_for_status()
            results = resp.json()
            save_cookies(self._session)
            if results and isinstance(results, list):
                best_score = -1
                best_conid = None
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    score, conid = self._score_secdef_item(symbol, item, hint=hint)
                    if conid and score > best_score:
                        best_score = score
                        best_conid = conid
                if best_conid:
                    return best_conid
                if results[0].get("conid"):
                    return int(results[0]["conid"])
        except Exception as e:
            last_error = e

        if last_error:
            logger.error("IBKR secdef search failed for %s: %s", symbol, last_error)
        return None

    def _search_secdef_candidates(self, query: str, hint: Optional[dict] = None) -> List[dict]:
        text = str(query or "").strip()
        if not text:
            return []

        preferred_sec_types = []
        for sec_type in list((hint or {}).get("preferred_sec_types") or []):
            normalized = self._normalize_sec_type(sec_type)
            if normalized and normalized not in preferred_sec_types:
                preferred_sec_types.append(normalized)
        if not preferred_sec_types:
            preferred_sec_types = ["STK", "ETF", "IND"]

        attempts = []
        for sec_type in preferred_sec_types:
            attempts.append({"symbol": text, "name": False, "secType": sec_type})
        attempts.append({"symbol": text, "name": False})
        for sec_type in preferred_sec_types:
            attempts.append({"symbol": text, "name": True, "secType": sec_type})
        attempts.append({"symbol": text, "name": True})

        results: List[dict] = []
        seen_requests = set()
        for payload in attempts:
            signature = tuple(sorted(payload.items()))
            if signature in seen_requests:
                continue
            seen_requests.add(signature)
            try:
                load_cookies(self._session)
                resp = self._session.post(
                    self._api_url("/iserver/secdef/search"),
                    json=payload,
                    timeout=15,
                )
                resp.raise_for_status()
                batch = resp.json()
                save_cookies(self._session)
            except Exception as exc:
                logger.debug("IBKR secdef candidate search failed for %s payload=%s: %s", text, payload, exc)
                continue

            if not isinstance(batch, list):
                continue

            for item in batch:
                if not isinstance(item, dict):
                    continue
                candidate = self._build_secdef_candidate(text, item, hint=hint)
                if candidate:
                    results.append(candidate)

        if results:
            return results

        try:
            load_cookies(self._session)
            resp = self._session.get(
                self._api_url("/iserver/secdef/search"),
                params={"symbol": text},
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            save_cookies(self._session)
            if not isinstance(batch, list):
                return []

            fallback = []
            for item in batch:
                if not isinstance(item, dict):
                    continue
                candidate = self._build_secdef_candidate(text, item, hint=hint)
                if candidate:
                    fallback.append(candidate)
            return fallback
        except Exception as exc:
            logger.warning("IBKR secdef candidate GET search failed for %s: %s", text, exc)
            return []

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

        unresolved = [symbol for symbol in normalized_symbols if symbol not in result]
        if unresolved:
            logger.warning("Conid resolve missed symbols: %s", ",".join(unresolved))
        return result

    def search_contracts(self, query: str, limit: int = CONTRACT_SEARCH_LIMIT) -> List[dict]:
        text = str(query or "").strip()
        if not text:
            return []

        normalized_symbol = text.upper()
        hint = INDEX_HINTS.get(normalized_symbol)
        candidates: List[dict] = []

        candidates.extend(self._search_stocks_candidates(normalized_symbol))
        candidates.extend(self._search_secdef_candidates(text, hint=hint))

        merged = self._merge_candidates(text, candidates, limit)
        best = merged[0] if merged else None
        best_conid = self._coerce_conid(best.get("conid")) if isinstance(best, dict) else None
        if best_conid and str(best.get("symbol") or "").strip().upper() == normalized_symbol:
            self._cache[normalized_symbol] = best_conid
            self._save_to_pb(normalized_symbol, best_conid)

        return merged

    def get_reverse(self, conid: int) -> Optional[str]:
        for symbol, cid in self._cache.items():
            if cid == conid:
                return symbol
        return None
