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
                elif first.get("order_id"):
                    order_ids = [item.get("order_id") for item in data if item.get("order_id")]
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
                elif first.get("order_id"):
                    order_ids = [item.get("order_id") for item in data if item.get("order_id")]
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
            order_ids = [str(item).strip() for item in (kwargs.get("order_ids") or []) if str(item).strip()]
            entry_order_id = order_ids[0] if len(order_ids) > 0 else ""
            tp_order_id = order_ids[1] if len(order_ids) > 1 else ""
            sl_order_id = order_ids[2] if len(order_ids) > 2 else ""

            # 兼容旧表，避免现网依赖被一次性切断。
            self.pb_client.create_record("ibkr_orders", {
                "symbol": symbol,
                "conid": kwargs.get("conid"),
                "side": "BUY" if direction == "long" else "SELL",
                "orderType": "BRACKET",
                "cOID": entry_unique_id,
                "orderId": entry_order_id,
                "price": kwargs.get("entry_price"),
                "quantity": quantity,
                "status": "submitted",
                "parentId": "",
                "bracket_group": entry_unique_id,
                "signal_id": signal_id,
                "account": kwargs.get("account"),
                "tp_price": kwargs.get("tp_price"),
                "sl_price": kwargs.get("sl_price"),
                "us_time": us_time,
            })

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
