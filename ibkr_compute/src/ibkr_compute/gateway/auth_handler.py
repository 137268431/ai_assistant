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

from ibkr_compute.gateway.cookie_store import save_browser_cookies, save_cookies

logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
LOGIN_TIMEOUT = int(os.environ.get("IBKR_LOGIN_TIMEOUT", "120"))
MAX_2FA_WAIT = int(os.environ.get("IBKR_2FA_WAIT", "180"))
CHALLENGE_RESPONSE_WAIT = int(os.environ.get("IBKR_CHALLENGE_RESPONSE_WAIT", "240"))
POST_RESPONSE_GRACE_SECONDS = int(os.environ.get("IBKR_2FA_RESPONSE_GRACE", "90"))
MAX_LOGIN_RETRIES = 3
MAX_2FA_RETRY_ROUNDS = max(0, int(os.environ.get("IBKR_2FA_RETRY_ROUNDS", "2")))
TWO_FA_RETRY_INTERVAL_SECONDS = max(0, int(os.environ.get("IBKR_2FA_RETRY_INTERVAL_SECONDS", "60")))
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

    def _future_et(self, offset_seconds: int) -> str:
        from datetime import datetime, timezone, timedelta
        et_now = datetime.now(timezone(timedelta(hours=-4))) + timedelta(seconds=max(0, offset_seconds))
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
            # NOTE: Do NOT install trace hooks here. They monkey-patch
            # window.fetch / XMLHttpRequest and intercept /sso/Authenticator,
            # which the SSO page itself uses to poll for 2FA confirmation.
            # Installing them breaks the SSO redirect to /sso/Dispatcher.
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

        mode_timeline = str(state.get("mode_timeline") or "").strip()
        if mode_timeline:
            wait_detail["模式轨迹"] = mode_timeline

        mode_changed_at = str(state.get("mode_changed_at") or "").strip()
        if mode_changed_at:
            wait_detail["最近模式切换"] = mode_changed_at

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

        mode_timeline = str(state.get("mode_timeline") or "").strip()
        if mode_timeline:
            patch["mode_timeline"] = mode_timeline

        mode_changed_at = str(state.get("mode_changed_at") or "").strip()
        if mode_changed_at:
            patch["mode_changed_at"] = mode_changed_at

        if extra_patch:
            patch.update(extra_patch)
        return patch

    def _build_retry_detail(
        self,
        detail: Optional[Dict[str, Any]],
        retry_round: int,
        total_rounds: int,
    ) -> Dict[str, Any]:
        payload = dict(detail or {})
        payload["2FA轮次"] = f"{retry_round}/{total_rounds}"
        if total_rounds > 1:
            payload["自动重试"] = "enabled"
            payload["自动重试间隔秒"] = TWO_FA_RETRY_INTERVAL_SECONDS
        return payload

    def _build_retry_cycle_patch(
        self,
        cycle_started_at: str,
        retry_round: int,
        total_rounds: int,
        extra_patch: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        patch: Dict[str, Any] = {
            "requested_at": cycle_started_at,
            "triggered_at": cycle_started_at,
            "retry_round": retry_round,
            "retry_total_rounds": total_rounds,
            "retry_interval_seconds": TWO_FA_RETRY_INTERVAL_SECONDS,
            "auto_retry_enabled": total_rounds > 1,
            "next_retry_at": "",
        }
        if extra_patch:
            patch.update(extra_patch)
        return patch

    def _sleep_with_cancel(self, wait_seconds: int) -> bool:
        deadline = time.time() + max(0, wait_seconds)
        while time.time() < deadline:
            if self._cancel_requested:
                return False
            time.sleep(min(1.0, max(0.1, deadline - time.time())))
        return not self._cancel_requested

    def _check_backend_auth(self, session) -> Dict[str, Any]:
        try:
            resp = session.post(
                f"{self.gateway_url}/v1/api/iserver/auth/status",
                timeout=10,
            )
            payload = resp.json() if resp.status_code == 200 else {}
            if resp.status_code == 200:
                save_cookies(session)
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

        total_2fa_rounds = 1 + MAX_2FA_RETRY_ROUNDS
        last_failure_kind = "failed"
        last_failure_detail = dict(detail or {})
        last_failure_error = ""
        last_failure_retryable = False
        last_cycle_started_at = self._now_et()
        executed_retry_round = 0

        for retry_round in range(1, total_2fa_rounds + 1):
            if self._cancel_requested:
                logger.info("Login cancelled before 2FA round %d", retry_round)
                break

            executed_retry_round = retry_round
            last_cycle_started_at = self._now_et()
            cycle_detail = self._build_retry_detail(detail, retry_round, total_2fa_rounds)
            cycle_state_patch = self._build_retry_cycle_patch(last_cycle_started_at, retry_round, total_2fa_rounds)
            last_failure_retryable = False

            for attempt in range(1, MAX_LOGIN_RETRIES + 1):
                if self._cancel_requested:
                    logger.info("Login cancelled before attempt %d in 2FA round %d", attempt, retry_round)
                    break
                logger.info(
                    "Login attempt %d/%d (2FA round %d/%d)",
                    attempt,
                    MAX_LOGIN_RETRIES,
                    retry_round,
                    total_2fa_rounds,
                )
                self._log_to_pb("login_attempt", "info", f"Attempt {attempt} round {retry_round}/{total_2fa_rounds}")
                self._last_wait_context = {}
                reached_2fa_stage = False
                allow_retry = True

                try:
                    self._ensure_driver()
                    login_url = f"{self.gateway_url}/sso/Login?forwardTo=22&RL=1&ip2loc=on"
                    self._driver.get(login_url)
                    time.sleep(3)
                    # Trace hooks intentionally NOT installed here — they
                    # monkey-patch fetch/XHR and interfere with SSO page JS.

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
                    # DO NOT install trace hooks here. They monkey-patch window.fetch
                    # and XMLHttpRequest, intercepting /sso/Authenticator which the
                    # SSO page itself uses to poll for 2FA confirmation. Hijacking
                    # that endpoint can break the SSO confirmation callback.

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
                        detail=cycle_detail,
                        page_state=page_state,
                        state_patch=cycle_state_patch,
                    )

                    if self._wait_for_2fa_completion(
                        reason=reason,
                        source=source,
                        attempt=attempt,
                        detail=cycle_detail,
                        cycle_state_patch=cycle_state_patch,
                    ):
                        self._last_login_time = time.time()
                        logger.info("Login successful!")
                        self._log_to_pb("login_success", "ok", "")
                        self._report_2fa_status(
                            status="success",
                            reason=reason,
                            source=source,
                            attempt=attempt,
                            detail=self._build_wait_detail(self._last_wait_context, cycle_detail),
                            message="2FA 验证成功，Gateway 已恢复认证。",
                            last_result="验证成功。",
                            state_patch=self._build_state_patch(
                                self._last_wait_context,
                                {
                                    **cycle_state_patch,
                                    "next_retry_at": "",
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
                    wait_abort_reason = str(wait_state.get("abort_reason") or "").strip()
                    allow_retry = False
                    last_failure_retryable = True
                    last_failure_kind = "timeout"
                    if wait_abort_reason in {"push_to_challenge_transition", "challenge_response_unsupported"}:
                        last_failure_error = wait_abort_reason
                    else:
                        last_failure_error = "challenge_response_timeout" if wait_mode == "challenge_response" else "2fa_timeout"
                    last_failure_detail = {
                        **self._build_wait_detail(wait_state, cycle_detail),
                        "最后尝试": f"{attempt}/{MAX_LOGIN_RETRIES}",
                    }
                    logger.warning(
                        "2FA flow ended without auth in mode=%s abort_reason=%s",
                        wait_mode or "unknown",
                        wait_abort_reason or "-",
                    )
                    self._log_to_pb("2fa_timeout", "warning", f"Attempt {attempt} mode={wait_mode or 'unknown'}")

                except Exception as e:
                    logger.error("Login error on attempt %d: %s", attempt, e)
                    self._log_to_pb("login_error", "error", str(e)[:500])
                    last_failure_kind = "failed"
                    last_failure_error = str(e)[:500]
                    last_failure_detail = {
                        **self._build_wait_detail(self._last_wait_context, cycle_detail),
                        "最后尝试": f"{attempt}/{MAX_LOGIN_RETRIES}",
                    }
                    allow_retry = not reached_2fa_stage
                    last_failure_retryable = reached_2fa_stage
                finally:
                    if attempt < MAX_LOGIN_RETRIES and allow_retry:
                        self._close_driver()
                        time.sleep(5)

                if not allow_retry:
                    break

            if self._cancel_requested:
                break

            has_next_retry_round = last_failure_retryable and retry_round < total_2fa_rounds
            if not has_next_retry_round:
                break

            next_retry_round = retry_round + 1
            next_retry_at = self._future_et(TWO_FA_RETRY_INTERVAL_SECONDS)
            retry_state_patch = self._build_retry_cycle_patch(
                last_cycle_started_at,
                retry_round,
                total_2fa_rounds,
                {
                    "next_retry_at": next_retry_at,
                    "challenge_code": "",
                    "challenge_detected_at": "",
                    "response_code": "",
                    "response_status": "",
                    "response_received_at": "",
                    "response_submitted_at": "",
                },
            )

            if last_failure_kind == "timeout":
                is_challenge_timeout = last_failure_error == "challenge_response_timeout"
                is_push_to_challenge = last_failure_error == "push_to_challenge_transition"
                is_challenge_unsupported = last_failure_error == "challenge_response_unsupported"
                message = (
                    f"本轮手机确认没有建立可用 Session，IBKR 已切到 Challenge/Response；系统将在 {TWO_FA_RETRY_INTERVAL_SECONDS}s 后自动重试新的 push（下一轮 {next_retry_round}/{total_2fa_rounds}）。"
                    if is_push_to_challenge
                    else f"IBKR 当前进入 Challenge/Response；当前策略不走 challenge，将在 {TWO_FA_RETRY_INTERVAL_SECONDS}s 后自动重试新的 push（下一轮 {next_retry_round}/{total_2fa_rounds}）。"
                    if is_challenge_unsupported
                    else
                    f"等待 Challenge Response Code 超时，将在 {TWO_FA_RETRY_INTERVAL_SECONDS}s 后自动重试（下一轮 {next_retry_round}/{total_2fa_rounds}）。"
                    if is_challenge_timeout
                    else f"等待 IBKR Mobile 确认超时，将在 {TWO_FA_RETRY_INTERVAL_SECONDS}s 后自动重试（下一轮 {next_retry_round}/{total_2fa_rounds}）。"
                )
                last_result = (
                    f"检测到 push_notification -> challenge_response，计划自动重试 {next_retry_round}/{total_2fa_rounds}。"
                    if is_push_to_challenge
                    else f"检测到 challenge_response，计划自动重试 {next_retry_round}/{total_2fa_rounds}。"
                    if is_challenge_unsupported
                    else
                    f"等待 Challenge Response 超时，计划自动重试 {next_retry_round}/{total_2fa_rounds}。"
                    if is_challenge_timeout
                    else f"等待手机确认超时，计划自动重试 {next_retry_round}/{total_2fa_rounds}。"
                )
                status = "timeout"
            else:
                message = f"IBKR 登录流程失败，将在 {TWO_FA_RETRY_INTERVAL_SECONDS}s 后自动重试（下一轮 {next_retry_round}/{total_2fa_rounds}）。"
                last_result = f"登录流程失败，计划自动重试 {next_retry_round}/{total_2fa_rounds}。"
                status = "failed"

            self._report_2fa_status(
                status=status,
                reason=reason,
                source=source,
                detail=last_failure_detail,
                message=message,
                last_result=last_result,
                error=last_failure_error,
                state_patch=self._build_state_patch(self._last_wait_context, retry_state_patch),
            )
            logger.warning(
                "Scheduling automatic 2FA retry round %d/%d in %ss (last_failure=%s)",
                next_retry_round,
                total_2fa_rounds,
                TWO_FA_RETRY_INTERVAL_SECONDS,
                last_failure_kind,
            )
            self._close_driver()
            if not self._sleep_with_cancel(TWO_FA_RETRY_INTERVAL_SECONDS):
                break

        self._close_driver()
        logger.error("Login flow ended without authentication")
        self._log_to_pb("login_failed", "error", "Login flow ended without authentication")
        final_cycle_patch = self._build_retry_cycle_patch(
            last_cycle_started_at,
            max(executed_retry_round, 1),
            total_2fa_rounds,
            {"next_retry_at": ""},
        )
        exhausted_2fa_retries = (
            last_failure_retryable
            and executed_retry_round >= total_2fa_rounds
            and total_2fa_rounds > 1
        )
        if last_failure_kind == "timeout":
            is_challenge_timeout = last_failure_error == "challenge_response_timeout"
            is_push_to_challenge = last_failure_error == "push_to_challenge_transition"
            is_challenge_unsupported = last_failure_error == "challenge_response_unsupported"
            self._report_2fa_status(
                status="timeout",
                reason=reason,
                source=source,
                detail=last_failure_detail,
                message=(
                    "本轮手机确认没有建立可用 Session，IBKR 已切到 Challenge/Response；已达到自动重试上限，请重新点击按钮触发。"
                    if is_push_to_challenge and exhausted_2fa_retries
                    else "本轮手机确认没有建立可用 Session，IBKR 已切到 Challenge/Response；请重新点击按钮触发。"
                    if is_push_to_challenge
                    else "IBKR 当前进入 Challenge/Response；当前策略不走 challenge，已达到自动重试上限，请重新点击按钮触发。"
                    if is_challenge_unsupported and exhausted_2fa_retries
                    else "IBKR 当前进入 Challenge/Response；当前策略不走 challenge，请重新点击按钮触发。"
                    if is_challenge_unsupported
                    else
                    "等待 Challenge Response Code 超时，已达到自动重试上限，请重新点击按钮触发。"
                    if is_challenge_timeout and exhausted_2fa_retries
                    else "等待 Challenge Response Code 超时，请重新点击按钮触发。"
                    if is_challenge_timeout
                    else "等待 IBKR Mobile 确认超时，已达到自动重试上限，请重新点击按钮触发。"
                    if exhausted_2fa_retries
                    else "等待 IBKR Mobile 确认超时，请重新点击按钮触发。"
                ),
                last_result=(
                    "检测到 push_notification -> challenge_response，自动重试次数已耗尽。"
                    if is_push_to_challenge and exhausted_2fa_retries
                    else "检测到 push_notification -> challenge_response。"
                    if is_push_to_challenge
                    else "检测到 challenge_response，自动重试次数已耗尽。"
                    if is_challenge_unsupported and exhausted_2fa_retries
                    else "检测到 challenge_response。"
                    if is_challenge_unsupported
                    else
                    "等待 Challenge Response 超时，自动重试次数已耗尽。"
                    if is_challenge_timeout and exhausted_2fa_retries
                    else "等待 Challenge Response 超时。"
                    if is_challenge_timeout
                    else "等待手机确认超时，自动重试次数已耗尽。"
                    if exhausted_2fa_retries
                    else "等待手机确认超时。"
                ),
                error=last_failure_error,
                state_patch=self._build_state_patch(
                    self._last_wait_context,
                    {
                        **final_cycle_patch,
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
                message=(
                    "IBKR 登录流程失败，已达到自动重试上限，请重新点击按钮触发。"
                    if exhausted_2fa_retries
                    else "IBKR 登录流程失败，请重新点击按钮触发。"
                ),
                last_result=(
                    "登录流程失败，自动重试次数已耗尽。"
                    if exhausted_2fa_retries
                    else "登录流程失败。"
                ),
                error=last_failure_error or "login_failed",
                state_patch=self._build_state_patch(self._last_wait_context, final_cycle_patch),
            )
        return False

    def _sync_browser_cookies(self, session) -> None:
        if not self._driver:
            return
        try:
            browser_cookies = self._driver.get_cookies()
            for cookie in browser_cookies:
                session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain", ""),
                    path=cookie.get("path", "/"),
                )
            save_browser_cookies(browser_cookies)
            save_cookies(session)
            logger.debug("Synced %d browser cookies to requests session", len(self._driver.get_cookies()))
        except Exception as exc:
            logger.debug("Failed to sync browser cookies: %s", exc)

    def _wait_for_2fa_completion(
        self,
        reason: str,
        source: str,
        attempt: int,
        detail: Optional[Dict[str, Any]] = None,
        cycle_state_patch: Optional[Dict[str, Any]] = None,
    ) -> bool:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        session = requests.Session()
        session.verify = False
        self._sync_browser_cookies(session)

        self._last_wait_context = {}
        start = time.time()
        base_deadline = start + MAX_2FA_WAIT
        challenge_deadline = 0.0
        response_deadline = 0.0
        last_report_key = None
        last_browser_probe_at = 0.0
        last_backend_promote_at = 0.0
        last_cookie_sync_at = time.time()
        browser_state: Dict[str, Any] = {}
        submitted_response = ""
        active_challenge_code = ""

        COOKIE_SYNC_INTERVAL = 10
        # Interval for lightweight URL-only checks (no JS execution).
        URL_CHECK_INTERVAL = 2
        # Heavier DOM/JS probes are run much less frequently so that the SSO
        # page's own JavaScript (which handles the 2FA confirmation callback
        # and triggers the redirect to /sso/Dispatcher) is not blocked by
        # Selenium execute_script() calls occupying the main thread.
        HEAVY_PROBE_INTERVAL = 30
        # Delay the first heavy probe so the SSO page has uninterrupted
        # time to set up its own JS polling and detect 2FA confirmation.
        last_heavy_probe_at = time.time()
        success_detected = False
        success_promote_cycles = 0
        initial_mode_detected = False
        last_page_mode = ""
        mode_history: list[str] = []

        # ================================================================
        # CRITICAL: During the 2FA wait loop, do NOT execute ANY
        # JavaScript in the browser (no execute_script, no
        # execute_async_script, no _capture_page_state, no
        # _fetch_browser_gateway_state, no driver.page_source).
        #
        # The SSO page uses its own JS polling (XHR/fetch to
        # /sso/Authenticator) to detect when the user confirms on
        # their phone. ANY Selenium JS execution blocks the browser
        # main thread and can cause the SSO page to miss the
        # confirmation callback, preventing the redirect to
        # /sso/Dispatcher.
        #
        # This loop mirrors the working legacy ibkr_login.py:
        # - Read current_url (does NOT block JS)
        # - Sync cookies periodically (Python-side only)
        # - Check auth via Python requests (external to browser)
        # - On success URL, promote via tickle + auth check
        # ================================================================
        # ================================================================
        # This loop mirrors the proven-working legacy ibkr_login.py:
        # 1. Read current_url + body.text every cycle
        # 2. Check for Dispatcher URL or success text
        # 3. Sync cookies every 10s
        # 4. Check backend auth via Python requests
        # 5. Sleep 5s between cycles (matching legacy)
        # ================================================================
        from selenium.webdriver.common.by import By

        POLL_INTERVAL = 5

        while True:
            now = time.time()
            effective_deadline = max(base_deadline, challenge_deadline, response_deadline)
            if now >= effective_deadline:
                break
            if self._cancel_requested:
                logger.info("2FA wait cancelled by service stop request")
                return False

            elapsed = int(now - start)

            try:
                # Sync cookies periodically.
                if (now - last_cookie_sync_at) >= COOKIE_SYNC_INTERVAL:
                    self._sync_browser_cookies(session)
                    last_cookie_sync_at = now

                # Read browser state — matches legacy ibkr_login.py pattern.
                page_url = ""
                page_text = ""
                try:
                    page_url = self._driver.current_url or "" if self._driver else ""
                    page_text = (self._driver.find_element(By.TAG_NAME, "body").text or "")[:300] if self._driver else ""
                except Exception:
                    pass

                # Check for success (URL redirect or page text).
                if "/sso/Dispatcher" in page_url or "Client login succeeds" in page_text:
                    logger.info("[2FA %ds] SUCCESS PAGE DETECTED: %s", elapsed, page_url)
                    self._sync_browser_cookies(session)
                    for promote_round in range(5):
                        time.sleep(2)
                        self._sync_browser_cookies(session)
                        try:
                            session.post(f"{self.gateway_url}/v1/api/tickle", timeout=10)
                        except Exception:
                            pass
                        backend_result = self._check_backend_auth(session)
                        if backend_result.get("authenticated"):
                            logger.info("[2FA %ds] Backend auth confirmed (round %d)", elapsed, promote_round + 1)
                            self._last_wait_context = {"mode": "success", "url": page_url, "backend_authenticated": True}
                            return True
                        logger.info("[2FA %ds] Promote attempt %d...", elapsed, promote_round + 1)
                    logger.warning("[2FA %ds] Success page but backend auth not confirmed", elapsed)

                # Detect challenge/response mode.
                page_text_lower = page_text.lower()
                if "enter the challenge code below" in page_text_lower or "response code" in page_text_lower:
                    logger.warning("[2FA %ds] Challenge/Response detected, aborting this round", elapsed)
                    page_state = {"mode": "challenge_response", "url": page_url, "body_excerpt": page_text[:240]}
                    page_state["abort_reason"] = "challenge_response_unsupported"
                    self._last_wait_context = page_state
                    self._report_2fa_status(
                        status="timeout",
                        reason=reason,
                        source=source,
                        attempt=attempt,
                        detail=self._build_wait_detail(page_state, detail),
                        message="IBKR 当前进入 Challenge/Response；当前策略不走 challenge，本轮将结束并重新触发新的 push。",
                        last_result="检测到 challenge_response；本轮不会进入 challenge 提交流程。",
                        error="challenge_response_unsupported",
                        state_patch=self._build_state_patch(page_state, cycle_state_patch),
                    )
                    return False

                # Backend auth check via Python requests.
                backend_result = self._check_backend_auth(session)
                if backend_result.get("authenticated"):
                    logger.info("[2FA %ds] Backend auth confirmed via requests session", elapsed)
                    self._last_wait_context = {"mode": "success", "url": page_url, "backend_authenticated": True}
                    return True

                if elapsed % 15 == 0:
                    auth_info = backend_result.get("authenticated", False)
                    competing = backend_result.get("payload", {}).get("competing", False)
                    connected = backend_result.get("payload", {}).get("connected", False)
                    logger.info(
                        "[2FA %ds] auth=%s connected=%s competing=%s url=%s",
                        elapsed, auth_info, connected, competing, page_url,
                    )
                    if competing:
                        logger.warning("[2FA] Competing session detected!")

            except Exception as e:
                if elapsed % 30 == 0:
                    logger.debug("[2FA %ds] Check error: %s", elapsed, e)

            time.sleep(POLL_INTERVAL)

        return False

    def status(self) -> dict:
        from datetime import datetime
        return {
            "has_credentials": bool(self._username and self._password),
            "last_login": datetime.fromtimestamp(self._last_login_time).isoformat()
            if self._last_login_time else None,
        }
