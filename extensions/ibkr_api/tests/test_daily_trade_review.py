import re
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_SRC_ROOTS = [
    REPO_ROOT / "runtime" / "ibkr_api" / "src",
    REPO_ROOT / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = types.SimpleNamespace(args={})
    sys.modules["flask"] = flask_stub

from ibkr_api.analytics.daily_trade_review import build_daily_trade_review_response
from ibkr_api.analytics.routes import register_analytics_routes
from ibkr_api.universe.today_targets_shared import effective_target_status, target_row_is_daily_scan_active


def _normalize_environment(value, default="live"):
    return str(value or default).strip().lower() or default


def _escape_filter_string(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


class _FakePB:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def get_state(self, *args, **kwargs):
        return None

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        self.calls.append((collection, filter, sort, per_page, page))
        rows = [dict(row) for row in self.records.get(collection, [])]
        text = str(filter or "")
        env_match = re.search(r'environment = "([^"]+)"', text)
        if env_match:
            rows = [row for row in rows if row.get("environment") == env_match.group(1)]
        date_match = re.search(r'date = "(\d{4}-\d{2}-\d{2})"', text)
        if date_match and collection in {"ibkr_targets", "ibkr_signals"}:
            token = date_match.group(1)
            rows = [row for row in rows if row.get("date") == token or str(row.get("us_time") or "").startswith(token)]
        market_date_match = re.search(r'market_date = "(\d{4}-\d{2}-\d{2})"', text)
        if market_date_match:
            token = market_date_match.group(1)
            rows = [row for row in rows if row.get("market_date") == token]
        symbol_matches = re.findall(r'symbol = "([^"]+)"', text)
        if symbol_matches:
            rows = [row for row in rows if row.get("symbol") in symbol_matches]
        signal_matches = re.findall(r'signal_id = "([^"]+)"', text)
        if signal_matches:
            rows = [row for row in rows if row.get("signal_id") in signal_matches]
        start = (page - 1) * per_page
        return rows[start:start + per_page]

    def get_all_records(self, collection, filter=None, sort=None, max_pages=10):
        return self.get_records(collection, filter=filter, sort=sort, per_page=200, page=1)


class _FakeApp:
    def __init__(self):
        self.routes = {}

    def route(self, path, methods=None):
        def decorator(func):
            self.routes[(path, tuple(methods or []))] = func
            return func
        return decorator


class DailyTradeReviewTest(unittest.TestCase):
    def _payload(self):
        records = {
            "ibkr_target_decisions": [
                {
                    "decision_key": "live|2026-05-21|daily_scan|early_expansion_seed|AAPL",
                    "environment": "live",
                    "market_date": "2026-05-21",
                    "source": "daily_scan",
                    "symbol": "AAPL",
                    "decision": "selected",
                    "reason_code": "selected_active",
                    "reason_text": "trend + context gate",
                    "rank": 1,
                },
                {
                    "decision_key": "live|2026-05-21|daily_scan|early_expansion_seed|MSFT",
                    "environment": "live",
                    "market_date": "2026-05-21",
                    "source": "daily_scan",
                    "symbol": "MSFT",
                    "decision": "rejected",
                    "reason_code": "context_gate_not_passed",
                    "reason_text": "context gate false",
                },
            ],
            "ibkr_targets": [
                {
                    "id": "target-aapl",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "AAPL",
                    "status": "active",
                    "direction_bias": "long",
                    "score": 88,
                    "scan_reason": "trend + context gate",
                    "extra": {"source": "daily_scan", "active_gate_passed": True, "context_gate_passed": True},
                }
            ],
            "ibkr_signals": [
                {
                    "signal_id": "sig-aapl",
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
                    "signal": "vwap_trend_pullback_long",
                    "us_time": "2026-05-21 09:35:00",
                },
                {
                    "signal_id": "sig-tsla",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "TSLA",
                    "direction": "short",
                    "status": "rejected",
                    "status_reason": "manual_rejected",
                    "us_time": "2026-05-21 09:40:00",
                },
            ],
            "orders": [
                {"id": "entry-aapl", "signal_id": "sig-aapl", "environment": "paper", "symbol": "AAPL", "role": "entry", "status": "Filled", "fill_price": 100, "filled_qty": 10, "us_time": "2026-05-21 09:36:00"},
                {"id": "tp-aapl", "signal_id": "sig-aapl", "environment": "paper", "symbol": "AAPL", "role": "take_profit", "status": "Filled", "fill_price": 110, "filled_qty": 10, "us_time": "2026-05-21 10:10:00"},
                {"id": "entry-tsla", "signal_id": "sig-tsla", "environment": "paper", "symbol": "TSLA", "role": "entry", "status": "Filled", "fill_price": 200, "filled_qty": 3, "us_time": "2026-05-21 09:41:00"},
            ],
        }
        payload, status = build_daily_trade_review_response(
            _FakePB(records),
            params={"market_date": "2026-05-21", "broker_mode": "paper", "data_environment": "live", "include_events": "1"},
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape_filter_string,
            time_strings=lambda: {"date": "2026-05-21"},
        )
        self.assertEqual(200, status)
        return payload

    def test_builds_symbol_review_with_decisions_events_and_issues(self):
        payload = self._payload()
        self.assertTrue(payload["ok"])
        self.assertEqual("2026-05-21", payload["market_date"])
        self.assertIn("target_decisions", {item["id"] for item in payload["integrations"]})
        by_symbol = {item["symbol"]: item for item in payload["items"]}
        self.assertEqual("selected", by_symbol["AAPL"]["selection_decision"])
        self.assertEqual(1, by_symbol["AAPL"]["order_summary"]["entry_filled"])
        self.assertTrue(by_symbol["MSFT"]["not_selected_reasons"])
        self.assertEqual("problem", by_symbol["TSLA"]["review_status"])
        self.assertEqual("rejected_signal_has_orders", by_symbol["TSLA"]["issue_flags"][0]["code"])
        self.assertTrue(by_symbol["AAPL"].get("events"))

    def test_registers_daily_trade_review_route(self):
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
        self.assertIn("custom_ibkr_daily_trade_review", exports)
        self.assertIn(("/api/custom/ibkr/analytics/daily-trade-review", ("GET",)), app.routes)

    def test_console_page_and_compat_wiring(self):
        html = (REPO_ROOT / "runtime" / "ibkr_console" / "static" / "ibkr_trade_review.html").read_text(encoding="utf-8")
        js = (REPO_ROOT / "runtime" / "ibkr_console" / "static" / "assets" / "js" / "pages" / "ibkr_trade_review" / "page.js").read_text(encoding="utf-8")
        compat = (REPO_ROOT / "runtime" / "ibkr_api" / "src" / "ibkr_api" / "compat" / "routes.py").read_text(encoding="utf-8")
        bridge = (REPO_ROOT / "runtime" / "ibkr_console" / "static" / "assets" / "js" / "shared" / "ui-bridges.js").read_text(encoding="utf-8")
        for token in ("/api/custom/ibkr/analytics/daily-trade-review", "ibkr_trade_review", "生命周期图"):
            self.assertIn(token, html)
        for token in ("daily-trade-review", "issue_flags", "lifecycle_url", "not_selected_reasons"):
            self.assertIn(token, js)
        self.assertIn("ibkr/analytics/daily-trade-review", compat)
        self.assertIn("/ibkr_trade_review.html", bridge)

    def test_active_target_policy_accepts_intraday_context_and_manual_sources(self):
        intraday = {"status": "active", "extra": {"source": "intraday_window_admission", "context_gate_passed": True}}
        manual = {"status": "active", "extra": {"source": "manual_page_add"}}
        stale = {"status": "active", "extra": {"source": "daily_scan", "active_gate_passed": False}}
        self.assertTrue(target_row_is_daily_scan_active(intraday))
        self.assertEqual("active", effective_target_status(intraday))
        self.assertEqual("active", effective_target_status(manual))
        self.assertEqual("candidate", effective_target_status(stale))


if __name__ == "__main__":
    unittest.main()
