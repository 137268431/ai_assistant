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
from typing import Any, Dict, Optional, Callable, List
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

        self._session_local = threading.local()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._known_orders: Dict[str, dict] = {}
        self._last_poll: Optional[float] = None
        self._account_selected = False

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def _get_session(self) -> requests.Session:
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            session.verify = False
            self._session_local.session = session
        load_cookies(session)
        return session

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _extract_live_symbol(order: Dict) -> str:
        return str(
            order.get("ticker")
            or order.get("symbol")
            or order.get("contractDesc")
            or ""
        ).strip().upper()

    @staticmethod
    def _extract_live_side(order: Dict) -> str:
        return str(order.get("side") or "").strip().upper()

    @staticmethod
    def _extract_live_order_type(order: Dict) -> str:
        return str(order.get("orderType") or order.get("order_type") or "").strip().upper()

    @staticmethod
    def _extract_live_parent_id(order: Dict) -> str:
        return str(order.get("parentId") or order.get("parent_id") or "").strip()

    @staticmethod
    def _is_open_order_status(status: str) -> bool:
        return str(status or "").strip().upper() not in {
            "",
            "FILLED",
            "EXECUTED",
            "CANCELLED",
            "CANCELED",
            "INACTIVE",
            "REJECTED",
            "EXPIRED",
            "API_CANCELLED",
        }

    def _ensure_account_selected(self) -> bool:
        try:
            session = self._get_session()
            resp = session.get(
                self._api_url("/iserver/accounts"),
                timeout=10,
            )
            if resp.status_code == 401:
                save_cookies(session)
                return False
            resp.raise_for_status()
            save_cookies(session)
            data = resp.json() if resp.text else {}
            accounts = data.get("accounts", []) if isinstance(data, dict) else []
            selected = data.get("selectedAccount", "") if isinstance(data, dict) else ""
            if accounts or selected:
                self._account_selected = True
                logger.debug("iserver account selected: selected=%s accounts=%s", selected, accounts)
                return True
            return False
        except Exception as exc:
            logger.debug("Failed to select iserver account: %s", exc)
            return False

    def _fetch_live_orders_once(self, *, force: bool = True, timeout: int = 15) -> List[Dict]:
        data = self._request_json(
            "/iserver/account/orders",
            params={"force": "true" if force else "false"},
            timeout=timeout,
        )

        if isinstance(data, dict):
            orders = data.get("orders", [])
        elif isinstance(data, list):
            orders = data
        else:
            orders = []
        return orders if isinstance(orders, list) else []

    def _request_json(self, path: str, *, params: Optional[Dict[str, Any]] = None, timeout: int = 15):
        session = self._get_session()
        resp = session.get(
            self._api_url(path),
            params=params or {},
            timeout=timeout,
        )
        if resp.status_code == 401:
            save_cookies(session)
            raise PermissionError("IBKR session is not authenticated")
        resp.raise_for_status()
        save_cookies(session)
        if not resp.text:
            return {}
        return resp.json()

    def get_live_orders(self, *, retries: int = 3, retry_delay: float = 0.5, force: bool = True) -> List[Dict]:
        try:
            if not self._account_selected:
                self._ensure_account_selected()

            attempts = max(1, int(retries or 1))
            orders: List[Dict] = []
            for attempt in range(attempts):
                orders = self._fetch_live_orders_once(force=force, timeout=15)
                if orders:
                    return orders
                if attempt == 0 and not orders and self._account_selected:
                    self._ensure_account_selected()
                if attempt + 1 < attempts:
                    time.sleep(max(0.0, float(retry_delay or 0.0)))
            return orders
        except Exception as e:
            logger.warning("Failed to get live orders: %s", e)
            return []

    def get_orders_by_ids(self, broker_order_ids: List[str]) -> List[Dict]:
        """Fetch individual order statuses by broker_order_id.
        Used to supplement the bulk live orders when the Gateway session cache is stale.
        Returns orders that are still in an active (non-closed) status.
        """
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED"}
        results = []
        for oid in (broker_order_ids or []):
            oid = str(oid or "").strip()
            if not oid:
                continue
            try:
                payload = self.get_order_status(oid)
                if not payload or not isinstance(payload, dict):
                    continue
                status = self._extract_order_status(payload)
                if status and status.upper() not in closed_statuses:
                    results.append(payload)
            except Exception as exc:
                logger.debug("get_orders_by_ids failed for %s: %s", oid, exc)
        return results

    def sync_live_orders_snapshot(self, orders: Optional[List[Dict]] = None) -> int:
        live_orders = orders if orders is not None else self.get_live_orders()
        synced = 0
        for order in live_orders or []:
            order_id = self._normalize_text(order.get("orderId") or order.get("order_id"))
            if not order_id:
                continue
            try:
                self._sync_to_pb(order)
                synced += 1
            except Exception as exc:
                logger.debug("sync_live_orders_snapshot failed for %s: %s", order_id, exc)
        return synced

    def find_duplicate_open_entry(
        self,
        *,
        symbol: str,
        direction: str,
        quantity: Any,
        entry_price: Any,
        entry_order_type: str = "LMT",
        price_tolerance: float = 0.02,
    ) -> Optional[Dict[str, Any]]:
        normalized_symbol = self._normalize_text(symbol).upper()
        normalized_direction = self._normalize_text(direction).lower()
        expected_side = "BUY" if normalized_direction == "long" else "SELL" if normalized_direction == "short" else ""
        expected_qty = round(self._to_float(quantity, 0.0), 8)
        expected_price = self._to_float(entry_price, 0.0)
        normalized_order_type = self._normalize_text(entry_order_type).upper() or "LMT"

        if not normalized_symbol or not expected_side or expected_qty <= 0:
            return None

        live_orders = self.get_live_orders()
        for order in live_orders:
            if self._extract_live_parent_id(order):
                continue

            live_status = self._extract_order_status(order)
            if not self._is_open_order_status(live_status):
                continue
            if self._extract_live_symbol(order) != normalized_symbol:
                continue
            if self._extract_live_side(order) != expected_side:
                continue

            live_qty = round(
                self._to_float(
                    order.get("totalSize") if order.get("totalSize") is not None else order.get("quantity"),
                    0.0,
                ),
                8,
            )
            if abs(live_qty - expected_qty) > 1e-8:
                continue

            live_type = self._extract_live_order_type(order)
            if normalized_order_type == "MKT":
                if live_type and live_type != "MKT":
                    continue
                return order

            if live_type and live_type not in {"LMT", "LIMIT"}:
                continue

            live_price = self._to_float(order.get("price"), 0.0)
            if expected_price <= 0 or live_price <= 0:
                continue
            if abs(live_price - expected_price) > max(price_tolerance, expected_price * 0.0005):
                continue
            return order

        return None

    def get_broker_order_history(self, days: int = 1, force: bool = True) -> Dict[str, Any]:
        requested_days = max(1, int(days or 1))
        # IBKR Client Portal `/iserver/account/orders` only covers the current market day.
        effective_days = 1
        if not self._account_selected:
            self._ensure_account_selected()
        try:
            data = self._request_json(
                "/iserver/account/orders",
                params={"force": "true" if force else "false"},
                timeout=20,
            )
            if isinstance(data, dict):
                orders = data.get("orders", [])
                snapshot = data
            elif isinstance(data, list):
                orders = data
                snapshot = {"orders": data}
            else:
                orders = []
                snapshot = {}

            return {
                "ok": True,
                "requested_days": requested_days,
                "effective_days": effective_days,
                "current_day_only": True,
                "orders": orders if isinstance(orders, list) else [],
                "raw": snapshot if isinstance(snapshot, dict) else {},
                "limitations": [
                    "IBKR Client Portal /iserver/account/orders 仅返回当前美东交易日订单。",
                    "如果需要跨日历史订单，请补充 Flex / Statement 链路。",
                ],
            }
        except Exception as e:
            logger.warning("Failed to get broker order history: %s", e)
            return {
                "ok": False,
                "requested_days": requested_days,
                "effective_days": effective_days,
                "current_day_only": True,
                "orders": [],
                "raw": {},
                "error": str(e),
                "limitations": [
                    "IBKR Client Portal /iserver/account/orders 仅返回当前美东交易日订单。",
                    "如果需要跨日历史订单，请补充 Flex / Statement 链路。",
                ],
            }

    def get_order_status(self, order_id: str) -> Dict:
        try:
            session = self._get_session()
            resp = session.get(
                self._api_url(f"/iserver/account/order/status/{order_id}"),
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
            save_cookies(session)
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

    def _stamp_known_order(self, order: Dict, *, seen_live: bool) -> Dict:
        stamped = dict(order or {})
        stamped["_seen_live"] = bool(seen_live)
        stamped["_last_seen_at"] = time.time() if seen_live else float(order.get("_last_seen_at") or 0.0)
        stamped["_missing_poll_count"] = 0
        stamped["_first_missing_at"] = 0.0
        return stamped

    def _order_sync_signature(self, order: Dict) -> tuple:
        return (
            self._extract_order_status(order),
            round(self._to_float(order.get("filledQuantity"), 0.0), 8),
            round(self._to_float(order.get("avgPrice"), 0.0), 8),
            round(self._to_float(order.get("price"), 0.0), 8),
            round(self._to_float(order.get("totalSize"), 0.0), 8),
            str(order.get("parentId") or "").strip(),
            str(order.get("cOID") or order.get("coid") or "").strip(),
            str(order.get("side") or "").strip().upper(),
            str(order.get("orderType") or "").strip().upper(),
        )

    def _order_needs_sync(self, previous: Dict, current: Dict) -> bool:
        if not previous:
            return True
        if not bool(previous.get("_seen_live")):
            return True
        return self._order_sync_signature(previous) != self._order_sync_signature(current)

    def _infer_disappeared_order_status(self, previous: Dict) -> str:
        status = self._extract_order_status(previous)
        if status in {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED"}:
            return status
        filled_qty = self._to_float(previous.get("filledQuantity"), 0.0)
        avg_price = self._to_float(previous.get("avgPrice"), 0.0)
        if filled_qty > 0 or avg_price > 0:
            return "FILLED"
        return "CANCELED"

    def register_submitted_orders(self, order_ids: List[str], seed: Optional[Dict] = None):
        seed = seed or {}
        indexed_ids = [str(item or "").strip() for item in (order_ids or [])]
        if not any(indexed_ids):
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

        entry_order_id = indexed_ids[0] if len(indexed_ids) > 0 else ""
        tp_order_id = indexed_ids[1] if len(indexed_ids) > 1 else ""
        sl_order_id = indexed_ids[2] if len(indexed_ids) > 2 else ""

        if entry_order_id:
            self._known_orders[entry_order_id] = self._stamp_known_order({
                "orderId": entry_order_id,
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
            }, seen_live=False)
        if tp_order_id:
            self._known_orders[tp_order_id] = self._stamp_known_order({
                "orderId": tp_order_id,
                "ticker": symbol,
                "side": close_side,
                "orderType": "LMT",
                "price": tp_price,
                "totalSize": quantity,
                "filledQuantity": 0,
                "avgPrice": 0,
                "parentId": entry_order_id,
                "status": "SUBMITTED",
                "cOID": tp_unique_id,
            }, seen_live=False)
        if sl_order_id:
            self._known_orders[sl_order_id] = self._stamp_known_order({
                "orderId": sl_order_id,
                "ticker": symbol,
                "side": close_side,
                "orderType": "STP",
                "price": sl_price,
                "totalSize": quantity,
                "filledQuantity": 0,
                "avgPrice": 0,
                "parentId": entry_order_id,
                "status": "SUBMITTED",
                "cOID": sl_unique_id,
            }, seen_live=False)

    def _finalize_disappeared_orders(self, current_order_ids: set[str]):
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED"}
        missing_ids = [order_id for order_id in list(self._known_orders.keys()) if order_id not in current_order_ids]

        for order_id in missing_ids:
            previous = dict(self._known_orders.get(order_id, {}))
            previous_status = self._extract_order_status(previous)
            missing_poll_count = int(previous.get("_missing_poll_count") or 0) + 1
            first_missing_at = float(previous.get("_first_missing_at") or 0.0) or time.time()
            previous["_missing_poll_count"] = missing_poll_count
            previous["_first_missing_at"] = first_missing_at

            payload = self.get_order_status(order_id)
            if not payload:
                if missing_poll_count < 2:
                    self._known_orders[order_id] = previous
                    continue
                merged = dict(previous)
                status = self._infer_disappeared_order_status(previous)
                merged["status"] = status
                merged["_status_inferred"] = True
                merged["_status_inferred_reason"] = "missing_from_live_orders_without_status"
            else:
                merged = dict(previous)
                merged.update(payload)
                merged["orderId"] = order_id
                status = self._extract_order_status(merged)
                if not status:
                    self._known_orders[order_id] = previous
                    continue
                merged["status"] = status

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

            prev = self._known_orders.get(order_id, {})
            prev_status = self._extract_order_status(prev)
            merged = dict(prev)
            merged.update(order)
            merged["orderId"] = order_id
            merged = self._stamp_known_order(merged, seen_live=True)
            status = self._extract_order_status(merged)
            should_sync = self._order_needs_sync(prev, merged)

            if should_sync:
                self._known_orders[order_id] = merged
                self._sync_to_pb(merged)

                if status in ("FILLED", "EXECUTED") and prev_status not in ("FILLED", "EXECUTED"):
                    logger.info("Order FILLED: %s %s %s@%s",
                                merged.get("ticker"), merged.get("side"),
                                merged.get("filledQuantity"), merged.get("avgPrice"))
                    if self.on_fill:
                        try:
                            self.on_fill(merged)
                        except Exception as e:
                            logger.error("on_fill callback error: %s", e)

                elif status in ("CANCELLED", "CANCELED"):
                    logger.info("Order CANCELLED: %s %s", merged.get("ticker"), order_id)
                    if self.on_cancel:
                        try:
                            self.on_cancel(merged)
                        except Exception as e:
                            logger.error("on_cancel callback error: %s", e)

            else:
                self._known_orders[order_id] = merged

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
            runtime_environment = os.environ.get("IBKR_ENVIRONMENT", "live")

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
                signal_id = ""
                trade_group_id = ""
                entry_order_unique_id = str(coid or "").strip()
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

                extra = {
                    "source": "order_tracker",
                    "seen_live": bool(order.get("_seen_live")),
                }
                if coid:
                    extra["coid"] = coid
                if order.get("_status_inferred"):
                    extra["status_inferred"] = True
                    extra["status_inferred_reason"] = str(order.get("_status_inferred_reason") or "")

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
                    "extra": extra,
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
