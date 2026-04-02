"""
IBKR Gateway 自动登录 + 2FA (IBKR Mobile App 推送)
- Selenium headless 自动填充登录表单
- 等待用户在手机端确认 2FA 推送通知
- 超时重试机制
"""

import os
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000")
LOGIN_TIMEOUT = int(os.environ.get("IBKR_LOGIN_TIMEOUT", "120"))
MAX_2FA_WAIT = int(os.environ.get("IBKR_2FA_WAIT", "180"))
MAX_LOGIN_RETRIES = 3


class AuthHandler:
    def __init__(self, gateway_url: str = None, pb_client=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.pb_client = pb_client
        self._username = os.environ.get("IBKR_USERNAME", "")
        self._password = os.environ.get("IBKR_PASSWORD", "")
        self._driver = None
        self._last_login_time: Optional[float] = None

    def _ensure_driver(self):
        if self._driver is not None:
            return

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--window-size=1280,720")
        options.add_argument("--remote-debugging-port=9222")

        snap_chrome = "/snap/chromium/current/usr/lib/chromium-browser/chrome"
        if os.path.isfile(snap_chrome):
            options.binary_location = snap_chrome

        service = Service(executable_path="/usr/local/bin/chromedriver")
        self._driver = webdriver.Chrome(service=service, options=options)
        self._driver.set_page_load_timeout(LOGIN_TIMEOUT)

    def _close_driver(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def _log_to_pb(self, event: str, status: str, detail: str = ""):
        if not self.pb_client:
            return
        try:
            from datetime import datetime, timezone, timedelta
            et_now = datetime.now(timezone(timedelta(hours=-4)))
            self.pb_client.create_record("ibkr_session", {
                "event": event,
                "status": status,
                "detail": detail[:500] if detail else "",
                "us_time": et_now.strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception as e:
            logger.debug("PB session log failed: %s", e)

    def login(self) -> bool:
        if not self._username or not self._password:
            logger.error("IBKR_USERNAME or IBKR_PASSWORD not set")
            return False

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        for attempt in range(1, MAX_LOGIN_RETRIES + 1):
            logger.info("Login attempt %d/%d", attempt, MAX_LOGIN_RETRIES)
            self._log_to_pb("login_attempt", "info", f"Attempt {attempt}")

            try:
                self._ensure_driver()
                login_url = f"{self.gateway_url}/sso/Login?forwardTo=22&RL=1&ip2loc=on"
                self._driver.get(login_url)
                time.sleep(3)

                wait = WebDriverWait(self._driver, 30)

                username_field = wait.until(
                    EC.presence_of_element_located((By.NAME, "username"))
                )
                username_field.clear()
                username_field.send_keys(self._username)

                password_field = self._driver.find_element(By.NAME, "password")
                password_field.clear()
                password_field.send_keys(self._password)

                submit_buttons = self._driver.find_elements(
                    By.CSS_SELECTOR, "button[type='submit']"
                )
                submit_btn = None
                for btn in submit_buttons:
                    if btn.text.strip().lower() in ("login", "log in", "submit"):
                        submit_btn = btn
                        break
                if submit_btn is None and submit_buttons:
                    submit_btn = submit_buttons[0]
                if submit_btn is None:
                    raise Exception("No submit button found on login page")
                submit_btn.click()

                logger.info("Credentials submitted, waiting for 2FA push notification...")
                self._log_to_pb("2fa_waiting", "info", "Waiting for mobile app confirmation")

                if self._wait_for_2fa_completion():
                    self._last_login_time = time.time()
                    logger.info("Login successful!")
                    self._log_to_pb("login_success", "ok", "")
                    self._close_driver()
                    return True
                else:
                    logger.warning("2FA confirmation timed out")
                    self._log_to_pb("2fa_timeout", "warning", f"Attempt {attempt}")

            except Exception as e:
                logger.error("Login error on attempt %d: %s", attempt, e)
                self._log_to_pb("login_error", "error", str(e)[:500])
            finally:
                if attempt < MAX_LOGIN_RETRIES:
                    self._close_driver()
                    time.sleep(5)

        self._close_driver()
        logger.error("All login attempts exhausted")
        self._log_to_pb("login_failed", "error", "All attempts exhausted")
        return False

    def _wait_for_2fa_completion(self) -> bool:
        import requests

        session = requests.Session()
        session.verify = False

        start = time.time()
        while time.time() - start < MAX_2FA_WAIT:
            time.sleep(5)
            try:
                resp = session.post(
                    f"{self.gateway_url}/v1/api/iserver/auth/status",
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("authenticated", False):
                        return True
                    competing = data.get("competing", False)
                    if competing:
                        logger.warning("Competing session detected")

                page_source = self._driver.page_source if self._driver else ""
                if "Client login succeeds" in page_source or "two_fa_result" in page_source:
                    time.sleep(3)
                    resp2 = session.post(
                        f"{self.gateway_url}/v1/api/iserver/auth/status",
                        timeout=10,
                    )
                    if resp2.status_code == 200 and resp2.json().get("authenticated"):
                        return True

            except Exception as e:
                logger.debug("2FA wait check: %s", e)

        return False

    def status(self) -> dict:
        from datetime import datetime
        return {
            "has_credentials": bool(self._username and self._password),
            "last_login": datetime.fromtimestamp(self._last_login_time).isoformat()
            if self._last_login_time else None,
        }
