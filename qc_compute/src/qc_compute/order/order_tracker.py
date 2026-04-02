"""
订单状态跟踪
- 定期轮询 IBKR 订单状态
- 同步到 PocketBase
- 检测 fill/cancel 事件
"""

import os
import time
import logging
import threading
import requests
from typing import Dict, Optional, Callable, List
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000")
ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")
POLL_INTERVAL = 5
ET = timezone(timedelta(hours=-4))


class OrderTracker:
    def __init__(self, gateway_url: str = None, account_id: str = None,
                 pb_client=None, on_fill: Callable = None, on_cancel: Callable = None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.on_fill = on_fill
        self.on_cancel = on_cancel

        self._session = requests.Session()
        self._session.verify = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._known_orders: Dict[str, dict] = {}
        self._last_poll: Optional[float] = None

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def get_live_orders(self) -> List[Dict]:
        try:
            resp = self._session.get(
                self._api_url("/iserver/account/orders"),
                params={"force": "true"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, dict):
                return data.get("orders", [])
            elif isinstance(data, list):
                return data

            return []
        except Exception as e:
            logger.warning("Failed to get live orders: %s", e)
            return []

    def get_order_status(self, order_id: str) -> Dict:
        try:
            resp = self._session.get(
                self._api_url(f"/iserver/account/order/status/{order_id}"),
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("Failed to get order status %s: %s", order_id, e)
            return {}

    def _poll_loop(self):
        logger.info("Order tracker started (interval=%ds)", POLL_INTERVAL)
        while self._running:
            try:
                self._poll_orders()
            except Exception as e:
                logger.error("Order poll error: %s", e)

            for _ in range(POLL_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

    def _poll_orders(self):
        orders = self.get_live_orders()
        self._last_poll = time.time()

        for order in orders:
            order_id = str(order.get("orderId", ""))
            if not order_id:
                continue

            status = order.get("status", "").upper()
            prev = self._known_orders.get(order_id, {})
            prev_status = prev.get("status", "")

            if status != prev_status:
                self._known_orders[order_id] = order
                self._sync_to_pb(order)

                if status in ("FILLED", "EXECUTED") and prev_status not in ("FILLED", "EXECUTED"):
                    logger.info("Order FILLED: %s %s %s@%s",
                                order.get("ticker"), order.get("side"),
                                order.get("filledQuantity"), order.get("avgPrice"))
                    if self.on_fill:
                        try:
                            self.on_fill(order)
                        except Exception as e:
                            logger.error("on_fill callback error: %s", e)

                elif status in ("CANCELLED", "CANCELED"):
                    logger.info("Order CANCELLED: %s %s", order.get("ticker"), order_id)
                    if self.on_cancel:
                        try:
                            self.on_cancel(order)
                        except Exception as e:
                            logger.error("on_cancel callback error: %s", e)

            else:
                self._known_orders[order_id] = order

    def _sync_to_pb(self, order: dict):
        if not self.pb_client:
            return
        try:
            order_id = str(order.get("orderId", ""))
            existing = self.pb_client.get_records(
                "ibkr_orders",
                filter=f'orderId = "{order_id}"',
                per_page=1,
            )

            data = {
                "orderId": order_id,
                "symbol": order.get("ticker", ""),
                "side": order.get("side", ""),
                "orderType": order.get("orderType", ""),
                "status": order.get("status", ""),
                "price": order.get("price", 0),
                "quantity": order.get("totalSize", 0),
                "filled_quantity": order.get("filledQuantity", 0),
                "avg_price": order.get("avgPrice", 0),
                "us_time": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
            }

            if existing:
                self.pb_client.update_record("ibkr_orders", existing[0]["id"], data)
            else:
                self.pb_client.create_record("ibkr_orders", data)

        except Exception as e:
            logger.debug("PB order sync failed: %s", e)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="order-tracker")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def status(self) -> dict:
        return {
            "running": self._running,
            "tracked_orders": len(self._known_orders),
            "last_poll": datetime.fromtimestamp(self._last_poll).isoformat()
            if self._last_poll else None,
        }
