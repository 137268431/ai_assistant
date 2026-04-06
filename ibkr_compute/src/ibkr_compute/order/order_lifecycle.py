"""
订单生命周期管理
- EOD 15:55 ET 平仓所有信号仓位
- 每日重置
- Ghost order 清理
- 持仓查询
"""

import os
import time
import logging
import requests
import threading
from typing import Dict, List, Optional, Callable
from datetime import datetime, timezone, timedelta

from ibkr_compute.gateway.cookie_store import load_cookies, save_cookies

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")
ET = timezone(timedelta(hours=-4))

EOD_CLOSE_HOUR = 15
EOD_CLOSE_MINUTE = 55
KEEP_SYMBOLS = set(os.environ.get("IBKR_EOD_KEEP_SYMBOLS", "").upper().split(","))


class OrderLifecycle:
    def __init__(self, gateway_url: str = None, account_id: str = None,
                 pb_client=None, order_modifier=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.account_id = account_id or ACCOUNT_ID
        self.pb_client = pb_client
        self.order_modifier = order_modifier

        self._session = requests.Session()
        self._session.verify = False
        load_cookies(self._session)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._eod_closed_today = False
        self._daily_sl_count = 0
        self._daily_position_count = 0

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def get_positions(self, acct_id: str = None) -> List[Dict]:
        acct = acct_id or self.account_id
        try:
            load_cookies(self._session)
            resp = self._session.get(
                self._api_url(f"/portfolio/{acct}/positions/0"),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            save_cookies(self._session)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning("Failed to get positions: %s", e)
            return []

    def get_account_summary(self, acct_id: str = None) -> Dict:
        acct = acct_id or self.account_id
        try:
            load_cookies(self._session)
            resp = self._session.get(
                self._api_url(f"/portfolio/{acct}/summary"),
                timeout=15,
            )
            resp.raise_for_status()
            payload = resp.json()
            save_cookies(self._session)
            return payload
        except Exception as e:
            logger.warning("Failed to get account summary: %s", e)
            return {}

    def eod_close_all(self, acct_id: str = None) -> Dict:
        acct = acct_id or self.account_id
        positions = self.get_positions(acct)
        closed = 0
        errors = 0

        if self.order_modifier:
            self.order_modifier.cancel_all_orders(acct)

        for pos in positions:
            symbol = pos.get("ticker", pos.get("contractDesc", "")).upper()
            position_qty = pos.get("position", 0)
            conid = pos.get("conid")

            if not position_qty or not conid:
                continue
            if symbol in KEEP_SYMBOLS:
                logger.info("Keeping EOD position: %s", symbol)
                continue

            side = "SELL" if position_qty > 0 else "BUY"
            qty = abs(position_qty)

            try:
                url = self._api_url(f"/iserver/account/{acct}/orders")
                orders = [{
                    "acctId": acct,
                    "conid": conid,
                    "orderType": "MKT",
                    "side": side,
                    "quantity": qty,
                    "tif": "DAY",
                    "cOID": f"eod_{symbol}_{datetime.now(ET).strftime('%H%M%S')}",
                }]

                load_cookies(self._session)
                resp = self._session.post(url, json={"orders": orders}, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                save_cookies(self._session)

                if isinstance(data, list) and data and data[0].get("id"):
                    reply_url = self._api_url(f"/iserver/reply/{data[0]['id']}")
                    load_cookies(self._session)
                    self._session.post(reply_url, json={"confirmed": True}, timeout=15)
                    save_cookies(self._session)

                closed += 1
                logger.info("EOD close: %s %s %d shares", symbol, side, qty)

            except Exception as e:
                errors += 1
                logger.error("EOD close failed for %s: %s", symbol, e)

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

    def increment_position_count(self):
        self._daily_position_count += 1

    @property
    def is_sl_circuit_breaker(self) -> bool:
        return self._daily_sl_count >= 3

    @property
    def is_position_limit_reached(self) -> bool:
        return self._daily_position_count >= 3

    def _lifecycle_loop(self):
        logger.info("Order lifecycle monitor started")
        while self._running:
            et_now = datetime.now(ET)

            if et_now.hour == 0 and et_now.minute < 5:
                self.daily_reset()

            if (et_now.hour == EOD_CLOSE_HOUR and
                    et_now.minute >= EOD_CLOSE_MINUTE and
                    not self._eod_closed_today):
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
                symbol = pos.get("ticker", pos.get("contractDesc", ""))
                conid = pos.get("conid")
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

        except Exception as e:
            logger.debug("Position sync failed: %s", e)

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
        return {
            "running": self._running,
            "eod_closed_today": self._eod_closed_today,
            "daily_sl_count": self._daily_sl_count,
            "daily_position_count": self._daily_position_count,
            "sl_circuit_breaker": self.is_sl_circuit_breaker,
            "position_limit_reached": self.is_position_limit_reached,
        }
