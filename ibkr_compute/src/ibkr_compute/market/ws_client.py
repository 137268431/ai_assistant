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
from typing import Callable, Dict, Optional, Set

import websocket

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60

MARKET_DATA_FIELDS = ["31", "84", "85", "86", "87", "88", "7059", "7295", "7296", "7297"]


class IBKRWebSocketClient:
    def __init__(self, gateway_url: str = None, on_tick: Callable = None):
        base = (gateway_url or GATEWAY_URL).rstrip("/")
        self.ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/v1/api/ws"
        self.on_tick = on_tick

        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._ready = False
        self._subscribed_conids: Set[int] = set()
        self._pending_subscriptions: Set[int] = set()
        self._reconnect_delay = RECONNECT_DELAY
        self._last_message_time: Optional[float] = None
        self._message_count = 0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._connection_loop, daemon=True, name="ibkr-ws")
        self._thread.start()
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
        logger.info("WebSocket client stopped")

    def subscribe(self, conid: int):
        self._pending_subscriptions.add(conid)
        if self._connected and self._ready and self._ws:
            self._send_subscription(conid)

    def unsubscribe(self, conid: int):
        self._pending_subscriptions.discard(conid)
        self._subscribed_conids.discard(conid)
        if self._connected and self._ws:
            try:
                self._ws.send(f"umd+{conid}+{{}}")
            except Exception as e:
                logger.debug("Unsubscribe send failed: %s", e)

    def _send_subscription(self, conid: int):
        fields_json = json.dumps({"fields": MARKET_DATA_FIELDS})
        msg = f"smd+{conid}+{fields_json}"
        try:
            self._ws.send(msg)
            self._subscribed_conids.add(conid)
            logger.debug("Subscribed to conid=%d", conid)
        except Exception as e:
            logger.warning("Subscription send failed for conid=%d: %s", conid, e)

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
                    for conid in list(self._pending_subscriptions):
                        self._send_subscription(conid)
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

    def _on_error(self, ws, error):
        logger.warning("WebSocket error: %s", error)
        self._connected = False
        self._ready = False

    def _on_close(self, ws, close_status_code, close_msg):
        self._connected = False
        self._ready = False
        self._subscribed_conids.clear()
        logger.info("WebSocket closed (code=%s, msg=%s)", close_status_code, close_msg)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "running": self._running,
            "ready": self._ready,
            "subscribed_conids": sorted(self._subscribed_conids),
            "pending_conids": sorted(self._pending_subscriptions),
            "message_count": self._message_count,
            "last_message": time.strftime("%H:%M:%S", time.localtime(self._last_message_time))
            if self._last_message_time else None,
        }
