"""
IBKR Gateway 自动登录 + 2FA
- Selenium headless 自动填充登录表单
- 识别 Push / Challenge-Response 两种 2FA 模式
- 将实时状态同步到 PocketBase / Feishu
"""

import os
import re
import time
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000")
LOGIN_TIMEOUT = int(os.environ.get("IBKR_LOGIN_TIMEOUT", "120"))
MAX_2FA_WAIT = int(os.environ.get("IBKR_2FA_WAIT", "180"))
CHALLENGE_RESPONSE_WAIT = int(os.environ.get("IBKR_CHALLENGE_RESPONSE_WAIT", "240"))
POST_RESPONSE_GRACE_SECONDS = int(os.environ.get("IBKR_2FA_RESPONSE_GRACE", "90"))
MAX_LOGIN_RETRIES = 3
ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")
ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")
PB_PUBLIC_URL = os.environ.get("PB_PUBLIC_URL", "").rstrip("/")
WAIT_POLL_SECONDS = 3
BROWSER_PROBE_SECONDS = 12
BACKEND_PROMOTE_SECONDS = 12


class AuthHandler:
    def __init__(self, gateway_url: str = None, pb_client=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.pb_client = pb_client
        self._username = os.environ.get("IBKR_USERNAME", "")
        self._password = os.environ.get("IBKR_PASSWORD", "")
        self._driver = None
        self._last_login_time: Optional[float] = None
        self._last_wait_context: Dict[str, Any] = {}
        self._cancel_requested = False

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

    def reset_cancel(self):
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True
        self._close_driver()

    def _now_et(self) -> str:
        from datetime import datetime, timezone, timedelta
        et_now = datetime.now(timezone(timedelta(hours=-4)))
        return et_now.strftime("%Y-%m-%d %H:%M:%S")

    def _log_to_pb(self, event: str, status: str, detail: str = ""):
        if not self.pb_client:
            return
        try:
            self.pb_client.create_record("ibkr_session", {
                "event": event,
                "status": status,
                "detail": detail[:500] if detail else "",
                "us_time": self._now_et(),
            })
        except Exception as e:
            logger.debug("PB session log failed: %s", e)

    @staticmethod
    def _normalize_code(value: Any) -> str:
        return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()

    def _capture_page_state(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {
            "mode": "unknown",
            "title": "",
            "url": "",
            "body": "",
            "body_excerpt": "",
            "challenge_code": "",
            "visible_blocks": [],
        }
        if not self._driver:
            return state

        try:
            state["title"] = self._driver.title or ""
        except Exception:
            pass

        try:
            state["url"] = self._driver.current_url or ""
        except Exception:
            pass

        try:
            page_info = self._driver.execute_script("""
                const body = document.body ? (document.body.innerText || "") : "";
                const blocks = Array.from(document.querySelectorAll(".xyzblock"))
                    .filter((el) => {
                        const style = window.getComputedStyle(el);
                        return style.display !== "none" && style.visibility !== "hidden" && el.offsetParent !== null;
                    })
                    .map((el) => el.className || "");
                const challenge = document.querySelector(".xyz-goldchallenge");
                const challengeContainer = challenge ? (challenge.closest(".xyzblock") || challenge) : null;
                const challengeVisible = challengeContainer ? (() => {
                    const style = window.getComputedStyle(challengeContainer);
                    return style.display !== "none" && style.visibility !== "hidden" && challengeContainer.offsetParent !== null;
                })() : false;
                return {
                    body: body,
                    visible_blocks: blocks,
                    challenge_code: challengeVisible && challenge ? (challenge.textContent || "").trim() : "",
                };
            """) or {}
            state["body"] = str(page_info.get("body") or "")
            state["visible_blocks"] = list(page_info.get("visible_blocks") or [])
            state["challenge_code"] = str(page_info.get("challenge_code") or "").strip()
        except Exception:
            try:
                from selenium.webdriver.common.by import By

                state["body"] = self._driver.find_element(By.TAG_NAME, "body").text or ""
            except Exception:
                state["body"] = ""

        body_lower = state["body"].lower()
        visible = " ".join(state["visible_blocks"]).lower()

        if (
            "xyzblock-finished" in visible
            or "xyzblock-success" in visible
            or "client login succeeds" in body_lower
            or "login complete" in body_lower
        ):
            state["mode"] = "success"
        elif (
            state["challenge_code"]
            or "xyzblock-manualibkey" in visible
            or "xyzblock-gold" in visible
            or "enter the challenge code below" in body_lower
            or "response code" in body_lower
        ):
            state["mode"] = "challenge_response"
        elif (
            "xyzblock-notification" in visible
            or "xyzblock-ibkey" in visible
            or "xyzblock-enablepush" in visible
            or "open the ibkr notification on your phone" in body_lower
            or "tap the notification" in body_lower
        ):
            state["mode"] = "push_notification"
        elif "name=\"username\"" in body_lower or "password" in body_lower:
            state["mode"] = "login_form"

        if state["body"]:
            compact = " | ".join(part.strip() for part in state["body"].splitlines() if part.strip())
            state["body_excerpt"] = compact[:240]

        return state

    def _install_trace_hooks(self):
        if not self._driver:
            return
        try:
            self._driver.execute_script("""
                if (window.__ibkrTraceInstalled) {
                    return;
                }
                window.__ibkrTraceInstalled = true;
                window.__ibkrTrace = [];

                function shouldTrack(url) {
                    if (!url) return false;
                    return [
                        "/sso/Authenticator",
                        "/v1/api/iserver/auth/status",
                        "/v1/api/tickle",
                    ].some(function(marker) {
                        return String(url).indexOf(marker) !== -1;
                    });
                }

                function pushTrace(entry) {
                    try {
                        var payload = Object.assign({ ts: Date.now() }, entry || {});
                        window.__ibkrTrace.push(payload);
                        if (window.__ibkrTrace.length > 40) {
                            window.__ibkrTrace = window.__ibkrTrace.slice(-40);
                        }
                    } catch (_) {}
                }

                var origFetch = window.fetch;
                if (origFetch) {
                    window.fetch = function() {
                        var args = Array.prototype.slice.call(arguments);
                        var req = args[0] || {};
                        var init = args[1] || {};
                        var url = typeof req === "string" ? req : (req.url || "");
                        var method = (init.method || req.method || "GET").toUpperCase();
                        return origFetch.apply(this, args).then(function(resp) {
                            if (!shouldTrack(url)) {
                                return resp;
                            }
                            try {
                                var clone = resp.clone();
                                clone.text().then(function(text) {
                                    pushTrace({
                                        type: "fetch",
                                        method: method,
                                        url: url,
                                        status: resp.status,
                                        body: String(text || "").slice(0, 600),
                                    });
                                }).catch(function(err) {
                                    pushTrace({
                                        type: "fetch",
                                        method: method,
                                        url: url,
                                        status: resp.status,
                                        error: String(err || ""),
                                    });
                                });
                            } catch (err) {
                                pushTrace({
                                    type: "fetch",
                                    method: method,
                                    url: url,
                                    status: resp.status,
                                    error: String(err || ""),
                                });
                            }
                            return resp;
                        }).catch(function(err) {
                            if (shouldTrack(url)) {
                                pushTrace({
                                    type: "fetch",
                                    method: method,
                                    url: url,
                                    error: String(err || ""),
                                });
                            }
                            throw err;
                        });
                    };
                }

                var origOpen = XMLHttpRequest.prototype.open;
                var origSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url) {
                    this.__ibkrTrace = {
                        method: String(method || "GET").toUpperCase(),
                        url: String(url || ""),
                    };
                    return origOpen.apply(this, arguments);
                };
                XMLHttpRequest.prototype.send = function() {
                    var meta = this.__ibkrTrace || {};
                    if (shouldTrack(meta.url)) {
                        this.addEventListener("loadend", function() {
                            pushTrace({
                                type: "xhr",
                                method: meta.method || "GET",
                                url: meta.url || "",
                                status: this.status,
                                body: String(this.responseText || "").slice(0, 600),
                            });
                        });
                    }
                    return origSend.apply(this, arguments);
                };
            """)
        except Exception as exc:
            logger.debug("Failed to install browser trace hooks: %s", exc)

    def _get_browser_trace_snapshot(self) -> Dict[str, Any]:
        if not self._driver:
            return {}
        try:
            trace = self._driver.execute_script("""
                var entries = Array.isArray(window.__ibkrTrace) ? window.__ibkrTrace.slice(-5) : [];
                return { installed: !!window.__ibkrTraceInstalled, entries: entries };
            """) or {}
            entries = trace.get("entries") or []
            if not entries:
                return {}
            last = entries[-1] or {}
            body = str(last.get("body") or "").strip().replace("\n", " ")
            summary = " | ".join(
                part for part in [
                    str(last.get("type") or "").upper(),
                    str(last.get("method") or "").upper(),
                    str(last.get("url") or ""),
                    f"status={last.get('status')}" if last.get("status") is not None else "",
                    body[:220],
                ]
                if part
            )
            return {
                "installed": bool(trace.get("installed")),
                "entries": entries,
                "last_summary": summary[:260],
            }
        except Exception as exc:
            logger.debug("Failed to read browser trace snapshot: %s", exc)
            return {}

    def _fetch_browser_gateway_state(self) -> Dict[str, Any]:
        if not self._driver:
            return {}
        try:
            self._install_trace_hooks()
            payload = self._driver.execute_async_script("""
                const done = arguments[arguments.length - 1];
                async function fetchJson(path, method) {
                    const resp = await fetch(path, {
                        method: method || "POST",
                        credentials: "include",
                    });
                    const text = await resp.text();
                    let data = null;
                    try {
                        data = JSON.parse(text);
                    } catch (_) {}
                    return {
                        ok: true,
                        status: resp.status,
                        text: String(text || "").slice(0, 600),
                        data: data,
                    };
                }
                Promise.all([
                    fetchJson("/v1/api/iserver/auth/status", "POST"),
                    fetchJson("/v1/api/tickle", "POST"),
                ]).then(function(results) {
                    done({
                        auth: results[0],
                        tickle: results[1],
                    });
                }).catch(function(err) {
                    done({
                        error: String(err || ""),
                    });
                });
            """) or {}
            auth_payload = payload.get("auth") or {}
            tickle_payload = payload.get("tickle") or {}
            auth_data = auth_payload.get("data") or {}
            tickle_data = tickle_payload.get("data") or {}
            trace = self._get_browser_trace_snapshot()
            return {
                "authenticated": bool(auth_data.get("authenticated", False)),
                "auth_status": auth_payload.get("status"),
                "session": str(tickle_data.get("session") or ""),
                "sso_expires_ms": tickle_data.get("ssoExpires"),
                "auth_payload": auth_payload,
                "tickle_payload": tickle_payload,
                "trace_summary": trace.get("last_summary") or "",
                "trace_entries": trace.get("entries") or [],
                "error": str(payload.get("error") or "").strip(),
            }
        except Exception as exc:
            logger.debug("Browser gateway probe failed: %s", exc)
            return {"error": str(exc)}

    def _build_2fa_detail(self, attempt: Optional[int] = None, detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "账号": ACCOUNT_ID or "-",
            "等待秒数": MAX_2FA_WAIT,
        }
        if attempt:
            payload["尝试"] = f"{attempt}/{MAX_LOGIN_RETRIES}"
        if PB_PUBLIC_URL:
            payload["状态页"] = f"{PB_PUBLIC_URL}/ibkr_runtime.html?environment={ENVIRONMENT}"
        if detail:
            payload.update(detail)
        return payload

    def request_2fa_approval(
        self,
        reason: str = "manual_reauth",
        source: str = "ibkr_compute",
        detail: Optional[Dict[str, Any]] = None,
        force_reset: bool = False,
        message: str = "",
    ) -> bool:
        if not self.pb_client:
            return False
        try:
            self.pb_client.request_ibkr_2fa(
                reason=reason,
                detail=self._build_2fa_detail(detail=detail),
                source=source,
                environment=ENVIRONMENT,
                message=message or "检测到需要人工触发的 IBKR 2FA，请在准备验证时点击按钮。",
                force_reset=force_reset,
            )
            return True
        except Exception as e:
            logger.debug("PB 2FA request failed: %s", e)
            return False

    def _report_2fa_status(
        self,
        status: str,
        reason: str,
        source: str,
        attempt: Optional[int] = None,
        detail: Optional[Dict[str, Any]] = None,
        message: str = "",
        last_result: str = "",
        error: str = "",
        state_patch: Optional[Dict[str, Any]] = None,
    ):
        if not self.pb_client:
            return
        try:
            self.pb_client.report_ibkr_2fa_result(
                status=status,
                detail=self._build_2fa_detail(attempt=attempt, detail=detail),
                source=source,
                environment=ENVIRONMENT,
                message=message,
                last_result=last_result or message,
                error=error,
                state_patch=state_patch or {},
            )
        except Exception as e:
            logger.debug("PB 2FA result sync failed: %s", e)

    def _build_wait_detail(
        self,
        page_state: Optional[Dict[str, Any]],
        detail: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        wait_detail = dict(detail or {})
        state = page_state or {}
        mode = str(state.get("mode") or "").strip()
        browser_state = state.get("browser_gateway_state") or {}
        backend_authenticated = state.get("backend_authenticated")
        if mode == "push_notification":
            wait_detail["验证方式"] = "push_notification"
        elif mode == "challenge_response":
            wait_detail["验证方式"] = "challenge_response"
        elif mode:
            wait_detail["验证方式"] = mode

        challenge_code = str(state.get("challenge_code") or "").strip()
        if challenge_code:
            wait_detail["Challenge"] = challenge_code

        blocks = state.get("visible_blocks") or []
        if blocks:
            wait_detail["页面块"] = ", ".join(str(block) for block in blocks)

        body_excerpt = str(state.get("body_excerpt") or "").strip()
        if body_excerpt:
            wait_detail["页面提示"] = body_excerpt

        sso_expires_ms = browser_state.get("sso_expires_ms")
        if isinstance(sso_expires_ms, (int, float)):
            wait_detail["确认窗口剩余秒"] = max(int(float(sso_expires_ms) / 1000), 0)

        gateway_session = str(browser_state.get("session") or "").strip()
        if gateway_session:
            wait_detail["Gateway会话"] = gateway_session

        browser_authenticated = browser_state.get("authenticated")
        if browser_authenticated is not None or backend_authenticated is not None:
            browser_text = "true" if browser_authenticated else "false"
            backend_text = "true" if backend_authenticated else "false"
            wait_detail["认证探针"] = f"browser={browser_text}, backend={backend_text}"

        trace_summary = str(browser_state.get("trace_summary") or "").strip()
        if trace_summary:
            wait_detail["最近网关回包"] = trace_summary

        return wait_detail

    def _build_state_patch(self, page_state: Optional[Dict[str, Any]], extra_patch: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        state = page_state or {}
        browser_state = state.get("browser_gateway_state") or {}
        patch: Dict[str, Any] = {
            "mode": state.get("mode") or "",
            "page_title": state.get("title") or "",
            "page_url": state.get("url") or "",
        }

        challenge_code = str(state.get("challenge_code") or "").strip()
        mode = str(state.get("mode") or "").strip()

        if challenge_code:
            patch["challenge_code"] = challenge_code
            patch["challenge_detected_at"] = self._now_et()
        elif mode != "challenge_response":
            patch["challenge_code"] = ""
            patch["challenge_detected_at"] = ""

        if mode != "challenge_response":
            patch["response_code"] = ""
            patch["response_status"] = ""
            patch["response_received_at"] = ""
            patch["response_submitted_at"] = ""

        if isinstance(browser_state.get("sso_expires_ms"), (int, float)):
            patch["gateway_sso_expires_ms"] = max(int(float(browser_state.get("sso_expires_ms"))), 0)
        else:
            patch["gateway_sso_expires_ms"] = 0

        patch["browser_authenticated"] = bool(browser_state.get("authenticated", False))
        patch["gateway_authenticated"] = bool(state.get("backend_authenticated", False))

        gateway_session = str(browser_state.get("session") or "").strip()
        if gateway_session:
            patch["gateway_session"] = gateway_session

        trace_summary = str(browser_state.get("trace_summary") or "").strip()
        if trace_summary:
            patch["gateway_trace"] = trace_summary[:260]

        if extra_patch:
            patch.update(extra_patch)
        return patch

    def _check_backend_auth(self, session) -> Dict[str, Any]:
        try:
            resp = session.post(
                f"{self.gateway_url}/v1/api/iserver/auth/status",
                timeout=10,
            )
            payload = resp.json() if resp.status_code == 200 else {}
            return {
                "ok": resp.status_code == 200,
                "status_code": resp.status_code,
                "authenticated": bool(payload.get("authenticated", False)),
                "payload": payload,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status_code": 0,
                "authenticated": False,
                "payload": {},
                "error": str(exc),
            }

    def _promote_backend_auth(self, session) -> Dict[str, Any]:
        for step in ("tickle", "reauthenticate", "tickle"):
            try:
                if step == "tickle":
                    session.post(f"{self.gateway_url}/v1/api/tickle", timeout=10)
                else:
                    session.post(f"{self.gateway_url}/v1/api/iserver/reauthenticate", timeout=15)
            except Exception as exc:
                logger.debug("Gateway %s bridge failed: %s", step, exc)

        result = self._check_backend_auth(session)
        if result.get("authenticated"):
            logger.info("Gateway backend auth promoted after browser confirmation bridge")
        return result

    def _get_pending_response_code(self, current_challenge: str) -> str:
        if not self.pb_client:
            return ""
        try:
            payload = self.pb_client.get_ibkr_2fa_status(environment=ENVIRONMENT)
            state = payload.get("state") or {}
            response_status = str(state.get("response_status") or "").strip().lower()
            response_code = self._normalize_code(state.get("response_code"))
            if not response_code:
                return ""

            stored_challenge = self._normalize_code(state.get("challenge_code"))
            expected_challenge = self._normalize_code(current_challenge)
            if stored_challenge and expected_challenge and stored_challenge != expected_challenge:
                return ""

            if response_status and response_status not in ("pending", "received"):
                return ""

            return response_code
        except Exception as exc:
            logger.debug("PB 2FA response poll failed: %s", exc)
            return ""

    def _submit_challenge_response(self, response_code: str) -> bool:
        if not self._driver:
            return False

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        code = self._normalize_code(response_code)
        if not code:
            return False

        try:
            wait = WebDriverWait(self._driver, 10)
            field = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".xyz-gold-response")))
            field.clear()
            field.send_keys(code)

            submit_btn = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".xyzform-gold button[type='submit']"))
            )
            submit_btn.click()
            return True
        except Exception as exc:
            logger.warning("Challenge response submit failed: %s", exc)
            return False

    def _report_wait_state(
        self,
        reason: str,
        source: str,
        attempt: int,
        detail: Optional[Dict[str, Any]],
        page_state: Optional[Dict[str, Any]],
        state_patch: Optional[Dict[str, Any]] = None,
    ):
        state = page_state or {}
        mode = str(state.get("mode") or "").strip()
        browser_state = state.get("browser_gateway_state") or {}
        sso_expires_ms = browser_state.get("sso_expires_ms")
        expires_seconds = max(int(float(sso_expires_ms) / 1000), 0) if isinstance(sso_expires_ms, (int, float)) else None
        browser_authenticated = bool(browser_state.get("authenticated", False))
        backend_authenticated = bool(state.get("backend_authenticated", False))
        if mode == "challenge_response":
            message = "已切换为 IBKR Challenge/Response，请在 Runtime 页面输入 Response Code。"
            last_result = "等待 Challenge Response Code。"
            status = "waiting_response"
        elif browser_authenticated and not backend_authenticated:
            message = "浏览器侧疑似已通过 2FA，正在等待 Gateway API 会话同步。"
            last_result = "浏览器已回到认证态，后端会话仍未同步。"
            status = "waiting_confirm"
        elif expires_seconds is not None and expires_seconds <= 0:
            message = "本轮手机确认窗口已过期，请重新点击按钮触发。"
            last_result = "手机确认窗口已过期。"
            status = "waiting_confirm"
        elif expires_seconds is not None and expires_seconds <= 30:
            message = f"已提交账号密码，请在 {expires_seconds}s 内完成手机确认，否则本轮会失效。"
            last_result = f"等待手机通知确认中（剩余 {expires_seconds}s）。"
            status = "waiting_confirm"
        else:
            message = "已提交账号密码，请点击手机通知完成 IBKR 2FA。"
            last_result = "等待手机通知确认中。"
            status = "waiting_confirm"

        self._report_2fa_status(
            status=status,
            reason=reason,
            source=source,
            attempt=attempt,
            detail=self._build_wait_detail(state, detail),
            message=message,
            last_result=last_result,
            state_patch=self._build_state_patch(state, state_patch),
        )

    def login(
        self,
        reason: str = "manual_reauth",
        source: str = "ibkr_compute",
        detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
        self.reset_cancel()
        if not self._username or not self._password:
            logger.error("IBKR_USERNAME or IBKR_PASSWORD not set")
            self._report_2fa_status(
                status="failed",
                reason=reason,
                source=source,
                detail=detail,
                message="缺少 IBKR 登录凭证，无法发起验证。",
                error="missing_credentials",
            )
            return False

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        last_failure_kind = "failed"
        last_failure_detail = dict(detail or {})
        last_failure_error = ""

        for attempt in range(1, MAX_LOGIN_RETRIES + 1):
            if self._cancel_requested:
                logger.info("Login cancelled before attempt %d", attempt)
                break
            logger.info("Login attempt %d/%d", attempt, MAX_LOGIN_RETRIES)
            self._log_to_pb("login_attempt", "info", f"Attempt {attempt}")
            self._last_wait_context = {}
            reached_2fa_stage = False
            allow_retry = True

            try:
                self._ensure_driver()
                login_url = f"{self.gateway_url}/sso/Login?forwardTo=22&RL=1&ip2loc=on"
                self._driver.get(login_url)
                time.sleep(3)
                self._install_trace_hooks()

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
                reached_2fa_stage = True
                time.sleep(2)
                self._install_trace_hooks()

                page_state = self._capture_page_state()
                logger.info("Credentials submitted, current 2FA mode=%s", page_state.get("mode") or "unknown")
                self._log_to_pb(
                    "2fa_waiting",
                    "info",
                    page_state.get("body_excerpt") or "Waiting for 2FA confirmation",
                )
                self._report_wait_state(
                    reason=reason,
                    source=source,
                    attempt=attempt,
                    detail=detail,
                    page_state=page_state,
                )

                if self._wait_for_2fa_completion(
                    reason=reason,
                    source=source,
                    attempt=attempt,
                    detail=detail,
                ):
                    self._last_login_time = time.time()
                    logger.info("Login successful!")
                    self._log_to_pb("login_success", "ok", "")
                    self._report_2fa_status(
                        status="success",
                        reason=reason,
                        source=source,
                        attempt=attempt,
                        detail=self._build_wait_detail(self._last_wait_context, detail),
                        message="2FA 验证成功，Gateway 已恢复认证。",
                        last_result="验证成功。",
                        state_patch=self._build_state_patch(
                            self._last_wait_context,
                            {
                                "response_code": "",
                                "response_status": "",
                                "response_received_at": "",
                                "response_submitted_at": "",
                            },
                        ),
                    )
                    self._close_driver()
                    return True

                wait_state = dict(self._last_wait_context or {})
                wait_mode = str(wait_state.get("mode") or "").strip()
                allow_retry = False
                last_failure_kind = "timeout"
                last_failure_error = "challenge_response_timeout" if wait_mode == "challenge_response" else "2fa_timeout"
                last_failure_detail = {
                    **self._build_wait_detail(wait_state, detail),
                    "最后尝试": f"{attempt}/{MAX_LOGIN_RETRIES}",
                }
                logger.warning("2FA flow timed out in mode=%s", wait_mode or "unknown")
                self._log_to_pb("2fa_timeout", "warning", f"Attempt {attempt} mode={wait_mode or 'unknown'}")

            except Exception as e:
                logger.error("Login error on attempt %d: %s", attempt, e)
                self._log_to_pb("login_error", "error", str(e)[:500])
                last_failure_kind = "failed"
                last_failure_error = str(e)[:500]
                last_failure_detail = {
                    **self._build_wait_detail(self._last_wait_context, detail),
                    "最后尝试": f"{attempt}/{MAX_LOGIN_RETRIES}",
                }
                allow_retry = not reached_2fa_stage
            finally:
                if attempt < MAX_LOGIN_RETRIES and allow_retry:
                    self._close_driver()
                    time.sleep(5)

            if not allow_retry:
                break

        self._close_driver()
        logger.error("Login flow ended without authentication")
        self._log_to_pb("login_failed", "error", "Login flow ended without authentication")
        if last_failure_kind == "timeout":
            is_challenge_timeout = last_failure_error == "challenge_response_timeout"
            self._report_2fa_status(
                status="timeout",
                reason=reason,
                source=source,
                detail=last_failure_detail,
                message=(
                    "等待 Challenge Response Code 超时，请重新点击按钮触发。"
                    if is_challenge_timeout
                    else "等待 IBKR Mobile 确认超时，请重新点击按钮触发。"
                ),
                last_result=(
                    "等待 Challenge Response 超时。"
                    if is_challenge_timeout
                    else "等待手机确认超时。"
                ),
                error=last_failure_error,
                state_patch=self._build_state_patch(
                    self._last_wait_context,
                    {
                        "response_code": "",
                        "response_status": "",
                    },
                ),
            )
        else:
            self._report_2fa_status(
                status="failed",
                reason=reason,
                source=source,
                detail=last_failure_detail,
                message="IBKR 登录流程失败，请重新点击按钮触发。",
                last_result="登录流程失败。",
                error=last_failure_error or "login_failed",
                state_patch=self._build_state_patch(self._last_wait_context),
            )
        return False

    def _wait_for_2fa_completion(
        self,
        reason: str,
        source: str,
        attempt: int,
        detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
        import requests

        session = requests.Session()
        session.verify = False

        self._last_wait_context = {}
        start = time.time()
        base_deadline = start + MAX_2FA_WAIT
        challenge_deadline = 0.0
        response_deadline = 0.0
        last_report_key = None
        last_browser_probe_at = 0.0
        last_backend_promote_at = 0.0
        browser_state: Dict[str, Any] = {}
        submitted_response = ""
        active_challenge_code = ""

        while True:
            now = time.time()
            effective_deadline = max(base_deadline, challenge_deadline, response_deadline)
            if now >= effective_deadline:
                break
            if self._cancel_requested:
                logger.info("2FA wait cancelled by service stop request")
                return False
            try:
                if (now - last_browser_probe_at) >= BROWSER_PROBE_SECONDS:
                    browser_state = self._fetch_browser_gateway_state()
                    last_browser_probe_at = now
                page_state = self._capture_page_state()
                page_state["browser_gateway_state"] = dict(browser_state or {})
                self._last_wait_context = page_state

                page_mode = str(page_state.get("mode") or "").strip()
                challenge_code = str(page_state.get("challenge_code") or "").strip()
                normalized_challenge = self._normalize_code(challenge_code)
                if page_mode == "challenge_response":
                    if normalized_challenge and normalized_challenge != active_challenge_code:
                        active_challenge_code = normalized_challenge
                        challenge_deadline = max(challenge_deadline, time.time() + CHALLENGE_RESPONSE_WAIT)
                        logger.info(
                            "Challenge/Response detected for %s, extending wait by %ss",
                            challenge_code or normalized_challenge,
                            CHALLENGE_RESPONSE_WAIT,
                        )
                    elif not challenge_deadline:
                        challenge_deadline = time.time() + CHALLENGE_RESPONSE_WAIT

                sso_expires_ms = browser_state.get("sso_expires_ms")
                if isinstance(sso_expires_ms, (int, float)):
                    if sso_expires_ms <= 0:
                        expiry_bucket = "expired"
                    elif sso_expires_ms <= 30000:
                        expiry_bucket = "lt30"
                    else:
                        expiry_bucket = ""
                else:
                    expiry_bucket = ""

                report_key = (
                    page_state.get("mode"),
                    page_state.get("challenge_code"),
                    page_state.get("body_excerpt"),
                    expiry_bucket,
                    bool(browser_state.get("authenticated", False)),
                )
                if report_key != last_report_key:
                    self._report_wait_state(
                        reason=reason,
                        source=source,
                        attempt=attempt,
                        detail=detail,
                        page_state=page_state,
                    )
                    last_report_key = report_key

                if page_mode == "challenge_response":
                    response_code = self._get_pending_response_code(challenge_code)
                    if response_code and response_code != submitted_response:
                        if self._submit_challenge_response(response_code):
                            submitted_response = response_code
                            response_deadline = max(response_deadline, time.time() + POST_RESPONSE_GRACE_SECONDS)
                            logger.info("Submitted challenge response for active 2FA flow")
                            self._report_2fa_status(
                                status="waiting_confirm",
                                reason=reason,
                                source=source,
                                attempt=attempt,
                                detail=self._build_wait_detail(page_state, detail),
                                message="已提交 Challenge Response Code，等待 Gateway 完成认证。",
                                last_result="Response Code 已提交，等待认证完成。",
                                state_patch=self._build_state_patch(
                                    page_state,
                                    {
                                        "response_code": "",
                                        "response_status": "submitted",
                                        "response_submitted_at": self._now_et(),
                                    },
                                ),
                            )
                            time.sleep(1)
                        else:
                            self._report_2fa_status(
                                status="waiting_response",
                                reason=reason,
                                source=source,
                                attempt=attempt,
                                detail=self._build_wait_detail(page_state, detail),
                                message="读取到 Response Code，但浏览器提交失败，请重新提交或重新触发。",
                                last_result="Response Code 浏览器提交失败。",
                                error="challenge_submit_failed",
                                state_patch=self._build_state_patch(
                                    page_state,
                                    {
                                        "response_status": "submit_failed",
                                    },
                                ),
                            )

                backend_result = self._check_backend_auth(session)
                backend_authenticated = bool(backend_result.get("authenticated", False))
                page_state["backend_authenticated"] = backend_authenticated
                self._last_wait_context = page_state
                if backend_authenticated:
                    self._last_wait_context = page_state
                    return True
                competing = bool((backend_result.get("payload") or {}).get("competing", False))
                if competing:
                    logger.warning("Competing session detected")

                browser_authenticated = bool(browser_state.get("authenticated", False))
                if browser_authenticated and not backend_authenticated:
                    logger.warning("2FA auth mismatch: browser authenticated but backend auth/status still false")
                    self._log_to_pb("2fa_auth_mismatch", "warning", "browser=true backend=false")
                    if (time.time() - last_backend_promote_at) >= BACKEND_PROMOTE_SECONDS:
                        promoted = self._promote_backend_auth(session)
                        last_backend_promote_at = time.time()
                        page_state["backend_authenticated"] = bool(promoted.get("authenticated", False))
                        self._last_wait_context = page_state
                        if promoted.get("authenticated"):
                            return True

                page_source = self._driver.page_source if self._driver else ""
                if (
                    page_state.get("mode") == "success"
                    or "Client login succeeds" in page_source
                    or "two_fa_result" in page_source
                ):
                    time.sleep(2)
                    promoted = self._promote_backend_auth(session)
                    page_state["backend_authenticated"] = bool(promoted.get("authenticated", False))
                    self._last_wait_context = page_state
                    if promoted.get("authenticated"):
                        return True

            except Exception as e:
                logger.debug("2FA wait check: %s", e)

            time.sleep(WAIT_POLL_SECONDS)

        return False

    def status(self) -> dict:
        from datetime import datetime
        return {
            "has_credentials": bool(self._username and self._password),
            "last_login": datetime.fromtimestamp(self._last_login_time).isoformat()
            if self._last_login_time else None,
        }
