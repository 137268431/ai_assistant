"""IB Gateway market data subscription client."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Set

from ibkr_compute.broker import BrokerAdapter

logger = logging.getLogger(__name__)


class IBKRWebSocketClient:
    def __init__(self, gateway_url: str = None, on_tick: Callable = None, config=None, environment: str = "live", broker: BrokerAdapter | None = None):
        self.on_tick = on_tick
        self.on_order_update: Optional[Callable[[Dict[str, Any]], None]] = None
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"
        self.broker = broker or BrokerAdapter()

        self._running = False
        self._connected = False
        self._ready = False
        self._last_message_time: Optional[float] = None
        self._message_count = 0
        self._order_update_count = 0
        self._state_lock = threading.RLock()
        self._subscribed_conids: Set[int] = set()
        self._pending_subscriptions: Set[int] = set()
        self._order_updates_enabled = False

    @property
    def is_connected(self) -> bool:
        with self._state_lock:
            return bool(self._connected)

    def set_order_update_callback(self, callback: Callable[[Dict[str, Any]], None]):
        self.on_order_update = callback

    def set_order_updates_enabled(self, enabled: bool):
        self._order_updates_enabled = bool(enabled)

    def _handle_tick(self, tick_data: dict):
        with self._state_lock:
            self._connected = True
            self._ready = True
            self._last_message_time = time.time()
            self._message_count += 1
        if callable(self.on_tick):
            self.on_tick(dict(tick_data))

    def _handle_order_update(self, payload: dict):
        if not self._order_updates_enabled or not callable(self.on_order_update):
            return
        with self._state_lock:
            self._order_update_count += 1
        self.on_order_update(dict(payload))

    def start(self):
        if self._running:
            return
        self._running = True
        self.broker.add_market_data_listener(self._handle_tick)
        self.broker.add_order_update_listener(self._handle_order_update)
        try:
            self._ready = bool(self.broker.connect())
            self._connected = self._ready
        except Exception as exc:
            logger.warning("IB Gateway market data client start failed: %s", exc)
            self._connected = False
            self._ready = False
        self._flush_pending()

    def stop(self):
        self._running = False
        self.broker.remove_market_data_listener(self._handle_tick)
        self.broker.remove_order_update_listener(self._handle_order_update)
        with self._state_lock:
            subscribed = list(self._subscribed_conids)
            self._subscribed_conids.clear()
            self._pending_subscriptions.clear()
            self._connected = False
            self._ready = False
        for conid in subscribed:
            try:
                self.broker.unsubscribe_market_data(conid)
            except Exception:
                logger.exception("Failed to unsubscribe market data for %s", conid)

    def _flush_pending(self):
        with self._state_lock:
            pending = list(self._pending_subscriptions)
        for conid in pending:
            self._send_subscription(conid)

    def subscribe(self, conid: int):
        try:
            normalized = int(conid)
        except (TypeError, ValueError):
            return
        with self._state_lock:
            self._pending_subscriptions.add(normalized)
        if self._running:
            self._send_subscription(normalized)

    def unsubscribe(self, conid: int):
        try:
            normalized = int(conid)
        except (TypeError, ValueError):
            return
        with self._state_lock:
            self._pending_subscriptions.discard(normalized)
            self._subscribed_conids.discard(normalized)
        try:
            self.broker.unsubscribe_market_data(normalized)
        except Exception:
            logger.exception("Failed to unsubscribe %s", normalized)

    def resubscribe(self, conid: int):
        try:
            normalized = int(conid)
        except (TypeError, ValueError):
            return
        with self._state_lock:
            self._pending_subscriptions.discard(normalized)
            self._subscribed_conids.discard(normalized)
        try:
            self.broker.unsubscribe_market_data(normalized)
        except Exception:
            logger.exception("Failed to unsubscribe before resubscribe %s", normalized)
        with self._state_lock:
            self._pending_subscriptions.add(normalized)
        if self._running:
            self._send_subscription(normalized)

    def _send_subscription(self, conid: int):
        with self._state_lock:
            if conid in self._subscribed_conids:
                self._pending_subscriptions.discard(conid)
                return
        try:
            self.broker.subscribe_market_data(conid=conid, symbol="")
        except Exception as exc:
            logger.warning("Failed to subscribe conid=%s: %s", conid, exc)
            return
        with self._state_lock:
            self._subscribed_conids.add(conid)
            self._pending_subscriptions.discard(conid)

    def status(self) -> dict:
        now_ts = time.time()
        with self._state_lock:
            last_message = self._last_message_time
            return {
                "connected": bool(self._connected),
                "ready": bool(self._ready),
                "running": bool(self._running),
                "message_count": int(self._message_count),
                "order_update_count": int(self._order_update_count),
                "pending_count": len(self._pending_subscriptions),
                "subscribed_count": len(self._subscribed_conids),
                "pending_conids": sorted(self._pending_subscriptions),
                "subscribed_conids": sorted(self._subscribed_conids),
                "order_updates_subscribed": bool(self._order_updates_enabled),
                "last_message": (
                    time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(last_message))
                    if last_message else ""
                ),
                "last_message_age_s": round(max(0.0, now_ts - last_message), 1) if last_message else None,
                "ping_interval_s": 0,
            }
