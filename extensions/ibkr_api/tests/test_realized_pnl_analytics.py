import re
import sys
import types
import unittest
from pathlib import Path


SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = types.SimpleNamespace(args={})
    sys.modules["flask"] = flask_stub


from ibkr_api.analytics.realized_pnl import build_realized_pnl_summary_response
from ibkr_api.analytics.routes import register_analytics_routes
from ibkr_api.compat.routes import NATIVE_CUSTOM_ROUTES


def _normalize_environment(value, default="live"):
    return str(value or default).strip().lower() or default


def _escape_filter_string(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


class _FakePB:
    def __init__(self, records, errors=None):
        self.records = records
        self.errors = set(errors or [])
        self.calls = []

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        self.calls.append((collection, filter, sort, per_page, page))
        if collection in self.errors:
            raise RuntimeError(f"{collection} unavailable")
        rows = list(self.records.get(collection, []))
        env_match = re.search(r'environment = "([^"]+)"', str(filter or ""))
        if env_match:
            rows = [row for row in rows if row.get("environment") == env_match.group(1)]
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


class RealizedPnlAnalyticsTest(unittest.TestCase):
    def _build_response(self, records, params=None):
        pb = _FakePB(records)
        payload, status_code = build_realized_pnl_summary_response(
            pb,
            params={
                "start_date": "2026-05-21",
                "end_date": "2026-05-22",
                "broker_mode": "paper",
                "data_environment": "live",
                **(params or {}),
            },
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape_filter_string,
            time_strings=lambda: {"date": "2026-05-21"},
        )
        self.assertEqual(200, status_code)
        return payload, pb

    def test_execution_fills_aggregate_multi_day_multi_symbol_long_short_net_commissions(self):
        records = {
            "ibkr_execution_fills": [
                {
                    "environment": "paper",
                    "exec_id": "aapl-buy-1",
                    "symbol": "AAPL",
                    "side": "buy",
                    "shares": 10,
                    "price": 100,
                    "commission": 1,
                    "trade_time": "2026-05-21 09:30:00",
                },
                {
                    "environment": "paper",
                    "exec_id": "aapl-sell-1",
                    "symbol": "AAPL",
                    "side": "sell",
                    "shares": 10,
                    "price": 110,
                    "commission": 0.5,
                    "trade_time": "2026-05-21 10:00:00",
                },
                {
                    "environment": "paper",
                    "exec_id": "tsla-short-1",
                    "symbol": "TSLA",
                    "side": "sell",
                    "shares": 5,
                    "price": 200,
                    "commission": 1,
                    "trade_time": "2026-05-21 09:45:00",
                },
                {
                    "environment": "paper",
                    "exec_id": "tsla-cover-1",
                    "symbol": "TSLA",
                    "side": "buy",
                    "shares": 5,
                    "price": 190,
                    "commission": 0.5,
                    "trade_time": "2026-05-21 10:15:00",
                },
                {
                    "environment": "paper",
                    "exec_id": "aapl-buy-2",
                    "symbol": "AAPL",
                    "side": "buy",
                    "shares": 2,
                    "price": 120,
                    "commission": 0.2,
                    "trade_time": "2026-05-22 09:30:00",
                },
                {
                    "environment": "paper",
                    "exec_id": "aapl-sell-2",
                    "symbol": "AAPL",
                    "side": "sell",
                    "shares": 2,
                    "price": 115,
                    "commission": 0.2,
                    "trade_time": "2026-05-22 10:00:00",
                },
            ],
            "orders": [
                {
                    "environment": "paper",
                    "id": "order-should-not-win",
                    "role": "take_profit",
                    "status": "Filled",
                    "symbol": "AAPL",
                    "realized_net_pnl": 999,
                    "us_time": "2026-05-21 10:00:00",
                }
            ],
        }

        payload, pb = self._build_response(records)

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["fallback_used"])
        self.assertEqual("ibkr_execution_fills", payload["pnl_source"])
        self.assertAlmostEqual(136.6, payload["summary"]["realized_net_pnl"], places=2)
        self.assertAlmostEqual(140.0, payload["summary"]["realized_gross_pnl"], places=2)
        self.assertAlmostEqual(3.4, payload["summary"]["commission"], places=2)
        self.assertEqual(3, payload["summary"]["trade_count"])
        self.assertEqual(2, payload["summary"]["win_count"])
        self.assertEqual(1, payload["summary"]["loss_count"])
        self.assertEqual(2, payload["summary"]["long_count"])
        self.assertEqual(1, payload["summary"]["short_count"])

        rows = {(row["date"], row["symbol"]): row for row in payload["rows"]}
        self.assertAlmostEqual(98.5, rows[("2026-05-21", "AAPL")]["realized_net_pnl"], places=2)
        self.assertAlmostEqual(48.5, rows[("2026-05-21", "TSLA")]["realized_net_pnl"], places=2)
        self.assertEqual(1, rows[("2026-05-21", "TSLA")]["short_count"])
        self.assertAlmostEqual(-10.4, rows[("2026-05-22", "AAPL")]["realized_net_pnl"], places=2)

        by_symbol = {row["symbol"]: row for row in payload["by_symbol"]}
        self.assertAlmostEqual(88.1, by_symbol["AAPL"]["realized_net_pnl"], places=2)
        self.assertEqual(payload["by_day"], payload["daily_rows"])
        self.assertEqual(payload["by_symbol"], payload["symbol_rows"])
        self.assertEqual(payload["rows"], payload["day_symbol_rows"])
        self.assertFalse(any(call[0] == "orders" for call in pb.calls), "execution fills should take priority over orders")

    def test_falls_back_to_computable_orders_with_warning_when_fills_empty(self):
        records = {
            "ibkr_execution_fills": [],
            "orders": [
                {
                    "environment": "paper",
                    "id": "entry-msft",
                    "unique_id": "entry-msft",
                    "trade_group_id": "tg-msft",
                    "role": "entry",
                    "status": "Filled",
                    "symbol": "MSFT",
                    "direction": "long",
                    "fill_price": 50,
                    "filled_qty": 5,
                    "commission": 0.5,
                },
                {
                    "environment": "paper",
                    "id": "exit-msft",
                    "trade_group_id": "tg-msft",
                    "entry_order_unique_id": "entry-msft",
                    "role": "take_profit",
                    "status": "Filled",
                    "symbol": "MSFT",
                    "direction": "long",
                    "fill_price": 55,
                    "filled_qty": 5,
                    "commission": 0.25,
                    "us_time": "2026-05-21 10:00:00",
                },
            ],
        }

        payload, _ = self._build_response(records, params={"start_date": "2026-05-21", "end_date": "2026-05-21"})

        self.assertTrue(payload["fallback_used"])
        self.assertEqual("orders", payload["pnl_source"])
        self.assertAlmostEqual(24.25, payload["summary"]["realized_net_pnl"], places=2)
        self.assertEqual(1, payload["summary"]["trade_count"])
        warning_codes = {warning["code"] for warning in payload["warnings"]}
        self.assertIn("execution_fills_empty_using_orders", warning_codes)
        self.assertEqual({}, payload["missing_counts"])

    def test_registers_route_and_lists_compat_custom_route(self):
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

        self.assertIn("custom_ibkr_realized_pnl_summary", exports)
        self.assertIn(("/api/custom/ibkr/analytics/realized-pnl-summary", ("GET",)), app.routes)
        self.assertIn("ibkr/analytics/realized-pnl-summary", NATIVE_CUSTOM_ROUTES)


if __name__ == "__main__":
    unittest.main()
