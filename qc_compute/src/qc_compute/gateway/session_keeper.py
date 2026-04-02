"""
IBKR Gateway Session 保活
- 定期 tickle 维持会话
- 检测 session 过期并触发重新认证
- 健康状态上报到 PocketBase
"""

import os
import time
import logging
import threading
import requests
from typing import Optional, Callable
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000")
TICKLE_INTERVAL = 55
MAX_CONSECUTIVE_FAILURES = 3


class SessionKeeper:
    def __init__(self, gateway_url: str = None, pb_client=None,
                 on_session_expired: Optional[Callable] = None,
                 on_gateway_down: Optional[Callable] = None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.pb_client = pb_client
        self.on_session_expired = on_session_expired
        self.on_gateway_down = on_gateway_down

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._last_tickle_time: Optional[float] = None
        self._authenticated = False
        self._session = requests.Session()
        self._session.verify = False

    def _api_url(self, path: str) -> str:
        return f"{self.gateway_url}/v1/api{path}"

    def tickle(self) -> dict:
        try:
            resp = self._session.post(self._api_url("/tickle"), timeout=10)
            resp.raise_for_status()
            data = resp.json()
            self._last_tickle_time = time.time()
            self._consecutive_failures = 0
            return data
        except Exception as e:
            self._consecutive_failures += 1
            logger.warning("Tickle failed (%d/%d): %s",
                           self._consecutive_failures, MAX_CONSECUTIVE_FAILURES, e)
            return {"error": str(e)}

    def check_auth_status(self) -> dict:
        try:
            resp = self._session.post(self._api_url("/iserver/auth/status"), timeout=10)
            resp.raise_for_status()
            data = resp.json()
            self._authenticated = data.get("authenticated", False)
            return data
        except Exception as e:
            logger.warning("Auth status check failed: %s", e)
            self._authenticated = False
            return {"authenticated": False, "error": str(e)}

    def reauthenticate(self) -> dict:
        try:
            resp = self._session.post(self._api_url("/iserver/reauthenticate"), timeout=15)
            resp.raise_for_status()
            data = resp.json()
            logger.info("Reauthenticate response: %s", data)
            return data
        except Exception as e:
            logger.error("Reauthenticate failed: %s", e)
            return {"error": str(e)}

    def _log_to_pb(self, event: str, status: str, detail: str = ""):
        if not self.pb_client:
            return
        try:
            et_now = datetime.now(timezone(timedelta(hours=-4)))
            self.pb_client.create_record("ibkr_session", {
                "event": event,
                "status": status,
                "detail": detail[:500] if detail else "",
                "us_time": et_now.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as e:
            logger.debug("PB session log failed: %s", e)

    def _keeper_loop(self):
        logger.info("Session keeper started (interval=%ds)", TICKLE_INTERVAL)
        while self._running:
            tickle_result = self.tickle()

            if "error" not in tickle_result:
                session_info = tickle_result.get("session", "")
                if session_info:
                    logger.debug("Tickle OK, session=%s", session_info)

                auth = self.check_auth_status()
                if not auth.get("authenticated", False):
                    logger.warning("Session not authenticated, attempting reauthenticate")
                    self._log_to_pb("reauth_trigger", "warning", "Session expired")
                    reauth = self.reauthenticate()

                    if "error" in reauth:
                        logger.error("Reauthenticate failed, invoking callback")
                        self._log_to_pb("reauth_failed", "error", str(reauth))
                        if self.on_session_expired:
                            self.on_session_expired()
                    else:
                        self._log_to_pb("reauth_success", "ok", "")
                        self._authenticated = True
                else:
                    self._authenticated = True

            elif self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error("Gateway unreachable after %d consecutive failures",
                             self._consecutive_failures)
                self._log_to_pb("gateway_down", "error",
                                f"Consecutive failures: {self._consecutive_failures}")
                if self.on_gateway_down:
                    self.on_gateway_down()
                self._consecutive_failures = 0

            for _ in range(TICKLE_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("Session keeper stopped")

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._keeper_loop, daemon=True, name="session-keeper")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def status(self) -> dict:
        return {
            "running": self._running,
            "authenticated": self._authenticated,
            "consecutive_failures": self._consecutive_failures,
            "last_tickle": datetime.fromtimestamp(self._last_tickle_time).isoformat()
            if self._last_tickle_time else None,
            "gateway_url": self.gateway_url,
        }
