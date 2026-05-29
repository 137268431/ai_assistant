import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
COMPUTE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
for src_root in (SERVICE_SRC_ROOT, COMPUTE_SRC_ROOT):
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))


class _FakeApp:
    def route(self, _path, methods=None):
        def decorator(func):
            return func

        return decorator


sys.modules.setdefault(
    "flask",
    SimpleNamespace(
        Flask=lambda name: _FakeApp(),
        Response=object,
        jsonify=lambda payload: payload,
        request=SimpleNamespace(args={}, get_json=lambda silent=True: {}),
    ),
)


from ibkr_api.tradingview.ingest import (
    normalize_risk_reward_value,
    upsert_tv_indicator,
    upsert_tv_indicator_audit,
    upsert_tv_signal,
)
from ibkr_api.tradingview import routes as tv_routes


class _FakePB:
    def __init__(self, existing=None):
        self.existing = copy.deepcopy(existing)
        self.created = []
        self.lookup_filters = []

    def get_first_record(self, collection, filter=None, sort=None):
        self.lookup_filters.append((collection, str(filter or "")))
        return copy.deepcopy(self.existing)

    def create_record(self, collection, data):
        row = copy.deepcopy(data)
        row["id"] = f"{collection}-{len(self.created) + 1}"
        self.created.append((collection, row))
        return copy.deepcopy(row)


class TradingViewIngestTest(unittest.TestCase):
    def setUp(self):
        self.normalize_environment = lambda value, default: str(value or default).strip().lower() or default
        self.escape_filter_string = lambda value: str(value or "").replace("\\", "\\\\").replace('"', '\\"')
        self.time_strings = lambda: {"date": "2026-04-23"}

    def test_normalize_risk_reward_value_falls_back_to_price_ratio(self):
        value = normalize_risk_reward_value("", 100, 95, 110)

        self.assertEqual(value, "2.00")

    def test_upsert_tv_indicator_maps_aliases_and_creates_record(self):
        pb = _FakePB()

        response = upsert_tv_indicator(
            {
                "symbol": "aapl",
                "interval": "5m",
                "bar_time_ms": "1713859200000",
                "environment": "paper",
                "extra": '{"dayChangePct":"1.5","bar_index":"7","exchange":"nasdaq"}',
            },
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            jsonify_fn=lambda payload: payload,
        )

        self.assertEqual(response["ok"], True)
        self.assertEqual(len(pb.created), 1)
        collection, row = pb.created[0]
        self.assertEqual(collection, "tv_indicators")
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["environment"], "live")
        self.assertEqual(row["bar_index"], 7)
        self.assertEqual(row["extra"]["dayChangePct"], 1.5)
        self.assertEqual(row["extra"]["day_change_pct"], 1.5)
        self.assertEqual(row["extra"]["source"], "tradingview")

    def test_upsert_tv_indicator_skips_duplicates(self):
        pb = _FakePB(existing={"id": "tv-ind-1"})

        response = upsert_tv_indicator(
            {
                "symbol": "AAPL",
                "interval": "5m",
                "bar_time_ms": 1713859200000,
            },
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            jsonify_fn=lambda payload: payload,
        )

        self.assertEqual(response["msg"], "duplicate indicator, skipped")
        self.assertEqual(pb.created, [])

    def test_upsert_tv_indicator_audit_writes_snapshot_collection(self):
        pb = _FakePB()

        response = upsert_tv_indicator_audit(
            {
                "type": "audit_indicator",
                "symbol": "msft",
                "interval": "15",
                "bar_time_ms": "1713859200000",
                "bar_index": "11",
                "script_tag": 'IAC "audit"',
                "environment": "backtest",
                "extra": {"dayChangePct": "2.5", "close": "421.12"},
                "audit_reason": "snapshot",
            },
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            jsonify_fn=lambda payload: payload,
        )

        self.assertEqual(response["ok"], True)
        self.assertEqual(response["type"], "indicator_audit")
        self.assertEqual(len(pb.created), 1)
        collection, row = pb.created[0]
        self.assertEqual(collection, "tv_indicator_audit_snapshots")
        self.assertEqual(row["symbol"], "MSFT")
        self.assertEqual(row["environment"], "backtest")
        self.assertEqual(row["script_tag"], 'IAC "audit"')
        self.assertEqual(row["bar_index"], 11)
        self.assertEqual(row["extra"]["day_change_pct"], 2.5)
        self.assertEqual(row["extra"]["audit_reason"], "snapshot")
        self.assertEqual(row["extra"]["source"], "tradingview")

    def test_upsert_tv_indicator_audit_dedup_includes_script_tag(self):
        pb = _FakePB(existing={"id": "audit-1"})

        response = upsert_tv_indicator_audit(
            {
                "symbol": "MSFT",
                "interval": "15",
                "bar_time_ms": 1713859200000,
                "script_tag": 'IAC "audit"',
                "environment": "backtest",
            },
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            jsonify_fn=lambda payload: payload,
        )

        self.assertEqual(response["type"], "indicator_audit")
        self.assertEqual(response["msg"], "duplicate indicator_audit, skipped")
        self.assertEqual(pb.created, [])
        collection, filter_expr = pb.lookup_filters[0]
        self.assertEqual(collection, "tv_indicator_audit_snapshots")
        self.assertIn('symbol = "MSFT"', filter_expr)
        self.assertIn('interval = "15"', filter_expr)
        self.assertIn("bar_time_ms = 1713859200000", filter_expr)
        self.assertIn('environment = "backtest"', filter_expr)
        self.assertIn('script_tag = "IAC \\"audit\\""', filter_expr)

    def test_webhook_tv_routes_to_tv_primary_processor(self):
        calls = []
        handlers = tv_routes.register_tradingview_routes(
            _FakeApp(),
            deps={
                "process_tv_primary_event": lambda payload, **_kwargs: calls.append(payload) or ({"ok": True, "type": "entry"}, 200),
                "config_value": lambda key, default, environment: "TRUE",
                "parse_boolean": lambda value, default: default if value is None else str(value).upper() == "TRUE",
            },
        )

        request = SimpleNamespace(get_json=lambda silent=True: {"event_type": "entry", "symbol": "MSFT"})
        with mock.patch.object(tv_routes, "request", request):
            response = handlers["webhook_tv"]()

        self.assertEqual(response["type"], "entry")
        self.assertEqual(calls, [{"event_type": "entry", "symbol": "MSFT"}])

    def test_webhook_tv_skips_when_ingest_disabled(self):
        calls = []

        def config_value(key, default, environment):
            values = {
                "tv_webhook_ingest_enabled": "FALSE",
            }
            return values.get(key, default)

        handlers = tv_routes.register_tradingview_routes(
            _FakeApp(),
            deps={
                "process_tv_primary_event": lambda payload, **_kwargs: calls.append(payload) or ({"ok": True, "type": "entry"}, 200),
                "config_value": config_value,
                "parse_boolean": lambda value, default: default if value is None else str(value).upper() == "TRUE",
            },
        )

        request = SimpleNamespace(get_json=lambda silent=True: {"event_type": "entry", "symbol": "SPY"})
        with mock.patch.object(tv_routes, "request", request):
            response = handlers["webhook_tv"]()

        self.assertEqual(response["skipped"], True)
        self.assertEqual(response["reason"], "tv_webhook_ingest_enabled=false")
        self.assertEqual(calls, [])

    def test_webhook_tv_returns_non_200_status_tuple(self):
        handlers = tv_routes.register_tradingview_routes(
            _FakeApp(),
            deps={
                "process_tv_primary_event": lambda payload, **_kwargs: ({"ok": False, "error": "bad"}, 400),
                "config_value": lambda key, default, environment: "TRUE",
                "parse_boolean": lambda value, default: default if value is None else str(value).upper() == "TRUE",
            },
        )

        request = SimpleNamespace(get_json=lambda silent=True: {"event_type": "bad", "symbol": "SPY"})
        with mock.patch.object(tv_routes, "request", request):
            response = handlers["webhook_tv"]()

        self.assertEqual(response[0]["error"], "bad")
        self.assertEqual(response[1], 400)

    def test_upsert_tv_signal_creates_record_with_rr_and_date(self):
        pb = _FakePB()

        response = upsert_tv_signal(
            {
                "symbol": "AAPL",
                "direction": "Long",
                "entry": 100,
                "stop_loss": 95,
                "take_profit": 110,
                "signal_id": "sig-1",
                "us_time": "2026-04-23 09:35:00",
                "extra": {"reason": "breakout", "bar_time_ms": 1713859200000, "chart_tf": "5m"},
            },
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            jsonify_fn=lambda payload: payload,
            time_strings=self.time_strings,
        )

        self.assertEqual(response["created_environments"], ["live"])
        self.assertEqual(len(pb.created), 1)
        collection, row = pb.created[0]
        self.assertEqual(collection, "tv_signals")
        self.assertEqual(row["rr"], "2.00")
        self.assertEqual(row["date"], "2026-04-23")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["extra"]["environment"], "live")
        self.assertEqual(row["extra"]["source"], "tradingview")

    def test_upsert_tv_signal_rejects_invalid_direction(self):
        pb = _FakePB()

        response, status_code = upsert_tv_signal(
            {
                "symbol": "AAPL",
                "direction": "sideways",
                "entry": 100,
                "stop_loss": 95,
                "take_profit": 110,
                "signal_id": "sig-1",
            },
            pb=pb,
            normalize_environment=self.normalize_environment,
            escape_filter_string=self.escape_filter_string,
            jsonify_fn=lambda payload: payload,
            time_strings=self.time_strings,
        )

        self.assertEqual(status_code, 400)
        self.assertEqual(response["error"], "Invalid direction: must be 'long' or 'short'")


if __name__ == "__main__":
    unittest.main()
