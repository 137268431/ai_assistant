"""
IBKR Gateway 自动登录 + 2FA
- Selenium headless 自动填充登录表单
- 识别 Push / Challenge-Response 两种 2FA 模式
- 将实时状态同步到 PocketBase / Feishu
"""

import os
import re
import time
import json
import base64
import hashlib
import logging
from urllib.parse import urlparse
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
BACKEND_PROMOTE_ATTEMPTS = max(1, int(os.environ.get("IBKR_2FA_PROMOTE_ATTEMPTS", "5")))
BACKEND_PROMOTE_INTERVAL_SECONDS = max(1, int(os.environ.get("IBKR_2FA_PROMOTE_INTERVAL_SECONDS", "2")))
BACKEND_REAUTH_GRACE_SECONDS = max(1, int(os.environ.get("IBKR_2FA_REAUTH_GRACE_SECONDS", "3")))
GATEWAY_LOG_TAIL_LINES = max(5, int(os.environ.get("IBKR_GATEWAY_LOG_TAIL_LINES", "20")))
GATEWAY_LOG_TAIL_MINUTES = max(1, int(os.environ.get("IBKR_GATEWAY_LOG_TAIL_MINUTES", "5")))
PUSH_BODY_TEXT_SAMPLE_SECONDS = max(15, int(os.environ.get("IBKR_2FA_PUSH_BODY_TEXT_SAMPLE_SECONDS", "30")))
PUSH_BODY_TEXT_INITIAL_DELAY_SECONDS = max(
    30,
    int(os.environ.get("IBKR_2FA_PUSH_BODY_TEXT_INITIAL_DELAY_SECONDS", "90")),
)
PUSH_BODY_TEXT_FINAL_WINDOW_SECONDS = max(
    10,
    int(os.environ.get("IBKR_2FA_PUSH_BODY_TEXT_FINAL_WINDOW_SECONDS", "25")),
)
CHALLENGE_BODY_TEXT_SAMPLE_SECONDS = max(
    3,
    int(os.environ.get("IBKR_2FA_CHALLENGE_BODY_TEXT_SAMPLE_SECONDS", "5")),
)
WAIT_STATUS_LOG_SECONDS = max(10, int(os.environ.get("IBKR_2FA_WAIT_STATUS_LOG_SECONDS", "15")))
CURRENT_URL_SLOW_READ_MS = max(25, int(os.environ.get("IBKR_2FA_CURRENT_URL_SLOW_MS", "80")))
BODY_TEXT_SLOW_READ_MS = max(50, int(os.environ.get("IBKR_2FA_BODY_TEXT_SLOW_MS", "200")))
CAPTURE_PAGE_STATE_SLOW_MS = max(50, int(os.environ.get("IBKR_CAPTURE_PAGE_STATE_SLOW_MS", "200")))
PASSIVE_NETWORK_TRACE_ENABLED = str(
    os.environ.get("IBKR_2FA_PASSIVE_NETWORK_TRACE_ENABLED", "true")
).strip().lower() not in {"0", "false", "no", "off"}
PASSIVE_NETWORK_TRACE_POLL_SECONDS = max(
    10,
    int(os.environ.get("IBKR_2FA_PASSIVE_NETWORK_TRACE_POLL_SECONDS", "15")),
)
PASSIVE_NETWORK_TRACE_BODY_LIMIT = max(
    120,
    int(os.environ.get("IBKR_2FA_PASSIVE_NETWORK_TRACE_BODY_LIMIT", "260")),
)
PASSIVE_NETWORK_TRACE_HISTORY_LIMIT = max(
    4,
    int(os.environ.get("IBKR_2FA_PASSIVE_NETWORK_TRACE_HISTORY_LIMIT", "12")),
)
PASSIVE_NETWORK_TRACE_URL_MARKERS = (
    "/sso/Authenticator",
    "/sso/Dispatcher",
)
CHALLENGE_REJECTION_TEXT_PATTERNS = (
    (re.compile(r"authentication failed", re.IGNORECASE), "Authentication failed"),
    (re.compile(r"incorrect security code", re.IGNORECASE), "Incorrect security code"),
    (re.compile(r"invalid(?:\s+security)?\s+code", re.IGNORECASE), "Invalid security code"),
    (re.compile(r"invalid response code", re.IGNORECASE), "Invalid response code"),
    (re.compile(r"try again", re.IGNORECASE), "Try again"),
)


class AuthHandler:
    def __init__(self, gateway_url: str = None, pb_client=None, gateway_manager=None):
        self.gateway_url = (gateway_url or GATEWAY_URL).rstrip("/")
        self.pb_client = pb_client
        self.gateway_manager = gateway_manager
        self._username = os.environ.get("IBKR_USERNAME", "")
        self._password = os.environ.get("IBKR_PASSWORD", "")
        self._driver = None
        self._last_login_time: Optional[float] = None
        self._last_wait_context: Dict[str, Any] = {}
        self._cancel_requested = False
        self._last_cookie_summary_fingerprint = ""
        self._network_trace_requests: Dict[str, Dict[str, Any]] = {}
        self._network_trace_history: list[Dict[str, Any]] = []
        self._cookie_bridge_history: list[Dict[str, Any]] = []

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
        if PASSIVE_NETWORK_TRACE_ENABLED:
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        snap_chrome = "/snap/chromium/current/usr/lib/chromium-browser/chrome"
        if os.path.isfile(snap_chrome):
            options.binary_location = snap_chrome

        service = Service(executable_path="/usr/local/bin/chromedriver")
        self._driver = webdriver.Chrome(service=service, options=options)
        self._driver.set_page_load_timeout(LOGIN_TIMEOUT)
        self._reset_passive_network_trace()
        self._enable_passive_network_trace()

    def _close_driver(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
        self._reset_passive_network_trace()

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
        started_perf = time.perf_counter()
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

        elapsed_ms = round((time.perf_counter() - started_perf) * 1000, 1)
        if elapsed_ms >= CAPTURE_PAGE_STATE_SLOW_MS:
            logger.warning(
                "_capture_page_state slow_ms=%s mode=%s url=%s",
                elapsed_ms,
                state.get("mode") or "unknown",
                state.get("url") or "-",
            )
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

        challenge_feedback = str(state.get("challenge_feedback") or "").strip()
        if challenge_feedback:
            wait_detail["页面反馈"] = challenge_feedback

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

        body_sample_reason = str(state.get("body_sample_reason") or "").strip()
        if body_sample_reason:
            wait_detail["最近Body采样原因"] = body_sample_reason

        push_body_sample_count = state.get("push_body_sample_count")
        if isinstance(push_body_sample_count, int) and push_body_sample_count >= 0:
            wait_detail["Push期Body采样次数"] = push_body_sample_count

        passive_network_summary = str(state.get("passive_network_summary") or "").strip()
        if passive_network_summary:
            wait_detail["最近Authenticator回包"] = passive_network_summary

        passive_network_history = str(state.get("passive_network_history") or "").strip()
        if passive_network_history:
            wait_detail["Authenticator轨迹"] = passive_network_history

        cookie_bridge_timeline = str(state.get("cookie_bridge_timeline") or "").strip()
        if cookie_bridge_timeline:
            wait_detail["x-sess轨迹"] = cookie_bridge_timeline

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

        challenge_feedback = str(state.get("challenge_feedback") or "").strip()
        if challenge_feedback:
            patch["challenge_feedback"] = challenge_feedback[:260]
        elif mode != "challenge_response":
            patch["challenge_feedback"] = ""
            patch["response_rejected_at"] = ""

        if mode != "challenge_response":
            patch["response_code"] = ""
            patch["response_status"] = ""
            patch["response_received_at"] = ""
            patch["response_submitted_at"] = ""
            patch["response_rejected_at"] = ""

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

        body_sample_reason = str(state.get("body_sample_reason") or "").strip()
        if body_sample_reason:
            patch["body_sample_reason"] = body_sample_reason

        push_body_sample_count = state.get("push_body_sample_count")
        if isinstance(push_body_sample_count, int) and push_body_sample_count >= 0:
            patch["push_body_sample_count"] = push_body_sample_count

        passive_network_summary = str(state.get("passive_network_summary") or "").strip()
        if passive_network_summary:
            patch["passive_network_summary"] = passive_network_summary[:320]

        passive_network_history = str(state.get("passive_network_history") or "").strip()
        if passive_network_history:
            patch["passive_network_history"] = passive_network_history[:1200]

        cookie_bridge_timeline = str(state.get("cookie_bridge_timeline") or "").strip()
        if cookie_bridge_timeline:
            patch["cookie_bridge_timeline"] = cookie_bridge_timeline[:600]

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

    def _reset_passive_network_trace(self) -> None:
        self._network_trace_requests = {}
        self._network_trace_history = []
        self._cookie_bridge_history = []
        self._last_cookie_summary_fingerprint = ""

    def _enable_passive_network_trace(self) -> None:
        if not PASSIVE_NETWORK_TRACE_ENABLED or not self._driver:
            return
        try:
            self._driver.execute_cdp_cmd(
                "Network.enable",
                {
                    "maxTotalBufferSize": 1_000_000,
                    "maxResourceBufferSize": 250_000,
                },
            )
        except Exception as exc:
            logger.debug("Failed to enable passive network trace: %s", exc)

    @staticmethod
    def _flatten_text(value: Any, limit: int = 160) -> str:
        text = " ".join(str(value or "").split())
        return text[:limit]

    @staticmethod
    def _secret_fingerprint(value: Any, length: int = 10) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:length]

    @staticmethod
    def _format_mode_timeline(entries: list[str], limit: int = 8) -> str:
        cleaned = [str(entry or "").strip() for entry in entries if str(entry or "").strip()]
        if not cleaned:
            return ""
        return " -> ".join(cleaned[-limit:])

    @staticmethod
    def _body_sample_reason(
        *,
        elapsed: int,
        base_deadline: float,
        now: float,
        seconds_since_last_sample: float,
        last_observed_mode: str,
    ) -> str:
        if last_observed_mode == "challenge_response":
            return (
                "challenge_interval"
                if seconds_since_last_sample >= CHALLENGE_BODY_TEXT_SAMPLE_SECONDS
                else ""
            )

        if elapsed < PUSH_BODY_TEXT_INITIAL_DELAY_SECONDS:
            return ""

        seconds_until_push_deadline = max(0.0, base_deadline - now)
        if (
            seconds_until_push_deadline <= PUSH_BODY_TEXT_FINAL_WINDOW_SECONDS
            and seconds_since_last_sample >= max(5.0, min(PUSH_BODY_TEXT_SAMPLE_SECONDS, 10.0))
        ):
            return "push_deadline_window"

        if seconds_since_last_sample >= PUSH_BODY_TEXT_SAMPLE_SECONDS:
            return "push_interval"

        return ""

    @staticmethod
    def _is_passive_trace_url(url: str) -> bool:
        target = str(url or "")
        return any(marker in target for marker in PASSIVE_NETWORK_TRACE_URL_MARKERS)

    @staticmethod
    def _format_network_body_summary(body_text: str) -> str:
        raw = str(body_text or "").strip()
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
        except Exception:
            return AuthHandler._flatten_text(raw, PASSIVE_NETWORK_TRACE_BODY_LIMIT)

        if isinstance(payload, dict):
            preferred_keys = (
                "status",
                "message",
                "error",
                "reason",
                "auth_res",
                "authenticated",
                "connected",
                "established",
                "competing",
                "reached_max_login",
                "challenge",
                "challengeCode",
                "mode",
                "result",
            )
            parts = []
            for key in preferred_keys:
                value = payload.get(key)
                if isinstance(value, (str, int, float, bool)) and value not in ("", None):
                    parts.append(f"{key}={value}")
            if parts:
                return AuthHandler._flatten_text(" ".join(parts), PASSIVE_NETWORK_TRACE_BODY_LIMIT)
        return AuthHandler._flatten_text(json.dumps(payload, ensure_ascii=True), PASSIVE_NETWORK_TRACE_BODY_LIMIT)

    @staticmethod
    def _format_passive_trace_history(entries: list[Dict[str, Any]], started_at: Optional[float] = None, limit: int = 5) -> str:
        cleaned = [entry for entry in (entries or []) if isinstance(entry, dict)]
        if not cleaned:
            return ""
        segments: list[str] = []
        for entry in cleaned[-limit:]:
            captured_at = entry.get("captured_at")
            if isinstance(captured_at, (int, float)) and isinstance(started_at, (int, float)):
                prefix = f"+{max(int(captured_at - started_at), 0)}s"
            else:
                prefix = "t"
            url = str(entry.get("url") or "")
            path = urlparse(url).path or url or "-"
            status = entry.get("status")
            body_summary = str(entry.get("body_summary") or entry.get("body_error") or "").strip()
            status_text = f" status={status}" if status not in (None, "") else ""
            body_text = f" {body_summary}" if body_summary else ""
            segments.append(
                AuthHandler._flatten_text(f"{prefix}:{path}{status_text}{body_text}", PASSIVE_NETWORK_TRACE_BODY_LIMIT + 40)
            )
        return " | ".join(segment for segment in segments if segment)

    @staticmethod
    def _extract_browser_cookie_value(browser_cookies: Any, name: str) -> str:
        target = str(name or "").strip()
        for cookie in list(browser_cookies or []):
            if str(cookie.get("name") or "").strip() == target:
                return str(cookie.get("value") or "").strip()
        return ""

    @staticmethod
    def _extract_session_cookie_value(session, name: str) -> str:
        target = str(name or "").strip()
        for cookie in session.cookies:
            if str(cookie.name or "").strip() == target:
                return str(cookie.value or "").strip()
        return ""

    @staticmethod
    def _format_cookie_bridge_timeline(entries: list[Dict[str, Any]], started_at: Optional[float] = None, limit: int = 6) -> str:
        cleaned = [entry for entry in (entries or []) if isinstance(entry, dict)]
        if not cleaned:
            return ""
        segments: list[str] = []
        for entry in cleaned[-limit:]:
            captured_at = entry.get("captured_at")
            if isinstance(captured_at, (int, float)) and isinstance(started_at, (int, float)):
                prefix = f"+{max(int(captured_at - started_at), 0)}s"
            else:
                prefix = "t"
            browser_fp = str(entry.get("browser_x_sess_uuid_fp") or "").strip() or "-"
            session_fp = str(entry.get("session_x_sess_uuid_fp") or "").strip() or "-"
            match_text = "yes" if entry.get("x_sess_uuid_match") else "no"
            segments.append(f"{prefix}:b={browser_fp} s={session_fp} match={match_text}")
        return " | ".join(segments)

    def _fetch_passive_network_body(self, request_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        entry = dict(meta or {})
        entry["body_fetched"] = True
        entry["captured_at"] = time.time()
        try:
            payload = self._driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id}) or {}
            body = payload.get("body")
            if payload.get("base64Encoded") and isinstance(body, str):
                try:
                    body = base64.b64decode(body).decode("utf-8", errors="replace")
                except Exception:
                    pass
            entry["body_summary"] = self._format_network_body_summary(str(body or ""))
        except Exception as exc:
            entry["body_error"] = str(exc)
            entry["body_summary"] = ""
        return entry

    def _build_passive_trace_snapshot(self) -> Dict[str, Any]:
        entries = self._network_trace_history[-PASSIVE_NETWORK_TRACE_HISTORY_LIMIT:]
        latest = entries[-1] if entries else {}
        summary = ""
        if latest:
            url = str(latest.get("url") or "")
            path = urlparse(url).path or url or "-"
            status = latest.get("status") or 0
            body_summary = str(latest.get("body_summary") or latest.get("body_error") or "").strip()
            body_text = f" body={body_summary}" if body_summary else ""
            summary = self._flatten_text(f"{path} status={status}{body_text}", PASSIVE_NETWORK_TRACE_BODY_LIMIT + 60)
        return {
            "entries": entries,
            "entry_count": len(entries),
            "last_summary": summary,
            "last_url": str(latest.get("url") or ""),
            "last_status": int(latest.get("status") or 0) if latest else 0,
            "last_body_summary": str(latest.get("body_summary") or ""),
            "last_body_error": str(latest.get("body_error") or ""),
        }

    def _drain_passive_network_trace(self, force_body: bool = False) -> Dict[str, Any]:
        if not PASSIVE_NETWORK_TRACE_ENABLED or not self._driver:
            return self._build_passive_trace_snapshot()

        try:
            raw_entries = self._driver.get_log("performance") or []
        except Exception as exc:
            logger.debug("Passive network trace drain failed: %s", exc)
            return self._build_passive_trace_snapshot()

        for raw_entry in raw_entries:
            try:
                message = json.loads(raw_entry.get("message") or "{}").get("message") or {}
            except Exception:
                continue
            method = str(message.get("method") or "")
            params = message.get("params") or {}

            if method == "Network.responseReceived":
                response = params.get("response") or {}
                url = str(response.get("url") or "")
                request_id = str(params.get("requestId") or "")
                if not request_id or not self._is_passive_trace_url(url):
                    continue
                self._network_trace_requests[request_id] = {
                    "request_id": request_id,
                    "url": url,
                    "status": int(response.get("status") or 0),
                    "mime_type": str(response.get("mimeType") or ""),
                    "received_monotonic": params.get("timestamp") or 0,
                    "body_fetched": False,
                }
            elif method == "Network.loadingFinished":
                request_id = str(params.get("requestId") or "")
                meta = self._network_trace_requests.get(request_id)
                if not meta or meta.get("body_fetched"):
                    continue
                entry = self._fetch_passive_network_body(request_id, meta)
                self._network_trace_requests[request_id] = entry
                self._network_trace_history.append(entry)
                self._network_trace_history = self._network_trace_history[-PASSIVE_NETWORK_TRACE_HISTORY_LIMIT:]

        if force_body:
            for request_id, meta in list(self._network_trace_requests.items()):
                if meta.get("body_fetched"):
                    continue
                entry = self._fetch_passive_network_body(request_id, meta)
                self._network_trace_requests[request_id] = entry
                self._network_trace_history.append(entry)
            self._network_trace_history = self._network_trace_history[-PASSIVE_NETWORK_TRACE_HISTORY_LIMIT:]

        return self._build_passive_trace_snapshot()

    def _infer_wait_mode(self, page_url: str, page_text: str, fallback_mode: str = "") -> str:
        url = str(page_url or "")
        lower = str(page_text or "").lower()
        if "/sso/Dispatcher" in url or "client login succeeds" in lower or "login complete" in lower:
            return "success"
        if "enter the challenge code below" in lower or "response code" in lower:
            return "challenge_response"
        if "open the ibkr notification on your phone" in lower or "tap the notification" in lower:
            return "push_notification"
        if "username" in lower and "password" in lower:
            return "login_form"
        fallback = str(fallback_mode or "").strip()
        if fallback in {"push_notification", "challenge_response"} and "/sso/Login" in url:
            return fallback
        return fallback or "unknown"

    def _read_current_url(self) -> tuple[str, float]:
        started_perf = time.perf_counter()
        url = ""
        if self._driver:
            try:
                url = self._driver.current_url or ""
            except Exception:
                url = ""
        elapsed_ms = round((time.perf_counter() - started_perf) * 1000, 1)
        return url, elapsed_ms

    def _read_body_text(self, limit: int = 300) -> tuple[str, float]:
        started_perf = time.perf_counter()
        text = ""
        if self._driver:
            try:
                from selenium.webdriver.common.by import By

                text = self._driver.find_element(By.TAG_NAME, "body").text or ""
            except Exception:
                text = ""
        elapsed_ms = round((time.perf_counter() - started_perf) * 1000, 1)
        return text[:limit], elapsed_ms

    def _summarize_cookie_bridge(self, browser_cookies, session) -> Dict[str, Any]:
        browser_names = sorted({str(cookie.get("name") or "").strip() for cookie in (browser_cookies or []) if str(cookie.get("name") or "").strip()})
        browser_domains = sorted({str(cookie.get("domain") or "").strip() or "(host-only)" for cookie in (browser_cookies or [])})
        session_names = sorted({str(cookie.name or "").strip() for cookie in session.cookies if str(cookie.name or "").strip()})
        raw_session_domains = [str(cookie.domain or "").strip() for cookie in session.cookies]
        session_domains = sorted({domain or "(host-only)" for domain in raw_session_domains})
        gateway_host = str(urlparse(self.gateway_url).hostname or "").strip().lower()
        browser_x_sess_uuid = self._extract_browser_cookie_value(browser_cookies, "x-sess-uuid")
        session_x_sess_uuid = self._extract_session_cookie_value(session, "x-sess-uuid")

        def _matches_gateway(domain: str) -> bool:
            normalized = str(domain or "").strip().lower().lstrip(".")
            if not normalized:
                return True
            return gateway_host == normalized or gateway_host.endswith("." + normalized) or normalized.endswith("." + gateway_host)

        gateway_domain_match = any(_matches_gateway(domain) for domain in raw_session_domains)
        return {
            "browser_count": len(list(browser_cookies or [])),
            "browser_names": browser_names,
            "browser_domains": browser_domains,
            "session_count": len(list(session.cookies)),
            "session_names": session_names,
            "session_domains": session_domains,
            "gateway_host": gateway_host or "-",
            "gateway_domain_match": gateway_domain_match,
            "browser_x_sess_uuid_fp": self._secret_fingerprint(browser_x_sess_uuid),
            "session_x_sess_uuid_fp": self._secret_fingerprint(session_x_sess_uuid),
            "x_sess_uuid_match": bool(browser_x_sess_uuid and session_x_sess_uuid and browser_x_sess_uuid == session_x_sess_uuid),
        }

    def _format_backend_auth_summary(self, result: Dict[str, Any]) -> str:
        payload = result.get("payload") or {}
        return (
            f"ok={result.get('ok', False)} "
            f"status={result.get('status_code', 0)} "
            f"auth={result.get('authenticated', False)} "
            f"connected={payload.get('connected', False)} "
            f"competing={payload.get('competing', False)} "
            f"message={self._flatten_text(payload.get('message') or result.get('error') or '-', 120)}"
        )

    def _log_gateway_recent_lines(self, reason: str, since_minutes: int = GATEWAY_LOG_TAIL_MINUTES, max_lines: int = GATEWAY_LOG_TAIL_LINES) -> None:
        if not self.gateway_manager or not hasattr(self.gateway_manager, "recent_logs"):
            return
        try:
            lines = list(self.gateway_manager.recent_logs(lines=max_lines, since_minutes=since_minutes) or [])
        except Exception as exc:
            logger.debug("Gateway log tail failed (%s): %s", reason, exc)
            return
        if not lines:
            logger.info("[GatewayLog][%s] no recent gateway lines", reason)
            return
        logger.info("[GatewayLog][%s] recent gateway lines=%d", reason, len(lines))
        for line in lines[-max_lines:]:
            logger.info("[GatewayLog][%s] %s", reason, line)

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
                    resp = session.post(f"{self.gateway_url}/v1/api/iserver/reauthenticate", timeout=15)
                    payload = {}
                    try:
                        payload = resp.json() if resp.status_code == 200 else {}
                    except Exception:
                        payload = {}
                    message = self._flatten_text(payload.get("message") or "", 80).lower()
                    if resp.status_code == 200 and message == "triggered":
                        logger.info(
                            "Gateway reauthenticate triggered; waiting %ss before auth check",
                            BACKEND_REAUTH_GRACE_SECONDS,
                        )
                        time.sleep(BACKEND_REAUTH_GRACE_SECONDS)
            except Exception as exc:
                logger.debug("Gateway %s bridge failed: %s", step, exc)

        result = self._check_backend_auth(session)
        if result.get("authenticated"):
            logger.info("Gateway backend auth promoted after browser confirmation bridge")
        return result

    def _get_challenge_response_state(self, current_challenge: str) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {
            "response_code": "",
            "pending_response_code": "",
            "response_status": "",
            "challenge_feedback": "",
            "response_submitted_at": "",
            "response_rejected_at": "",
            "challenge_mismatch": False,
        }
        if not self.pb_client:
            return snapshot
        try:
            payload = self.pb_client.get_ibkr_2fa_status(environment=ENVIRONMENT)
            state = payload.get("state") or {}
            snapshot["response_status"] = str(state.get("response_status") or "").strip().lower()
            snapshot["response_code"] = self._normalize_code(state.get("response_code"))
            snapshot["challenge_feedback"] = str(state.get("challenge_feedback") or "").strip()
            snapshot["response_submitted_at"] = str(state.get("response_submitted_at") or "").strip()
            snapshot["response_rejected_at"] = str(state.get("response_rejected_at") or "").strip()

            stored_challenge = self._normalize_code(state.get("challenge_code"))
            expected_challenge = self._normalize_code(current_challenge)
            if stored_challenge and expected_challenge and stored_challenge != expected_challenge:
                snapshot["challenge_mismatch"] = True
                return snapshot

            if snapshot["response_code"] and snapshot["response_status"] in ("pending", "received"):
                snapshot["pending_response_code"] = snapshot["response_code"]

            return snapshot
        except Exception as exc:
            logger.debug("PB 2FA response poll failed: %s", exc)
            return snapshot

    def _extract_challenge_code(self, page_text: str = "") -> str:
        if self._driver:
            try:
                from selenium.webdriver.common.by import By

                challenge = self._driver.find_element(By.CSS_SELECTOR, ".xyz-goldchallenge")
                text = str(challenge.text or "").strip()
                normalized = self._normalize_code(text)
                if normalized:
                    return normalized
                if text:
                    return text
            except Exception:
                pass

        body_text = str(page_text or "")
        match = re.search(r"challenge(?:\s+code)?[^A-Za-z0-9]*([A-Za-z0-9]{6,16})", body_text, re.IGNORECASE)
        if not match:
            return ""
        return self._normalize_code(match.group(1))

    def _extract_challenge_feedback(self, page_text: str = "") -> str:
        body_text = " ".join(str(page_text or "").split())
        if not body_text:
            return ""
        for pattern, label in CHALLENGE_REJECTION_TEXT_PATTERNS:
            if pattern.search(body_text):
                return label
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

        # Manual-only 2FA policy: one explicit trigger equals one login round.
        # Any timeout or failure must be retried by a new manual action.
        total_2fa_rounds = 1
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

                    submit_url, submit_url_ms = self._read_current_url()
                    logger.info(
                        "Credentials submitted, entering 2FA wait url=%s current_url_ms=%s",
                        submit_url or "-",
                        submit_url_ms,
                    )
                    page_state = {
                        "mode": "unknown",
                        "url": submit_url,
                        "body_excerpt": "",
                    }
                    self._log_to_pb(
                        "2fa_waiting",
                        "info",
                        "Waiting for 2FA confirmation",
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
                                    "response_rejected_at": "",
                                    "challenge_feedback": "",
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
                    "response_rejected_at": "",
                    "challenge_feedback": "",
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
                        "response_rejected_at": "",
                        "challenge_feedback": "",
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
            summary = self._summarize_cookie_bridge(browser_cookies, session)
            fingerprint = json.dumps(summary, sort_keys=True, ensure_ascii=True)
            if fingerprint != self._last_cookie_summary_fingerprint:
                self._last_cookie_summary_fingerprint = fingerprint
                summary["captured_at"] = time.time()
                self._cookie_bridge_history.append(summary)
                self._cookie_bridge_history = self._cookie_bridge_history[-8:]
                logger.info(
                    "COOKIE_BRIDGE browser_count=%s session_count=%s gateway_host=%s gateway_domain_match=%s browser_domains=%s session_domains=%s browser_names=%s session_names=%s browser_x_sess_uuid=%s session_x_sess_uuid=%s x_sess_uuid_match=%s",
                    summary["browser_count"],
                    summary["session_count"],
                    summary["gateway_host"],
                    summary["gateway_domain_match"],
                    ",".join(summary["browser_domains"]) or "-",
                    ",".join(summary["session_domains"]) or "-",
                    ",".join(summary["browser_names"]) or "-",
                    ",".join(summary["session_names"]) or "-",
                    summary["browser_x_sess_uuid_fp"] or "-",
                    summary["session_x_sess_uuid_fp"] or "-",
                    summary["x_sess_uuid_match"],
                )
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
        last_cookie_sync_at = time.time()
        last_body_text_sample_at = time.time()
        last_network_trace_poll_at = time.time()
        submitted_response = ""
        active_challenge_code = ""
        last_observed_mode = ""
        last_observed_url = ""
        last_observed_excerpt = ""
        last_challenge_feedback = ""
        last_response_state_key = ""
        last_network_trace_summary = ""
        last_network_trace_history = ""
        push_body_sample_count = 0
        last_body_sample_reason = ""
        mode_timeline_entries: list[str] = []
        mode_changed_at = ""

        COOKIE_SYNC_INTERVAL = 10

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
        # During the push-notification window, minimize DOM reads and rely on
        # current_url + backend auth as the primary signals. body.text is sampled
        # at a lower cadence so we can still detect Challenge/Response transitions
        # without continuously competing with the SSO page's own polling loop.
        # ================================================================

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
                page_url, page_url_ms = self._read_current_url()
                if page_url_ms >= CURRENT_URL_SLOW_READ_MS:
                    logger.warning("[2FA %ds] current_url read slow_ms=%s", elapsed, page_url_ms)

                passive_trace_snapshot = self._build_passive_trace_snapshot()
                if PASSIVE_NETWORK_TRACE_ENABLED and (now - last_network_trace_poll_at) >= PASSIVE_NETWORK_TRACE_POLL_SECONDS:
                    passive_trace_snapshot = self._drain_passive_network_trace()
                    last_network_trace_poll_at = now
                    trace_summary = str(passive_trace_snapshot.get("last_summary") or "").strip()
                    trace_history = self._format_passive_trace_history(passive_trace_snapshot.get("entries") or [], start)
                    trace_changed = bool(
                        (trace_summary and trace_summary != last_network_trace_summary)
                        or (trace_history and trace_history != last_network_trace_history)
                    )
                    if trace_changed and trace_summary:
                        logger.info(
                            "[2FA %ds] passive_network count=%s latest=%s history=%s",
                            elapsed,
                            passive_trace_snapshot.get("entry_count") or 0,
                            trace_summary,
                            trace_history or "-",
                        )
                    if trace_summary:
                        last_network_trace_summary = trace_summary
                    if trace_history:
                        last_network_trace_history = trace_history

                body_sample_reason = self._body_sample_reason(
                    elapsed=elapsed,
                    base_deadline=base_deadline,
                    now=now,
                    seconds_since_last_sample=(now - last_body_text_sample_at),
                    last_observed_mode=last_observed_mode,
                )
                should_sample_body_text = bool(body_sample_reason)

                body_text_ms = 0.0
                if should_sample_body_text:
                    page_text, body_text_ms = self._read_body_text()
                    last_body_text_sample_at = now
                    last_body_sample_reason = body_sample_reason
                    if body_sample_reason.startswith("push_"):
                        push_body_sample_count += 1
                    logger.info(
                        "[2FA %ds] body_text sample_ms=%s reason=%s push_samples=%s mode_hint=%s url=%s",
                        elapsed,
                        body_text_ms,
                        body_sample_reason,
                        push_body_sample_count,
                        last_observed_mode or "unknown",
                        page_url or "-",
                    )
                    if body_text_ms >= BODY_TEXT_SLOW_READ_MS:
                        logger.warning(
                            "[2FA %ds] body_text read slow_ms=%s reason=%s push_samples=%s mode_hint=%s url=%s",
                            elapsed,
                            body_text_ms,
                            body_sample_reason,
                            push_body_sample_count,
                            last_observed_mode or "unknown",
                            page_url or "-",
                        )

                observed_mode = self._infer_wait_mode(page_url, page_text, last_observed_mode)
                page_excerpt = self._flatten_text(page_text, 180)
                if observed_mode != last_observed_mode:
                    mode_changed_at = self._now_et()
                    transition = f"{elapsed}s:{observed_mode or 'unknown'}"
                    if not mode_timeline_entries or mode_timeline_entries[-1] != transition:
                        mode_timeline_entries.append(transition)
                mode_timeline = self._format_mode_timeline(mode_timeline_entries)
                if observed_mode != last_observed_mode or page_url != last_observed_url:
                    logger.info(
                        "[2FA %ds] page mode=%s url=%s text=%s current_url_ms=%s body_text_ms=%s sampled_body=%s sample_reason=%s timeline=%s",
                        elapsed,
                        observed_mode,
                        page_url or "-",
                        page_excerpt or "-",
                        page_url_ms,
                        body_text_ms,
                        should_sample_body_text,
                        body_sample_reason or "-",
                        mode_timeline or "-",
                    )
                    last_observed_mode = observed_mode
                    last_observed_url = page_url
                    last_observed_excerpt = page_excerpt

                # Check for success (URL redirect or page text).
                if "/sso/Dispatcher" in page_url or "Client login succeeds" in page_text:
                    logger.info("[2FA %ds] SUCCESS PAGE DETECTED: %s", elapsed, page_url)
                    self._sync_browser_cookies(session)
                    passive_trace_snapshot = self._drain_passive_network_trace(force_body=True)
                    trace_summary = str(passive_trace_snapshot.get("last_summary") or "").strip()
                    trace_history = self._format_passive_trace_history(passive_trace_snapshot.get("entries") or [], start)
                    if trace_summary:
                        last_network_trace_summary = trace_summary
                    if trace_history:
                        last_network_trace_history = trace_history
                    if trace_summary:
                        logger.info(
                            "[2FA %ds] passive_network count=%s latest=%s history=%s",
                            elapsed,
                            passive_trace_snapshot.get("entry_count") or 0,
                            trace_summary,
                            trace_history or "-",
                        )
                    for promote_round in range(BACKEND_PROMOTE_ATTEMPTS):
                        time.sleep(BACKEND_PROMOTE_INTERVAL_SECONDS)
                        self._sync_browser_cookies(session)
                        backend_result = self._promote_backend_auth(session)
                        logger.info(
                            "[2FA %ds] Promote round %d/%d %s",
                            elapsed,
                            promote_round + 1,
                            BACKEND_PROMOTE_ATTEMPTS,
                            self._format_backend_auth_summary(backend_result),
                        )
                        if backend_result.get("authenticated"):
                            logger.info("[2FA %ds] Backend auth confirmed (round %d)", elapsed, promote_round + 1)
                            self._last_wait_context = {
                                "mode": "success",
                                "url": page_url,
                                "body_excerpt": page_excerpt,
                                "backend_authenticated": True,
                                "mode_timeline": mode_timeline,
                                "mode_changed_at": mode_changed_at,
                                "body_sample_reason": last_body_sample_reason,
                                "push_body_sample_count": push_body_sample_count,
                                "passive_network_summary": trace_summary,
                                "passive_network_history": trace_history,
                                "cookie_bridge_timeline": self._format_cookie_bridge_timeline(self._cookie_bridge_history, start),
                            }
                            return True
                    logger.warning("[2FA %ds] Success page but backend auth not confirmed", elapsed)
                    self._log_gateway_recent_lines("success_page_backend_not_confirmed")

                # Detect challenge/response mode.
                page_text_lower = page_text.lower()
                if "enter the challenge code below" in page_text_lower or "response code" in page_text_lower:
                    challenge_code = self._extract_challenge_code(page_text)
                    challenge_changed = bool(challenge_code and challenge_code != active_challenge_code)
                    if challenge_code and challenge_code != active_challenge_code:
                        active_challenge_code = challenge_code
                        submitted_response = ""

                    # Start the challenge-response timeout window only when the
                    # current challenge first appears or actually changes.
                    # Re-extending it on every polling loop would prevent timeout
                    # handling and auto-retry from ever firing.
                    if challenge_deadline <= 0 or challenge_changed:
                        challenge_deadline = now + CHALLENGE_RESPONSE_WAIT
                    challenge_feedback = self._extract_challenge_feedback(page_text)
                    passive_trace_snapshot = self._drain_passive_network_trace(force_body=True)
                    trace_summary = str(passive_trace_snapshot.get("last_summary") or "").strip()
                    trace_history = self._format_passive_trace_history(passive_trace_snapshot.get("entries") or [], start)
                    if trace_summary:
                        last_network_trace_summary = trace_summary
                    if trace_history:
                        last_network_trace_history = trace_history
                    if trace_summary:
                        logger.info(
                            "[2FA %ds] passive_network count=%s latest=%s history=%s",
                            elapsed,
                            passive_trace_snapshot.get("entry_count") or 0,
                            trace_summary,
                            trace_history or "-",
                        )
                    if challenge_changed:
                        logger.warning(
                            "[2FA %ds] challenge detected challenge=%s url=%s text=%s deadline_in=%ss",
                            elapsed,
                            active_challenge_code or "-",
                            page_url or "-",
                            page_excerpt or "-",
                            int(max(0, challenge_deadline - now)),
                        )
                    if challenge_feedback and challenge_feedback != last_challenge_feedback:
                        logger.warning(
                            "[2FA %ds] challenge feedback=%s url=%s text=%s",
                            elapsed,
                            challenge_feedback,
                            page_url or "-",
                            page_excerpt or "-",
                        )
                        last_challenge_feedback = challenge_feedback
                    response_state = self._get_challenge_response_state(active_challenge_code)
                    current_response_status = str(response_state.get("response_status") or "").strip().lower()
                    pending_response_code = str(response_state.get("pending_response_code") or "").strip()
                    current_response_code = str(response_state.get("response_code") or "").strip()
                    if current_response_status == "submitted" and current_response_code:
                        submitted_response = current_response_code
                    response_state_key = ":".join([
                        current_response_status or "",
                        "pending" if pending_response_code else "",
                        "submitted" if current_response_code else "",
                        "mismatch" if response_state.get("challenge_mismatch") else "",
                    ])
                    if response_state_key and response_state_key != last_response_state_key:
                        last_response_state_key = response_state_key
                        logger.info(
                            "[2FA %ds] response_state status=%s has_pending=%s has_submitted=%s challenge_match=%s",
                            elapsed,
                            current_response_status or "-",
                            bool(pending_response_code),
                            bool(current_response_code),
                            not bool(response_state.get("challenge_mismatch")),
                        )

                    page_state = {
                        "mode": "challenge_response",
                        "url": page_url,
                        "body_excerpt": page_text[:240],
                        "challenge_code": active_challenge_code,
                        "challenge_feedback": challenge_feedback,
                        "mode_timeline": mode_timeline,
                        "mode_changed_at": mode_changed_at,
                        "body_sample_reason": last_body_sample_reason,
                        "push_body_sample_count": push_body_sample_count,
                        "passive_network_summary": trace_summary,
                        "passive_network_history": trace_history,
                        "cookie_bridge_timeline": self._format_cookie_bridge_timeline(self._cookie_bridge_history, start),
                    }
                    self._last_wait_context = page_state
                    state_patch_base = {
                        **(cycle_state_patch or {}),
                    }
                    if challenge_changed:
                        state_patch_base.update({
                            "response_code": "",
                            "response_status": "",
                            "response_received_at": "",
                            "response_submitted_at": "",
                            "response_rejected_at": "",
                            "challenge_feedback": "",
                            "last_error": "",
                        })

                    if pending_response_code and pending_response_code != submitted_response:
                        if self._submit_challenge_response(pending_response_code):
                            submitted_response = pending_response_code
                            response_deadline = max(response_deadline, time.time() + POST_RESPONSE_GRACE_SECONDS)
                            logger.info("[2FA %ds] Submitted challenge response for challenge=%s", elapsed, active_challenge_code or "-")
                            report_key = f"challenge_response_submitted:{active_challenge_code}:{submitted_response}"
                            if report_key != last_report_key:
                                last_report_key = report_key
                                self._report_2fa_status(
                                    status="waiting_response",
                                    reason=reason,
                                    source=source,
                                    attempt=attempt,
                                    detail=self._build_wait_detail(page_state, detail),
                                    message="已收到并提交 Response Code，等待 Gateway 会话恢复认证。",
                                    last_result="Response Code 已提交，等待 Gateway 认证。",
                                    state_patch=self._build_state_patch(
                                        page_state,
                                        {
                                            **state_patch_base,
                                            "response_status": "submitted",
                                            "response_submitted_at": self._now_et(),
                                            "response_rejected_at": "",
                                            "challenge_feedback": "",
                                            "last_error": "",
                                        },
                                    ),
                                )
                        else:
                            report_key = f"challenge_response_submit_failed:{active_challenge_code}:{pending_response_code}"
                            if report_key != last_report_key:
                                last_report_key = report_key
                                self._report_2fa_status(
                                    status="waiting_response",
                                    reason=reason,
                                    source=source,
                                    attempt=attempt,
                                    detail=self._build_wait_detail(page_state, detail),
                                    message="已收到 Response Code，但浏览器提交失败；请重新检查当前 Challenge 与 Response 是否匹配。",
                                    last_result="Response Code 提交失败，等待重试。",
                                    error="challenge_response_submit_failed",
                                    state_patch=self._build_state_patch(
                                        page_state,
                                        {
                                            **state_patch_base,
                                            "response_status": "submit_failed",
                                            "response_rejected_at": "",
                                            "challenge_feedback": "",
                                        },
                                    ),
                                )
                    elif challenge_feedback:
                        report_key = f"challenge_response_rejected:{active_challenge_code}:{challenge_feedback}"
                        if report_key != last_report_key:
                            last_report_key = report_key
                            self._report_2fa_status(
                                status="waiting_response",
                                reason=reason,
                                source=source,
                                attempt=attempt,
                                detail=self._build_wait_detail(page_state, detail),
                                message=f"Gateway 返回“{challenge_feedback}”；请核对当前 Challenge 后重新生成并提交 Response Code。",
                                last_result=f"Gateway 返回：{challenge_feedback}",
                                error="challenge_response_rejected",
                                state_patch=self._build_state_patch(
                                    page_state,
                                    {
                                        **state_patch_base,
                                        "response_status": "gateway_rejected",
                                        "response_rejected_at": self._now_et(),
                                        "challenge_feedback": challenge_feedback,
                                    },
                                ),
                            )
                    elif current_response_status == "submit_failed":
                        report_key = f"challenge_response_submit_failed_waiting:{active_challenge_code}"
                        if report_key != last_report_key:
                            last_report_key = report_key
                            self._report_2fa_status(
                                status="waiting_response",
                                reason=reason,
                                source=source,
                                attempt=attempt,
                                detail=self._build_wait_detail(page_state, detail),
                                message="浏览器提交 Response Code 失败；请重新检查当前 Challenge 与 Response 是否匹配后重试。",
                                last_result="Response Code 提交失败，等待你重新提交。",
                                error="challenge_response_submit_failed",
                                state_patch=self._build_state_patch(
                                    page_state,
                                    {
                                        **state_patch_base,
                                        "response_status": "submit_failed",
                                    },
                                ),
                            )
                    elif current_response_status == "gateway_rejected":
                        feedback_text = challenge_feedback or str(response_state.get("challenge_feedback") or "").strip() or "Authentication failed"
                        report_key = f"challenge_response_rejected_waiting:{active_challenge_code}:{feedback_text}"
                        if report_key != last_report_key:
                            last_report_key = report_key
                            self._report_2fa_status(
                                status="waiting_response",
                                reason=reason,
                                source=source,
                                attempt=attempt,
                                detail=self._build_wait_detail(page_state, detail),
                                message=f"Gateway 已拒绝当前 Response Code（{feedback_text}）；请核对当前 Challenge 后重新生成并提交。",
                                last_result=f"Gateway 返回：{feedback_text}",
                                error="challenge_response_rejected",
                                state_patch=self._build_state_patch(
                                    page_state,
                                    {
                                        **state_patch_base,
                                        "response_status": "gateway_rejected",
                                        "challenge_feedback": feedback_text,
                                        "response_rejected_at": str(response_state.get("response_rejected_at") or "").strip() or self._now_et(),
                                    },
                                ),
                            )
                    elif current_response_status == "submitted" or submitted_response:
                        report_key = f"challenge_response_submitted_waiting:{active_challenge_code}:{submitted_response or current_response_code}"
                        if report_key != last_report_key:
                            last_report_key = report_key
                            self._report_2fa_status(
                                status="waiting_response",
                                reason=reason,
                                source=source,
                                attempt=attempt,
                                detail=self._build_wait_detail(page_state, detail),
                                message="Response Code 已提交，等待 Gateway 会话恢复认证。",
                                last_result="Response Code 已提交，等待 Gateway 认证。",
                                state_patch=self._build_state_patch(
                                    page_state,
                                    {
                                        **state_patch_base,
                                        "response_status": "submitted",
                                        "challenge_feedback": "",
                                        "response_rejected_at": "",
                                        "last_error": "",
                                    },
                                ),
                            )
                    else:
                        report_key = f"challenge_response_waiting:{active_challenge_code}"
                        if report_key != last_report_key:
                            last_report_key = report_key
                            self._report_2fa_status(
                                status="waiting_response",
                                reason=reason,
                                source=source,
                                attempt=attempt,
                                detail=self._build_wait_detail(page_state, detail),
                                message="IBKR 已切到 Challenge/Response，请在 Runtime 页面提交 Response Code。",
                                last_result="等待 Challenge Response Code。",
                                state_patch=self._build_state_patch(page_state, state_patch_base),
                            )

                # Backend auth check via Python requests.
                backend_result = self._check_backend_auth(session)
                if backend_result.get("authenticated"):
                    logger.info("[2FA %ds] Backend auth confirmed via requests session", elapsed)
                    self._last_wait_context = {
                        "mode": observed_mode or "success",
                        "url": page_url,
                        "body_excerpt": page_excerpt,
                        "backend_authenticated": True,
                        "mode_timeline": mode_timeline,
                        "mode_changed_at": mode_changed_at,
                        "body_sample_reason": last_body_sample_reason,
                        "push_body_sample_count": push_body_sample_count,
                        "passive_network_summary": last_network_trace_summary,
                        "passive_network_history": last_network_trace_history,
                        "cookie_bridge_timeline": self._format_cookie_bridge_timeline(self._cookie_bridge_history, start),
                    }
                    return True

                if elapsed % WAIT_STATUS_LOG_SECONDS == 0:
                    logger.info(
                        "[2FA %ds] mode=%s remaining=%ss url=%s text=%s current_url_ms=%s body_text_ms=%s sampled_body=%s sample_reason=%s push_samples=%s body_age_s=%s timeline=%s passive_network=%s backend=%s",
                        elapsed,
                        observed_mode,
                        int(max(0, effective_deadline - now)),
                        page_url or "-",
                        page_excerpt or "-",
                        page_url_ms,
                        body_text_ms,
                        should_sample_body_text,
                        body_sample_reason or "-",
                        push_body_sample_count,
                        round(max(0.0, now - last_body_text_sample_at), 1),
                        mode_timeline or "-",
                        last_network_trace_summary or "-",
                        self._format_backend_auth_summary(backend_result),
                    )
                    cookie_bridge_timeline = self._format_cookie_bridge_timeline(self._cookie_bridge_history, start)
                    if cookie_bridge_timeline:
                        logger.info("[2FA %ds] cookie_bridge timeline=%s", elapsed, cookie_bridge_timeline)
                    if backend_result.get("payload", {}).get("competing", False):
                        logger.warning("[2FA] Competing session detected!")

            except Exception as e:
                if elapsed % 30 == 0:
                    logger.debug("[2FA %ds] Check error: %s", elapsed, e)

            time.sleep(POLL_INTERVAL)

        final_elapsed = int(time.time() - start)
        logger.warning(
            "[2FA %ds] wait ended without auth final_mode=%s final_url=%s final_text=%s",
            final_elapsed,
            last_observed_mode or "unknown",
            last_observed_url or "-",
            last_observed_excerpt or "-",
        )
        passive_trace_snapshot = self._drain_passive_network_trace(force_body=True)
        trace_summary = str(passive_trace_snapshot.get("last_summary") or "").strip()
        trace_history = self._format_passive_trace_history(passive_trace_snapshot.get("entries") or [], start)
        if trace_summary:
            logger.info(
                "[2FA %ds] passive_network count=%s latest=%s history=%s",
                final_elapsed,
                passive_trace_snapshot.get("entry_count") or 0,
                trace_summary,
                trace_history or "-",
            )
            last_network_trace_summary = trace_summary
        if trace_history:
            last_network_trace_history = trace_history
        self._last_wait_context = {
            "mode": last_observed_mode or "unknown",
            "url": last_observed_url,
            "body_excerpt": last_observed_excerpt,
            "challenge_code": active_challenge_code,
            "challenge_feedback": last_challenge_feedback,
            "backend_authenticated": False,
            "mode_timeline": self._format_mode_timeline(mode_timeline_entries),
            "mode_changed_at": mode_changed_at,
            "body_sample_reason": last_body_sample_reason,
            "push_body_sample_count": push_body_sample_count,
            "passive_network_summary": last_network_trace_summary,
            "passive_network_history": last_network_trace_history,
            "cookie_bridge_timeline": self._format_cookie_bridge_timeline(self._cookie_bridge_history, start),
        }
        self._log_gateway_recent_lines("wait_ended_without_auth")
        return False

    def status(self) -> dict:
        from datetime import datetime
        return {
            "has_credentials": bool(self._username and self._password),
            "last_login": datetime.fromtimestamp(self._last_login_time).isoformat()
            if self._last_login_time else None,
        }
