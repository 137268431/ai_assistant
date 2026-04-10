"""
IBKR OCO Bracket 下单
- 一次性提交 Entry + Take Profit + Stop Loss
- 处理 CP API 二次确认 (reply)
- 支持 LMT, MKT, STP 订单类型
"""

import os
import time
import logging
import requests
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta

from ibkr_compute.gateway.cookie_store import load_cookies, save_cookies

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")
PAPER_ACCOUNT_ID = os.environ.get("IBKR_PAPER_ACCOUNT_ID", "")
ET = timezone(timedelta(hours=-4))


class OrderPlacer:
    def __init__(self, gateway_url: str = None, account_id: str = None, pb_client=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self._session = requests.Session()
        self._session.verify = False
        load_cookies(self._session)
        self._order_count = 0

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def get_active_account_id(self, use_paper: bool = False) -> str:
        if use_paper:
            return PAPER_ACCOUNT_ID or self.account_id
        return self.account_id

    def place_bracket_order(self, conid: int, symbol: str, direction: str,
                            quantity: int, entry_price: float,
                            take_profit_price: float, stop_loss_price: float,
                            use_paper: bool = False, signal_id: str = "",
                            entry_order_type: str = "LMT") -> Dict[str, Any]:
        acct_id = self.get_active_account_id(use_paper)
        if not acct_id:
            return {"ok": False, "error": "No account ID configured"}

        et_now = datetime.now(ET)
        ts = et_now.strftime("%Y%m%d_%H%M%S")
        side = "BUY" if direction == "long" else "SELL"
        close_side = "SELL" if direction == "long" else "BUY"

        entry_coid = f"entry_{symbol}_{direction}_{ts}"
        tp_coid = f"tp_{symbol}_{direction}_{ts}"
        sl_coid = f"sl_{symbol}_{direction}_{ts}"

        orders = [
            {
                "acctId": acct_id,
                "conid": conid,
                "cOID": entry_coid,
                "orderType": entry_order_type,
                "side": side,
                "quantity": quantity,
                "tif": "DAY",
            },
            {
                "acctId": acct_id,
                "conid": conid,
                "cOID": tp_coid,
                "parentId": entry_coid,
                "orderType": "LMT",
                "side": close_side,
                "quantity": quantity,
                "price": take_profit_price,
                "tif": "GTC",
            },
            {
                "acctId": acct_id,
                "conid": conid,
                "cOID": sl_coid,
                "parentId": entry_coid,
                "orderType": "STP",
                "side": close_side,
                "quantity": quantity,
                "price": stop_loss_price,
                "tif": "GTC",
            },
        ]

        if entry_order_type != "MKT":
            orders[0]["price"] = entry_price

        logger.info("Placing bracket order: %s %s %d@%.2f TP=%.2f SL=%.2f (acct=%s)",
                     symbol, direction, quantity, entry_price,
                     take_profit_price, stop_loss_price, acct_id)

        result = self._submit_orders(acct_id, orders)
        if result.get("ok"):
            result["order_ids"] = self._resolve_expected_order_ids(
                expected_coids=[entry_coid, tp_coid, sl_coid],
                initial_order_ids=result.get("order_ids", []),
            )

        if result.get("ok"):
            self._order_count += 1
            self._log_order_to_pb(
                symbol=symbol, conid=conid, direction=direction,
                entry_coid=entry_coid, tp_coid=tp_coid, sl_coid=sl_coid,
                entry_price=entry_price, tp_price=take_profit_price,
                sl_price=stop_loss_price, quantity=quantity,
                signal_id=signal_id, account=acct_id,
                order_ids=result.get("order_ids", []),
            )

        return {
            "ok": result.get("ok", False),
            "entry_coid": entry_coid,
            "tp_coid": tp_coid,
            "sl_coid": sl_coid,
            "bracket_group": entry_coid,
            "order_ids": result.get("order_ids", []),
            "error": result.get("error"),
            "raw_response": result.get("raw"),
        }

    def place_market_close(self, conid: int, symbol: str, direction: str,
                           quantity: int, use_paper: bool = False) -> Dict[str, Any]:
        acct_id = self.get_active_account_id(use_paper)
        side = "SELL" if direction == "long" else "BUY"
        et_now = datetime.now(ET)
        coid = f"close_{symbol}_{direction}_{et_now.strftime('%Y%m%d_%H%M%S')}"

        orders = [{
            "acctId": acct_id,
            "conid": conid,
            "cOID": coid,
            "orderType": "MKT",
            "side": side,
            "quantity": quantity,
            "tif": "DAY",
        }]

        return self._submit_orders(acct_id, orders)

    def _extract_response_order_ids(self, payload: Any) -> List[str]:
        order_ids: List[str] = []
        seen = set()

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                order_id = str(node.get("order_id") or node.get("orderId") or "").strip()
                if order_id and order_id not in seen:
                    seen.add(order_id)
                    order_ids.append(order_id)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(payload)
        return order_ids

    def _fetch_live_orders(self, *, force: bool = True, timeout: int = 15) -> List[Dict[str, Any]]:
        load_cookies(self._session)
        resp = self._session.get(
            self._api_url("/iserver/account/orders"),
            params={"force": "true" if force else "false"},
            timeout=timeout,
        )
        resp.raise_for_status()
        save_cookies(self._session)
        if not resp.text:
            return []
        data = resp.json()
        if isinstance(data, dict):
            orders = data.get("orders", [])
        elif isinstance(data, list):
            orders = data
        else:
            orders = []
        return orders if isinstance(orders, list) else []

    def _lookup_order_ids_by_coid(
        self,
        expected_coids: List[str],
        *,
        retries: int = 4,
        retry_delay: float = 0.75,
    ) -> List[str]:
        normalized_coids = [str(item or "").strip() for item in (expected_coids or [])]
        if not any(normalized_coids):
            return []

        resolved = {coid: "" for coid in normalized_coids if coid}
        attempts = max(1, int(retries or 1))
        for attempt in range(attempts):
            try:
                orders = self._fetch_live_orders(force=True, timeout=15)
            except Exception as exc:
                logger.warning("Live order lookup by cOID failed: %s", exc)
                break

            for order in orders:
                if not isinstance(order, dict):
                    continue
                coid = str(
                    order.get("cOID")
                    or order.get("coid")
                    or order.get("order_ref")
                    or order.get("orderRef")
                    or ""
                ).strip()
                order_id = str(order.get("orderId") or order.get("order_id") or order.get("id") or "").strip()
                if coid in resolved and order_id:
                    resolved[coid] = order_id

            if all(resolved.get(coid) for coid in normalized_coids if coid):
                break
            if attempt + 1 < attempts:
                time.sleep(max(0.0, float(retry_delay or 0.0)))

        return [resolved.get(coid, "") if coid else "" for coid in normalized_coids]

    def _resolve_expected_order_ids(self, expected_coids: List[str], initial_order_ids: List[str]) -> List[str]:
        clean_initial_ids = [str(item or "").strip() for item in (initial_order_ids or [])]
        normalized_coids = [str(item or "").strip() for item in (expected_coids or [])]
        if not any(normalized_coids):
            return [item for item in clean_initial_ids if item]

        resolved_ids = self._lookup_order_ids_by_coid(normalized_coids)
        for index in range(len(normalized_coids)):
            if resolved_ids[index]:
                continue
            if index < len(clean_initial_ids) and clean_initial_ids[index]:
                resolved_ids[index] = clean_initial_ids[index]

        compact_ids = [item for item in resolved_ids if item]
        if compact_ids and len(compact_ids) < len([item for item in normalized_coids if item]):
            logger.warning(
                "Bracket order ids partially resolved: expected=%s resolved=%s initial=%s",
                normalized_coids,
                resolved_ids,
                clean_initial_ids,
            )
        if compact_ids:
            last_non_empty_index = max(index for index, value in enumerate(resolved_ids) if value)
            return resolved_ids[:last_non_empty_index + 1]
        return [item for item in clean_initial_ids if item]

    def _submit_orders(self, acct_id: str, orders: List[Dict]) -> Dict[str, Any]:
        url = self._api_url(f"/iserver/account/{acct_id}/orders")

        try:
            load_cookies(self._session)
            resp = self._session.post(url, json={"orders": orders}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            save_cookies(self._session)

            if isinstance(data, list) and data:
                first = data[0]
                if first.get("id"):
                    reply_id = first["id"]
                    logger.info("Order requires confirmation, replying to id=%s", reply_id)
                    return self._confirm_order(reply_id)
                order_ids = self._extract_response_order_ids(data)
                if order_ids:
                    return {"ok": True, "order_ids": order_ids, "raw": data}
                elif first.get("error"):
                    return {"ok": False, "error": first.get("error"), "raw": data}

            return {"ok": True, "raw": data}

        except Exception as e:
            logger.error("Order submission failed: %s", e)
            return {"ok": False, "error": str(e)}

    def _confirm_order(self, reply_id: str) -> Dict[str, Any]:
        url = self._api_url(f"/iserver/reply/{reply_id}")
        try:
            load_cookies(self._session)
            resp = self._session.post(url, json={"confirmed": True}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            save_cookies(self._session)

            if isinstance(data, list) and data:
                first = data[0]
                if first.get("id"):
                    return self._confirm_order(first["id"])
                order_ids = self._extract_response_order_ids(data)
                if order_ids:
                    return {"ok": True, "order_ids": order_ids, "raw": data}
                elif first.get("error"):
                    return {"ok": False, "error": first.get("error"), "raw": data}

            return {"ok": True, "raw": data}
        except Exception as e:
            logger.error("Order confirmation failed: %s", e)
            return {"ok": False, "error": str(e)}

    def _log_order_to_pb(self, **kwargs):
        if not self.pb_client:
            return
        try:
            et_now = datetime.now(ET)
            us_time = et_now.strftime("%Y-%m-%d %H:%M:%S")
            direction = kwargs.get("direction")
            quantity = kwargs.get("quantity")
            entry_unique_id = kwargs.get("entry_coid")
            tp_unique_id = kwargs.get("tp_coid")
            sl_unique_id = kwargs.get("sl_coid")
            symbol = kwargs.get("symbol")
            signal_id = kwargs.get("signal_id", "")
            order_ids = [str(item or "").strip() for item in (kwargs.get("order_ids") or [])]
            entry_order_id = order_ids[0] if len(order_ids) > 0 else ""
            tp_order_id = order_ids[1] if len(order_ids) > 1 else ""
            sl_order_id = order_ids[2] if len(order_ids) > 2 else ""

            if hasattr(self.pb_client, "upsert_order"):
                base_payload = {
                    "symbol": symbol,
                    "direction": direction,
                    "position_side": direction,
                    "trade_group_id": entry_unique_id,
                    "entry_order_unique_id": entry_unique_id,
                    "quantity": quantity,
                    "signal_id": signal_id,
                    "us_time": us_time,
                    "bar_time_ms": int(et_now.timestamp() * 1000),
                }
                self.pb_client.upsert_order({
                    **base_payload,
                    "unique_id": entry_unique_id,
                    "order_id": entry_order_id,
                    "broker_order_id": entry_order_id,
                    "order_type": "Entry",
                    "role": "entry",
                    "relation_status": "active",
                    "limit_price": kwargs.get("entry_price"),
                    "status": "Submitted",
                    "filled_qty": 0,
                    "fill_price": 0,
                    "tp_price": kwargs.get("tp_price"),
                    "sl_price": kwargs.get("sl_price"),
                })
                self.pb_client.upsert_order({
                    **base_payload,
                    "unique_id": tp_unique_id,
                    "order_id": tp_order_id,
                    "broker_order_id": tp_order_id,
                    "order_type": "TakeProfit",
                    "role": "take_profit",
                    "relation_status": "planned",
                    "parent_order_unique_id": entry_unique_id,
                    "sibling_order_unique_id": sl_unique_id,
                    "limit_price": kwargs.get("tp_price"),
                    "status": "Init",
                    "filled_qty": 0,
                    "fill_price": 0,
                })
                self.pb_client.upsert_order({
                    **base_payload,
                    "unique_id": sl_unique_id,
                    "order_id": sl_order_id,
                    "broker_order_id": sl_order_id,
                    "order_type": "StopLoss",
                    "role": "stop_loss",
                    "relation_status": "planned",
                    "parent_order_unique_id": entry_unique_id,
                    "sibling_order_unique_id": tp_unique_id,
                    "limit_price": kwargs.get("sl_price"),
                    "status": "Init",
                    "filled_qty": 0,
                    "fill_price": 0,
                })
        except Exception as e:
            logger.debug("PB order log failed: %s", e)

    def status(self) -> dict:
        return {
            "account_id": self.account_id,
            "total_orders": self._order_count,
        }
