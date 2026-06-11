"""IB Gateway market data subscription client."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Set

from ibkr_compute.broker import BrokerAdapter

logger = logging.getLogger(__name__)

TERMINAL_SUBSCRIPTION_ERROR_MARKERS = (
    "contract_not_found",
    "invalid_conid",
    "no_security_definition",
    "no security definition",
)


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
        self._tick_subscribed_conids: Set[int] = set()
        self._tick_pending_subscriptions: Set[int] = set()
        self._tick_subscription_types: Dict[int, str] = {}
        self._tick_last_errors: Dict[int, str] = {}
        self._subscription_last_errors: Dict[int, Dict[str, Any]] = {}
        self._subscription_meta: Dict[int, Dict[str, Any]] = {}
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
            tick_subscribed = list(self._tick_subscribed_conids)
            self._subscribed_conids.clear()
            self._pending_subscriptions.clear()
            self._tick_subscribed_conids.clear()
            self._tick_pending_subscriptions.clear()
            self._tick_subscription_types.clear()
            self._tick_last_errors.clear()
            self._subscription_last_errors.clear()
            self._subscription_meta.clear()
            self._connected = False
            self._ready = False
        for conid in subscribed:
            try:
                self.broker.unsubscribe_market_data(conid)
            except Exception:
                logger.exception("Failed to unsubscribe market data for %s", conid)
        for conid in tick_subscribed:
            try:
                self.broker.unsubscribe_tick_by_tick(conid)
            except Exception:
                logger.exception("Failed to unsubscribe tick-by-tick for %s", conid)

    def _flush_pending(self):
        with self._state_lock:
            pending = list(self._pending_subscriptions)
            tick_pending = list(self._tick_pending_subscriptions)
        for conid in pending:
            self._send_subscription(conid)
        for conid in tick_pending:
            self._send_tick_subscription(conid, self._tick_subscription_types.get(conid, "Last"))

    def subscribe(self, conid: int, symbol: str = "", exchange: str = "SMART", kind: str = "quote"):
        try:
            normalized = int(conid)
        except (TypeError, ValueError):
            return
        meta = {
            "symbol": str(symbol or "").strip().upper(),
            "exchange": str(exchange or "SMART").strip().upper() or "SMART",
            "kind": str(kind or "quote").strip() or "quote",
        }
        with self._state_lock:
            self._pending_subscriptions.add(normalized)
            self._subscription_meta[normalized] = {
                **self._subscription_meta.get(normalized, {}),
                **{key: value for key, value in meta.items() if value},
            }
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
            self._subscription_last_errors.pop(normalized, None)
            self._subscription_meta.pop(normalized, None)
        try:
            self.broker.unsubscribe_market_data(normalized)
        except Exception:
            logger.exception("Failed to unsubscribe %s", normalized)

    def request_market_data_snapshot(
        self,
        conid: int,
        symbol: str = "",
        exchange: str = "SMART",
        timeout: float = 3.0,
    ) -> dict:
        try:
            normalized = int(conid)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_conid", "quote": {}, "payload": {}}
        requester = getattr(self.broker, "request_market_data_snapshot", None)
        if not callable(requester):
            return {
                "ok": False,
                "error": "broker_market_data_snapshot_unavailable",
                "conid": normalized,
                "symbol": str(symbol or "").upper(),
                "quote": {},
                "payload": {},
            }
        try:
            return dict(
                requester(
                    conid=normalized,
                    symbol=str(symbol or ""),
                    exchange=str(exchange or "SMART"),
                    timeout=timeout,
                )
                or {}
            )
        except Exception as exc:
            logger.warning("Market data snapshot failed conid=%s symbol=%s: %s", normalized, symbol, exc)
            return {
                "ok": False,
                "error": str(exc),
                "conid": normalized,
                "symbol": str(symbol or "").upper(),
                "quote": {},
                "payload": {},
            }

    def subscribe_tick_by_tick(self, conid: int, tick_type: str = "Last"):
        try:
            normalized = int(conid)
        except (TypeError, ValueError):
            return
        normalized_tick_type = str(tick_type or "Last").strip() or "Last"
        with self._state_lock:
            self._tick_subscription_types[normalized] = normalized_tick_type
            self._tick_pending_subscriptions.add(normalized)
        if self._running:
            self._send_tick_subscription(normalized, normalized_tick_type)

    def unsubscribe_tick_by_tick(self, conid: int):
        try:
            normalized = int(conid)
        except (TypeError, ValueError):
            return
        with self._state_lock:
            self._tick_pending_subscriptions.discard(normalized)
            self._tick_subscribed_conids.discard(normalized)
            self._tick_subscription_types.pop(normalized, None)
            self._tick_last_errors.pop(normalized, None)
        try:
            self.broker.unsubscribe_tick_by_tick(normalized)
        except Exception:
            logger.exception("Failed to unsubscribe tick-by-tick %s", normalized)

    def resubscribe(self, conid: int, symbol: str = "", exchange: str = "SMART", kind: str = "quote"):
        try:
            normalized = int(conid)
        except (TypeError, ValueError):
            return
        meta_update = {
            "symbol": str(symbol or "").strip().upper(),
            "exchange": str(exchange or "SMART").strip().upper() or "SMART",
            "kind": str(kind or "quote").strip() or "quote",
        }
        with self._state_lock:
            self._pending_subscriptions.discard(normalized)
            self._subscribed_conids.discard(normalized)
            self._subscription_last_errors.pop(normalized, None)
            self._subscription_meta[normalized] = {
                **self._subscription_meta.get(normalized, {}),
                **{key: value for key, value in meta_update.items() if value},
            }
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
                self._subscription_last_errors.pop(conid, None)
                return
            meta = dict(self._subscription_meta.get(conid) or {})
        symbol = str(meta.get("symbol") or "").strip().upper()
        exchange = str(meta.get("exchange") or "SMART").strip().upper() or "SMART"
        try:
            # Keep exchange out of the broker request so conid+symbol can use the
            # fast contract path instead of forcing another contract-details lookup.
            self.broker.subscribe_market_data(conid=conid, symbol=symbol, exchange="")
        except Exception as exc:
            terminal = self._is_terminal_subscription_error(exc)
            with self._state_lock:
                if terminal:
                    self._pending_subscriptions.discard(conid)
                self._subscription_last_errors[conid] = {
                    "conid": int(conid),
                    "symbol": symbol,
                    "exchange": exchange,
                    "kind": str(meta.get("kind") or "quote"),
                    "error": str(exc),
                    "terminal": bool(terminal),
                    "at": time.time(),
                }
            logger.warning("Failed to subscribe conid=%s symbol=%s: %s", conid, symbol or "-", exc)
            return
        with self._state_lock:
            self._subscribed_conids.add(conid)
            self._pending_subscriptions.discard(conid)
            self._subscription_last_errors.pop(conid, None)

    @staticmethod
    def _is_terminal_subscription_error(exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        return any(marker in text for marker in TERMINAL_SUBSCRIPTION_ERROR_MARKERS)

    def _send_tick_subscription(self, conid: int, tick_type: str = "Last"):
        with self._state_lock:
            if conid in self._tick_subscribed_conids:
                self._tick_pending_subscriptions.discard(conid)
                return
        subscriber = getattr(self.broker, "subscribe_tick_by_tick", None)
        if not callable(subscriber):
            with self._state_lock:
                self._tick_last_errors[conid] = "broker_tick_by_tick_unavailable"
            return
        try:
            subscriber(conid=conid, symbol="", tick_type=tick_type)
        except Exception as exc:
            with self._state_lock:
                self._tick_last_errors[conid] = str(exc)
            logger.warning("Failed to subscribe tick-by-tick conid=%s: %s", conid, exc)
            return
        with self._state_lock:
            self._tick_subscribed_conids.add(conid)
            self._tick_pending_subscriptions.discard(conid)
            self._tick_last_errors.pop(conid, None)

    def status(self) -> dict:
        now_ts = time.time()
        with self._state_lock:
            last_message = self._last_message_time
            recent_failures = [
                {
                    **dict(item),
                    "at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(item.get("at") or 0))),
                }
                for item in sorted(
                    self._subscription_last_errors.values(),
                    key=lambda row: float(row.get("at") or 0),
                    reverse=True,
                )
            ][:8]
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
                "subscription_last_errors": {
                    str(conid): dict(error) for conid, error in sorted(self._subscription_last_errors.items())
                },
                "recent_failures": recent_failures,
                "subscription_meta": {str(conid): dict(meta) for conid, meta in sorted(self._subscription_meta.items())},
                "tick_by_tick_pending_count": len(self._tick_pending_subscriptions),
                "tick_by_tick_subscribed_count": len(self._tick_subscribed_conids),
                "tick_by_tick_pending_conids": sorted(self._tick_pending_subscriptions),
                "tick_by_tick_subscribed_conids": sorted(self._tick_subscribed_conids),
                "tick_by_tick_types": dict(self._tick_subscription_types),
                "tick_by_tick_last_errors": dict(self._tick_last_errors),
                "order_updates_subscribed": bool(self._order_updates_enabled),
                "last_message": (
                    time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(last_message))
                    if last_message else ""
                ),
                "last_message_age_s": round(max(0.0, now_ts - last_message), 1) if last_message else None,
                "ping_interval_s": 0,
            }
