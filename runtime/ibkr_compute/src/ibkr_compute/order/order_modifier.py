"""
IB Gateway order modification and cancellation helpers.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from ibkr_compute.broker import BrokerAdapter

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")


class OrderModifier:
    def __init__(
        self,
        gateway_url: str = None,
        account_id: str = None,
        pb_client=None,
        broker: BrokerAdapter | None = None,
    ):
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.broker = broker or BrokerAdapter()

    def modify_order(self, order_id: str, updates: Dict[str, Any], acct_id: str = None) -> Dict[str, Any]:
        result = self.broker.modify_order(
            str(order_id or "").strip(),
            dict(updates or {}),
            account_id=str(acct_id or self.account_id or "").strip(),
        )
        if not result.get("ok"):
            logger.error("Order modify failed for %s: %s", order_id, result.get("error"))
        return result

    def update_stop_loss(self, order_id: str, new_sl_price: float, acct_id: str = None) -> Dict[str, Any]:
        logger.info("Updating stop loss %s to %.4f", order_id, float(new_sl_price or 0.0))
        return self.modify_order(order_id, {"auxPrice": float(new_sl_price or 0.0)}, acct_id)

    def update_take_profit(self, order_id: str, new_tp_price: float, acct_id: str = None) -> Dict[str, Any]:
        logger.info("Updating take profit %s to %.4f", order_id, float(new_tp_price or 0.0))
        return self.modify_order(order_id, {"price": float(new_tp_price or 0.0)}, acct_id)

    def cancel_order(self, order_id: str, acct_id: str = None) -> Dict[str, Any]:
        result = self.broker.cancel_order(str(order_id or "").strip())
        if not result.get("ok"):
            logger.error("Cancel order %s failed: %s", order_id, result.get("error"))
        return result

    def cancel_all_orders(self, acct_id: str = None) -> Dict[str, Any]:
        return self.broker.cancel_all_orders()
