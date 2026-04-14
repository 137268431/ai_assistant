#!/usr/bin/env python3
"""
IBKR 每周自动重新认证 — 由 cron 每周日凌晨执行
用于替代旧服务命名的每周服务重启机制

1. 重启 IB Gateway (清理旧 session)
2. 发送飞书 2FA 卡片，等待人工点击触发
3. 停在等待态，不自动继续重启 compute；只有人工触发验证成功后才继续恢复

crontab:
  0 5 * * 0 source /opt/ibkr_compute/.env && PYTHONPATH=/opt/ibkr_compute/src /opt/ibkr_compute/venv/bin/python3 /opt/ibkr_compute/ops/auth/ibkr_weekly_reauth.py >> /var/log/ibkr_weekly_reauth.log 2>&1
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


def pb_begin_startup_progress(reason, summary, detail=None):
    payload = {
        "environment": ENVIRONMENT,
        "action": "begin",
        "status": "active",
        "title": "IBKR Runtime 启动中",
        "summary": summary,
        "current_step": "manual_trigger",
        "current_blocker": "等待手动触发 2FA",
        "operator_action": "去飞书点击“开始 2FA 验证”",
        "reason": reason,
        "source": "ibkr_weekly_reauth",
        "create_if_missing": True,
        "fields": detail or {},
        "steps": {
            "service_boot": {
                "status": "done",
                "detail": "Gateway 已完成每周重启，等待重新认证。",
            },
            "card_ready": {
                "status": "running",
                "detail": "正在准备本周重登使用的 2FA 卡片。",
            },
        },
    }
    try:
        requests.post(
            PB_URL + "/api/custom/ibkr/startup/progress",
            json=payload,
            timeout=8,
        )
        return True
    except Exception as exc:
        log("startup progress notify failed: %s" % exc)
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

    # Step 2: Mark weekly reauth as waiting for manual auth
    log("Step 2: Publishing weekly reauth waiting state...")
    pb_begin_startup_progress(
        "weekly_reauth",
        "本周重登窗口已开始，等待你在飞书手动开始验证。",
        {"来源": "ibkr_weekly_reauth", "原因": "weekly_reauth"},
    )

    # Step 3: Request manual 2FA
    log("Step 3: Sending manual 2FA request card...")
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

    log("Step 4: Waiting for manual Feishu trigger and phone confirmation...")
    pb_log("weekly_reauth_pending", "warning", "Gateway restarted; waiting manual 2FA confirmation")
    log("  Weekly reauth is now gated on manual action; compute resume must wait for auth success")

    log("Weekly re-authentication complete")
    log("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
