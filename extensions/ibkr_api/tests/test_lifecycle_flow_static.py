import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
STATIC_ROOT = REPO_ROOT / "runtime" / "ibkr_console" / "static"
API_ROOT = REPO_ROOT / "runtime" / "ibkr_api" / "src" / "ibkr_api"


def read_repo_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class LifecycleFlowStaticSmokeTest(unittest.TestCase):
    def test_page_wires_graph_dependencies_and_assets(self):
        html = read_repo_text("runtime/ibkr_console/static/ibkr_lifecycle_flow.html")

        self.assertIn("cytoscape", html)
        self.assertIn("dagre", html)
        self.assertIn("/assets/css/pages/ibkr_lifecycle_flow/page.css", html)
        self.assertIn("/assets/js/pages/ibkr_lifecycle_flow/page.js", html)
        self.assertIn("/api/custom/ibkr/lifecycle-flow", html)

    def test_page_js_keeps_fill_source_policy_visible(self):
        js = read_repo_text("runtime/ibkr_console/static/assets/js/pages/ibkr_lifecycle_flow/page.js")

        for token in (
            "/api/custom/ibkr/lifecycle-flow",
            "actual_ibkr",
            "paper_ibkr",
            "backtest_simulated",
            "unknown",
            "normalizeFillSource",
            "price_kind",
            "take_profit_price",
            "stop_loss_price",
            "entry_limit",
            "order_detail_unverified_fill",
            "order_detail_snapshot",
            "lifecycle_endpoint",
            "change_summary",
            "changeSummaryForRecord",
            "renderChangePanel",
            "formatTime(time)",
            "estimated",
            "fill_source_policy",
        ):
            self.assertIn(token, js)

    def test_entry_points_link_to_lifecycle_page_with_context(self):
        entry_points = {
            "runtime/ibkr_console/static/assets/js/pages/ibkr_screener/current-targets.js": (
                "/ibkr_lifecycle_flow.html",
                "signal_id",
                "trade_group_id",
                "bar_time_ms",
            ),
            "runtime/ibkr_console/static/ibkr_signals.html": (
                "/ibkr_lifecycle_flow.html",
                "signal_id",
                "trade_group_id",
                "trace",
            ),
            "runtime/ibkr_console/static/ibkr_order_details.html": (
                "/ibkr_lifecycle_flow.html",
                "order_id",
                "signal_id",
                "trade_group_id",
            ),
            "runtime/ibkr_console/static/orders.html": (
                "/ibkr_lifecycle_flow.html",
                "buildOrderLifecycleFlowUrl",
                "order_id",
                "trade_group_id",
            ),
            "runtime/ibkr_console/static/assets/js/pages/ibkr_backtests/tracking.js": (
                "/ibkr_lifecycle_flow.html",
                "mode: 'backtest'",
                "run_id",
                "backtest_date",
            ),
            "runtime/ibkr_console/static/assets/js/pages/ibkr_chart/utils.js": (
                "/ibkr_lifecycle_flow.html",
                "signal_id",
                "trade_group_id",
                "order_id",
            ),
            "runtime/ibkr_console/static/index.html": (
                "/ibkr_lifecycle_flow.html",
                "actionLifecycleLink",
                "生命周期流程图",
            ),
            "runtime/ibkr_console/static/assets/js/shared/ui-toast-nav.js": (
                "/ibkr_lifecycle_flow.html",
                "复盘",
            ),
            "runtime/ibkr_console/static/assets/js/shared/ui-bridges.js": (
                "/ibkr_lifecycle_flow.html",
                "生命周期",
                "renderReviewBridge",
            ),
        }

        for relative_path, tokens in entry_points.items():
            with self.subTest(relative_path=relative_path):
                text = read_repo_text(relative_path)
                for token in tokens:
                    self.assertIn(token, text)

    def test_screener_and_monitor_surface_execution_layer_controls(self):
        screener_html = read_repo_text("runtime/ibkr_console/static/ibkr_screener.html")
        screener_utils = read_repo_text("runtime/ibkr_console/static/assets/js/pages/ibkr_screener/utils-api.js")
        current_targets_js = read_repo_text("runtime/ibkr_console/static/assets/js/pages/ibkr_screener/current-targets.js")
        monitor_html = read_repo_text("runtime/ibkr_console/static/ibkr_monitor.html")

        for token in (
            "currentExecutionLayerFilter",
            "/assets/js/pages/ibkr_screener/current-targets.js",
        ):
            self.assertIn(token, screener_html)
        for token in (
            "getExecutionLayerState",
            "renderExecutionLayerPills",
            "renderExecutionLayerBlock",
            "execution_blockers",
            "watch only",
        ):
            self.assertIn(token, screener_utils)
        for token in (
            "execution_layer",
            "execution_eligible_count",
            "observe_only_count",
            "watch_only_count",
        ):
            self.assertIn(token, current_targets_js)
        for token in (
            "no_execution_eligible_targets",
            "requestAdmissionPreviewFromMonitor",
            "/api/custom/system/jobs/intraday_window_admission",
            "monitor_page_preview",
            "Dry-run 入池预览",
        ):
            self.assertIn(token, monitor_html)

    def test_backend_route_is_registered(self):
        universe_routes = read_repo_text("runtime/ibkr_api/src/ibkr_api/universe/routes.py")
        compat_routes = read_repo_text("runtime/ibkr_api/src/ibkr_api/compat/routes.py")

        self.assertIn("/api/custom/ibkr/lifecycle-flow", universe_routes)
        self.assertIn("build_lifecycle_flow_response", universe_routes)
        self.assertIn("ibkr/lifecycle-flow", compat_routes)

    def test_flow_page_avoids_global_orphan_default(self):
        js = read_repo_text("runtime/ibkr_console/static/assets/js/pages/ibkr_lifecycle_flow/page.js")

        for token in (
            "hasLifecycleContext",
            "renderGuideState",
            "emptyLifecycleNotice",
            "params.get('market_date')",
            "已隐藏无关系统事件",
            "isPlaceholderUnknownNode",
            "displayLifecycleLabel",
        ):
            self.assertIn(token, js)

    def test_daily_review_lifecycle_url_uses_canonical_date_param(self):
        daily_review = read_repo_text("runtime/ibkr_api/src/ibkr_api/analytics/daily_trade_review.py")

        self.assertIn("lifecycle_url", daily_review)
        self.assertIn("date={market_date}", daily_review)

    def test_review_domain_nav_and_bridge_are_split_from_execution(self):
        nav_js = read_repo_text("runtime/ibkr_console/static/assets/js/shared/ui-toast-nav.js")
        bridge_js = read_repo_text("runtime/ibkr_console/static/assets/js/shared/ui-bridges.js")
        self.assertIn("function renderExecutionBridge", bridge_js)
        self.assertIn("function renderAnalyticsBridge", bridge_js)
        execution_body = bridge_js.split("function renderExecutionBridge", 1)[1].split("function renderAnalyticsBridge", 1)[0]

        for token in ("首页", "执行", "标的", "复盘", "回测", "系统", "/ibkr_stats.html"):
            self.assertIn(token, nav_js)
        for token in ("function renderReviewBridge", "收益统计", "每日复盘", "生命周期"):
            self.assertIn(token, bridge_js)
        for relative_path in (
            "runtime/ibkr_console/static/ibkr_trade_review.html",
            "runtime/ibkr_console/static/ibkr_lifecycle_flow.html",
        ):
            with self.subTest(relative_path=relative_path):
                self.assertIn("renderReviewBridge", read_repo_text(relative_path))
        self.assertIn("renderReviewBridge(activePage, params)", execution_body)
        self.assertNotIn("path: '/ibkr_trade_review.html'", execution_body)
        self.assertNotIn("path: '/ibkr_lifecycle_flow.html'", execution_body)
        self.assertNotIn("label: '每日复盘'", execution_body)


if __name__ == "__main__":
    unittest.main()
