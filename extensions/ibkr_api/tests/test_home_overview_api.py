import re
import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

from ibkr_api.home.dashboard import build_home_dashboard_response
from ibkr_api.home.market import build_home_market_response
from ibkr_api.app_core.route_cache import RouteSWRCache, request_cache_bypass, canonical_cache_key


REPO_ROOT = Path(__file__).resolve().parents[3]


class _CountResponse:
    def __init__(self, total):
        self.total = total

    def json(self):
        return {"totalItems": self.total}


class _HomePB:
    base_url = "http://pb.local"

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def _request(self, method, url, params=None, timeout=15):
        collection = re.search(r"/api/collections/([^/]+)/records", url).group(1)
        total = len(self._filtered(collection, (params or {}).get("filter", "")))
        self.calls.append({"type": "count", "collection": collection, "filter": (params or {}).get("filter", "")})
        return _CountResponse(total)

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        self.calls.append({"type": "records", "collection": collection, "filter": filter or "", "per_page": per_page, "page": page})
        rows = self._filtered(collection, filter or "")
        if sort and "-bar_time_ms" in sort:
            rows = sorted(rows, key=lambda row: (int(row.get("bar_time_ms") or 0), str(row.get("created") or "")), reverse=True)
        elif sort == "-created":
            rows = sorted(rows, key=lambda row: str(row.get("created") or ""), reverse=True)
        start = max(0, int(page - 1) * int(per_page))
        return [dict(row) for row in rows[start:start + int(per_page)]]

    def _filtered(self, collection, filter_text):
        rows = list(self.rows.get(collection, []))
        text = filter_text or ""
        return [row for row in rows if self._matches(row, text)]

    def _matches(self, row, text):
        if not text:
            return True
        env_values = re.findall(r'environment = "([^"]*)"', text)
        if env_values and str(row.get("environment", "")) not in env_values:
            return False
        symbol_values = re.findall(r'symbol = "([^"]*)"', text)
        if symbol_values and str(row.get("symbol", "")).upper() not in {item.upper() for item in symbol_values}:
            return False
        if 'symbol_role = "market_monitor"' in text and row.get("symbol_role") != "market_monitor":
            return False
        if 'interval = "1d"' in text and row.get("interval") != "1d":
            return False
        market_time_match = re.search(
            r'\(\(us_time >= "([^"]*)" && us_time <= "([^"]*)"\) \|\| \(bar_time_ms >= (\d+) && bar_time_ms < (\d+)\)\)',
            text,
        )
        if market_time_match:
            us_time = str(row.get("us_time") or "")
            us_time_matches = bool(us_time) and market_time_match.group(1) <= us_time <= market_time_match.group(2)
            bar_time_ms = int(row.get("bar_time_ms") or 0)
            bar_time_matches = int(market_time_match.group(3)) <= bar_time_ms < int(market_time_match.group(4))
            if not us_time_matches and not bar_time_matches:
                return False
        else:
            start_match = re.search(r"bar_time_ms >= (\d+)", text)
            if start_match and int(row.get("bar_time_ms") or 0) < int(start_match.group(1)):
                return False
            end_match = re.search(r"bar_time_ms < (\d+)", text)
            if end_match and int(row.get("bar_time_ms") or 0) >= int(end_match.group(1)):
                return False
        for field in ("direction", "status", "order_type", "source"):
            match = re.search(rf'{field} = "([^"]*)"', text)
            if match and str(row.get(field, "")) != match.group(1):
                return False
        if '(position_side = "long" || direction = "long")' in text:
            if row.get("position_side") != "long" and row.get("direction") != "long":
                return False
        if '(position_side = "short" || direction = "short")' in text:
            if row.get("position_side") != "short" and row.get("direction") != "short":
                return False
        return True


def _runtime_account_request(*, positions=None, status_code=200, error=""):
    def fake_request(method, base_url, path, params=None, timeout=0, **kwargs):
        return {
            "ok": status_code < 400,
            "status_code": status_code,
            "payload": {
                "ok": status_code < 400,
                "environment": "paper",
                "account_id": "DU123",
                "positions": list(positions or []),
                "counts": {"open_positions": len([item for item in (positions or []) if float(item.get("quantity", 0) or 0) != 0])},
                **({"error": error} if error else {}),
            },
            "error": error,
        }

    return fake_request


class HomeOverviewApiTest(unittest.TestCase):
    def test_dashboard_aligns_signal_order_and_gateway_position_counts(self):
        start_ms = 1776916800000  # 2026-04-23 00:00 ET
        rows = {
            "ibkr_signals": [
                {"environment": "live", "direction": "long", "symbol": "AAPL", "bar_time_ms": start_ms + 1, "created": "2026-04-23 09:35:00"},
                {"environment": "live", "direction": "long", "symbol": "AMD", "us_time": "2026-04-23 09:34:00", "bar_time_ms": 0, "created": "2026-04-23 09:34:02"},
                {"environment": "live", "direction": "short", "symbol": "MSFT", "bar_time_ms": start_ms + 2, "created": "2026-04-23 09:36:00"},
                {"environment": "paper", "direction": "long", "symbol": "TSLA", "bar_time_ms": start_ms + 3, "created": "2026-04-23 09:37:00"},
            ],
            "ibkr_reverse_signals": [
                {"environment": "paper", "source": "tradingview", "symbol": "AAPL", "status": "pending", "bar_time_ms": start_ms + 4, "created": "2026-04-23 09:40:00"},
                {"environment": "paper", "source": "tradingview", "symbol": "MSFT", "status": "confirmed", "bar_time_ms": start_ms + 5, "created": "2026-04-23 09:41:00"},
                {"environment": "paper", "source": "indicator", "symbol": "NVDA", "status": "pending", "bar_time_ms": start_ms + 5, "created": "2026-04-23 09:42:00"},
            ],
            "orders": [
                {"environment": "paper", "symbol": "AAPL", "status": "Filled", "order_type": "Entry", "role": "entry", "direction": "long", "position_side": "long", "signal_id": "sig-a", "trade_group_id": "g1", "fill_price": 100, "filled_qty": 10, "commission": 1, "bar_time_ms": start_ms + 6, "created": "2026-04-23 09:45:00"},
                {"environment": "paper", "symbol": "AAPL", "status": "Filled", "order_type": "Entry", "role": "entry", "direction": "long", "position_side": "long", "signal_id": "sig-a", "trade_group_id": "g1", "fill_price": 100, "filled_qty": 5, "commission": 1, "bar_time_ms": start_ms + 6, "created": "2026-04-23 09:45:10"},
                {"environment": "paper", "symbol": "AAPL", "status": "Filled", "order_type": "TakeProfit", "role": "take_profit", "direction": "long", "position_side": "long", "signal_id": "sig-a", "trade_group_id": "g1", "fill_price": 110, "filled_qty": 10, "commission": 1, "bar_time_ms": start_ms + 7, "created": "2026-04-23 10:10:00"},
                {"environment": "paper", "symbol": "MSFT", "status": "Filled", "order_type": "Entry", "role": "entry", "direction": "short", "position_side": "short", "signal_id": "sig-b", "trade_group_id": "g2", "fill_price": 50, "filled_qty": 2, "bar_time_ms": start_ms + 8, "created": "2026-04-23 09:50:00"},
                {"environment": "paper", "symbol": "ORPHAN", "status": "Submitted", "order_type": "StopLoss", "role": "stop_loss", "direction": "short", "position_side": "short", "signal_id": "orphan", "trade_group_id": "orphan", "bar_time_ms": start_ms + 9, "created": "2026-04-23 09:51:00"},
                {"environment": "paper", "symbol": "OLD", "status": "Filled", "order_type": "Entry", "role": "entry", "direction": "long", "position_side": "long", "bar_time_ms": start_ms - 1, "created": "2026-04-22 09:50:00"},
            ],
        }
        payload, status = build_home_dashboard_response(
            _HomePB(rows),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(
                positions=[
                    {"symbol": "AAPL", "quantity": 100},
                    {"symbol": "MSFT", "quantity": -20},
                    {"symbol": "FLAT", "quantity": 0},
                ],
            ),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["signals"], {"long": 2, "short": 1, "total": 3})
        self.assertEqual(payload["summary"]["execution_actions"], {"pending": 1, "total": 2})
        self.assertEqual(payload["summary"]["reverse_signals"]["pending"], 1)
        self.assertEqual(payload["summary"]["orders"], {"long": 1, "short": 1, "total": 2, "entry_order_count": 3})
        self.assertEqual(
            payload["summary"]["positions"],
            {"long": 1, "short": 1, "total": 2, "available": True, "source": "runtime_account", "account_id": "DU123"},
        )
        self.assertAlmostEqual(payload["summary"]["pnl"]["total"], 98.0)
        self.assertEqual(payload["summary"]["pnl"]["win_count"], 1)
        self.assertEqual(payload["recent_activity"][0]["type"], "order")

    def test_dashboard_does_not_fallback_to_historical_entries_for_positions(self):
        start_ms = 1776916800000
        rows = {
            "ibkr_signals": [],
            "ibkr_reverse_signals": [],
            "orders": [
                {"environment": "paper", "symbol": "OLD1", "status": "Filled", "order_type": "Entry", "role": "entry", "direction": "long", "position_side": "long", "bar_time_ms": start_ms - 1, "created": "2026-04-22 09:50:00"},
                {"environment": "paper", "symbol": "OLD2", "status": "Filled", "order_type": "Entry", "role": "entry", "direction": "short", "position_side": "short", "bar_time_ms": start_ms - 2, "created": "2026-04-22 09:55:00"},
            ],
        }

        payload, status = build_home_dashboard_response(
            _HomePB(rows),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(status_code=503, error="runtime offline"),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["positions"]["long"], 0)
        self.assertEqual(payload["summary"]["positions"]["short"], 0)
        self.assertEqual(payload["summary"]["positions"]["total"], 0)
        self.assertFalse(payload["summary"]["positions"]["available"])
        self.assertEqual(payload["summary"]["positions"]["error"], "runtime offline")

    def test_market_payload_merges_config_watchlist_quotes_and_daily_fallback(self):
        rows = {
            "watchlist": [
                {"environment": "global", "symbol": "QQQ", "symbol_role": "market_monitor", "updated": "2026-04-23 08:00:00"},
            ],
            "ibkr_bars": [
                {"environment": "live", "symbol": "SPY", "interval": "1d", "close": 500, "us_time": "2026-04-22 16:00:00", "bar_time_ms": 1776902400000},
                {"environment": "live", "symbol": "QQQ", "interval": "1d", "close": 400, "us_time": "2026-04-22 16:00:00", "bar_time_ms": 1776902400000},
                {"environment": "live", "symbol": "QQQ", "interval": "1d", "close": 390, "us_time": "2026-04-21 16:00:00", "bar_time_ms": 1776816000000},
            ],
        }

        def fake_request(method, base_url, path, params=None, timeout=0, **kwargs):
            return {
                "ok": True,
                "status_code": 200,
                "elapsed_ms": 12.3,
                "payload": {
                    "items": [
                        {"symbol": "SPY", "last_price": 505, "day_change_pct": 1.0, "quote_age_s": 1.2, "last_update": "2026-04-23 10:00:00"},
                        {"symbol": "VIX", "last_price": 18.5, "quote_fallback": True, "data_age_s": 300, "us_time": "2026-04-23 09:55:00"},
                    ]
                },
            }

        payload, status = build_home_market_response(
            _HomePB(rows),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            config_value=lambda key, default, environment: "SPY,VIX",
            request_json_request=fake_request,
            runtime_base_url="http://runtime.local",
            time_strings=lambda: {"date": "2026-04-23"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["symbols"], ["SPY", "VIX", "QQQ"])
        self.assertEqual(payload["realtime_count"], 1)
        self.assertEqual(payload["fallback_count"], 2)
        by_symbol = {item["symbol"]: item for item in payload["items"]}
        self.assertEqual(by_symbol["SPY"]["source_kind"], "quote")
        self.assertEqual(by_symbol["QQQ"]["display_price"], 400)
        self.assertEqual(by_symbol["VIX"]["data_source_text"], "BAR 5m")

    def test_home_route_cache_hits_and_bypasses(self):
        cache = RouteSWRCache("home-test")
        calls = []

        def builder():
            calls.append(len(calls) + 1)
            return {"ok": True, "value": calls[-1]}, 200

        key = canonical_cache_key("home-dashboard", {"a": "1"})
        first, _ = cache.get(key, builder=builder, ttl_seconds=15, stale_seconds=45)
        second, _ = cache.get(key, builder=builder, ttl_seconds=15, stale_seconds=45)
        bypass, _ = cache.get(
            key,
            builder=builder,
            ttl_seconds=15,
            stale_seconds=45,
            force=request_cache_bypass({"cache_bust": "1"}),
        )

        self.assertEqual(first["value"], 1)
        self.assertEqual(second["value"], 1)
        self.assertEqual(bypass["value"], 2)
        self.assertEqual(first["_cache"]["state"], "miss")
        self.assertEqual(second["_cache"]["state"], "hit")
        self.assertEqual(bypass["_cache"]["state"], "bypass")

    def test_home_static_uses_aggregated_endpoints_and_static_cache_headers(self):
        index_html = (REPO_ROOT / "runtime/ibkr_console/static/index.html").read_text(encoding="utf-8")
        caddy = (REPO_ROOT / "ops/templates/caddy/quant.lzw-glory.top.caddy").read_text(encoding="utf-8")

        self.assertIn("/api/custom/ibkr/home-dashboard", index_html)
        self.assertIn("/api/custom/ibkr/home-market", index_html)
        self.assertNotIn("getFullList", index_html)
        self.assertNotIn("pocketbase.umd.min.js", index_html)
        self.assertIn('Cache-Control "no-cache"', caddy)
        self.assertIn('Cache-Control "public, max-age=300, stale-while-revalidate=86400"', caddy)


if __name__ == "__main__":
    unittest.main()
