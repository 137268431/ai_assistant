"""
IBKR Gateway 一次性登录脚本
在远程服务器运行: PYTHONPATH=/opt/ibkr_compute/src python3 /opt/ibkr_compute/deploy/ibkr_login.py
"""

import os
import sys
import time
import json

sys.path.insert(0, "/opt/ibkr_compute/src")

GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
USERNAME = os.environ.get("IBKR_USERNAME", "")
PASSWORD = os.environ.get("IBKR_PASSWORD", "")
MAX_2FA_WAIT = 180
PB_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")
PB_PUBLIC_URL = os.environ.get("PB_PUBLIC_URL", "").rstrip("/")
ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")
ACCOUNT_ID = os.environ.get("IBKR_ACCOUNT_ID", "")


def notify_2fa_pending():
    try:
        from ibkr_compute.integrations.pb_client import PBClient

        detail = {
            "账号": ACCOUNT_ID or "-",
            "等待秒数": MAX_2FA_WAIT,
            "下一步": "请在 IBKR Mobile 中确认 2FA 推送后等待系统继续。",
        }
        if PB_PUBLIC_URL:
            detail["状态页"] = "%s/ibkr_runtime.html?environment=%s" % (PB_PUBLIC_URL, ENVIRONMENT)

        PBClient(base_url=PB_URL).report_ibkr_2fa_result(
            status="waiting_confirm",
            detail=detail,
            source="ibkr_login_script",
            environment=ENVIRONMENT,
            message="已提交账号密码，请在 IBKR Mobile 确认 2FA。",
            last_result="等待 IBKR Mobile 确认中。",
        )
        print("  Feishu reminder sent")
    except Exception as exc:
        print("  Feishu reminder failed:", exc)


def report_2fa_result(status, message, error=""):
    try:
        from ibkr_compute.integrations.pb_client import PBClient

        detail = {
            "账号": ACCOUNT_ID or "-",
            "等待秒数": MAX_2FA_WAIT,
        }
        if PB_PUBLIC_URL:
            detail["状态页"] = "%s/ibkr_runtime.html?environment=%s" % (PB_PUBLIC_URL, ENVIRONMENT)

        PBClient(base_url=PB_URL).report_ibkr_2fa_result(
            status=status,
            detail=detail,
            source="ibkr_login_script",
            environment=ENVIRONMENT,
            message=message,
            last_result=message,
            error=error,
        )
    except Exception as exc:
        print("  Feishu result sync failed:", exc)


def main():
    if not USERNAME or not PASSWORD:
        print("ERROR: IBKR_USERNAME and IBKR_PASSWORD must be set")
        sys.exit(1)

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
        print("Using snap Chromium")

    service = Service(executable_path="/usr/local/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)

    try:
        login_url = GATEWAY_URL + "/sso/Login?forwardTo=22&RL=1&ip2loc=on"
        print("[1/5] Loading login page:", login_url)
        driver.get(login_url)
        time.sleep(3)
        print("  Page title:", driver.title)

        print("[2/5] Filling credentials...")
        wait = WebDriverWait(driver, 30)
        username_field = wait.until(EC.presence_of_element_located((By.NAME, "username")))
        username_field.clear()
        username_field.send_keys(USERNAME)

        password_field = driver.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys(PASSWORD)
        print("  Credentials filled")

        print("[3/5] Submitting login form...")
        submit_buttons = driver.find_elements(By.CSS_SELECTOR, "button[type='submit']")
        submit_btn = None
        for btn in submit_buttons:
            if btn.text.strip().lower() in ("login", "log in", "submit"):
                submit_btn = btn
                break
        if submit_btn is None and submit_buttons:
            submit_btn = submit_buttons[0]
        if submit_btn is None:
            print("  ERROR: No submit button found")
            sys.exit(1)
        submit_btn.click()
        time.sleep(5)
        print("  Post-submit URL:", driver.current_url)

        page_text = driver.find_element(By.TAG_NAME, "body").text[:500]
        print("  Page text:", page_text[:200])

        print("[4/5] Waiting for 2FA - CHECK YOUR IBKR MOBILE APP NOW!")
        notify_2fa_pending()
        print("=" * 50)

        session = requests.Session()
        session.verify = False

        def sync_cookies():
            try:
                for cookie in driver.get_cookies():
                    session.cookies.set(cookie["name"], cookie["value"],
                                        domain=cookie.get("domain", ""),
                                        path=cookie.get("path", "/"))
            except Exception:
                pass

        sync_cookies()
        start = time.time()
        authenticated = False
        last_cookie_sync = time.time()

        while time.time() - start < MAX_2FA_WAIT:
            elapsed = int(time.time() - start)
            if time.time() - last_cookie_sync >= 10:
                sync_cookies()
                last_cookie_sync = time.time()

            # Check browser page state for success/redirect
            try:
                current_url = driver.current_url or ""
                page_text = driver.find_element(By.TAG_NAME, "body").text[:300]
                if "/sso/Dispatcher" in current_url or "Client login succeeds" in page_text:
                    print("  [%ds] SUCCESS PAGE DETECTED: %s" % (elapsed, current_url))
                    sync_cookies()
                    for promote_round in range(5):
                        time.sleep(2)
                        sync_cookies()
                        try:
                            resp = session.post(GATEWAY_URL + "/v1/api/tickle", timeout=10)
                        except Exception:
                            pass
                        try:
                            resp = session.post(GATEWAY_URL + "/v1/api/iserver/auth/status", timeout=10)
                            if resp.status_code == 200 and resp.text.strip():
                                data = resp.json()
                                if data.get("authenticated", False):
                                    print("  [%ds] Backend auth confirmed (round %d)" % (elapsed, promote_round + 1))
                                    authenticated = True
                                    break
                        except Exception:
                            pass
                        print("  [%ds] Promote attempt %d..." % (elapsed, promote_round + 1))
                    if authenticated:
                        break
                    print("  [%ds] WARNING: Success page but backend auth not confirmed" % elapsed)
            except Exception:
                pass

            try:
                resp = session.post(GATEWAY_URL + "/v1/api/iserver/auth/status", timeout=10)
                if resp.status_code == 200 and resp.text.strip():
                    data = resp.json()
                    auth = data.get("authenticated", False)
                    competing = data.get("competing", False)
                    connected = data.get("connected", False)
                    print("  [%ds] auth=%s connected=%s competing=%s" % (elapsed, auth, connected, competing))
                    if auth:
                        authenticated = True
                        break
                    if competing:
                        print("  WARNING: Competing session detected!")
                else:
                    if elapsed % 15 == 0:
                        print("  [%ds] Waiting for 2FA confirmation..." % elapsed)
            except Exception as e:
                if elapsed % 30 == 0:
                    print("  [%ds] Check error: %s" % (elapsed, e))

            time.sleep(5)

        print("=" * 50)
        if authenticated:
            print("[5/5] LOGIN SUCCESSFUL! (took %ds)" % int(time.time() - start))
            report_2fa_result("success", "2FA 验证成功，Gateway 已恢复认证。")

            # Verify with tickle
            try:
                tickle_resp = session.post(GATEWAY_URL + "/v1/api/tickle", timeout=10)
                if tickle_resp.status_code == 200 and tickle_resp.text.strip():
                    print("  Tickle response:", tickle_resp.text[:200])
            except Exception:
                pass

            sys.exit(0)
        else:
            print("[5/5] 2FA TIMEOUT after %ds" % MAX_2FA_WAIT)
            print("  Final page:", driver.title, "-", driver.current_url)
            report_2fa_result("timeout", "等待 IBKR Mobile 确认超时，请重新触发。", "2fa_timeout")
            sys.exit(1)

    except Exception as e:
        print("LOGIN ERROR:", str(e))
        import traceback
        traceback.print_exc()
        report_2fa_result("failed", "IBKR 登录流程失败，请重新触发。", str(e))
        sys.exit(1)
    finally:
        driver.quit()
        print("Browser closed.")


if __name__ == "__main__":
    main()
