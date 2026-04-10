"""
IBKR WebSocket 实时行情客户端
- 连接 wss://localhost:5001/v1/api/ws
- 订阅多个 conid 的实时 tick 数据
- 自动重连
- tick 回调分发到 BarAggregator
"""

import os
import json
import time
import ssl
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Set

import websocket

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60
WS_PING_INTERVAL_SECONDS = max(15, int(os.environ.get("IBKR_WS_PING_INTERVAL_SEC", "45")))
WS_RESUBSCRIBE_BATCH_SIZE = max(1, int(os.environ.get("IBKR_WS_RESUBSCRIBE_BATCH_SIZE", "8")))
WS_RESUBSCRIBE_GAP_SECONDS = max(0.0, float(os.environ.get("IBKR_WS_RESUBSCRIBE_GAP_MS", "150")) / 1000.0)

MARKET_DATA_FIELDS = ["31", "84", "85", "86", "87", "88", "7059", "7295", "7296", "7297"]


class IBKRWebSocketClient:
    def __init__(self, gateway_url: str = None, on_tick: Callable = None, config=None, environment: str = "live"):
        base = (gateway_url or GATEWAY_URL).rstrip("/")
        self.ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/v1/api/ws"
        self.on_tick = on_tick
        self.on_order_update: Optional[Callable[[Dict[str, Any]], None]] = None
        self.config = config
        self.environment = str(environment or "live").strip().lower() or "live"

        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._ready = False
        self._subscribed_conids: Set[int] = set()
        self._pending_subscriptions: Set[int] = set()
        self._reconnect_delay = RECONNECT_DELAY
        self._last_message_time: Optional[float] = None
        self._last_tic_at: Optional[float] = None
        self._message_count = 0
        self._order_update_count = 0
        self._order_updates_enabled = False
        self._order_updates_subscribed = False
        self._state_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._subscription_flush_lock = threading.Lock()

    def _get_int_setting(self, key: str, fallback: int) -> int:
        if not self.config:
            return fallback
        return self.config.get_int_for_environment(key, self.environment, fallback)

    def _ping_interval(self) -> int:
        return max(15, self._get_int_setting("ibkr_ws_ping_interval_sec", WS_PING_INTERVAL_SECONDS))

    def _resubscribe_batch_size(self) -> int:
        return max(1, self._get_int_setting("ibkr_ws_resubscribe_batch_size", WS_RESUBSCRIBE_BATCH_SIZE))

    def _resubscribe_gap_seconds(self) -> float:
        if not self.config:
            return WS_RESUBSCRIBE_GAP_SECONDS
        return max(
            0.0,
            self.config.get_float_for_environment(
                "ibkr_ws_resubscribe_gap_ms",
                self.environment,
                WS_RESUBSCRIBE_GAP_SECONDS * 1000.0,
            ) / 1000.0,
        )

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._connection_loop, daemon=True, name="ibkr-ws")
        self._thread.start()
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True, name="ibkr-ws-keepalive")
        self._keepalive_thread.start()
        logger.info("WebSocket client started, url=%s", self.ws_url)

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=5)
            self._keepalive_thread = None
        logger.info("WebSocket client stopped")

    def subscribe(self, conid: int):
        with self._state_lock:
            self._pending_subscriptions.add(conid)
        if self._connected and self._ready and self._ws:
            self._send_subscription(conid)

    def unsubscribe(self, conid: int):
        with self._state_lock:
            self._pending_subscriptions.discard(conid)
            self._subscribed_conids.discard(conid)
        if self._connected and self._ws:
            try:
                self._send_message(f"umd+{conid}+{{}}")
            except Exception as e:
                logger.debug("Unsubscribe send failed: %s", e)

    def set_order_update_callback(self, callback: Optional[Callable[[Dict[str, Any]], None]]):
        self.on_order_update = callback

    def set_order_updates_enabled(self, enabled: bool):
        with self._state_lock:
            self._order_updates_enabled = bool(enabled)
        if self._connected and self._ready and self._order_updates_enabled:
            self._subscribe_order_updates()

    def _send_message(self, message: str):
        ws = self._ws
        if not ws:
            return
        with self._send_lock:
            ws.send(message)

    def _send_subscription(self, conid: int):
        fields_json = json.dumps({"fields": MARKET_DATA_FIELDS})
        msg = f"smd+{conid}+{fields_json}"
        try:
            self._send_message(msg)
            with self._state_lock:
                self._subscribed_conids.add(conid)
            logger.debug("Subscribed to conid=%d", conid)
        except Exception as e:
            logger.warning("Subscription send failed for conid=%d: %s", conid, e)

    def _subscribe_order_updates(self):
        with self._state_lock:
            if not self._order_updates_enabled or self._order_updates_subscribed:
                return
        try:
            self._send_message("sor+{}")
            with self._state_lock:
                self._order_updates_subscribed = True
            logger.info("Subscribed to live order updates (sor)")
        except Exception as exc:
            logger.warning("Order update subscription failed: %s", exc)

    def _flush_market_subscriptions_async(self):
        def worker():
            if not self._subscription_flush_lock.acquire(blocking=False):
                return
            try:
                while self._running and self._connected and self._ready:
                    with self._state_lock:
                        pending = [conid for conid in self._pending_subscriptions if conid not in self._subscribed_conids]
                    if not pending:
                        return
                    batch_size = self._resubscribe_batch_size()
                    gap_seconds = self._resubscribe_gap_seconds()
                    for start in range(0, len(pending), batch_size):
                        batch = pending[start:start + batch_size]
                        for conid in batch:
                            self._send_subscription(conid)
                        if start + batch_size < len(pending) and gap_seconds > 0:
                            time.sleep(gap_seconds)
                    return
            finally:
                self._subscription_flush_lock.release()

        threading.Thread(target=worker, daemon=True, name="ibkr-ws-resubscribe").start()

    def _keepalive_loop(self):
        while self._running:
            if not self._running:
                break
            time.sleep(self._ping_interval())
            if not self._running or not self._connected or not self._ws:
                continue
            try:
                self._send_message("tic")
                self._last_tic_at = time.time()
            except Exception as exc:
                logger.debug("WebSocket tic failed: %s", exc)

    def _connection_loop(self):
        while self._running:
            try:
                self._connect()
            except Exception as e:
                logger.error("WebSocket connection error: %s", e)

            if self._running:
                delay = min(self._reconnect_delay, MAX_RECONNECT_DELAY)
                logger.info("Reconnecting in %ds...", delay)
                for _ in range(int(delay)):
                    if not self._running:
                        return
                    time.sleep(1)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, MAX_RECONNECT_DELAY)

    def _connect(self):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        self._ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False})

    def _on_open(self, ws):
        self._connected = True
        self._ready = False
        self._reconnect_delay = RECONNECT_DELAY
        with self._state_lock:
            self._order_updates_subscribed = False
        logger.info("WebSocket connected")

    def _on_message(self, ws, message):
        self._last_message_time = time.time()
        self._message_count += 1

        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            if message == "tic":
                return
            logger.debug("Non-JSON message: %s", message[:200])
            return

        if isinstance(data, dict):
            topic = str(data.get("topic") or "").strip()
            if topic == "sts":
                args = data.get("args") or {}
                if args.get("authenticated") and args.get("established"):
                    if not self._ready:
                        self._ready = True
                        logger.info(
                            "WebSocket session ready, flushing %d market-data subscriptions",
                            len(self._pending_subscriptions),
                        )
                    self._flush_market_subscriptions_async()
                    self._subscribe_order_updates()
                return

            if topic.startswith("sor"):
                self._handle_order_update_payload(data.get("args"))
                return

            conid = data.get("conid") or data.get("conidEx")
            if not conid and topic.startswith("smd+"):
                _, _, maybe_conid = topic.partition("+")
                try:
                    conid = int((maybe_conid or "").split("+", 1)[0])
                    data["conid"] = conid
                except (TypeError, ValueError):
                    conid = None
            if conid and self.on_tick:
                try:
                    self.on_tick(data)
                except Exception as e:
                    logger.error("Tick callback error: %s", e)

        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    conid = item.get("conid") or item.get("conidEx")
                    if conid and self.on_tick:
                        try:
                            self.on_tick(item)
                        except Exception as e:
                            logger.error("Tick callback error: %s", e)

    def _handle_order_update_payload(self, payload: Any):
        if not self.on_order_update:
            return

        items: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            items = [payload]
        elif isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]

        for item in items:
            try:
                self.on_order_update(item)
                self._order_update_count += 1
            except Exception as exc:
                logger.error("Order update callback error: %s", exc)

    def _on_error(self, ws, error):
        logger.warning("WebSocket error: %s", error)
        self._connected = False
        self._ready = False

    def _on_close(self, ws, close_status_code, close_msg):
        self._connected = False
        self._ready = False
        with self._state_lock:
            self._subscribed_conids.clear()
            self._order_updates_subscribed = False
        logger.info("WebSocket closed (code=%s, msg=%s)", close_status_code, close_msg)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def status(self) -> dict:
        with self._state_lock:
            subscribed_conids = sorted(self._subscribed_conids)
            pending_conids = sorted(self._pending_subscriptions)
            order_updates_enabled = self._order_updates_enabled
            order_updates_subscribed = self._order_updates_subscribed
        return {
            "connected": self._connected,
            "running": self._running,
            "ready": self._ready,
            "subscribed_conids": subscribed_conids,
            "pending_conids": pending_conids,
            "message_count": self._message_count,
            "order_update_count": self._order_update_count,
            "order_updates_enabled": order_updates_enabled,
            "order_updates_subscribed": order_updates_subscribed,
            "ping_interval_s": self._ping_interval(),
            "resubscribe_batch_size": self._resubscribe_batch_size(),
            "resubscribe_gap_ms": round(self._resubscribe_gap_seconds() * 1000),
            "last_message": time.strftime("%H:%M:%S", time.localtime(self._last_message_time))
            if self._last_message_time else None,
            "last_tic": time.strftime("%H:%M:%S", time.localtime(self._last_tic_at))
            if self._last_tic_at else None,
        }
