from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

from ibkr_compute.broker.ib_gateway_support import (
    DEFAULT_ENVIRONMENT,
    _iso_now,
)


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ibkr_compute.broker.ib_gateway import BrokerAdapter
    from ibkr_compute.broker.ib_gateway_service import GatewayServiceManager


class SocketSessionKeeper:
    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        gateway_manager: GatewayServiceManager,
        pb_client=None,
        on_session_expired: Optional[Callable[[], None]] = None,
        on_gateway_down: Optional[Callable[[], None]] = None,
        environment: str = DEFAULT_ENVIRONMENT,
    ):
        self.broker = broker
        self.gateway_manager = gateway_manager
        self.pb_client = pb_client
        self.environment = str(environment or DEFAULT_ENVIRONMENT)
        self.on_session_expired = on_session_expired
        self.on_gateway_down = on_gateway_down
        self.is_authenticated = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_check = ""
        self._consecutive_failures = 0
        self._last_transition = ""
        self._last_status_code = 0

    def check_auth_status(self) -> dict:
        running = bool(self.gateway_manager.is_running)
        previous = bool(self.is_authenticated)
        status = {
            "authenticated": False,
            "running": running,
            "gateway_running": running,
            "consecutive_failures": int(self._consecutive_failures or 0),
            "last_check": self._last_check,
        }
        if not running:
            self._consecutive_failures += 1
            self.is_authenticated = False
            self._last_status_code = 503
            if previous and callable(self.on_gateway_down):
                self.on_gateway_down()
            self._last_transition = "gateway_down"
            self._last_check = _iso_now()
            return {
                **status,
                "status_code": self._last_status_code,
                "last_check": self._last_check,
                "last_tickle": self._last_check,
            }
        try:
            health = self.broker.health()
            authenticated = bool(health.get("ready"))
            status.update(health)
            self.is_authenticated = authenticated
            self._last_status_code = int(health.get("status_code", 0) or 0)
            self._consecutive_failures = 0 if authenticated else self._consecutive_failures + 1
            self._last_transition = "authenticated" if authenticated else "unauthenticated"
            if previous and not authenticated and callable(self.on_session_expired):
                self.on_session_expired()
        except Exception as exc:
            self.is_authenticated = False
            self._consecutive_failures += 1
            status["error"] = str(exc)
            self._last_status_code = 503
            self._last_transition = "probe_failed"
            if previous and callable(self.on_session_expired):
                self.on_session_expired()
        self._last_check = _iso_now()
        status["authenticated"] = bool(self.is_authenticated)
        status["consecutive_failures"] = int(self._consecutive_failures or 0)
        status["status_code"] = int(self._last_status_code or 0)
        status["last_check"] = self._last_check
        status["last_tickle"] = self._last_check
        return status

    def start(self):
        if self._running:
            return
        self._running = True

        def loop():
            while self._running:
                try:
                    self.check_auth_status()
                except Exception:
                    logger.exception("Session keeper check failed")
                time.sleep(30)

        self._thread = threading.Thread(target=loop, daemon=True, name="ibgw-session-keeper")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def status(self) -> dict:
        return {
            "authenticated": bool(self.is_authenticated),
            "running": bool(self._running),
            "consecutive_failures": int(self._consecutive_failures or 0),
            "last_check": self._last_check,
            "last_tickle": self._last_check,
            "last_transition": self._last_transition,
            "status_code": int(self._last_status_code or 0),
            "gateway_running": bool(self.gateway_manager.is_running),
        }
