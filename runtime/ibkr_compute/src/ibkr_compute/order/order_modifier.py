"""
IB Gateway order modification and cancellation helpers.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict

from ibkr_compute.broker import BrokerAdapter
from ibkr_compute.observability.prometheus import record_gateway_order_serial_event, record_order_event
from ibkr_compute.order.gateway_serial import GatewayOrderMutationGate, GatewayOrderMutationTimeout
from ibkr_compute.order.symbol_queue import SymbolOrderCommandScheduler

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")


class OrderModifier:
    def __init__(
        self,
        gateway_url: str = None,
        account_id: str = None,
        pb_client=None,
        broker: BrokerAdapter | None = None,
        config=None,
        environment: str = "live",
        gateway_gate: GatewayOrderMutationGate | None = None,
        symbol_scheduler: SymbolOrderCommandScheduler | None = None,
    ):
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.broker = broker or BrokerAdapter()
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.gateway_gate = gateway_gate or GatewayOrderMutationGate(config=config, environment=self.environment)
        self.symbol_scheduler = symbol_scheduler or SymbolOrderCommandScheduler(
            config=config,
            environment=self.environment,
        )

    @staticmethod
    def _infer_modify_family(updates: Dict[str, Any] | None, fallback: str = "unknown") -> str:
        update_keys = {str(key or "").strip() for key in (updates or {}).keys()}
        if "auxPrice" in update_keys and not ({"price", "lmtPrice"} & update_keys):
            return "stop_loss"
        if {"price", "lmtPrice"} & update_keys and "auxPrice" not in update_keys:
            return "take_profit"
        if "quantity" in update_keys or "tif" in update_keys:
            return "entry"
        if update_keys:
            return "mixed"
        return fallback or "unknown"

    def modify_order(
        self,
        order_id: str,
        updates: Dict[str, Any],
        acct_id: str = None,
        *,
        operation: str = "modify_manual",
        order_family_type: str = "",
        symbol: str = "",
    ) -> Dict[str, Any]:
        family = str(order_family_type or "").strip() or self._infer_modify_family(updates)
        queue_symbol = str(symbol or self._infer_order_symbol(order_id) or "").strip().upper()
        return self.symbol_scheduler.submit(
            symbol=queue_symbol,
            operation="modify_order",
            priority=20,
            metadata={"order_id": str(order_id or "").strip(), "family": family},
            fn=lambda: self._modify_order_unqueued(
                order_id,
                updates,
                acct_id,
                operation=operation,
                order_family_type=family,
            ),
        )

    def _modify_order_unqueued(
        self,
        order_id: str,
        updates: Dict[str, Any],
        acct_id: str = None,
        *,
        operation: str = "modify_manual",
        order_family_type: str = "",
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        family = str(order_family_type or "").strip() or self._infer_modify_family(updates)
        try:
            if getattr(self.broker, "uses_internal_gateway_write_lock", False):
                result = self.broker.modify_order(
                    str(order_id or "").strip(),
                    dict(updates or {}),
                    account_id=str(acct_id or self.account_id or "").strip(),
                    metric_environment=self.environment,
                )
            else:
                with self.gateway_gate.hold("modify_order", order_id=str(order_id or "").strip(), family=family) as gate_info:
                    result = self.broker.modify_order(
                        str(order_id or "").strip(),
                        dict(updates or {}),
                        account_id=str(acct_id or self.account_id or "").strip(),
                    )
                record_gateway_order_serial_event(
                    environment=self.environment,
                    operation="modify_order",
                    result="ok" if result.get("ok") else "error",
                    queue_wait_s=gate_info.get("queue_wait_s"),
                )
        except GatewayOrderMutationTimeout as exc:
            result = {
                "ok": False,
                "error": "gateway_order_queue_timeout",
                "queue_timeout_s": exc.timeout_s,
                "gateway_operation": exc.operation,
                "order_id": str(order_id or "").strip(),
            }
            record_gateway_order_serial_event(
                environment=self.environment,
                operation=exc.operation,
                result="timeout",
                queue_wait_s=exc.timeout_s,
            )
        record_order_event(
            operation=str(operation or "modify_manual"),
            order_family_type=family,
            result="ok" if result.get("ok") else "error",
            reason_code=str(result.get("error") or result.get("reason") or "ok"),
            duration_s=time.perf_counter() - started,
        )
        if not result.get("ok"):
            logger.error("Order modify failed for %s: %s", order_id, result.get("error"))
        return result

    def update_stop_loss(self, order_id: str, new_sl_price: float, acct_id: str = None) -> Dict[str, Any]:
        logger.info("Updating stop loss %s to %.4f", order_id, float(new_sl_price or 0.0))
        return self.modify_order(
            order_id,
            {"auxPrice": float(new_sl_price or 0.0)},
            acct_id,
            operation="adjust_stop_loss",
            order_family_type="stop_loss",
        )

    def update_take_profit(self, order_id: str, new_tp_price: float, acct_id: str = None) -> Dict[str, Any]:
        logger.info("Updating take profit %s to %.4f", order_id, float(new_tp_price or 0.0))
        return self.modify_order(
            order_id,
            {"price": float(new_tp_price or 0.0)},
            acct_id,
            operation="adjust_take_profit",
            order_family_type="take_profit",
        )

    def cancel_order(
        self,
        order_id: str,
        acct_id: str = None,
        *,
        operation: str = "cancel_order",
        order_family_type: str = "unknown",
        symbol: str = "",
    ) -> Dict[str, Any]:
        queue_symbol = str(symbol or self._infer_order_symbol(order_id) or "").strip().upper()
        return self.symbol_scheduler.submit(
            symbol=queue_symbol,
            operation="cancel_order",
            priority=10,
            metadata={"order_id": str(order_id or "").strip(), "family": str(order_family_type or "unknown")},
            fn=lambda: self._cancel_order_unqueued(
                order_id,
                acct_id,
                operation=operation,
                order_family_type=order_family_type,
            ),
        )

    def _cancel_order_unqueued(
        self,
        order_id: str,
        acct_id: str = None,
        *,
        operation: str = "cancel_order",
        order_family_type: str = "unknown",
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            if getattr(self.broker, "uses_internal_gateway_write_lock", False):
                result = self.broker.cancel_order(
                    str(order_id or "").strip(),
                    metric_environment=self.environment,
                )
            else:
                with self.gateway_gate.hold("cancel_order", order_id=str(order_id or "").strip()) as gate_info:
                    result = self.broker.cancel_order(str(order_id or "").strip())
                record_gateway_order_serial_event(
                    environment=self.environment,
                    operation="cancel_order",
                    result="ok" if result.get("ok") else "error",
                    queue_wait_s=gate_info.get("queue_wait_s"),
                )
        except GatewayOrderMutationTimeout as exc:
            result = {
                "ok": False,
                "error": "gateway_order_queue_timeout",
                "queue_timeout_s": exc.timeout_s,
                "gateway_operation": exc.operation,
                "order_id": str(order_id or "").strip(),
            }
            record_gateway_order_serial_event(
                environment=self.environment,
                operation=exc.operation,
                result="timeout",
                queue_wait_s=exc.timeout_s,
            )
        record_order_event(
            operation=str(operation or "cancel_order"),
            order_family_type=str(order_family_type or "unknown"),
            result="ok" if result.get("ok") else "error",
            reason_code=str(result.get("error") or result.get("reason") or "ok"),
            duration_s=time.perf_counter() - started,
        )
        if not result.get("ok"):
            logger.error("Cancel order %s failed: %s", order_id, result.get("error"))
        return result

    def cancel_all_orders(self, acct_id: str = None) -> Dict[str, Any]:
        try:
            if getattr(self.broker, "uses_internal_gateway_write_lock", False):
                result = self.broker.cancel_all_orders(metric_environment=self.environment)
            else:
                with self.gateway_gate.hold("cancel_all_orders") as gate_info:
                    result = self.broker.cancel_all_orders()
                record_gateway_order_serial_event(
                    environment=self.environment,
                    operation="cancel_all_orders",
                    result="ok" if result.get("ok") else "error",
                    queue_wait_s=gate_info.get("queue_wait_s"),
                )
            return result
        except GatewayOrderMutationTimeout as exc:
            record_gateway_order_serial_event(
                environment=self.environment,
                operation=exc.operation,
                result="timeout",
                queue_wait_s=exc.timeout_s,
            )
            return {
                "ok": False,
                "error": "gateway_order_queue_timeout",
                "queue_timeout_s": exc.timeout_s,
                "gateway_operation": exc.operation,
            }

    def _infer_order_symbol(self, order_id: str) -> str:
        snapshotter = getattr(getattr(self.broker, "client", None), "get_order_snapshot", None)
        if not callable(snapshotter):
            return ""
        try:
            snapshot = snapshotter(str(order_id or "").strip())
        except Exception:
            return ""
        if not isinstance(snapshot, dict):
            return ""
        return str(snapshot.get("symbol") or snapshot.get("ticker") or "").strip().upper()
