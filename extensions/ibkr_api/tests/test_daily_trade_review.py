import re
import sys
import types
import unittest
from datetime import datetime
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
        state = self.records.get("__state__")
        if isinstance(state, dict):
            return dict(state)
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
        range_match = re.search(r'(?:us_time|created) >= "(\d{4}-\d{2}-\d{2}) 00:00:00"', text)
        if range_match and collection == "orders":
            token = range_match.group(1)
            rows = [
                row for row in rows
                if str(row.get("us_time") or "").startswith(token) or str(row.get("created") or "").startswith(token)
            ]
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
                    "metrics": {"score": 91.25, "premarket_volume": 123456},
                    "thresholds": {"active_min_score": 80},
                    "rank": 1,
                    "active_gate_passed": True,
                    "context_gate_passed": True,
                    "created": "2026-05-21 08:45:00",
                    "created_ms": 1779343500000,
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
                    "metrics": {"score": 72.4, "context_score": 0.42},
                    "thresholds": {"context_gate_passed": True, "active_min_score": 80},
                    "rank": 12,
                    "created": "2026-05-21 08:46:00",
                    "extra": {
                        "rejection_examples": [
                            {"bucket": "context_gate_not_passed", "symbol": "MSFT", "actual": "false", "threshold": "true"}
                        ]
                    },
                },
                {
                    "decision_key": "live|2026-05-21|daily_scan|early_expansion_seed|GOOG|selected",
                    "environment": "live",
                    "market_date": "2026-05-21",
                    "source": "daily_scan",
                    "symbol": "GOOG",
                    "decision": "selected",
                    "reason_code": "selected_active",
                    "reason_text": "positive scan reason",
                    "created": "2026-05-21 08:47:00",
                },
                {
                    "decision_key": "live|2026-05-21|daily_scan|early_expansion_seed|GOOG|blocked",
                    "environment": "live",
                    "market_date": "2026-05-21",
                    "source": "daily_scan",
                    "symbol": "GOOG",
                    "decision": "blocked",
                    "reason_code": "duplicate_target",
                    "reason_text": "already exists",
                    "created": "2026-05-21 08:48:00",
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
                },
                {
                    "id": "target-goog",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "GOOG",
                    "status": "active",
                    "direction_bias": "long",
                    "score": 87,
                    "scan_reason": "positive target reason",
                    "extra": {"source": "daily_scan", "active_gate_passed": True, "context_gate_passed": True},
                },
                {
                    "id": "target-intc",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "INTC",
                    "status": "active",
                    "direction_bias": "long",
                    "score": 83,
                    "scan_reason": "intraday context only",
                    "extra": {"source": "intraday_window_admission", "context_gate_passed": True},
                },
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
                {
                    "signal_id": "sig-nflx",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "NFLX",
                    "direction": "long",
                    "status": "closed",
                    "us_time": "2026-05-21 10:00:00",
                },
                {
                    "signal_id": "sig-amd",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "AMD",
                    "direction": "long",
                    "status": "expired",
                    "status_reason": "signal_timeout",
                    "signal": "opening_range_breakout_long",
                    "bar_time_ms": 1779345600000,
                    "expired_at": "2026-05-21T14:15:27Z",
                    "us_time": "2026-05-21 10:15:00",
                },
                {
                    "signal_id": "sig-meta",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "META",
                    "direction": "long",
                    "status": "protected_active",
                    "us_time": "2026-05-21 10:30:00",
                },
            ],
            "orders": [
                {"id": "entry-aapl", "signal_id": "sig-aapl", "environment": "paper", "symbol": "AAPL", "role": "entry", "status": "Filled", "fill_price": 100, "filled_qty": 10, "us_time": "2026-05-21 09:36:00"},
                {"id": "tp-aapl", "signal_id": "sig-aapl", "environment": "paper", "symbol": "AAPL", "role": "take_profit", "status": "Filled", "fill_price": 110, "filled_qty": 10, "us_time": "2026-05-21 10:10:00"},
                {"id": "entry-tsla", "signal_id": "sig-tsla", "environment": "paper", "symbol": "TSLA", "role": "entry", "status": "Filled", "fill_price": 200, "filled_qty": 3, "us_time": "2026-05-21 09:41:00"},
                {"id": "entry-nflx", "signal_id": "sig-nflx", "environment": "paper", "symbol": "NFLX", "role": "entry", "status": "Closed", "reason": "order_flow_adverse_delta_exit", "us_time": "2026-05-21 10:01:00"},
                {"id": "close-nflx", "environment": "paper", "symbol": "NFLX", "role": "close", "status": "Filled", "us_time": "2026-05-21 10:01:05"},
                {"id": "entry-meta", "signal_id": "sig-meta", "environment": "paper", "symbol": "META", "role": "entry", "status": "Filled", "filled_qty": 5, "us_time": "2026-05-21 10:31:00"},
                {"id": "tp-meta", "signal_id": "sig-meta", "environment": "paper", "symbol": "META", "role": "take_profit", "status": "protected_active", "us_time": "2026-05-21 10:31:05"},
                {"id": "sl-meta", "signal_id": "sig-meta", "environment": "paper", "symbol": "META", "role": "stop_loss", "status": "Submitted", "us_time": "2026-05-21 10:31:05"},
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
        self.assertTrue(payload["coverage"]["target_decisions"]["exists"])
        self.assertEqual(4, payload["coverage"]["target_decisions"]["rows"])
        self.assertEqual("selected", by_symbol["AAPL"]["selection_decision"])
        self.assertTrue(by_symbol["AAPL"]["selection_summary"]["selected"])
        self.assertEqual("selected", by_symbol["AAPL"]["selection_summary"]["decision"])
        self.assertEqual("daily_scan", by_symbol["AAPL"]["selection_summary"]["source"])
        self.assertEqual(91.25, by_symbol["AAPL"]["selection_diagnostics"]["metrics"]["score"])
        self.assertEqual(1, by_symbol["AAPL"]["order_summary"]["entry_filled"])
        self.assertTrue(by_symbol["MSFT"]["not_selected_reasons"])
        msft_reason = by_symbol["MSFT"]["not_selected_reasons"][0]
        self.assertEqual("2026-05-21 08:46:00", msft_reason["created"])
        self.assertEqual(12, msft_reason["rank"])
        self.assertEqual(72.4, msft_reason["metrics"]["score"])
        self.assertEqual(80, msft_reason["thresholds"]["active_min_score"])
        self.assertEqual("daily_scan", msft_reason["source"])
        self.assertEqual("MSFT", msft_reason["rejection_examples"][0]["symbol"])
        amd_signal = by_symbol["AMD"]["signal_summary"]
        self.assertEqual("opening_range_breakout_long", amd_signal["signal"])
        self.assertEqual("expired", amd_signal["latest_status"])
        self.assertEqual("signal_timeout", amd_signal["status_reason"])
        self.assertEqual(1779345600000, amd_signal["bar_time_ms"])
        self.assertEqual("2026-05-21T14:15:27Z", amd_signal["expired_at"])
        self.assertEqual(int(datetime.fromisoformat("2026-05-21T14:15:27+00:00").timestamp() * 1000), amd_signal["expired_at_ms"])
        self.assertEqual("signal_timeout", amd_signal["status_explanation"])
        self.assertEqual("open", by_symbol["TSLA"]["review_status"])
        self.assertEqual("open", by_symbol["TSLA"]["lifecycle_status"])
        self.assertIn("traded", by_symbol["TSLA"]["tab_flags"])
        self.assertIn("problem", by_symbol["TSLA"]["tab_flags"])
        self.assertTrue(by_symbol["TSLA"]["has_problem"])
        self.assertEqual("rejected_signal_has_orders", by_symbol["TSLA"]["issue_flags"][0]["code"])
        self.assertTrue(by_symbol["AAPL"].get("events"))
        self.assertEqual(1, by_symbol["NFLX"]["order_summary"]["entry_filled"])
        self.assertFalse(by_symbol["NFLX"]["issue_flags"])
        nflx_reasons = [event.get("reason") for event in by_symbol["NFLX"].get("events", []) if event.get("role") == "close"]
        self.assertEqual(["order_flow_adverse_delta_exit"], nflx_reasons)
        self.assertEqual("open", by_symbol["META"]["review_status"])
        self.assertEqual(2, by_symbol["META"]["order_summary"]["protection_orders"])
        self.assertEqual(0, by_symbol["META"]["order_summary"]["exit_filled"])
        self.assertFalse(by_symbol["META"]["issue_flags"])
        self.assertEqual("selected", by_symbol["GOOG"]["selection_decision"])
        self.assertEqual("positive target reason", by_symbol["GOOG"]["selection_reason"])
        self.assertEqual("positive target reason", by_symbol["GOOG"]["selection_summary"]["selected_reason"])
        self.assertEqual(["blocked"], [reason["decision"] for reason in by_symbol["GOOG"]["not_selected_reasons"]])
        self.assertIn("active_status_policy_mismatch", {flag["code"] for flag in by_symbol["INTC"]["issue_flags"]})
        self.assertIn("selected", by_symbol["INTC"]["tab_flags"])
        self.assertIn("problem", by_symbol["INTC"]["tab_flags"])
        self.assertGreaterEqual(payload["summary"]["tab_counts"]["traded"], 4)
        self.assertGreaterEqual(payload["summary"]["tab_counts"]["problem"], 2)

    def test_symbol_filter_uses_same_day_orders_only(self):
        records = {
            "orders": [
                {"id": "old-ibm", "environment": "paper", "symbol": "IBM", "role": "entry", "status": "Filled", "filled_qty": 1, "us_time": "2026-05-20 11:00:00"},
                {"id": "day-ibm", "environment": "paper", "symbol": "IBM", "role": "entry", "status": "Filled", "filled_qty": 2, "us_time": "2026-05-21 11:00:00"},
            ],
        }
        payload, status = build_daily_trade_review_response(
            _FakePB(records),
            params={"market_date": "2026-05-21", "broker_mode": "paper", "data_environment": "live", "symbol": "IBM", "include_events": "1"},
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape_filter_string,
            time_strings=lambda: {"date": "2026-05-21"},
        )
        self.assertEqual(200, status)
        self.assertEqual(["IBM"], [item["symbol"] for item in payload["items"]])
        item = payload["items"][0]
        self.assertEqual(1, item["order_summary"]["total"])
        self.assertEqual(["day-ibm"], [event["order_id"] for event in item["events"] if event["type"] == "order"])

    def test_summary_counts_use_full_matched_rows_before_limit(self):
        records = {
            "ibkr_target_decisions": [
                {"environment": "live", "market_date": "2026-05-21", "symbol": "AAA", "decision": "rejected", "reason_text": "no setup"},
                {"environment": "live", "market_date": "2026-05-21", "symbol": "BBB", "decision": "blocked", "reason_text": "blocked gate"},
            ],
        }
        payload, status = build_daily_trade_review_response(
            _FakePB(records),
            params={"market_date": "2026-05-21", "broker_mode": "paper", "data_environment": "live", "limit": "1"},
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape_filter_string,
            time_strings=lambda: {"date": "2026-05-21"},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["summary"]["truncated"])
        self.assertEqual(1, payload["summary"]["returned"])
        self.assertEqual(2, payload["summary"]["matched"])
        self.assertEqual(2, payload["summary"]["tab_counts"]["all"])
        self.assertEqual(2, payload["summary"]["not_selected_count"])
        self.assertEqual(2, payload["summary"]["tab_counts"]["not_selected"])

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
        css = (REPO_ROOT / "runtime" / "ibkr_console" / "static" / "assets" / "css" / "pages" / "ibkr_trade_review" / "page.css").read_text(encoding="utf-8")
        compat = (REPO_ROOT / "runtime" / "ibkr_api" / "src" / "ibkr_api" / "compat" / "routes.py").read_text(encoding="utf-8")
        bridge = (REPO_ROOT / "runtime" / "ibkr_console" / "static" / "assets" / "js" / "shared" / "ui-bridges.js").read_text(encoding="utf-8")
        for token in ("/api/custom/ibkr/analytics/daily-trade-review", "ibkr_trade_review", "生命周期图"):
            self.assertIn(token, html)
        for token in (
            "daily-trade-review",
            "issue_flags",
            "lifecycle_status",
            "tab_flags",
            "lifecycle_url",
            "not_selected_reasons",
            "{ id: 'traded', label: '已交易' }",
            "function isTradedItem",
            "reviewStatusLabel",
            "拒绝/跳过记录",
        ):
            self.assertIn(token, js)
        for token in (
            ".status-pill.traded",
            ".status-pill.closed",
            ".symbol-row.is-traded",
            ".symbol-row.is-closed",
            ".explain-card.secondary",
        ):
            self.assertIn(token, css)
        self.assertIn("ibkr/analytics/daily-trade-review", compat)
        self.assertIn("/ibkr_trade_review.html", bridge)

    def test_active_target_policy_keeps_intraday_context_as_candidate_until_entry(self):
        intraday = {"status": "active", "extra": {"source": "intraday_window_admission", "context_gate_passed": True}}
        manual = {"status": "active", "extra": {"source": "manual_page_add"}}
        stale = {"status": "active", "extra": {"source": "daily_scan", "active_gate_passed": False}}
        self.assertFalse(target_row_is_daily_scan_active(intraday))
        self.assertEqual("candidate", effective_target_status(intraday))
        self.assertEqual("active", effective_target_status(manual))
        self.assertEqual("candidate", effective_target_status(stale))

    def test_coverage_reports_legacy_rejections_when_decisions_are_missing(self):
        records = {
            "__state__": {
                "data": {
                    "result": {
                        "rejection_summary": {"no_snapshot": 2},
                        "rejection_examples": [{"bucket": "no_snapshot", "symbol": "ZZZ"}],
                    }
                }
            }
        }
        payload, status = build_daily_trade_review_response(
            _FakePB(records),
            params={"market_date": "2026-05-21", "broker_mode": "paper", "data_environment": "live"},
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape_filter_string,
            time_strings=lambda: {"date": "2026-05-21"},
        )
        self.assertEqual(200, status)
        coverage = payload["coverage"]
        self.assertFalse(coverage["target_decisions"]["exists"])
        self.assertEqual(0, coverage["target_decisions"]["rows"])
        self.assertTrue(coverage["using_legacy_rejections_only"])
        self.assertTrue(coverage["legacy_rejections"]["summary_exists"])
        self.assertTrue(coverage["legacy_rejections"]["examples_exists"])
        self.assertEqual({"no_snapshot": 2}, coverage["legacy_rejections"]["rejection_summary"])
        self.assertEqual("ZZZ", coverage["legacy_rejections"]["rejection_examples"][0]["symbol"])
        self.assertIn("legacy_rejection_summary_only", {warning["code"] for warning in payload["warnings"]})


if __name__ == "__main__":
    unittest.main()
