"""
反向信号处理
- close: 平仓
- cancel: 取消待成交订单
- adjust_sl: 调整止损
- adjust_tp: 调整止盈
"""

import logging
from typing import Dict, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

ET = timezone(timedelta(hours=-4))

REVERSE_ACTIONS = {"close", "cancel", "adjust_sl", "adjust_tp"}


class ReverseSignalHandler:
    def __init__(self, pb_client, order_placer=None, order_modifier=None,
                 order_lifecycle=None, signal_processor=None,
                 conid_resolver=None, environment: str = "live"):
        self.pb_client = pb_client
        self.order_placer = order_placer
        self.order_modifier = order_modifier
        self.order_lifecycle = order_lifecycle
        self.signal_processor = signal_processor
        self.conid_resolver = conid_resolver
        self.environment = environment
        self._processed_ids = set()

    def check_and_process(self):
        try:
            et_now = datetime.now(ET)
            today = et_now.strftime("%Y-%m-%d")

            records = self.pb_client.get_records(
                "reverse_signals",
                filter=f'status = "pending" && date = "{today}"',
                sort="-created",
                per_page=20,
            )

            for r in records:
                rid = r.get("id", "")
                if rid in self._processed_ids:
                    continue

                action = r.get("action", "").lower()
                if action not in REVERSE_ACTIONS:
                    continue

                result = self._process_reverse(r, action)
                self._processed_ids.add(rid)

                try:
                    self.pb_client.update_record("reverse_signals", rid, {
                        "status": "processed" if result else "failed",
                        "processed_time": et_now.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except Exception as e:
                    logger.debug("Failed to update reverse signal status: %s", e)

        except Exception as e:
            logger.error("Reverse signal check failed: %s", e)

    def _process_reverse(self, signal: dict, action: str) -> bool:
        symbol = signal.get("symbol", "").upper()
        logger.info("Processing reverse signal: %s %s", action, symbol)

        if action == "close":
            return self._handle_close(signal)
        elif action == "cancel":
            return self._handle_cancel(signal)
        elif action == "adjust_sl":
            return self._handle_adjust_sl(signal)
        elif action == "adjust_tp":
            return self._handle_adjust_tp(signal)

        return False

    def _handle_close(self, signal: dict) -> bool:
        if not self.order_lifecycle or not self.order_placer:
            logger.warning("Order placer/lifecycle not configured for close")
            return False

        symbol = signal.get("symbol", "").upper()
        conid = self._resolve_conid(symbol)
        if not conid:
            return False

        positions = self.order_lifecycle.get_positions()
        for pos in positions:
            pos_symbol = pos.get("ticker", pos.get("contractDesc", "")).upper()
            if pos_symbol == symbol:
                qty = pos.get("position", 0)
                if qty == 0:
                    continue
                direction = "long" if qty > 0 else "short"
                result = self.order_placer.place_market_close(
                    conid, symbol, direction, abs(qty),
                )
                if result.get("ok"):
                    if self.signal_processor:
                        self.signal_processor.remove_position(symbol)
                    return True

        logger.warning("No position found for close: %s", symbol)
        return False

    def _handle_cancel(self, signal: dict) -> bool:
        if not self.order_modifier:
            return False

        bracket_group = signal.get("bracket_group", "")
        order_id = signal.get("order_id", "")

        if order_id:
            result = self.order_modifier.cancel_order(order_id)
            return result.get("ok", False)

        if bracket_group:
            orders = []
            try:
                orders = self.pb_client.get_records(
                    "orders",
                    filter=f'trade_group_id = "{bracket_group}" && status != "Filled" && status != "Canceled"',
                )
            except Exception:
                orders = []

            if not orders:
                orders = self.pb_client.get_records(
                    "ibkr_orders",
                    filter=f'bracket_group = "{bracket_group}" && status != "FILLED"',
                )

            cancelled = 0
            for o in orders:
                oid = o.get("broker_order_id") or o.get("orderId")
                if oid:
                    r = self.order_modifier.cancel_order(oid)
                    if r.get("ok"):
                        cancelled += 1
            return cancelled > 0

        return False

    def _handle_adjust_sl(self, signal: dict) -> bool:
        if not self.order_modifier:
            return False
        order_id = signal.get("sl_order_id", "")
        new_price = float(signal.get("new_sl", 0))
        if not order_id or new_price <= 0:
            return False
        result = self.order_modifier.update_stop_loss(order_id, new_price)
        return result.get("ok", False)

    def _handle_adjust_tp(self, signal: dict) -> bool:
        if not self.order_modifier:
            return False
        order_id = signal.get("tp_order_id", "")
        new_price = float(signal.get("new_tp", 0))
        if not order_id or new_price <= 0:
            return False
        result = self.order_modifier.update_take_profit(order_id, new_price)
        return result.get("ok", False)

    def _resolve_conid(self, symbol: str) -> Optional[int]:
        if self.conid_resolver:
            return self.conid_resolver.resolve(symbol)
        return None

    def daily_reset(self):
        self._processed_ids.clear()
