"""
Order tracking built on top of IB Gateway socket events plus polling fallback.
"""

from __future__ import annotations

import os
import time
import logging
import threading
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime, timezone
from ibkr_compute.core.time_utils import ET

from ibkr_compute.broker import BrokerAdapter

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")
ORDER_UPDATES_MODE = str(os.environ.get("IBKR_ORDER_UPDATES_MODE", "hybrid") or "hybrid").strip().lower() or "hybrid"
POLL_INTERVAL_ACTIVE = max(5, int(os.environ.get("IBKR_ORDER_POLL_INTERVAL_ACTIVE_SEC", "5")))
POLL_INTERVAL_IDLE = max(POLL_INTERVAL_ACTIVE, int(os.environ.get("IBKR_ORDER_POLL_INTERVAL_IDLE_SEC", "15")))
ORDER_FAST_TRACK_WINDOW = max(POLL_INTERVAL_ACTIVE, int(os.environ.get("IBKR_ORDER_FAST_TRACK_SEC", "30")))


class OrderTracker:
    def __init__(
        self,
        gateway_url: str = None,
        account_id: str = None,
        pb_client=None,
        on_fill: Callable = None,
        on_cancel: Callable = None,
        config=None,
        environment: str = "live",
        broker: BrokerAdapter | None = None,
    ):
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.on_fill = on_fill
        self.on_cancel = on_cancel
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.broker = broker or BrokerAdapter()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._known_orders: Dict[str, dict] = {}
        self._last_poll: Optional[float] = None
        self._last_live_update: Optional[float] = None
        self._last_order_activity: Optional[float] = None
        self._live_update_count = 0
        self._poll_wakeup = threading.Event()
        self._initial_snapshot_pending = True

    def _get_int_setting(self, key: str, fallback: int) -> int:
        if not self.config:
            return fallback
        return self.config.get_int_for_environment(key, self.environment, fallback)

    def _get_mode_setting(self, key: str, fallback: str) -> str:
        if not self.config:
            return fallback
        return str(self.config.get_for_environment(key, self.environment, fallback) or fallback).strip().lower() or fallback

    def _updates_mode(self) -> str:
        mode = self._get_mode_setting("ibkr_order_updates_mode", ORDER_UPDATES_MODE)
        if mode not in {"poll", "websocket", "hybrid"}:
            return ORDER_UPDATES_MODE
        return mode

    def uses_websocket_updates(self) -> bool:
        return self._updates_mode() in {"websocket", "hybrid"}

    def _active_poll_interval(self) -> int:
        return max(5, self._get_int_setting("ibkr_order_poll_interval_active_sec", POLL_INTERVAL_ACTIVE))

    def _idle_poll_interval(self) -> int:
        return max(self._active_poll_interval(), self._get_int_setting("ibkr_order_poll_interval_idle_sec", POLL_INTERVAL_IDLE))

    def _fast_track_window(self) -> int:
        return max(self._active_poll_interval(), self._get_int_setting("ibkr_order_fast_track_sec", ORDER_FAST_TRACK_WINDOW))

    def _mark_order_activity(self):
        self._last_order_activity = time.time()

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def _normalize_trade_direction(cls, value: Any) -> str:
        normalized = cls._normalize_text(value).lower()
        return normalized if normalized in {"long", "short"} else ""

    @classmethod
    def _direction_from_identifier(cls, *values: Any, allow_signal_suffix: bool = False) -> str:
        for value in values:
            text = cls._normalize_text(value).lower()
            if not text:
                continue
            normalized = text
            for separator in ("-", ":", "/", ".", " "):
                normalized = normalized.replace(separator, "_")
            tokens = [token for token in normalized.split("_") if token]
            direction_tokens = [(index, token) for index, token in enumerate(tokens) if token in {"long", "short"}]
            if direction_tokens:
                return direction_tokens[-1][1]
            if allow_signal_suffix and tokens:
                if tokens[-1] == "l":
                    return "long"
                if tokens[-1] == "s":
                    return "short"
        return ""

    @classmethod
    def _record_identifier_direction(cls, record: Optional[Dict[str, Any]]) -> str:
        if not isinstance(record, dict):
            return ""
        return cls._direction_from_identifier(
            record.get("trade_group_id"),
            record.get("entry_order_unique_id"),
            record.get("unique_id"),
        ) or cls._direction_from_identifier(record.get("signal_id"), allow_signal_suffix=True)

    @classmethod
    def _record_explicit_direction(cls, record: Optional[Dict[str, Any]]) -> str:
        if not isinstance(record, dict):
            return ""
        return cls._normalize_trade_direction(record.get("position_side")) or cls._normalize_trade_direction(record.get("direction"))

    @classmethod
    def _position_side_from_close_order_side(cls, side: Any) -> str:
        normalized_side = cls._normalize_text(side).upper()
        if normalized_side == "SELL":
            return "long"
        if normalized_side == "BUY":
            return "short"
        return ""

    @classmethod
    def _infer_position_side(
        cls,
        *,
        side: Any,
        role: Any,
        parent_id: Any,
        coid: Any,
        trade_group_id: Any,
        entry_order_unique_id: Any,
        signal_id: Any,
        existing_order: Optional[Dict[str, Any]],
        parent_record: Optional[Dict[str, Any]],
    ) -> str:
        identity_direction = cls._direction_from_identifier(
            coid,
            trade_group_id,
            entry_order_unique_id,
        ) or cls._direction_from_identifier(signal_id, allow_signal_suffix=True)
        if identity_direction:
            return identity_direction

        for record in (existing_order, parent_record):
            identity_direction = cls._record_identifier_direction(record)
            if identity_direction:
                return identity_direction

        parent_direction = cls._record_explicit_direction(parent_record)
        if parent_direction:
            return parent_direction

        existing_direction = cls._record_explicit_direction(existing_order)
        if existing_direction:
            return existing_direction

        normalized_side = cls._normalize_text(side).upper()
        normalized_role = cls._normalize_text(role).lower()
        is_exit_leg = bool(cls._normalize_text(parent_id)) or normalized_role in {"take_profit", "stop_loss"}
        if normalized_side == "BUY":
            return "short" if is_exit_leg else "long"
        if normalized_side == "SELL":
            return "long" if is_exit_leg else "short"
        return ""

    @staticmethod
    def _extract_live_symbol(order: Dict) -> str:
        return str(order.get("ticker") or order.get("symbol") or order.get("contractDesc") or "").strip().upper()

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

    def _has_broker_live_identity(self, order: Dict) -> bool:
        if not isinstance(order, dict):
            return False
        if self._extract_live_symbol(order):
            return True
        if self._normalize_text(
            order.get("cOID")
            or order.get("coid")
            or order.get("client_order_id")
            or order.get("order_ref")
            or order.get("orderRef")
        ):
            return True
        if self._extract_live_side(order) and self._extract_live_order_type(order):
            return True
        quantity = self._to_float(
            order.get("totalSize")
            if order.get("totalSize") not in (None, "")
            else order.get("quantity"),
            0.0,
        )
        return quantity > 0 and bool(self._extract_live_side(order))

    def _should_skip_unidentified_live_order(self, order_id: str, order: Dict, sources: List[str]) -> bool:
        # PB-seeded orders can be enriched later by account snapshot matching. Tracker-only
        # status patches without contract/client identity are stale-prone and should not
        # appear as broker-confirmed live orders.
        if "pb" in set(sources or []):
            return False
        if self._has_broker_live_identity(order):
            return False
        logger.debug(
            "Skipping unidentified live open order: order_id=%s status=%s sources=%s payload_keys=%s",
            order_id,
            self._extract_order_status(order),
            sources,
            sorted((order or {}).keys()),
        )
        return True

    def get_live_orders(
        self,
        *,
        retries: int = 3,
        retry_delay: float = 0.5,
        force: bool = True,
        include_all: bool = False,
    ) -> List[Dict]:
        attempts = max(1, int(retries or 1))
        for attempt in range(attempts):
            try:
                orders = list(self.broker.list_open_orders(include_all=include_all) or [])
                if orders or attempt + 1 >= attempts:
                    return orders
            except Exception as exc:
                logger.warning("Failed to get live orders: %s", exc)
                if attempt + 1 >= attempts:
                    return []
            time.sleep(max(0.0, float(retry_delay or 0.0)))
        return []

    def _build_fill_history_index(self) -> Dict[str, Dict[str, Any]]:
        index: Dict[str, Dict[str, Any]] = {}
        try:
            fills = list(self.broker.list_recent_fills() or [])
        except Exception as exc:
            logger.debug("Recent fills fetch failed: %s", exc)
            return index

        for fill in fills:
            order_id = self._normalize_text(fill.get("orderId"))
            if not order_id:
                continue
            bucket = index.setdefault(order_id, {
                "orderId": order_id,
                "id": order_id,
                "conid": int(fill.get("conid", 0) or 0),
                "ticker": self._normalize_text(fill.get("ticker")).upper(),
                "side": self._normalize_text(fill.get("side")).upper(),
                "status": "FILLED",
                "orderType": self._normalize_text(fill.get("orderType")).upper(),
                "filledQuantity": 0.0,
                "remainingQuantity": 0.0,
                "avgPrice": 0.0,
                "price": 0.0,
                "commission": 0.0,
                "submittedTime": self._normalize_text(fill.get("time")),
                "lastExecutionTime": self._normalize_text(fill.get("time")),
                "_fill_value": 0.0,
            })
            shares = self._to_float(fill.get("shares"), 0.0)
            price = self._to_float(fill.get("price"), 0.0)
            commission = self._to_float(fill.get("commission"), 0.0)
            bucket["filledQuantity"] += shares
            bucket["_fill_value"] += shares * price
            bucket["commission"] += commission
            if price > 0:
                bucket["price"] = price
            if self._normalize_text(fill.get("time")):
                bucket["lastExecutionTime"] = self._normalize_text(fill.get("time"))

        for payload in index.values():
            filled_qty = self._to_float(payload.get("filledQuantity"), 0.0)
            payload["avgPrice"] = round(payload.get("_fill_value", 0.0) / filled_qty, 6) if filled_qty > 0 else 0.0
            payload["totalSize"] = filled_qty
            payload.pop("_fill_value", None)
        return index

    def get_order_status(self, order_id: str) -> Dict:
        normalized = self._normalize_text(order_id)
        if not normalized:
            return {}

        snapshot = self.broker.get_order_snapshot(normalized)
        if snapshot:
            fill_payload = dict(self._build_fill_history_index().get(normalized) or {})
            if fill_payload:
                merged = dict(snapshot)
                merged.update({key: value for key, value in fill_payload.items() if value not in (None, "")})
                return merged
            return snapshot

        for order in self.get_live_orders():
            live_order_id = self._normalize_text(order.get("orderId") or order.get("order_id") or order.get("id"))
            if live_order_id == normalized:
                return dict(order)

        return dict(self._build_fill_history_index().get(normalized) or {})

    def get_orders_by_ids(self, broker_order_ids: List[str]) -> List[Dict]:
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED"}
        results = []
        for oid in broker_order_ids or []:
            payload = self.get_order_status(str(oid or "").strip())
            if not payload:
                continue
            status = self._extract_order_status(payload)
            if status and status.upper() not in closed_statuses:
                results.append(payload)
        return results

    def _build_live_seed_source_map(self, pb_seed_ids: Optional[List[str]] = None) -> Dict[str, List[str]]:
        seed_sources: Dict[str, List[str]] = {}
        for tracked in list(self._known_orders.values()):
            order_id = self._normalize_text(tracked.get("orderId") or tracked.get("order_id"))
            if not order_id:
                continue
            status = self._extract_order_status(tracked)
            if not self._is_open_order_status(status):
                continue
            seed_sources.setdefault(order_id, [])
            if "tracker" not in seed_sources[order_id]:
                seed_sources[order_id].append("tracker")

        for oid in pb_seed_ids or []:
            order_id = self._normalize_text(oid)
            if not order_id:
                continue
            seed_sources.setdefault(order_id, [])
            if "pb" not in seed_sources[order_id]:
                seed_sources[order_id].append("pb")
        return seed_sources

    def _merge_order_with_known_state(self, order_id: str, payload: Optional[Dict]) -> Dict[str, Any]:
        merged = dict(self._known_orders.get(order_id) or {})
        if isinstance(payload, dict):
            merged.update(payload)
        if order_id:
            merged["orderId"] = order_id
        return merged

    def get_complete_live_open_orders(
        self,
        *,
        pb_seed_ids: Optional[List[str]] = None,
        bulk_orders: Optional[List[Dict]] = None,
        retries: int = 3,
        retry_delay: float = 0.5,
        force: bool = True,
    ) -> Dict[str, Any]:
        seed_sources = self._build_live_seed_source_map(pb_seed_ids)
        bulk_list = bulk_orders if bulk_orders is not None else self.get_live_orders(
            retries=retries,
            retry_delay=retry_delay,
            force=force,
        )
        if not bulk_list and seed_sources:
            try:
                bulk_list = self.get_live_orders(
                    retries=1,
                    retry_delay=retry_delay,
                    force=force,
                    include_all=True,
                )
            except Exception as exc:
                logger.debug("All-open-orders recovery fallback failed: %s", exc)
        existing_ids = set()
        open_orders: List[Dict[str, Any]] = []
        skipped_unidentified_order_ids: List[str] = []

        for item in bulk_list or []:
            if not isinstance(item, dict):
                continue
            order_id = self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
            if not order_id or order_id in existing_ids:
                continue
            merged = self._merge_order_with_known_state(order_id, item)
            status = self._extract_order_status(merged)
            if not self._is_open_order_status(status):
                continue
            sources = list(seed_sources.get(order_id) or [])
            if self._should_skip_unidentified_live_order(order_id, merged, sources):
                skipped_unidentified_order_ids.append(order_id)
                existing_ids.add(order_id)
                continue
            merged["_recovery_source"] = "bulk"
            merged["_seed_sources"] = sources
            open_orders.append(merged)
            existing_ids.add(order_id)

        recovered_order_ids: List[str] = []
        resolved_closed_ids: List[str] = []
        unresolved_order_ids: List[str] = []

        for order_id, sources in seed_sources.items():
            if order_id in existing_ids:
                continue
            payload = self.get_order_status(order_id)
            if payload and isinstance(payload, dict):
                merged = self._merge_order_with_known_state(order_id, payload)
                status = self._extract_order_status(merged)
                if status and self._is_open_order_status(status):
                    if self._should_skip_unidentified_live_order(order_id, merged, list(sources or [])):
                        skipped_unidentified_order_ids.append(order_id)
                        existing_ids.add(order_id)
                        continue
                    merged["_recovery_source"] = "status_recovered"
                    merged["_seed_sources"] = list(sources or [])
                    open_orders.append(merged)
                    existing_ids.add(order_id)
                    recovered_order_ids.append(order_id)
                    continue
                if status:
                    resolved_closed_ids.append(order_id)
                    continue
            unresolved_order_ids.append(order_id)

        tracker_seed_count = len([order_id for order_id, sources in seed_sources.items() if "tracker" in sources])
        pb_seed_count = len([order_id for order_id, sources in seed_sources.items() if "pb" in sources])
        coverage_state = "degraded" if unresolved_order_ids else ("recovered" if recovered_order_ids else "complete")

        return {
            "orders": open_orders,
            "coverage": {
                "coverage_state": coverage_state,
                "bulk_open_count": len([item for item in open_orders if str(item.get("_recovery_source") or "") == "bulk"]),
                "recovered_from_status_count": len(recovered_order_ids),
                "tracker_seed_count": tracker_seed_count,
                "pb_seed_count": pb_seed_count,
                "unresolved_seed_count": len(unresolved_order_ids),
                "unresolved_order_ids": unresolved_order_ids,
            },
            "diagnostics": {
                "seed_sources": seed_sources,
                "recovered_order_ids": recovered_order_ids,
                "resolved_closed_order_ids": resolved_closed_ids,
                "skipped_unidentified_order_ids": skipped_unidentified_order_ids,
                "bulk_order_ids": [
                    self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
                    for item in bulk_list or []
                    if isinstance(item, dict) and self._normalize_text(item.get("orderId") or item.get("order_id") or item.get("id"))
                ],
            },
        }

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
        orders_by_id: Dict[str, Dict[str, Any]] = {}

        try:
            for order in self.get_live_orders(force=force):
                order_id = self._normalize_text(order.get("orderId") or order.get("order_id") or order.get("id"))
                if order_id:
                    orders_by_id[order_id] = dict(order)
        except Exception as exc:
            logger.warning("Failed to get live orders for history: %s", exc)

        try:
            fill_index = self._build_fill_history_index()
            for order_id, payload in fill_index.items():
                merged = dict(payload)
                merged.update(orders_by_id.get(order_id) or {})
                merged["orderId"] = order_id
                if "status" not in merged or not merged["status"]:
                    merged["status"] = "FILLED"
                orders_by_id[order_id] = merged
        except Exception as exc:
            logger.warning("Failed to get recent fills for history: %s", exc)

        return {
            "ok": True,
            "requested_days": requested_days,
            "effective_days": 1,
            "current_day_only": False,
            "orders": list(orders_by_id.values()),
            "raw": {"orders": list(orders_by_id.values())},
            "limitations": [
                "IB Gateway socket API 当前返回 open orders 与最近 executions 的组合视图。",
                "如果需要完整跨日订单历史，请补充 Flex / Statement 链路。",
            ],
        }

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

    def _find_pb_order_by_broker_id(
        self,
        order_id: str,
        *,
        runtime_environment: str,
        symbol: str = "",
        role: str = "",
        per_page: int = 5,
    ) -> Optional[Dict[str, Any]]:
        normalized_order_id = self._normalize_text(order_id)
        if not normalized_order_id:
            return None

        order_id_filter = self._escape_filter_value(normalized_order_id)
        environment_filter = self._escape_filter_value(runtime_environment)
        matches = self.pb_client.get_records(
            "orders",
            filter=(
                f'(broker_order_id = "{order_id_filter}" || order_id = "{order_id_filter}") '
                f'&& environment = "{environment_filter}"'
            ),
            sort="-updated",
            per_page=max(1, int(per_page or 1)),
        )
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]

        open_matches = [
            item for item in matches
            if self._normalize_text(item.get("relation_status")).lower() in {"active", "planned"}
            or self._normalize_text(item.get("status")).upper() in {"SUBMITTED", "PRESUBMITTED", "PENDINGSUBMIT", "PENDING", "INIT", "APIPENDING", "API_PENDING"}
        ]
        if len(open_matches) == 1:
            return open_matches[0]
        if len(open_matches) > 1:
            matches = open_matches

        normalized_symbol = self._normalize_text(symbol).upper()
        if normalized_symbol:
            symbol_matches = [
                item for item in matches
                if self._normalize_text(item.get("symbol")).upper() == normalized_symbol
            ]
            if len(symbol_matches) == 1:
                return symbol_matches[0]
            if len(symbol_matches) > 1:
                matches = symbol_matches

        normalized_role = self._normalize_text(role)
        if normalized_role:
            role_matches = [
                item for item in matches
                if self._normalize_text(item.get("role")) == normalized_role
            ]
            if len(role_matches) == 1:
                return role_matches[0]
            if len(role_matches) > 1:
                matches = role_matches

        logger.warning(
            "Ambiguous PB order match by broker_order_id: order_id=%s symbol=%s role=%s environment=%s candidates=%s",
            normalized_order_id,
            normalized_symbol,
            normalized_role,
            runtime_environment,
            [
                {
                    "id": str(item.get("id") or ""),
                    "unique_id": str(item.get("unique_id") or ""),
                    "symbol": str(item.get("symbol") or ""),
                    "role": str(item.get("role") or ""),
                    "status": str(item.get("status") or ""),
                }
                for item in matches
            ],
        )
        return None

    def _find_pb_entry_for_close_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Any,
        runtime_environment: str,
    ) -> Optional[Dict[str, Any]]:
        """Best-effort link for broker-side market closes that lack PB identity."""
        if not getattr(self, "pb_client", None):
            return None
        normalized_symbol = self._normalize_text(symbol).upper()
        position_side = self._position_side_from_close_order_side(side)
        if not normalized_symbol or not position_side:
            return None

        try:
            rows = self.pb_client.get_records(
                "orders",
                filter=(
                    f'symbol = "{self._escape_filter_value(normalized_symbol)}" && '
                    f'environment = "{self._escape_filter_value(runtime_environment)}" && '
                    f'role = "entry"'
                ),
                sort="-updated,-created",
                per_page=25,
            )
        except Exception as exc:
            logger.debug("Close-order PB entry lookup failed: symbol=%s error=%s", normalized_symbol, exc)
            return None

        close_qty = self._to_float(quantity, 0.0)
        candidates: list[Dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            unique_id = self._normalize_text(row.get("unique_id"))
            if unique_id.lower().startswith("close_"):
                continue
            row_side = self._record_explicit_direction(row) or self._record_identifier_direction(row)
            if row_side and row_side != position_side:
                continue
            row_qty = self._to_float(
                row.get("filled_qty")
                if row.get("filled_qty") not in (None, "")
                else row.get("quantity"),
                0.0,
            )
            if close_qty > 0 and row_qty > 0 and close_qty - row_qty > 1e-6:
                continue
            status = self._normalize_text(row.get("status")).lower()
            if status not in {"filled", "closed", "protected_active"}:
                continue
            candidates.append(dict(row))

        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                1 if self._normalize_text(item.get("status")).lower() == "filled" else 0,
                self._normalize_text(item.get("updated") or item.get("created")),
            ),
            reverse=True,
        )
        return candidates[0]

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
            bool(order.get("broker_realtime_callback")),
            str(order.get("ib_callback_type") or "").strip(),
            str(order.get("ib_exec_id") or "").strip(),
        )

    def _order_needs_sync(self, previous: Dict, current: Dict) -> bool:
        if not previous:
            return True
        if not bool(previous.get("_seen_live")):
            return True
        return self._order_sync_signature(previous) != self._order_sync_signature(current)

    def _emit_order_transition_callbacks(self, previous_status: str, merged: Dict):
        status = self._extract_order_status(merged)
        if status in ("FILLED", "EXECUTED") and previous_status not in ("FILLED", "EXECUTED"):
            logger.info(
                "Order FILLED: %s %s %s@%s",
                merged.get("ticker"),
                merged.get("side"),
                merged.get("filledQuantity"),
                merged.get("avgPrice"),
            )
            if self.on_fill:
                try:
                    self.on_fill(merged)
                except Exception as exc:
                    logger.error("on_fill callback error: %s", exc)
        elif status in ("CANCELLED", "CANCELED", "INACTIVE", "REJECTED") and previous_status not in ("CANCELLED", "CANCELED", "INACTIVE", "REJECTED"):
            logger.info("Order CLOSED: %s %s status=%s", merged.get("ticker"), merged.get("orderId"), status)
            if self.on_cancel:
                try:
                    self.on_cancel(merged)
                except Exception as exc:
                    logger.error("on_cancel callback error: %s", exc)

    def _handle_live_order_payload(self, order: Dict, source: str) -> bool:
        order_id = self._normalize_text(order.get("orderId") or order.get("order_id"))
        if not order_id:
            return False

        prev = dict(self._known_orders.get(order_id, {}))
        prev_status = self._extract_order_status(prev)
        merged = dict(prev)
        merged.update(order)
        merged["orderId"] = order_id
        merged["_order_update_source"] = str(source or "")
        merged["broker_realtime_callback"] = bool(source == "ws" and order.get("broker_realtime_callback"))
        merged = self._stamp_known_order(merged, seen_live=True)
        should_sync = self._order_needs_sync(prev, merged)
        self._known_orders[order_id] = merged

        if should_sync:
            self._sync_to_pb(merged)
            self._emit_order_transition_callbacks(prev_status, merged)

        logger.debug(
            "Order update applied: source=%s order_id=%s status=%s sync=%s",
            source,
            order_id,
            self._extract_order_status(merged),
            should_sync,
        )
        return True

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
        entry_order_type = str(seed.get("entry_order_type") or "LMT").strip().upper() or "LMT"

        entry_order_id = indexed_ids[0] if len(indexed_ids) > 0 else ""
        tp_order_id = indexed_ids[1] if len(indexed_ids) > 1 else ""
        sl_order_id = indexed_ids[2] if len(indexed_ids) > 2 else ""

        if entry_order_id:
            self._known_orders[entry_order_id] = self._stamp_known_order({
                "orderId": entry_order_id,
                "ticker": symbol,
                "side": side,
                "orderType": entry_order_type,
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
                "auxPrice": sl_price,
                "totalSize": quantity,
                "filledQuantity": 0,
                "avgPrice": 0,
                "parentId": entry_order_id,
                "status": "SUBMITTED",
                "cOID": sl_unique_id,
            }, seen_live=False)

        self._mark_order_activity()
        self._initial_snapshot_pending = True
        self._poll_wakeup.set()

    def on_order_update(self, order: Dict):
        if not isinstance(order, dict):
            return
        applied = self._handle_live_order_payload(order, source="ws")
        if applied:
            self._live_update_count += 1
            self._last_live_update = time.time()
            self._mark_order_activity()

    def _finalize_disappeared_orders(self, current_order_ids: set[str]):
        closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED"}
        missing_ids = [order_id for order_id in list(self._known_orders.keys()) if order_id not in current_order_ids]

        for order_id in missing_ids:
            previous = dict(self._known_orders.get(order_id, {}))
            previous_status = self._extract_order_status(previous)
            missing_poll_count = int(previous.get("_missing_poll_count") or 0) + 1
            previous["_missing_poll_count"] = missing_poll_count
            previous["_first_missing_at"] = float(previous.get("_first_missing_at") or 0.0) or time.time()

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
                self._emit_order_transition_callbacks(previous_status, merged)

            if status in closed_statuses:
                self._known_orders.pop(order_id, None)
            else:
                self._known_orders[order_id] = merged

    def _poll_loop(self):
        logger.info(
            "Order tracker started (mode=%s active_poll=%ds idle_poll=%ds)",
            self._updates_mode(),
            self._active_poll_interval(),
            self._idle_poll_interval(),
        )
        while self._running:
            try:
                self._poll_orders(force=self._initial_snapshot_pending or self._updates_mode() == "poll")
                self._initial_snapshot_pending = False
            except Exception as exc:
                logger.error("Order poll error: %s", exc)

            if not self._running:
                break
            self._poll_wakeup.wait(timeout=self._next_poll_interval())
            self._poll_wakeup.clear()

    def _next_poll_interval(self) -> int:
        if self._updates_mode() == "poll":
            return self._active_poll_interval()
        active_orders = any(self._is_open_order_status(self._extract_order_status(order)) for order in self._known_orders.values())
        last_activity = float(self._last_order_activity or 0.0)
        if last_activity > 0 and (time.time() - last_activity) <= self._fast_track_window():
            active_orders = True
        return self._active_poll_interval() if active_orders else self._idle_poll_interval()

    def _poll_orders(self, force: bool = False):
        orders = self.get_live_orders(force=bool(force))
        self._last_poll = time.time()
        current_order_ids = set()
        for order in orders:
            order_id = self._normalize_text(order.get("orderId") or order.get("order_id"))
            if not order_id:
                continue
            current_order_ids.add(order_id)
            self._handle_live_order_payload(order, source="poll")
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
            runtime_environment = self.environment

            if hasattr(self.pb_client, "upsert_order"):
                normalized_side = str(order.get("side", "")).upper()
                quantity = order.get("totalSize", order.get("quantity", 0))
                fill_qty = order.get("filledQuantity", 0)
                avg_price = order.get("avgPrice", 0)
                commission = abs(self._to_float(order.get("commission"), 0.0))
                limit_price = self._to_float(order.get("price", 0), 0.0)
                stop_trigger_price = self._to_float(order.get("auxPrice", order.get("stop_price", 0)), 0.0)
                parent_id = order.get("parentId") or ""
                order_type = order.get("orderType", "Entry") or "Entry"
                upper_type = str(order_type).upper()
                if not parent_id:
                    role = "entry"
                    if coid.lower().startswith("close_"):
                        role = "close"
                elif upper_type in ("STP", "STOP", "STOPLOSS"):
                    role = "stop_loss"
                else:
                    role = "take_profit"
                if role == "stop_loss" and limit_price <= 0 and stop_trigger_price > 0:
                    limit_price = stop_trigger_price
                relation_status = "closed" if str(status).upper() in ("FILLED", "EXECUTED", "CANCELLED", "CANCELED") else "active"
                mapped_status = {
                    "PRESUBMITTED": "Submitted",
                    "SUBMITTED": "Submitted",
                    "APIPENDING": "Submitted",
                    "API_PENDING": "Submitted",
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
                parent_record = None
                linked_close_entry = None

                if coid:
                    coid_order_filter = (
                        f'unique_id = "{coid_filter}" && environment = "{self._escape_filter_value(runtime_environment)}"'
                    )
                    matches = self.pb_client.get_records("orders", filter=coid_order_filter, sort="-updated", per_page=1)
                    existing_order = matches[0] if matches else None
                if not existing_order and order_id:
                    existing_order = self._find_pb_order_by_broker_id(
                        order_id,
                        runtime_environment=runtime_environment,
                        symbol=symbol,
                        role=role,
                    )

                if existing_order:
                    canonical_unique_id = str(existing_order.get("unique_id") or canonical_unique_id or order_id).strip()
                    signal_id = signal_id or str(existing_order.get("signal_id") or "").strip()
                    trade_group_id = str(existing_order.get("trade_group_id") or trade_group_id or "").strip()
                    entry_order_unique_id = str(existing_order.get("entry_order_unique_id") or entry_order_unique_id or canonical_unique_id).strip()
                    parent_order_unique_id = str(existing_order.get("parent_order_unique_id") or "").strip()
                    role = str(existing_order.get("role") or role).strip() or role
                elif parent_id:
                    parent_record = self._find_pb_order_by_broker_id(
                        str(parent_id),
                        runtime_environment=runtime_environment,
                        symbol=symbol,
                        role="entry",
                    )
                    if parent_record:
                        parent_order_unique_id = str(parent_record.get("unique_id") or "").strip()
                        trade_group_id = str(parent_record.get("trade_group_id") or parent_record.get("entry_order_unique_id") or trade_group_id).strip()
                        entry_order_unique_id = str(parent_record.get("entry_order_unique_id") or parent_order_unique_id or entry_order_unique_id).strip()
                        signal_id = signal_id or str(parent_record.get("signal_id") or "").strip()
                elif not parent_id and role in {"entry", "close"} and upper_type in {"MKT", "MARKET"}:
                    linked_close_entry = self._find_pb_entry_for_close_order(
                        symbol=symbol,
                        side=normalized_side,
                        quantity=quantity,
                        runtime_environment=runtime_environment,
                    )
                    if linked_close_entry:
                        role = "close"
                        parent_order_unique_id = str(linked_close_entry.get("unique_id") or "").strip()
                        trade_group_id = str(
                            linked_close_entry.get("trade_group_id")
                            or linked_close_entry.get("entry_order_unique_id")
                            or parent_order_unique_id
                            or trade_group_id
                        ).strip()
                        entry_order_unique_id = str(
                            linked_close_entry.get("entry_order_unique_id")
                            or parent_order_unique_id
                            or entry_order_unique_id
                        ).strip()
                        signal_id = signal_id or str(linked_close_entry.get("signal_id") or "").strip()

                if not canonical_unique_id:
                    canonical_unique_id = order_id
                if not trade_group_id:
                    trade_group_id = entry_order_unique_id or canonical_unique_id
                if not entry_order_unique_id:
                    entry_order_unique_id = canonical_unique_id

                position_side = self._infer_position_side(
                    side=normalized_side,
                    role=role,
                    parent_id=parent_id,
                    coid=coid,
                    trade_group_id=trade_group_id,
                    entry_order_unique_id=entry_order_unique_id,
                    signal_id=signal_id,
                    existing_order=existing_order,
                    parent_record=parent_record,
                )
                if role == "close":
                    position_side = (
                        self._record_explicit_direction(existing_order)
                        or self._record_explicit_direction(linked_close_entry)
                        or self._position_side_from_close_order_side(normalized_side)
                        or position_side
                    )

                extra = {
                    "source": "order_tracker",
                    "seen_live": bool(order.get("_seen_live")),
                    "broker_update_source": str(order.get("_order_update_source") or ""),
                    "broker_realtime_callback": bool(order.get("broker_realtime_callback")),
                }
                for source_key, target_key in (
                    ("ib_callback_type", "ib_callback_type"),
                    ("broker_callback_source", "broker_callback_source"),
                    ("broker_callback_received_at", "broker_callback_received_at"),
                    ("broker_callback_received_at_ms", "broker_callback_received_at_ms"),
                    ("ib_exec_id", "ib_exec_id"),
                    ("execution_shares", "execution_shares"),
                    ("execution_price", "execution_price"),
                    ("lastFillPrice", "last_fill_price"),
                    ("lastExecutionTime", "last_execution_time"),
                ):
                    if order.get(source_key) not in (None, ""):
                        extra[target_key] = order.get(source_key)
                if coid:
                    extra["coid"] = coid
                if role == "close":
                    extra.update(
                        {
                            "close_order": True,
                            "close_link_source": "existing_order" if existing_order else ("symbol_side_quantity" if linked_close_entry else "client_order_id"),
                            "linked_entry_order_unique_id": entry_order_unique_id,
                            "linked_trade_group_id": trade_group_id,
                            "close_side": normalized_side,
                        }
                    )
                if commission:
                    extra.update(
                        {
                            "commission": commission,
                            "ibkr_commission": commission,
                            "commission_currency": str(order.get("commissionCurrency") or "USD"),
                        }
                    )
                if order.get("_status_inferred"):
                    extra["status_inferred"] = True
                    extra["status_inferred_reason"] = str(order.get("_status_inferred_reason") or "")

                order_payload = {
                    "unique_id": canonical_unique_id,
                    "order_id": order_id,
                    "broker_order_id": order_id,
                    "order_type": order_type,
                    "symbol": symbol,
                    "direction": position_side,
                    "position_side": position_side,
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
                    "commission": commission,
                    "us_time": now_str,
                    "bar_time_ms": int(time.time() * 1000),
                    "extra": extra,
                }
                if role == "stop_loss" and limit_price > 0:
                    order_payload["sl_price"] = limit_price
                elif role == "take_profit" and limit_price > 0:
                    order_payload["tp_price"] = limit_price
                self.pb_client.upsert_order(order_payload)
        except Exception as exc:
            logger.debug("PB order sync failed: %s", exc)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="order-tracker")
        self._thread.start()
        self._poll_wakeup.set()

    def stop(self):
        self._running = False
        self._poll_wakeup.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def status(self) -> dict:
        return {
            "running": self._running,
            "mode": self._updates_mode(),
            "tracked_orders": len(self._known_orders),
            "active_poll_interval_s": self._active_poll_interval(),
            "idle_poll_interval_s": self._idle_poll_interval(),
            "fast_track_window_s": self._fast_track_window(),
            "live_update_count": self._live_update_count,
            "last_poll": datetime.fromtimestamp(self._last_poll, timezone.utc).isoformat() if self._last_poll else None,
            "last_live_update": datetime.fromtimestamp(self._last_live_update, timezone.utc).isoformat() if self._last_live_update else None,
        }
