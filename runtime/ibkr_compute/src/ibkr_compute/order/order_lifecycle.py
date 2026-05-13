"""
Order lifecycle helpers built on top of IB Gateway socket API.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from ibkr_compute.core.time_utils import ET
from typing import Dict, List, Optional

from ibkr_compute.broker import BrokerAdapter

logger = logging.getLogger(__name__)

ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")

DEFAULT_EOD_CLOSE_TIME = (15, 55)
DEFAULT_POSITION_LIMIT_MAX = 0
DEFAULT_CONSECUTIVE_STOP_LOSS_LIMIT = 3
DEFAULT_KEEP_SYMBOLS = tuple(
    symbol.strip().upper()
    for symbol in os.environ.get("IBKR_EOD_KEEP_SYMBOLS", "").split(",")
    if symbol.strip()
)


class OrderLifecycle:
    def __init__(
        self,
        gateway_url: str = None,
        account_id: str = None,
        pb_client=None,
        order_modifier=None,
        config=None,
        environment: str = "live",
        broker: BrokerAdapter | None = None,
    ):
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.order_modifier = order_modifier
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.broker = broker or BrokerAdapter()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._eod_closed_today = False
        self._daily_sl_count = 0
        self._daily_position_count = 0

    def _get_config_value(self, key: str, default: str) -> str:
        if not self.config:
            return default
        return str(self.config.get_for_environment(key, self.environment, default) or default)

    def _get_config_int(self, key: str, default: int) -> int:
        if not self.config:
            return default
        return self.config.get_int_for_environment(key, self.environment, default)

    def _eod_close_time(self) -> tuple[int, int]:
        raw_value = self._get_config_value(
            "eod_close_time",
            f"{DEFAULT_EOD_CLOSE_TIME[0]:02d}:{DEFAULT_EOD_CLOSE_TIME[1]:02d}",
        ).strip()
        try:
            hour_text, minute_text = raw_value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        return DEFAULT_EOD_CLOSE_TIME

    def _position_limit_max(self) -> int:
        return max(0, self._get_config_int("position_limit_max", DEFAULT_POSITION_LIMIT_MAX))

    def _consecutive_stop_loss_limit(self) -> int:
        return max(1, self._get_config_int("consecutive_stop_loss_limit", DEFAULT_CONSECUTIVE_STOP_LOSS_LIMIT))

    def _keep_symbols(self) -> set[str]:
        raw_value = self._get_config_value("eod_keep_symbols", ",".join(DEFAULT_KEEP_SYMBOLS))
        return {
            symbol.strip().upper()
            for symbol in str(raw_value or "").split(",")
            if symbol.strip()
        }

    def get_positions(self, acct_id: str = None) -> List[Dict]:
        try:
            return list(self.broker.list_positions() or [])
        except Exception as exc:
            logger.warning("Failed to get positions: %s", exc)
            return []

    def get_account_summary(self, acct_id: str = None) -> Dict:
        try:
            return dict(self.broker.get_account_summary() or {})
        except Exception as exc:
            logger.warning("Failed to get account summary: %s", exc)
            return {}

    def get_account_snapshot(self, acct_id: str = None) -> Dict:
        try:
            return dict(self.broker.get_account_snapshot(account=acct_id) or {})
        except Exception as exc:
            logger.warning("Failed to get account snapshot: %s", exc)
            return {}

    def eod_close_all(self, acct_id: str = None) -> Dict:
        positions = self.get_positions(acct_id)
        closed = 0
        errors = 0

        if self.order_modifier:
            self.order_modifier.cancel_all_orders(acct_id)

        for pos in positions:
            symbol = str(pos.get("ticker") or pos.get("contractDesc") or "").upper()
            position_qty = float(pos.get("position", 0) or 0)
            conid = int(pos.get("conid", 0) or 0)
            if not position_qty or not conid:
                continue
            if symbol in self._keep_symbols():
                logger.info("Keeping EOD position: %s", symbol)
                continue

            direction = "long" if position_qty > 0 else "short"
            result = self.broker.place_market_close(
                conid=conid,
                symbol=symbol,
                direction=direction,
                quantity=abs(int(round(position_qty))),
            )
            if result.get("ok"):
                closed += 1
                logger.info("EOD close: %s %s %s shares", symbol, direction, abs(position_qty))
            else:
                errors += 1
                logger.error("EOD close failed for %s: %s", symbol, result.get("error"))

        self._eod_closed_today = True
        logger.info("EOD close complete: %d closed, %d errors", closed, errors)
        return {"closed": closed, "errors": errors}

    def daily_reset(self):
        self._eod_closed_today = False
        self._daily_sl_count = 0
        self._daily_position_count = 0
        logger.info("Daily lifecycle reset")

    def increment_sl_count(self):
        self._daily_sl_count += 1

    def reset_sl_count(self):
        self._daily_sl_count = 0

    def increment_position_count(self):
        self._daily_position_count += 1

    def handle_protection_incomplete(
        self,
        *,
        signal_id: str = "",
        symbol: str = "",
        direction: str = "",
        result: Optional[Dict] = None,
        order: Optional[Dict] = None,
        reason: str = "protection_incomplete",
        auto_cancel: bool = False,
    ) -> Dict:
        result = result if isinstance(result, dict) else {}
        order = order if isinstance(order, dict) else {}
        order_ids = [
            str(item or "").strip()
            for item in (result.get("order_ids") or [])
            if str(item or "").strip()
        ]
        missing_order_ids = [
            str(item or "").strip()
            for item in (result.get("missing_order_ids") or [])
            if str(item or "").strip()
        ]
        missing_protection_roles = [
            str(item or "").strip()
            for item in (result.get("missing_protection_roles") or [])
            if str(item or "").strip()
        ]
        diagnostic = {
            "status": "protection_incomplete",
            "reason": str(reason or "protection_incomplete"),
            "signal_id": str(signal_id or result.get("signal_id") or order.get("signal_id") or "").strip(),
            "symbol": str(symbol or order.get("ticker") or order.get("symbol") or "").strip().upper(),
            "direction": str(direction or result.get("direction") or "").strip().lower(),
            "protection_complete": False,
            "missing_order_ids": missing_order_ids,
            "submitted_order_ids": order_ids,
            "missing_protection_roles": missing_protection_roles,
            "protection_order_statuses": dict(result.get("protection_order_statuses") or {}),
            "protection_orders_checked": int(result.get("protection_orders_checked") or 0),
            "entry_order_id": str(
                order.get("orderId")
                or order.get("order_id")
                or (order_ids[0] if order_ids else "")
                or ""
            ).strip(),
            "bracket_group": str(result.get("bracket_group") or "").strip(),
            "auto_cancel_requested": bool(auto_cancel),
            "auto_cancel_executed": False,
            "safe_action": "diagnostic_only_no_broker_call",
            "recommended_action": "review_and_cancel_or_repair_unprotected_entry",
            "cancel_recommended": True,
        }
        logger.error(
            "Bracket protection incomplete: signal_id=%s symbol=%s missing_order_ids=%s action=%s",
            diagnostic["signal_id"] or "-",
            diagnostic["symbol"] or "-",
            ",".join(missing_order_ids) or "-",
            diagnostic["safe_action"],
        )
        return diagnostic

    @property
    def is_sl_circuit_breaker(self) -> bool:
        return self._daily_sl_count >= self._consecutive_stop_loss_limit()

    @property
    def is_position_limit_reached(self) -> bool:
        position_limit = self._position_limit_max()
        return position_limit > 0 and self._daily_position_count >= position_limit

    def _lifecycle_loop(self):
        logger.info("Order lifecycle monitor started")
        while self._running:
            et_now = datetime.now(ET)
            if et_now.hour == 0 and et_now.minute < 5:
                self.daily_reset()

            eod_close_hour, eod_close_minute = self._eod_close_time()
            if (et_now.hour, et_now.minute) >= (eod_close_hour, eod_close_minute) and not self._eod_closed_today:
                logger.info("EOD close triggered at %s", et_now.strftime("%H:%M:%S"))
                self.eod_close_all()

            self._sync_positions_to_pb()
            for _ in range(30):
                if not self._running:
                    break
                time.sleep(1)

    def _sync_positions_to_pb(self):
        if not self.pb_client:
            return
        try:
            positions = self.get_positions()
            for pos in positions:
                symbol = str(pos.get("ticker") or pos.get("contractDesc") or "").upper()
                conid = int(pos.get("conid", 0) or 0)
                if not symbol or not conid:
                    continue
                data = {
                    "symbol": symbol,
                    "conid": conid,
                    "quantity": pos.get("position", 0),
                    "avgCost": pos.get("avgCost", 0),
                    "mktPrice": pos.get("mktPrice", 0),
                    "unrealizedPnl": pos.get("unrealizedPnl", 0),
                    "account": self.account_id,
                    "us_time": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                }
                existing = self.pb_client.get_records(
                    "ibkr_positions",
                    filter=f'symbol = "{symbol}" && account = "{self.account_id}"',
                    per_page=1,
                )
                if existing:
                    self.pb_client.update_record("ibkr_positions", existing[0]["id"], data)
                else:
                    self.pb_client.create_record("ibkr_positions", data)
        except Exception as exc:
            logger.debug("Position sync failed: %s", exc)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._lifecycle_loop, daemon=True, name="order-lifecycle")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def status(self) -> dict:
        eod_close_hour, eod_close_minute = self._eod_close_time()
        return {
            "running": self._running,
            "environment": self.environment,
            "eod_closed_today": self._eod_closed_today,
            "eod_close_time": f"{eod_close_hour:02d}:{eod_close_minute:02d}",
            "eod_keep_symbols": sorted(self._keep_symbols()),
            "daily_sl_count": self._daily_sl_count,
            "consecutive_stop_loss_count": self._daily_sl_count,
            "consecutive_stop_loss_limit": self._consecutive_stop_loss_limit(),
            "daily_position_count": self._daily_position_count,
            "position_limit_max": self._position_limit_max(),
            "sl_circuit_breaker": self.is_sl_circuit_breaker,
            "position_limit_reached": self.is_position_limit_reached,
        }
