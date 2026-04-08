#!/usr/bin/env python3
"""
IB Gateway 保活脚本 — 由 cron 每分钟执行
1. tickle 保持 session
2. 检测认证状态，失效时请求飞书 2FA 卡片
3. 连续失败3次则重启 gateway + 触发飞书告警
4. 写日志到 PB ibkr_session

crontab:
  * * * * * source /opt/ibkr_compute/.env && PYTHONPATH=/opt/ibkr_compute/src /opt/ibkr_compute/venv/bin/python3 /opt/ibkr_compute/deploy/ibkr_keepalive.py >> /var/log/ibkr_keepalive.log 2>&1
"""

import os
import sys
import json
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, "/opt/ibkr_compute/src")

GW_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
PB_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")
STATE_FILE = "/tmp/ibkr_keepalive_state.json"
MAX_FAILURES = 3
REQUEST_COOLDOWN = int(os.environ.get("IBKR_2FA_REMINDER_COOLDOWN", "900"))
ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")
ACTIVE_2FA_STATUSES = {"requested", "triggered", "waiting_confirm", "waiting_response"}


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg))


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"consecutive_failures": 0, "last_restart": 0, "last_2fa_request": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def pb_log(event, status, detail=""):
    try:
        requests.post(
            PB_URL + "/api/collections/ibkr_session/records",
            json={
                "event": event,
                "status": status,
                "detail": str(detail)[:500],
                "us_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            timeout=5,
        )
    except Exception:
        pass


def pb_request_2fa(reason, message, detail=None, force_reset=False):
    payload = {
        "reason": reason,
        "message": message,
        "source": "ibkr_keepalive",
        "environment": ENVIRONMENT,
        "force_reset": force_reset,
        "detail": detail or {},
    }
    try:
        requests.post(
            PB_URL + "/api/custom/ibkr/2fa/request",
            json=payload,
            timeout=8,
        )
    except Exception as exc:
        log("2FA request notify failed: %s" % exc)


def pb_get_2fa_state():
    try:
        resp = requests.get(
            PB_URL + "/api/custom/ibkr/2fa/status",
            params={"environment": ENVIRONMENT},
            timeout=8,
        )
        if resp.status_code != 200:
            return {}
        payload = resp.json()
        state = payload.get("state") or {}
        return state if isinstance(state, dict) else {}
    except Exception as exc:
        log("2FA status fetch failed: %s" % exc)
        return {}


def is_active_2fa_state(state):
    status = str((state or {}).get("status", "")).strip().lower()
    return status in ACTIVE_2FA_STATUSES


def handle_auth_attention(state, reason, message, detail=None, force_reset=False):
    active_2fa = pb_get_2fa_state()
    active_status = str(active_2fa.get("status", "")).strip().lower()
    state["consecutive_failures"] = 0
    pb_log("reauth_trigger", "warning", message)
    if is_active_2fa_state(active_2fa):
        log("AUTH ATTENTION - active 2FA flow detected (%s), skipping gateway restart path" % (active_status or "unknown"))
        save_state(state)
        return

    if time.time() - state.get("last_2fa_request", 0) >= REQUEST_COOLDOWN:
        pb_request_2fa(
            reason,
            message,
            detail or {},
            force_reset=force_reset,
        )
        state["last_2fa_request"] = time.time()
    save_state(state)


def gw_post(path):
    s = requests.Session()
    s.verify = False
    return s.post(GW_URL + "/v1/api" + path, data="", timeout=10)


def main():
    state = load_state()

    # 1. Tickle
    try:
        r = gw_post("/tickle")
        if r.status_code == 401:
            log("TICKLE UNAUTHENTICATED - waiting manual 2FA trigger")
            handle_auth_attention(
                state,
                "session_expired",
                "Keepalive 检测到会话失效，等待点击飞书按钮触发 2FA。",
                {"来源": "ibkr_keepalive", "原因": "session_expired", "阶段": "tickle_401"},
                force_reset=False,
            )
            return
        if r.status_code != 200 or not r.text.strip():
            raise Exception("tickle empty response (code=%d)" % r.status_code)
        tickle_data = r.json()
        session = tickle_data.get("session", "")
        if not session:
            log("TICKLE SESSION MISSING - waiting manual 2FA trigger")
            handle_auth_attention(
                state,
                "session_expired",
                "Keepalive 检测到 Gateway 会话缺失，等待点击飞书按钮触发 2FA。",
                {"来源": "ibkr_keepalive", "原因": "session_expired", "阶段": "tickle_no_session"},
                force_reset=False,
            )
            return
    except Exception as e:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        save_state(state)
        log("TICKLE FAILED (%d/%d): %s" % (state["consecutive_failures"], MAX_FAILURES, e))

        if state["consecutive_failures"] >= MAX_FAILURES:
            log("MAX FAILURES REACHED - restarting gateway")
            pb_log("gateway_restart", "error", "Consecutive failures: %d" % state["consecutive_failures"])
            os.system("systemctl restart ibkr-gateway")
            pb_request_2fa(
                "gateway_down",
                "Gateway 已由 keepalive 重启，等待点击飞书按钮触发 2FA。",
                {"来源": "ibkr_keepalive", "原因": "gateway_down"},
                force_reset=True,
            )
            state["consecutive_failures"] = 0
            state["last_restart"] = time.time()
            state["last_2fa_request"] = time.time()
            save_state(state)
        return

    # 2. Check auth
    try:
        r2 = gw_post("/iserver/auth/status")
        if r2.status_code == 401:
            log("AUTH STATUS 401 - waiting manual 2FA trigger")
            handle_auth_attention(
                state,
                "session_expired",
                "Keepalive 检测到会话失效，等待点击飞书按钮触发 2FA。",
                {"来源": "ibkr_keepalive", "原因": "session_expired", "阶段": "auth_401"},
                force_reset=False,
            )
            return
        if r2.status_code == 200 and r2.text.strip():
            auth_data = r2.json()
            authenticated = auth_data.get("authenticated", False)

            if authenticated:
                state["consecutive_failures"] = 0
                state["last_2fa_request"] = 0
                save_state(state)
                return  # all good, silent exit

            log("NOT AUTHENTICATED - waiting manual 2FA trigger")
            handle_auth_attention(
                state,
                "session_expired",
                "Keepalive 检测到会话失效，等待点击飞书按钮触发 2FA。",
                {"来源": "ibkr_keepalive", "原因": "session_expired", "阶段": "auth_not_authenticated"},
                force_reset=False,
            )
            return

        else:
            # Auth status empty = gateway running but no session
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            save_state(state)
            log("AUTH STATUS EMPTY (failures: %d)" % state["consecutive_failures"])

    except Exception as e:
        log("AUTH CHECK ERROR: %s" % e)
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        save_state(state)


if __name__ == "__main__":
    main()
