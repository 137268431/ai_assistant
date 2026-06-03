import re
import sys
import types
import unittest
from pathlib import Path


SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = types.SimpleNamespace(args={})
    sys.modules["flask"] = flask_stub


from ibkr_api.analytics.daily_signals import build_daily_signal_analytics_response
from ibkr_api.analytics.routes import register_analytics_routes


def _normalize_environment(value, default="live"):
    return str(value or default).strip().lower() or default


def _escape_filter_string(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


class _FakePB:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        self.calls.append((collection, filter, sort, per_page, page))
        rows = list(self.records.get(collection, []))
        text = str(filter or "")
        env_match = re.search(r'environment = "([^"]+)"', text)
        if env_match:
            rows = [row for row in rows if row.get("environment") == env_match.group(1)]
        if collection == "ibkr_signals":
            date_match = re.search(r'date = "(\d{4}-\d{2}-\d{2})"', text)
            if date_match:
                token = date_match.group(1)
                rows = [row for row in rows if row.get("date") == token or str(row.get("us_time") or "").startswith(token)]
        if collection == "orders":
            signal_ids = re.findall(r'signal_id = "([^"]+)"', text)
            if signal_ids:
                rows = [row for row in rows if row.get("signal_id") in signal_ids]
        start = (page - 1) * per_page
        return rows[start : start + per_page]


class _FakeApp:
    def __init__(self):
        self.routes = {}

    def route(self, path, methods=None):
        def decorator(func):
            self.routes[(path, tuple(methods or []))] = func
            return func

        return decorator


class DailySignalAnalyticsTest(unittest.TestCase):
    def _build_payload(self):
        records = {
            "ibkr_signals": [
                {
                    "signal_id": "sig-long-tp",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "AAPL",
                    "direction": "long",
                    "status": "closed",
                    "entry": 100,
                    "take_profit": 110,
                    "stop_loss": 95,
                    "shares": 10,
                    "rr": "2.0:1",
                    "signal": "legacy_long",
                    "extra": {"setup": "vwap_trend_pullback_long", "setup_family": "trend_pullback"},
                    "us_time": "2026-05-21 09:35:00",
                },
                {
                    "signal_id": "sig-short-sl",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "TSLA",
                    "direction": "short",
                    "status": "closed",
                    "entry": 100,
                    "take_profit": 90,
                    "stop_loss": 105,
                    "shares": 10,
                    "rr": "2.0:1",
                    "signal": "sd_squeeze_breakout_short",
                    "extra": {"setup_family": "breakout"},
                    "us_time": "2026-05-21 09:40:00",
                },
                {
                    "signal_id": "sig-mr-open",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "MSFT",
                    "direction": "long",
                    "status": "pending",
                    "entry": 50,
                    "take_profit": 53,
                    "stop_loss": 48,
                    "shares": 20,
                    "rr": "1.5:1",
                    "signal": "sd_mr_reversal_long",
                    "extra": {"setup": "sd_mr_reversal_long", "setup_family": "mean_reversion"},
                    "us_time": "2026-05-21 10:05:00",
                },
                {
                    "signal_id": "sig-old",
                    "environment": "live",
                    "date": "2026-05-20",
                    "symbol": "OLD",
                    "direction": "long",
                    "status": "closed",
                    "entry": 10,
                    "take_profit": 11,
                    "stop_loss": 9,
                    "shares": 10,
                    "rr": "1.0:1",
                    "signal": "old_setup",
                    "us_time": "2026-05-20 09:35:00",
                },
            ],
            "orders": [
                {
                    "id": "entry-1",
                    "signal_id": "sig-long-tp",
                    "environment": "paper",
                    "role": "entry",
                    "status": "Filled",
                    "direction": "long",
                    "fill_price": 100,
                    "filled_qty": 10,
                    "commission": 1,
                    "unique_id": "entry-1",
                    "trade_group_id": "tg-1",
                },
                {
                    "id": "tp-1",
                    "signal_id": "sig-long-tp",
                    "environment": "paper",
                    "role": "take_profit",
                    "status": "Filled",
                    "direction": "long",
                    "fill_price": 110,
                    "filled_qty": 10,
                    "commission": 0.5,
                    "entry_order_unique_id": "entry-1",
                    "trade_group_id": "tg-1",
                    "us_time": "2026-05-21 10:00:00",
                },
                {
                    "id": "entry-2",
                    "signal_id": "sig-short-sl",
                    "environment": "paper",
                    "role": "entry",
                    "status": "Filled",
                    "direction": "short",
                    "fill_price": 100,
                    "filled_qty": 10,
                    "commission": 1,
                    "unique_id": "entry-2",
                    "trade_group_id": "tg-2",
                },
                {
                    "id": "sl-2",
                    "signal_id": "sig-short-sl",
                    "environment": "paper",
                    "role": "stop_loss",
                    "status": "Filled",
                    "direction": "short",
                    "fill_price": 105,
                    "filled_qty": 10,
                    "commission": 0.5,
                    "entry_order_unique_id": "entry-2",
                    "trade_group_id": "tg-2",
                    "us_time": "2026-05-21 10:30:00",
                },
                {
                    "id": "old-entry",
                    "signal_id": "sig-old",
                    "environment": "paper",
                    "role": "entry",
                    "status": "Filled",
                    "direction": "long",
                    "fill_price": 10,
                    "filled_qty": 100,
                },
            ],
        }
        payload, status_code = build_daily_signal_analytics_response(
            _FakePB(records),
            params={"date": "2026-05-21", "broker_mode": "paper", "data_environment": "live"},
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape_filter_string,
            time_strings=lambda: {"date": "2026-05-21"},
        )
        self.assertEqual(200, status_code)
        return payload

    def test_builds_setup_rr_and_realized_pnl_summary(self):
        payload = self._build_payload()

        self.assertTrue(payload["ok"])
        self.assertEqual("2026-05-21", payload["date"])
        self.assertEqual("paper", payload["broker_mode"])
        self.assertEqual("live", payload["data_environment"])
        self.assertEqual(3, payload["summary"]["total_signals"])
        self.assertEqual(3, payload["summary"]["unique_symbols"])
        self.assertEqual(2, payload["summary"]["closed_trades"])
        self.assertEqual(2, payload["summary"]["entry_filled_signals"])
        self.assertAlmostEqual(1.8333, payload["summary"]["avg_rr"], places=4)
        self.assertAlmostEqual(260.0, payload["summary"]["planned_profit"], places=2)
        self.assertAlmostEqual(140.0, payload["summary"]["planned_risk"], places=2)
        self.assertAlmostEqual(1.8571, payload["summary"]["planned_portfolio_rr"], places=4)

        realized = payload["summary"]["realized"]
        self.assertEqual(1, realized["wins"])
        self.assertEqual(1, realized["losses"])
        self.assertAlmostEqual(47.0, realized["net_pnl"], places=2)
        self.assertAlmostEqual(98.5, realized["gross_profit"], places=2)
        self.assertAlmostEqual(-51.5, realized["gross_loss"], places=2)
        self.assertAlmostEqual(1.9126, realized["profit_loss_ratio"], places=4)
        self.assertAlmostEqual(1.9126, realized["profit_factor"], places=4)

        setup_counts = {row["key"]: row["count"] for row in payload["setup_rows"]}
        self.assertEqual(1, setup_counts["vwap_trend_pullback_long"])
        self.assertEqual(1, setup_counts["sd_squeeze_breakout_short"])
        self.assertEqual(1, setup_counts["sd_mr_reversal_long"])

    def test_no_winners_returns_null_profit_loss_ratio_with_reason(self):
        records = {
            "ibkr_signals": [
                {
                    "signal_id": "sig-loss",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "AAPL",
                    "direction": "long",
                    "entry": 100,
                    "take_profit": 110,
                    "stop_loss": 95,
                    "shares": 10,
                    "rr": "2.0:1",
                    "signal": "vwap_trend_pullback_long",
                }
            ],
            "orders": [
                {
                    "id": "entry-loss",
                    "signal_id": "sig-loss",
                    "environment": "paper",
                    "role": "entry",
                    "status": "Filled",
                    "direction": "long",
                    "fill_price": 100,
                    "filled_qty": 10,
                    "unique_id": "entry-loss",
                },
                {
                    "id": "sl-loss",
                    "signal_id": "sig-loss",
                    "environment": "paper",
                    "role": "stop_loss",
                    "status": "Filled",
                    "direction": "long",
                    "fill_price": 95,
                    "filled_qty": 10,
                    "entry_order_unique_id": "entry-loss",
                },
            ],
        }
        payload, _ = build_daily_signal_analytics_response(
            _FakePB(records),
            params={"date": "2026-05-21"},
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape_filter_string,
            time_strings=lambda: {"date": "2026-05-21"},
        )

        realized = payload["summary"]["realized"]
        self.assertIsNone(realized["profit_loss_ratio"])
        self.assertEqual("no_winning_trades", realized["profit_loss_ratio_reason"])
        self.assertEqual(-50.0, realized["net_pnl"])

    def test_registers_daily_signal_analytics_route(self):
        app = _FakeApp()
        exports = register_analytics_routes(
            app,
            deps={
                "pb": _FakePB({}),
                "normalize_environment": _normalize_environment,
                "escape_filter_string": _escape_filter_string,
                "time_strings": lambda: {"date": "2026-05-21"},
            },
        )

        self.assertIn("custom_ibkr_daily_signal_analytics", exports)
        self.assertIn(("/api/custom/ibkr/analytics/daily-signals", ("GET",)), app.routes)

    def test_console_stats_page_wires_daily_signal_analysis_panel(self):
        stats_html = (REPO_ROOT / "runtime" / "ibkr_console" / "static" / "ibkr_stats.html").read_text(encoding="utf-8")
        signals_html = (REPO_ROOT / "runtime" / "ibkr_console" / "static" / "ibkr_signals.html").read_text(encoding="utf-8")
        nav_js = (REPO_ROOT / "runtime" / "ibkr_console" / "static" / "assets" / "js" / "shared" / "ui-toast-nav.js").read_text(
            encoding="utf-8"
        )
        compat_routes = (REPO_ROOT / "runtime" / "ibkr_api" / "src" / "ibkr_api" / "compat" / "routes.py").read_text(
            encoding="utf-8"
        )

        for token in (
            "signal-rule-analysis",
            "/api/custom/ibkr/analytics/daily-signals",
            "allow_fallback: 0",
            "strict: 1",
            "dataInsufficient",
            "signalRuleChart",
            "signalRrChart",
            "loadSignalAnalysis",
        ):
            self.assertIn(token, stats_html)
        self.assertNotIn("signalsAnalysisLink", signals_html)
        self.assertIn("/ibkr_stats.html", nav_js)
        self.assertIn("label: '复盘'", nav_js)
        self.assertIn("ibkr/analytics/daily-signals", compat_routes)


if __name__ == "__main__":
    unittest.main()
