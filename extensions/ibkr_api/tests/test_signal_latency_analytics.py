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


from ibkr_api.analytics.routes import register_analytics_routes
from ibkr_api.analytics.signal_latency import build_signal_latency_analytics_response


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
        event_type_match = re.search(r'event_type = "([^"]+)"', text)
        if event_type_match:
            rows = [row for row in rows if row.get("event_type") == event_type_match.group(1)]
        date_match = re.search(r'date = "(\d{4}-\d{2}-\d{2})"', text)
        if date_match:
            rows = [row for row in rows if row.get("date") == date_match.group(1)]
        range_match = re.search(r'date >= "(\d{4}-\d{2}-\d{2})" && date <= "(\d{4}-\d{2}-\d{2})"', text)
        if range_match:
            start_token, end_token = range_match.groups()
            rows = [row for row in rows if start_token <= str(row.get("date") or "") <= end_token]
        rows.sort(key=lambda row: str(row.get("created") or ""), reverse=str(sort or "").startswith("-"))
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


class SignalLatencyAnalyticsTest(unittest.TestCase):
    def test_builds_stage_latency_summary_for_entry_events(self):
        base_ms = 1780073160000
        records = {
            "tv_webhook_events": [
                {
                    "id": "event-direct",
                    "event_id": "tv-direct",
                    "event_type": "entry",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "AAPL",
                    "status": "routed",
                    "created": "2026-05-21 13:35:01.100Z",
                    "updated": "2026-05-21 13:35:02.000Z",
                    "extra": {
                        "latency_trace": {
                            "bar_close_to_pine_eval_ms": 1000,
                            "pine_eval_to_api_received_ms": 5000,
                            "api_received_to_pb_created_ms": 100,
                            "pb_created_to_route_finished_ms": 900,
                        }
                    },
                },
                {
                    "id": "event-derived",
                    "event_id": "tv-derived",
                    "event_type": "entry",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "TSLA",
                    "status": "routed",
                    "created": "2026-05-21 13:34:01.100Z",
                    "updated": "2026-05-21 13:34:02.600Z",
                    "extra": {
                        "latency_trace": {
                            "bar_close_ms": base_ms,
                            "pine_eval_ms": base_ms + 700,
                            "api_received_at_ms": base_ms + 1300,
                            "pb_created_at_ms": base_ms + 1400,
                            "route_finished_at_ms": base_ms + 2900,
                        }
                    },
                },
                {
                    "id": "event-missing",
                    "event_id": "tv-missing",
                    "event_type": "entry",
                    "environment": "live",
                    "date": "2026-05-21",
                    "symbol": "MSFT",
                    "created": "2026-05-21 13:33:00.000Z",
                    "updated": "2026-05-21 13:33:00.000Z",
                    "extra": {},
                },
                {
                    "event_id": "tv-pre-alert",
                    "event_type": "pre_alert",
                    "environment": "live",
                    "date": "2026-05-21",
                    "extra": {"latency_trace": {"bar_close_to_pine_eval_ms": 999999}},
                },
                {
                    "event_id": "tv-paper",
                    "event_type": "entry",
                    "environment": "paper",
                    "date": "2026-05-21",
                    "extra": {"latency_trace": {"bar_close_to_pine_eval_ms": 999999}},
                },
                {
                    "event_id": "tv-outside",
                    "event_type": "entry",
                    "environment": "live",
                    "date": "2026-05-22",
                    "extra": {"latency_trace": {"bar_close_to_pine_eval_ms": 999999}},
                },
            ],
        }

        payload, status_code = build_signal_latency_analytics_response(
            _FakePB(records),
            params={"date": "2026-05-21", "data_environment": "live"},
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape_filter_string,
            time_strings=lambda: {"date": "2026-05-21"},
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(3, payload["total_events"])
        self.assertEqual(2, payload["complete_events"])
        self.assertEqual(1, payload["missing_trace_count"])
        rows = {row["key"]: row for row in payload["stage_rows"]}
        self.assertEqual(2, rows["bar_close_to_pine_eval_ms"]["count"])
        self.assertEqual(1, rows["bar_close_to_pine_eval_ms"]["missing"])
        self.assertAlmostEqual(850.0, rows["bar_close_to_pine_eval_ms"]["avg_ms"], places=2)
        self.assertEqual(850, rows["bar_close_to_pine_eval_ms"]["p50_ms"])
        self.assertEqual(1000, rows["bar_close_to_pine_eval_ms"]["p95_ms"])
        self.assertEqual(5000, rows["pine_eval_to_api_received_ms"]["p95_ms"])
        self.assertEqual("pine_eval_to_api_received_ms", payload["slowest_stage"]["key"])
        self.assertEqual("tv-direct", payload["latest_event"]["event_id"])
        self.assertTrue(payload["latest_event"]["complete"])

    def test_builds_range_filter_and_rejects_invalid_dates(self):
        records = {
            "tv_webhook_events": [
                {
                    "event_id": "day-1",
                    "event_type": "entry",
                    "environment": "live",
                    "date": "2026-05-20",
                    "extra": {"latency_trace": {"bar_close_to_pine_eval_ms": 1000}},
                },
                {
                    "event_id": "day-2",
                    "event_type": "entry",
                    "environment": "live",
                    "date": "2026-05-21",
                    "extra": {"latency_trace": {"bar_close_to_pine_eval_ms": 2000}},
                },
                {
                    "event_id": "outside",
                    "event_type": "entry",
                    "environment": "live",
                    "date": "2026-05-22",
                    "extra": {"latency_trace": {"bar_close_to_pine_eval_ms": 3000}},
                },
            ],
        }
        payload, status_code = build_signal_latency_analytics_response(
            _FakePB(records),
            params={"start_date": "2026-05-20", "end_date": "2026-05-21"},
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape_filter_string,
            time_strings=lambda: {"date": "2026-05-21"},
        )

        self.assertEqual(200, status_code)
        self.assertEqual(2, payload["total_events"])
        self.assertEqual("2026-05-20 ~ 2026-05-21", payload["range_label"])

        invalid_payload, invalid_status = build_signal_latency_analytics_response(
            _FakePB(records),
            params={"start_date": "2026-05-22", "end_date": "2026-05-21"},
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape_filter_string,
            time_strings=lambda: {"date": "2026-05-21"},
        )
        self.assertEqual(400, invalid_status)
        self.assertFalse(invalid_payload["ok"])
        self.assertEqual("invalid_date_range", invalid_payload["error"])

    def test_registers_route_and_console_wiring(self):
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

        self.assertIn("custom_ibkr_signal_latency_analytics", exports)
        self.assertIn(("/api/custom/ibkr/analytics/signal-latency", ("GET",)), app.routes)

        stats_html = (REPO_ROOT / "runtime" / "ibkr_console" / "static" / "ibkr_stats.html").read_text(encoding="utf-8")
        stats_css = (
            REPO_ROOT / "runtime" / "ibkr_console" / "static" / "assets" / "css" / "pages" / "ibkr_stats" / "page.css"
        ).read_text(encoding="utf-8")
        compat_routes = (REPO_ROOT / "runtime" / "ibkr_api" / "src" / "ibkr_api" / "compat" / "routes.py").read_text(
            encoding="utf-8"
        )

        for token in (
            "signal-latency-analysis",
            "/api/custom/ibkr/analytics/signal-latency",
            "signalLatencyChart",
            "loadSignalLatencyAnalysis",
            "renderSignalLatencyAnalysis",
        ):
            self.assertIn(token, stats_html)
        self.assertIn("signal-latency-panel", stats_css)
        self.assertIn("ibkr/analytics/signal-latency", compat_routes)


if __name__ == "__main__":
    unittest.main()
