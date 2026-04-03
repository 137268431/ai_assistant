"""
订单修改
- 动态 ATR 止损调整
- 修改价格/数量
- 取消订单
"""

import os
import logging
import requests
from typing import Dict, Any, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000")
ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")
ET = timezone(timedelta(hours=-4))


class OrderModifier:
    def __init__(self, gateway_url: str = None, account_id: str = None, pb_client=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self._session = requests.Session()
        self._session.verify = False

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def modify_order(self, order_id: str, updates: Dict[str, Any],
                     acct_id: str = None) -> Dict[str, Any]:
        acct = acct_id or self.account_id
        url = self._api_url(f"/iserver/account/{acct}/order/{order_id}")

        try:
            resp = self._session.put(url, json=updates, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list) and data and data[0].get("id"):
                return self._confirm_modify(data[0]["id"])

            logger.info("Order %s modified: %s", order_id, updates)
            return {"ok": True, "raw": data}

        except Exception as e:
            logger.error("Order modify failed for %s: %s", order_id, e)
            return {"ok": False, "error": str(e)}

    def _confirm_modify(self, reply_id: str) -> Dict[str, Any]:
        url = self._api_url(f"/iserver/reply/{reply_id}")
        try:
            resp = self._session.post(url, json={"confirmed": True}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            return {"ok": True, "raw": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def update_stop_loss(self, order_id: str, new_sl_price: float,
                         acct_id: str = None) -> Dict[str, Any]:
        logger.info("Updating SL order %s to price=%.2f", order_id, new_sl_price)
        return self.modify_order(order_id, {"price": new_sl_price}, acct_id)

    def update_take_profit(self, order_id: str, new_tp_price: float,
                           acct_id: str = None) -> Dict[str, Any]:
        logger.info("Updating TP order %s to price=%.2f", order_id, new_tp_price)
        return self.modify_order(order_id, {"price": new_tp_price}, acct_id)

    def cancel_order(self, order_id: str, acct_id: str = None) -> Dict[str, Any]:
        acct = acct_id or self.account_id
        url = self._api_url(f"/iserver/account/{acct}/order/{order_id}")

        try:
            resp = self._session.delete(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            logger.info("Order %s cancelled", order_id)
            return {"ok": True, "raw": data}
        except Exception as e:
            logger.error("Cancel order %s failed: %s", order_id, e)
            return {"ok": False, "error": str(e)}

    def cancel_all_orders(self, acct_id: str = None) -> Dict[str, Any]:
        """Caution: cancels ALL open orders for the account."""
        # Use order tracker to get all open orders, then cancel each
        from ibkr_compute.order.order_tracker import OrderTracker
        tracker = OrderTracker(self.gateway_url, self.account_id)
        orders = tracker.get_live_orders()

        cancelled = 0
        errors = 0
        for order in orders:
            oid = str(order.get("orderId", ""))
            status = order.get("status", "").upper()
            if oid and status not in ("FILLED", "CANCELLED", "CANCELED", "EXECUTED"):
                result = self.cancel_order(oid, acct_id)
                if result.get("ok"):
                    cancelled += 1
                else:
                    errors += 1

        return {"ok": True, "cancelled": cancelled, "errors": errors}
