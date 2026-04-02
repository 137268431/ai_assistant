#!/usr/bin/env python3
"""
IB Gateway 保活脚本 — 由 cron 每分钟执行
1. tickle 保持 session
2. 检测认证状态，失败则尝试 reauthenticate
3. 连续失败3次则重启 gateway + 触发飞书告警
4. 写日志到 PB ibkr_session

crontab:
  * * * * * source /opt/qc_compute/.env && PYTHONPATH=/opt/qc_compute/src /opt/qc_compute/venv/bin/python3 /opt/qc_compute/deploy/ibkr_keepalive.py >> /var/log/ibkr_keepalive.log 2>&1
"""

import os
import sys
import json
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, "/opt/qc_compute/src")

GW_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
PB_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")
STATE_FILE = "/tmp/ibkr_keepalive_state.json"
MAX_FAILURES = 3


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg))


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"consecutive_failures": 0, "last_restart": 0}


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


def gw_post(path):
    s = requests.Session()
    s.verify = False
    return s.post(GW_URL + "/v1/api" + path, data="", timeout=10)


def main():
    state = load_state()

    # 1. Tickle
    try:
        r = gw_post("/tickle")
        if r.status_code != 200 or not r.text.strip():
            raise Exception("tickle empty response (code=%d)" % r.status_code)
        tickle_data = r.json()
        session = tickle_data.get("session", "")
        if not session:
            raise Exception("no session in tickle response")
    except Exception as e:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        save_state(state)
        log("TICKLE FAILED (%d/%d): %s" % (state["consecutive_failures"], MAX_FAILURES, e))

        if state["consecutive_failures"] >= MAX_FAILURES:
            log("MAX FAILURES REACHED - restarting gateway")
            pb_log("gateway_restart", "error", "Consecutive failures: %d" % state["consecutive_failures"])
            os.system("systemctl restart ibkr-gateway")
            state["consecutive_failures"] = 0
            state["last_restart"] = time.time()
            save_state(state)
        return

    # 2. Check auth
    try:
        r2 = gw_post("/iserver/auth/status")
        if r2.status_code == 200 and r2.text.strip():
            auth_data = r2.json()
            authenticated = auth_data.get("authenticated", False)

            if authenticated:
                state["consecutive_failures"] = 0
                save_state(state)
                return  # all good, silent exit

            # Not authenticated - try reauthenticate
            log("NOT AUTHENTICATED - attempting reauthenticate")
            pb_log("reauth_trigger", "warning", "Session expired")

            r3 = gw_post("/iserver/reauthenticate")
            time.sleep(5)

            r4 = gw_post("/iserver/auth/status")
            if r4.status_code == 200 and r4.text.strip():
                if r4.json().get("authenticated"):
                    log("REAUTHENTICATE SUCCESS")
                    pb_log("reauth_success", "ok", "")
                    state["consecutive_failures"] = 0
                    save_state(state)
                    return

            log("REAUTHENTICATE FAILED - manual login may be needed")
            pb_log("reauth_failed", "error", "Need manual login")
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            save_state(state)

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
