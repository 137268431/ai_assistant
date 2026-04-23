import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _extract_route_block(path: Path, marker: str) -> str:
    text = path.read_text()
    start = text.find(marker)
    if start < 0:
        raise AssertionError(f"marker not found: {marker} in {path}")
    brace_start = text.find("{", start)
    if brace_start < 0:
        raise AssertionError(f"route body not found: {marker} in {path}")
    i = brace_start
    depth = 0
    in_single = in_double = in_backtick = False
    in_line_comment = in_block_comment = False
    escaped = False
    end = None
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 1
        elif in_single:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                in_single = False
        elif in_double:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
        elif in_backtick:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "`":
                in_backtick = False
        else:
            if ch == "/" and nxt == "/":
                in_line_comment = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                i += 1
            elif ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "`":
                in_backtick = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    j = i + 1
                    while j < len(text) and text[j] in " \t\r\n":
                        j += 1
                    if j < len(text) and text[j] == ")":
                        end = j + 1
                        break
        i += 1
    if end is None:
        raise AssertionError(f"route end not found: {marker} in {path}")
    return text[start:end]


class PocketBaseCompatProxyTest(unittest.TestCase):
    def test_api_proxy_exposes_html_proxy(self):
        path = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks" / "lib" / "system" / "api_proxy.js"
        text = path.read_text()
        self.assertIn("function proxyIbkrApiHtml", text)
        self.assertIn("proxyIbkrApiHtml,", text)

    def test_signal_routes_in_ibkr_actions_are_api_proxies(self):
        path = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks" / "modules" / "actions" / "ibkr_actions.js"
        single = _extract_route_block(path, 'routerAdd("POST", "/api/custom/ibkr/signal", (c) => {')
        batch = _extract_route_block(path, 'routerAdd("POST", "/api/custom/ibkr/signals", (c) => {')
        self.assertIn('proxyIbkrApiJson(c, "/api/custom/ibkr/signal"', single)
        self.assertIn('proxyIbkrApiJson(c, "/api/custom/ibkr/signals"', batch)

    def test_runtime_status_config_startup_routes_in_ibkr_actions_are_api_proxies(self):
        path = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks" / "modules" / "actions" / "ibkr_actions.js"
        expectations = {
            'routerAdd("POST", "/api/custom/ibkr/proxy", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/proxy"',
            'routerAdd("GET", "/api/custom/ibkr/statusz", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/statusz"',
            'routerAdd("GET", "/api/custom/ibkr/healthz", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/healthz"',
            'routerAdd("GET", "/api/custom/ibkr/runtime/config", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/runtime/config"',
            'routerAdd("POST", "/api/custom/ibkr/startup/progress", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/startup/progress"',
            'routerAdd("GET", "/api/custom/ibkr/startup/status", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/startup/status"',
            'routerAdd("GET", "/api/custom/ibkr/2fa/status", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/2fa/status"',
            'routerAdd("POST", "/api/custom/ibkr/2fa/request", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/2fa/request"',
            'routerAdd("POST", "/api/custom/ibkr/2fa/result", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/2fa/result"',
            'routerAdd("POST", "/api/custom/ibkr/2fa/respond", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/2fa/respond"',
            'routerAdd("POST", "/api/custom/ibkr/emergency-stop", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/emergency-stop"',
            'routerAdd("POST", "/api/custom/ibkr/recover", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/recover"',
            'routerAdd("POST", "/api/custom/ibkr/reauth", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/reauth"',
        }
        for marker, expected in expectations.items():
            with self.subTest(marker=marker):
                block = _extract_route_block(path, marker)
                self.assertIn(expected, block)

    def test_state_and_notify_routes_in_ibkr_actions_are_api_proxies(self):
        path = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks" / "modules" / "actions" / "ibkr_actions.js"
        expectations = {
            'routerAdd("GET", "/api/custom/ibkr/state/signals", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/state/signals"',
            'routerAdd("POST", "/api/custom/ibkr/state/signals", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/state/signals"',
            'routerAdd("GET", "/api/custom/ibkr/state/orders", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/state/orders"',
            'routerAdd("POST", "/api/custom/ibkr/state/orders", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/state/orders"',
            'routerAdd("POST", "/api/custom/ibkr/health-report", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/health-report"',
            'routerAdd("POST", "/api/custom/ibkr/notify", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/notify"',
        }
        for marker, expected in expectations.items():
            with self.subTest(marker=marker):
                block = _extract_route_block(path, marker)
                self.assertIn(expected, block)

    def test_signal_action_routes_are_compatibility_proxies(self):
        path = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks" / "modules" / "actions" / "ibkr_signal_actions.js"
        expectations = {
            'routerAdd("GET", "/webhook/signal/confirm", (c) => {': 'proxyIbkrApiHtml(c, "/webhook/signal/confirm"',
            'routerAdd("GET", "/webhook/signal/cancel", (c) => {': 'proxyIbkrApiHtml(c, "/webhook/signal/cancel"',
            'routerAdd("GET", "/api/custom/ibkr/signals/pending", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/signals/pending"',
            'routerAdd("POST", "/api/custom/ibkr/signals/ack", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/signals/ack"',
            'routerAdd("GET", "/webhook/order/cancel", (c) => {': 'proxyIbkrApiHtml(c, "/webhook/order/cancel"',
            'routerAdd("GET", "/webhook/order/close", (c) => {': 'proxyIbkrApiHtml(c, "/webhook/order/close"',
            'routerAdd("POST", "/api/custom/ibkr/orders/cancel_group", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/orders/cancel_group"',
            'routerAdd("POST", "/api/custom/ibkr/orders/close_group", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/orders/close_group"',
        }
        for marker, expected in expectations.items():
            with self.subTest(marker=marker):
                block = _extract_route_block(path, marker)
                self.assertIn(expected, block)

    def test_order_manage_routes_are_api_proxies(self):
        path = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks" / "modules" / "actions" / "order_manage.js"
        upsert = _extract_route_block(path, 'routerAdd("POST", "/api/custom/ibkr/orders/upsert", (c) => {')
        reconcile = _extract_route_block(path, 'routerAdd("POST", "/api/custom/ibkr/orders/reconcile", (c) => {')
        self.assertIn('proxyIbkrApiJson(c, "/api/custom/ibkr/orders/upsert"', upsert)
        self.assertIn('proxyIbkrApiJson(c, "/api/custom/ibkr/orders/reconcile"', reconcile)

    def test_reverse_routes_are_api_proxies(self):
        path = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks" / "modules" / "actions" / "ibkr_reverse_signals.js"
        expectations = {
            'routerAdd("GET", "/api/custom/ibkr/reverse/list", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/reverse/list"',
            'routerAdd("POST", "/api/custom/ibkr/reverse/calculate", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/reverse/calculate"',
            'routerAdd("GET", "/api/custom/ibkr/reverse/pending", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/reverse/pending"',
            'routerAdd("POST", "/api/custom/ibkr/reverse/dispatch", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/reverse/dispatch"',
            'routerAdd("POST", "/api/custom/ibkr/reverse/ack", (c) => {': 'proxyIbkrApiJson(c, "/api/custom/ibkr/reverse/ack"',
        }
        for marker, expected in expectations.items():
            with self.subTest(marker=marker):
                block = _extract_route_block(path, marker)
                self.assertIn(expected, block)

    def test_order_scheduler_crons_are_scheduler_proxies(self):
        path = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks" / "modules" / "schedulers" / "order_scheduler.js"
        expectations = {
            'cronAdd("order_expiry_check", "*/5 * * * *", () => {': 'proxySchedulerCronJob("order_expiry_check"',
            'cronAdd("order_detail_integrity_guard", "*/10 * * * *", () => {': 'proxySchedulerCronJob("order_detail_integrity_guard"',
        }
        for marker, expected in expectations.items():
            with self.subTest(marker=marker):
                block = _extract_route_block(path, marker)
                self.assertIn(expected, block)

    def test_system_monitor_crons_proxy_native_scheduler_jobs(self):
        path = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks" / "modules" / "schedulers" / "ibkr_system_monitor.js"
        expectations = {
            'cronAdd("system_market_open_reminder", "*/5 * * * *", () => {': 'proxySchedulerCronJob("system_market_open_reminder"',
            'cronAdd("ibkr_auth_edge_guard", "* 4-20 * * 1-5", () => {': 'proxySchedulerCronJob("ibkr_auth_edge_guard"',
            'cronAdd("ibkr_auth_pending_guard", "*/10 4-20 * * 1-5", () => {': 'proxySchedulerCronJob("ibkr_auth_pending_guard"',
            'cronAdd("system_data_gap_guard", "*/10 4-20 * * 1-5", () => {': 'proxySchedulerCronJob("system_data_gap_guard"',
            'cronAdd("ibkr_2fa_hourly_check", "5 4-20 * * 1-5", () => {': 'proxySchedulerCronJob("ibkr_2fa_hourly_check"',
            'cronAdd("ibkr_weekly_reauth_reminder", "0 5 * * 1", () => {': 'proxySchedulerCronJob("ibkr_weekly_reauth_reminder"',
            'cronAdd("ibkr_weekly_reauth_followup", "30 7 * * 1", () => {': 'proxySchedulerCronJob("ibkr_weekly_reauth_followup"',
            'cronAdd("system_daily_report", "*/5 * * * *", () => {': 'proxySchedulerCronJob("system_daily_report"',
        }
        for marker, expected in expectations.items():
            with self.subTest(marker=marker):
                block = _extract_route_block(path, marker)
                self.assertIn(expected, block)


if __name__ == "__main__":
    unittest.main()
