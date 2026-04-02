#!/usr/bin/env python3
"""
IBKR 每周自动重新认证 — 由 cron 每周日凌晨执行
替代 QuantConnect 的每周服务重启机制

1. 重启 IB Gateway (清理旧 session)
2. 执行自动登录 + 2FA
3. 验证认证状态
4. 通知飞书结果

crontab:
  0 5 * * 0 source /opt/qc_compute/.env && PYTHONPATH=/opt/qc_compute/src /opt/qc_compute/venv/bin/python3 /opt/qc_compute/deploy/ibkr_weekly_reauth.py >> /var/log/ibkr_weekly_reauth.log 2>&1
"""

import os
import sys
import time
import json
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, "/opt/qc_compute/src")

GW_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
PB_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg))


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


def main():
    log("=" * 50)
    log("Weekly IBKR Re-authentication starting")
    pb_log("weekly_reauth_start", "info", "Starting weekly re-authentication")

    # Step 1: Restart Gateway
    log("Step 1: Restarting IB Gateway...")
    os.system("systemctl restart ibkr-gateway")
    time.sleep(10)

    # Verify gateway is running
    r = os.popen("systemctl is-active ibkr-gateway").read().strip()
    if r != "active":
        log("FATAL: Gateway failed to start after restart")
        pb_log("weekly_reauth_failed", "error", "Gateway not active after restart")
        return 1

    log("  Gateway restarted successfully")

    # Step 2: Auto-login
    log("Step 2: Executing auto-login (2FA required)...")
    log("  CHECK YOUR IBKR MOBILE APP FOR 2FA PUSH")

    exit_code = os.system(
        "PYTHONPATH=/opt/qc_compute/src /opt/qc_compute/venv/bin/python3 "
        "/opt/qc_compute/deploy/ibkr_login.py"
    )

    if exit_code != 0:
        log("FATAL: Auto-login failed (exit code %d)" % exit_code)
        pb_log("weekly_reauth_failed", "error", "Login failed, exit code %d" % exit_code)
        return 1

    log("  Login successful")

    # Step 3: Verify
    log("Step 3: Verifying authentication...")
    time.sleep(3)

    s = requests.Session()
    s.verify = False
    try:
        r = s.post(GW_URL + "/v1/api/iserver/auth/status", data="", timeout=10)
        if r.status_code == 200 and r.text.strip():
            data = r.json()
            if data.get("authenticated"):
                log("  Verified: authenticated=True")
                pb_log("weekly_reauth_success", "ok", "Weekly re-auth completed")
            else:
                log("  WARNING: auth status says not authenticated")
                pb_log("weekly_reauth_warning", "warning", "Auth status false after login")
        else:
            log("  WARNING: empty auth response")
    except Exception as e:
        log("  Verify error: %s" % e)

    # Step 4: Restart qc_compute to pick up fresh session
    log("Step 4: Restarting qc_compute...")
    os.system("systemctl restart qc_compute")
    time.sleep(3)

    r = os.popen("systemctl is-active qc_compute").read().strip()
    log("  qc_compute: %s" % r)

    log("Weekly re-authentication complete")
    log("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
