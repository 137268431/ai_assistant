import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def read_repo_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class SystemLogicStaticSmokeTest(unittest.TestCase):
    def test_system_logic_page_wires_assets_and_endpoints(self):
        html = read_repo_text("runtime/ibkr_console/static/ibkr_system_logic.html")
        js = read_repo_text("runtime/ibkr_console/static/assets/js/pages/ibkr_system_logic/page.js")
        css = read_repo_text("runtime/ibkr_console/static/assets/css/pages/ibkr_system_logic/page.css")

        self.assertIn("/assets/css/pages/ibkr_system_logic/page.css", html)
        self.assertIn("/assets/js/pages/ibkr_system_logic/page.js", html)
        self.assertIn("/api/custom/ibkr/rules", js)
        self.assertIn("/api/custom/system/cronz", js)
        self.assertIn("logic_coverage", js)
        self.assertIn("order_flow", js)
        self.assertIn("broker_mode_switch", js)
        self.assertIn("renderDetails", js)
        self.assertIn("System Logic Map", html)
        self.assertIn("当前代码事实", html)
        self.assertIn("data correctness proof gate", html)
        self.assertIn("TV 指标审计", html)
        self.assertIn("Truth Repair", html)
        self.assertIn("20260528-data-proof-logic-v1", html)
        self.assertIn("active_window_progress_status", js)
        self.assertIn("logic-anchor-strip", css)
        self.assertIn("flex-direction: row", css)
        self.assertIn("--page-panel-min-height: 0px", css)

    def test_system_navigation_includes_logic_page(self):
        index_html = read_repo_text("runtime/ibkr_console/static/index.html")
        bridge_js = read_repo_text("runtime/ibkr_console/static/assets/js/shared/ui-bridges.js")
        nav_js = read_repo_text("runtime/ibkr_console/static/assets/js/shared/ui-toast-nav.js")
        facade_js = read_repo_text("runtime/ibkr_console/static/assets/js/shared/ui.js")

        for text in (bridge_js, nav_js, facade_js):
            self.assertIn("/ibkr_system_logic.html", text)
        self.assertIn("/ibkr_system_logic.html", index_html)
        self.assertIn("actionSystemLogicLink", index_html)
        self.assertIn("renderHomeBridge", index_html)
        self.assertIn("系统逻辑", index_html)
        self.assertIn("代码规则 / 调度", bridge_js)
        self.assertIn("代码规则 / 调度", facade_js)
        self.assertIn("规则 / 调度", bridge_js)

    def test_screener_no_longer_hosts_rules_board(self):
        html = read_repo_text("runtime/ibkr_console/static/ibkr_screener.html")
        summary_js = read_repo_text("runtime/ibkr_console/static/assets/js/pages/ibkr_screener/summary.js")
        ready_rules_js = read_repo_text("runtime/ibkr_console/static/assets/js/pages/ibkr_screener/ready-rules.js")

        self.assertNotIn("id=\"rulesBoard\"", html)
        self.assertIn("/ibkr_system_logic.html", html)
        self.assertNotIn("loadRulesSummary", summary_js)
        self.assertNotIn("/api/custom/ibkr/rules", ready_rules_js)

    def test_sync_guard_lists_core_logic_and_logic_page(self):
        sync_script = read_repo_text("ops/validate/check_system_logic_sync.py")

        for token in (
            "runtime/ibkr_compute/src/ibkr_compute/core/signal_generator.py",
            "runtime/ibkr_compute/src/ibkr_compute/core/indicator_engine.py",
            "runtime/ibkr_compute/src/ibkr_compute/order_flow",
            "runtime/ibkr_api/src/ibkr_api/control/broker_mode_switch.py",
            "runtime/ibkr_scheduler/src/ibkr_scheduler/cron_registry.py",
            "runtime/ibkr_console/static/ibkr_system_logic.html",
            "runtime/ibkr_compute/src/ibkr_compute/api/market/rules_views.py",
        ):
            self.assertIn(token, sync_script)


if __name__ == "__main__":
    unittest.main()
