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

from ibkr_compute.gateway.cookie_store import load_cookies, save_cookies

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
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
        load_cookies(self._session)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._known_orders: Dict[str, dict] = {}
        self._last_poll: Optional[float] = None

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def get_live_orders(self) -> List[Dict]:
        try:
            load_cookies(self._session)
            resp = self._session.get(
                self._api_url("/iserver/account/orders"),
                params={"force": "true"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            save_cookies(self._session)

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
            load_cookies(self._session)
            resp = self._session.get(
                self._api_url(f"/iserver/account/order/status/{order_id}"),
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
            save_cookies(self._session)
            return payload
        except Exception as e:
            logger.warning("Failed to get order status %s: %s", order_id, e)
            return {}

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _extract_order_status(order: Dict) -> str:
        return str(
            order.get("status")
            or order.get("order_status")
            or order.get("orderStatus")
            or order.get("state")
            or ""
        ).strip().upper()

    def register_submitted_orders(self, order_ids: List[str], seed: Optional[Dict] = None):
        seed = seed or {}
        clean_ids = [str(item).strip() for item in (order_ids or []) if str(item).strip()]
        if not clean_ids:
            return

        symbol = str(seed.get("symbol") or "").strip().upper()
        direction = str(seed.get("direction") or "").strip().lower()
        entry_unique_id = str(seed.get("entry_unique_id") or "").strip()
        tp_unique_id = str(seed.get("tp_unique_id") or "").strip()
        sl_unique_id = str(seed.get("sl_unique_id") or "").strip()
        side = "BUY" if direction == "long" else "SELL" if direction == "short" else ""
        close_side = "SELL" if side == "BUY" else "BUY" if side == "SELL" else ""
        quantity = seed.get("quantity", 0)
        entry_price = seed.get("entry_price", 0)
        tp_price = seed.get("tp_price", 0)
        sl_price = seed.get("sl_price", 0)

        for index, order_id in enumerate(clean_ids):
            if index == 0:
                self._known_orders[order_id] = {
                    "orderId": order_id,
                    "ticker": symbol,
                    "side": side,
                    "orderType": "LMT",
                    "price": entry_price,
                    "totalSize": quantity,
                    "filledQuantity": 0,
                    "avgPrice": 0,
                    "parentId": "",
                    "status": "SUBMITTED",
                    "cOID": entry_unique_id,
                }
            elif index == 1:
                self._known_orders[order_id] = {
                    "orderId": order_id,
                    "ticker": symbol,
                    "side": close_side,
                    "orderType": "LMT",
                    "price": tp_price,
                    "totalSize": quantity,
                    "filledQuantity": 0,
                    "avgPrice": 0,
                    "parentId": clean_ids[0],
                    "status": "SUBMITTED",
                    "cOID": tp_unique_id,
                }
            elif index == 2:
                self._known_orders[order_id] = {
                    "orderId": order_id,
                    "ticker": symbol,
                    "side": close_side,
                    "orderType": "STP",
                    "price": sl_price,
                    "totalSize": quantity,
                    "filledQuantity": 0,
                    "avgPrice": 0,
                    "parentId": clean_ids[0],
                    "status": "SUBMITTED",
                    "cOID": sl_unique_id,
                }

    def _finalize_disappeared_orders(self, current_order_ids: set[str]):
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED"}
        missing_ids = [order_id for order_id in list(self._known_orders.keys()) if order_id not in current_order_ids]

        for order_id in missing_ids:
            previous = self._known_orders.get(order_id, {})
            payload = self.get_order_status(order_id)
            if not payload:
                continue

            merged = dict(previous)
            merged.update(payload)
            merged["orderId"] = order_id
            status = self._extract_order_status(merged)
            if not status:
                continue
            merged["status"] = status

            previous_status = self._extract_order_status(previous)
            if status != previous_status:
                self._sync_to_pb(merged)

                if status in ("FILLED", "EXECUTED") and previous_status not in ("FILLED", "EXECUTED"):
                    logger.info("Order FILLED after disappearance: %s %s", merged.get("ticker"), order_id)
                    if self.on_fill:
                        try:
                            self.on_fill(merged)
                        except Exception as e:
                            logger.error("on_fill callback error: %s", e)
                elif status in ("CANCELLED", "CANCELED", "INACTIVE", "REJECTED"):
                    logger.info("Order CLOSED after disappearance: %s %s status=%s", merged.get("ticker"), order_id, status)
                    if self.on_cancel:
                        try:
                            self.on_cancel(merged)
                        except Exception as e:
                            logger.error("on_cancel callback error: %s", e)

            if status in closed_statuses:
                self._known_orders.pop(order_id, None)
            else:
                self._known_orders[order_id] = merged

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
        current_order_ids = set()

        for order in orders:
            order_id = str(order.get("orderId", ""))
            if not order_id:
                continue
            current_order_ids.add(order_id)

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

        self._finalize_disappeared_orders(current_order_ids)

    def _sync_to_pb(self, order: dict):
        if not self.pb_client:
            return
        try:
            order_id = str(order.get("orderId", ""))
            if not order_id:
                return
            coid = str(order.get("cOID") or order.get("coid") or order.get("order_ref") or order.get("orderRef") or "").strip()
            symbol = order.get("ticker", "")
            status = self._extract_order_status(order)
            now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
            order_id_filter = self._escape_filter_value(order_id)
            coid_filter = self._escape_filter_value(coid)
            ibkr_filter = f'orderId = "{order_id_filter}"'
            if coid:
                ibkr_filter = f'orderId = "{order_id_filter}" || cOID = "{coid_filter}"'
            existing = self.pb_client.get_records("ibkr_orders", filter=ibkr_filter, per_page=1)
            existing_ibkr = existing[0] if existing else {}
            runtime_environment = os.environ.get("IBKR_ENVIRONMENT", "live")

            data = {
                "orderId": order_id,
                "symbol": symbol,
                "side": order.get("side", ""),
                "orderType": order.get("orderType", ""),
                "status": status,
                "price": order.get("price", 0),
                "quantity": order.get("totalSize", 0),
                "filled_quantity": order.get("filledQuantity", 0),
                "avg_price": order.get("avgPrice", 0),
                "us_time": now_str,
                "cOID": coid or existing_ibkr.get("cOID", ""),
                "signal_id": existing_ibkr.get("signal_id", ""),
                "bracket_group": existing_ibkr.get("bracket_group", ""),
                "parentId": order.get("parentId", "") or existing_ibkr.get("parentId", ""),
                "account": order.get("acctId", "") or order.get("acct", "") or existing_ibkr.get("account", ""),
            }

            if existing:
                self.pb_client.update_record("ibkr_orders", existing[0]["id"], data)
            else:
                self.pb_client.create_record("ibkr_orders", data)

            if hasattr(self.pb_client, "upsert_order"):
                normalized_side = str(order.get("side", "")).upper()
                direction = "long" if normalized_side == "BUY" else "short" if normalized_side == "SELL" else ""
                quantity = order.get("totalSize", 0)
                fill_qty = order.get("filledQuantity", 0)
                avg_price = order.get("avgPrice", 0)
                limit_price = order.get("price", 0)
                parent_id = order.get("parentId") or ""
                order_type = order.get("orderType", "Entry") or "Entry"
                upper_type = str(order_type).upper()
                if not parent_id:
                    role = "entry"
                elif upper_type in ("STP", "STOP", "STOPLOSS"):
                    role = "stop_loss"
                else:
                    role = "take_profit"
                relation_status = "closed" if str(status).upper() in ("FILLED", "EXECUTED", "CANCELLED", "CANCELED") else "active"
                mapped_status = {
                    "PRESUBMITTED": "Submitted",
                    "SUBMITTED": "Submitted",
                    "FILLED": "Filled",
                    "EXECUTED": "Filled",
                    "CANCELLED": "Canceled",
                    "CANCELED": "Canceled",
                    "INACTIVE": "Canceled",
                    "REJECTED": "Canceled",
                }.get(str(status).upper(), "Submitted")
                signal_id = str(existing_ibkr.get("signal_id") or "").strip()
                trade_group_id = str(existing_ibkr.get("bracket_group") or "").strip()
                entry_order_unique_id = str(existing_ibkr.get("cOID") or coid or "").strip()
                parent_order_unique_id = ""
                canonical_unique_id = entry_order_unique_id if not parent_id else coid
                existing_order = None

                if order_id:
                    order_filter = (
                        f'(broker_order_id = "{order_id_filter}" || order_id = "{order_id_filter}") '
                        f'&& environment = "{self._escape_filter_value(runtime_environment)}"'
                    )
                    matches = self.pb_client.get_records("orders", filter=order_filter, per_page=1)
                    existing_order = matches[0] if matches else None
                if not existing_order and coid:
                    coid_order_filter = (
                        f'unique_id = "{coid_filter}" && environment = "{self._escape_filter_value(runtime_environment)}"'
                    )
                    matches = self.pb_client.get_records("orders", filter=coid_order_filter, per_page=1)
                    existing_order = matches[0] if matches else None

                if existing_order:
                    canonical_unique_id = str(existing_order.get("unique_id") or canonical_unique_id or order_id).strip()
                    signal_id = signal_id or str(existing_order.get("signal_id") or "").strip()
                    trade_group_id = str(existing_order.get("trade_group_id") or trade_group_id or "").strip()
                    entry_order_unique_id = str(existing_order.get("entry_order_unique_id") or entry_order_unique_id or canonical_unique_id).strip()
                    parent_order_unique_id = str(existing_order.get("parent_order_unique_id") or "").strip()
                    role = str(existing_order.get("role") or role).strip() or role
                elif parent_id:
                    parent_filter = self._escape_filter_value(str(parent_id))
                    parent_order_filter = (
                        f'(broker_order_id = "{parent_filter}" || order_id = "{parent_filter}") '
                        f'&& environment = "{self._escape_filter_value(runtime_environment)}"'
                    )
                    matches = self.pb_client.get_records("orders", filter=parent_order_filter, per_page=1)
                    if matches:
                        parent_record = matches[0]
                        parent_order_unique_id = str(parent_record.get("unique_id") or "").strip()
                        trade_group_id = str(parent_record.get("trade_group_id") or parent_record.get("entry_order_unique_id") or trade_group_id).strip()
                        entry_order_unique_id = str(parent_record.get("entry_order_unique_id") or parent_order_unique_id or entry_order_unique_id).strip()
                        signal_id = signal_id or str(parent_record.get("signal_id") or "").strip()

                if not canonical_unique_id:
                    canonical_unique_id = order_id
                if not trade_group_id:
                    trade_group_id = entry_order_unique_id or canonical_unique_id
                if not entry_order_unique_id:
                    entry_order_unique_id = canonical_unique_id

                self.pb_client.upsert_order({
                    "unique_id": canonical_unique_id,
                    "order_id": order_id,
                    "broker_order_id": order_id,
                    "order_type": order_type,
                    "symbol": symbol,
                    "direction": direction,
                    "position_side": direction,
                    "trade_group_id": trade_group_id,
                    "entry_order_unique_id": entry_order_unique_id,
                    "parent_order_unique_id": parent_order_unique_id,
                    "role": role,
                    "relation_status": relation_status,
                    "signal_id": signal_id,
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "status": mapped_status,
                    "filled_qty": fill_qty,
                    "fill_price": avg_price,
                    "us_time": now_str,
                    "bar_time_ms": int(time.time() * 1000),
                })

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
