#!/usr/bin/env python3
"""
IBKR 每周自动重新认证 — 由 cron 每周日凌晨执行
用于替代旧服务命名的每周服务重启机制

1. 重启 IB Gateway (清理旧 session)
2. 发送飞书 2FA 卡片，等待人工点击触发
3. 重启 ibkr-compute，确保后续手动触发走最新代码

crontab:
  0 5 * * 0 source /opt/ibkr_compute/.env && PYTHONPATH=/opt/ibkr_compute/src /opt/ibkr_compute/venv/bin/python3 /opt/ibkr_compute/deploy/ibkr_weekly_reauth.py >> /var/log/ibkr_weekly_reauth.log 2>&1
"""

import os
import sys
import time
import json
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, "/opt/ibkr_compute/src")

GW_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
PB_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")
ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")


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


def pb_request_2fa(reason, message, detail=None, force_reset=False):
    payload = {
        "reason": reason,
        "message": message,
        "source": "ibkr_weekly_reauth",
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
        return True
    except Exception as exc:
        log("2FA request notify failed: %s" % exc)
        return False


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

    # Step 2: Request manual 2FA
    log("Step 2: Sending manual 2FA request card...")
    notified = pb_request_2fa(
        "weekly_reauth",
        "每周重认证窗口已开始，等待你点击飞书按钮触发 2FA。",
        {"来源": "ibkr_weekly_reauth", "原因": "weekly_reauth"},
        force_reset=True,
    )
    if not notified:
        pb_log("weekly_reauth_failed", "error", "Failed to send manual 2FA request")
        return 1
    pb_log("weekly_reauth_pending", "warning", "Manual 2FA requested")
    log("  Manual 2FA request sent")

    # Step 3: Restart ibkr-compute to pick up fresh session
    log("Step 3: Restarting ibkr-compute...")
    os.system("systemctl restart ibkr-compute")
    time.sleep(3)

    r = os.popen("systemctl is-active ibkr-compute").read().strip()
    log("  ibkr-compute: %s" % r)
    if r == "active":
        pb_log("weekly_reauth_pending", "warning", "Gateway restarted; waiting manual 2FA confirmation")
    else:
        pb_log("weekly_reauth_failed", "error", "ibkr-compute not active after restart")

    log("Weekly re-authentication complete")
    log("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
