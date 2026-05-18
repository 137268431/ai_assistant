import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

from ibkr_api.orders.cancel_sync import build_order_cancel_sync_response
from ibkr_api.universe.fundamentals import build_fundamentals_list_response, build_fundamentals_refresh_response
from ibkr_api.universe.screener import build_screener_proxy_response
from ibkr_api.universe.today_targets import build_today_targets_response
from ibkr_api.universe.targets import build_screener_targets_upsert_response, build_target_upsert_response
from ibkr_api.universe.watchlist import build_watchlist_eligibility_response, build_watchlist_upsert_response
from ibkr_api.universe.watchlist_eligibility import _build_duplicate_warning, _load_duplicate_context
from ibkr_compute.market.timeframe_utils import ET


class _MinimalPB:
    def __init__(self):
        self.created = []
        self.updated = []
        self.deleted = []
        self._records = {}
        self._all_records = {}
        self._states = {}

    def get_first_record(self, collection, filter=None, sort=None):
        return None

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        return list(self._records.get(collection, []))

    def get_all_records(self, collection, filter=None, sort=None, max_pages=10):
        return list(self._all_records.get(collection, []))

    def get_state(self, state_key, environment, date="global"):
        return self._states.get((state_key, environment, date))

    def create_record(self, collection, data):
        record = {"id": f"{collection}-1", **data}
        self.created.append((collection, data))
        return record

    def update_record(self, collection, record_id, data):
        record = {"id": record_id, **data}
        self.updated.append((collection, record_id, data))
        return record

    def delete_record(self, collection, record_id):
        self.deleted.append((collection, record_id))
        return True


class UniverseRoutesTest(unittest.TestCase):
    def test_screener_proxy_forwards_internal_compute_request(self):
        payload, status_code = build_screener_proxy_response(
            payload={"environment": "live", "market_date": "2026-04-23", "symbols": "aapl,msft", "limit": "12"},
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            request_json_request=lambda method, base_url, path, params=None, json_body=None, timeout=5.0: {
                "status_code": 200,
                "payload": {"ok": True, "items": [{"symbol": "AAPL"}]},
                "target_url": f"{base_url.rstrip('/')}{path}",
                "params": params,
            },
            compute_base_url="http://127.0.0.1:5100",
        )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("http://127.0.0.1:5100/screener", payload["proxy_upstream"])
        self.assertEqual("ibkr-api", payload["source"])

    def test_watchlist_upsert_creates_record_and_calls_reconcile(self):
        pb = _MinimalPB()
        payload, status_code = build_watchlist_upsert_response(
            pb,
            payload={"symbol": "aapl", "environment": "live", "exchange": "nasdaq"},
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            time_strings=lambda: {"us": "2026-04-23 09:30:00", "cn": "2026-04-23 21:30:00", "date": "2026-04-23"},
            request_json_request=lambda *args, **kwargs: {
                "status_code": 200,
                "payload": {"ok": True, "queued": ["AAPL"]},
                "target_url": "http://127.0.0.1:5100/ibkr/universe/reconcile",
            },
            compute_base_url="http://127.0.0.1:5100",
        )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("created", payload["action"])
        self.assertEqual("AAPL", payload["symbol"])
        self.assertEqual("AAPL", payload["runtime_reconcile"]["queued"][0])
        self.assertEqual("NASDAQ", pb.created[0][1]["exchange"])

    def test_watchlist_eligibility_warns_for_duplicate_and_weak_metrics(self):
        pb = _MinimalPB()
        pb._records["watchlist"] = [
            {"symbol": "GOOGL", "environment": "global", "symbol_role": "trade"},
        ]

        screener_rows = {
            "2026-05-08": {
                "items": [
                    {
                        "symbol": "GOOG",
                        "has_live_bar": True,
                        "price": 120,
                        "avg_10d_volume": 80_000,
                        "premarket_volume": 200,
                        "atr_pct": 0.2,
                        "day_change_pct": 2.0,
                    }
                ]
            },
            "2026-05-07": {
                "items": [
                    {
                        "symbol": "GOOG",
                        "has_live_bar": True,
                        "price": 119,
                        "avg_10d_volume": 90_000,
                        "premarket_volume": 100,
                        "atr_pct": 0.1,
                        "day_change_pct": 0.2,
                    }
                ]
            },
            "2026-05-06": {
                "items": [
                    {
                        "symbol": "GOOG",
                        "has_live_bar": True,
                        "price": 118,
                        "avg_10d_volume": 95_000,
                        "premarket_volume": 150,
                        "atr_pct": 0.1,
                        "day_change_pct": 0.3,
                    }
                ]
            },
        }

        def fake_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            market_date = dict(params or {}).get("market_date")
            return {
                "status_code": 200,
                "payload": {"ok": True, **screener_rows.get(market_date, {"items": []})},
                "target_url": f"{base_url.rstrip('/')}{path}",
            }

        payload, status_code = build_watchlist_eligibility_response(
            pb,
            payload={
                "environment": "live",
                "symbols": ["GOOG"],
                "window_trading_days": 3,
                "market_dates": ["2026-05-08", "2026-05-07", "2026-05-06"],
            },
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            request_json_request=fake_request,
            compute_base_url="http://127.0.0.1:5100",
        )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        item = payload["items"][0]
        self.assertEqual("warn", item["status"])
        self.assertEqual("market_monitor", item["recommendation"])
        self.assertTrue(item["duplicate"]["duplicate"])
        self.assertEqual("GOOGL", item["duplicate"]["canonical_symbol"])
        self.assertEqual(0, item["pass_days"])
        gate_reasons = item.get("failed_gates") or item["fail_reasons"]
        gate_buckets = {reason["bucket"] for reason in gate_reasons}
        self.assertIn("avg_10d_volume_below_threshold", gate_buckets)
        self.assertIn("premarket_volume_below_threshold", gate_buckets)
        self.assertIn("atr_pct_below_threshold", gate_buckets)
        self.assertIn("day_change_pct_below_threshold", gate_buckets)
        self.assertEqual(100000, item["thresholds"]["avg_10d_volume_gte"])
        self.assertEqual(5000, item["thresholds"]["premarket_volume_gte"])
        self.assertEqual("2026-05-08", item["latest_metrics"]["market_date"])
        self.assertEqual(80_000, item["latest_metrics"]["avg_10d_volume"])
        self.assertEqual(1, payload["summary"]["warn"])

    def test_watchlist_eligibility_uses_canonical_for_fox_and_xyz_groups(self):
        fox_duplicate = _build_duplicate_warning("FOX", {"FOXA": {"symbol": "FOXA"}})
        self.assertTrue(fox_duplicate["duplicate"])
        self.assertEqual("FOXA", fox_duplicate["canonical_symbol"])
        self.assertTrue(fox_duplicate["non_canonical"])
        self.assertEqual(["FOXA"], fox_duplicate["existing_trade_symbols"])

        xyz_duplicate = _build_duplicate_warning("SQ", {})
        self.assertFalse(xyz_duplicate["duplicate"])
        self.assertEqual("XYZ", xyz_duplicate["canonical_symbol"])
        self.assertTrue(xyz_duplicate["non_canonical"])
        self.assertIn("canonical XYZ", xyz_duplicate["message"])

    def test_watchlist_duplicate_context_normalizes_class_share_separators_only(self):
        class FilteringPB(_MinimalPB):
            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                rows = super().get_records(collection, filter=filter, sort=sort, per_page=per_page, page=page)
                return [row for row in rows if str(row.get("symbol") or "") in str(filter or "")]

        pb = FilteringPB()
        pb._records["watchlist"] = [
            {"symbol": "BRK-B", "environment": "global", "symbol_role": "trade"},
            {"symbol": "BABA", "environment": "global", "symbol_role": "trade"},
        ]
        context = _load_duplicate_context(
            pb,
            environment="live",
            symbols=["BRK.A", "JD"],
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
        )
        self.assertIn("BRK.B", context)
        self.assertNotIn("BABA", context)

        brk_duplicate = _build_duplicate_warning("BRK.A", context)
        self.assertTrue(brk_duplicate["duplicate"])
        self.assertEqual("BRK.B", brk_duplicate["canonical_symbol"])
        self.assertEqual(["BRK-B"], brk_duplicate["existing_trade_symbols"])

        adr_duplicate = _build_duplicate_warning("JD", context)
        self.assertFalse(adr_duplicate["duplicate"])
        self.assertNotIn("group", adr_duplicate)

    def test_watchlist_upsert_returns_trade_eligibility_warning_payload(self):
        pb = _MinimalPB()
        pb._records["ibkr_bars"] = [
            {"symbol": "MSFT", "environment": "live", "interval": "5m", "us_time": "2026-05-08 16:00:00", "bar_time_ms": 3},
            {"symbol": "MSFT", "environment": "live", "interval": "5m", "us_time": "2026-05-07 16:00:00", "bar_time_ms": 2},
            {"symbol": "MSFT", "environment": "live", "interval": "5m", "us_time": "2026-05-06 16:00:00", "bar_time_ms": 1},
        ]

        def fake_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            if path == "/screener":
                market_date = dict(params or {}).get("market_date")
                return {
                    "status_code": 200,
                    "payload": {
                        "ok": True,
                        "items": [
                            {
                                "symbol": "MSFT",
                                "has_live_bar": True,
                                "price": 18,
                                "avg_10d_volume": 10_000,
                                "premarket_volume": 200,
                                "atr_pct": 0.05,
                                "day_change_pct": 0.1,
                                "market_date": market_date,
                            }
                        ],
                    },
                    "target_url": f"{base_url.rstrip('/')}{path}",
                }
            return {
                "status_code": 200,
                "payload": {"ok": True, "queued": ["MSFT"]},
                "target_url": f"{base_url.rstrip('/')}{path}",
            }

        payload, status_code = build_watchlist_upsert_response(
            pb,
            payload={
                "symbol": "msft",
                "environment": "live",
                "source": "manual_page_add",
                "symbol_role": "trade",
                "exchange": "nasdaq",
                "window_trading_days": 3,
            },
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            time_strings=lambda: {"us": "2026-05-08 09:30:00", "cn": "2026-05-08 21:30:00", "date": "2026-05-08"},
            request_json_request=fake_request,
            compute_base_url="http://127.0.0.1:5100",
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("MSFT", payload["symbol"])
        self.assertEqual("warn", payload["eligibility"]["status"])
        self.assertEqual("market_monitor", payload["eligibility"]["recommendation"])
        self.assertIn("不建议", payload["warning"])
        eligibility_gates = payload["eligibility"].get("failed_gates") or payload["eligibility"]["fail_reasons"]
        self.assertIn("avg_10d_volume_below_threshold", {reason["bucket"] for reason in eligibility_gates})
        self.assertEqual("MSFT", pb.created[0][1]["symbol"])

    def test_fundamentals_refresh_requires_finnhub_api_key(self):
        pb = _MinimalPB()
        payload, status_code = build_fundamentals_refresh_response(
            pb,
            payload={"symbols": ["AMZN"]},
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            environ={},
        )
        self.assertEqual(503, status_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("finnhub_api_key_missing", payload["error"])
        self.assertEqual([], pb.created)

    def test_fundamentals_refresh_skips_market_context_without_api_key(self):
        pb = _MinimalPB()
        calls = []
        payload, status_code = build_fundamentals_refresh_response(
            pb,
            payload={"symbols": ["QQQ", "SPY", "VIX"]},
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            http_get=lambda *args, **kwargs: calls.append((args, kwargs)),
            environ={},
        )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("only_market_context_symbols", payload["reason"])
        self.assertEqual(3, payload["requested"])
        self.assertEqual(0, payload["eligible_symbols"])
        self.assertEqual(3, payload["skipped"])
        self.assertEqual(["QQQ", "SPY", "VIX"], payload["skipped_market_context_symbols"])
        self.assertEqual([], calls)
        self.assertEqual([], pb.created)

    def test_fundamentals_refresh_fetches_only_trade_symbols_by_default(self):
        pb = _MinimalPB()
        calls = []

        class FakeResponse:
            status_code = 200
            content = True

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        def fake_get(url, params=None, timeout=10.0):
            calls.append((url, dict(params or {}), timeout))
            self.assertEqual("AAPL", dict(params or {}).get("symbol"))
            if url.endswith("/stock/profile2"):
                return FakeResponse(
                    {
                        "ticker": "AAPL",
                        "name": "Apple Inc",
                        "exchange": "NASDAQ",
                        "finnhubIndustry": "Technology",
                        "currency": "USD",
                        "marketCapitalization": 2_900_000,
                        "shareOutstanding": 15_000,
                    }
                )
            return FakeResponse({"symbol": "AAPL", "metricType": "all", "metric": {"beta": 1.2}})

        payload, status_code = build_fundamentals_refresh_response(
            pb,
            payload={"symbols": ["AAPL", "SPY", "VIX"]},
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            http_get=fake_get,
            environ={"FINNHUB_API_KEY": "test-key"},
        )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(3, payload["requested"])
        self.assertEqual(1, payload["eligible_symbols"])
        self.assertEqual(2, payload["skipped"])
        self.assertEqual(["SPY", "VIX"], payload["skipped_market_context_symbols"])
        self.assertEqual(["AAPL", "AAPL"], [call[1]["symbol"] for call in calls])
        self.assertEqual(1, len(pb.created))
        self.assertEqual("AAPL", pb.created[0][1]["symbol"])

    def test_fundamentals_refresh_caches_redacted_finnhub_profile(self):
        pb = _MinimalPB()

        class FakeResponse:
            status_code = 200
            content = True

            def json(self):
                return {
                    "ticker": "AMZN",
                    "name": "Amazon.com Inc",
                    "exchange": "NASDAQ",
                    "finnhubIndustry": "Internet Retail",
                    "currency": "USD",
                    "marketCapitalization": 2_300_000,
                    "shareOutstanding": 10_300,
                    "token": "should-not-leak",
                }

        payload, status_code = build_fundamentals_refresh_response(
            pb,
            payload={"symbols": ["AMZN"], "include_raw": True},
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            http_get=lambda *args, **kwargs: FakeResponse(),
            environ={"FINNHUB_API_KEY": "test-key"},
        )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["refreshed"])
        item = payload["items"][0]
        self.assertEqual("AMZN", item["symbol"])
        self.assertEqual("mega_cap", item["profile"])
        self.assertEqual("finnhub", item["extra"]["provider"])
        self.assertEqual("stock/profile2", item["extra"]["source"])
        self.assertEqual("marketCapitalization_millions", item["extra"]["market_cap_source"])
        self.assertEqual("***", item["raw_profile"]["token"])
        self.assertNotIn("test-key", str(payload))
        self.assertEqual("finnhub_profile2_v1", pb.created[0][1]["extra"]["provider_payload_version"])

        pb._records["ibkr_fundamentals"] = [{**pb.created[0][1], "extra": "legacy-extra"}]
        list_payload, list_status = build_fundamentals_list_response(
            pb,
            payload={"symbols": ["AMZN"]},
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
        )
        self.assertEqual(200, list_status)
        self.assertEqual(1, list_payload["count"])
        self.assertEqual({}, list_payload["items"][0]["extra"])
        self.assertNotIn("raw_profile", list_payload["items"][0])

    def test_fundamentals_refresh_caches_finnhub_metric_fields(self):
        pb = _MinimalPB()
        calls = []

        class FakeResponse:
            status_code = 200
            content = True

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        def fake_get(url, params=None, timeout=10.0):
            calls.append((url, dict(params or {}), timeout))
            if url.endswith("/stock/profile2"):
                return FakeResponse(
                    {
                        "ticker": "MSFT",
                        "name": "Microsoft Corp",
                        "exchange": "NASDAQ",
                        "finnhubIndustry": "Technology",
                        "currency": "USD",
                        "marketCapitalization": 3_100_000,
                        "shareOutstanding": 7_400,
                    }
                )
            return FakeResponse(
                {
                    "symbol": "MSFT",
                    "metricType": "all",
                    "metric": {
                        "beta": 0.91,
                        "10DayAverageTradingVolume": 31.2,
                        "3MonthAverageTradingVolume": 24.5,
                        "52WeekHigh": 468.35,
                        "52WeekLow": 344.79,
                        "floatShares": 7_390_000_000,
                        "shortInterest": 65_000_000,
                        "shortPercentOfFloat": 0.88,
                    },
                }
            )

        payload, status_code = build_fundamentals_refresh_response(
            pb,
            payload={"symbols": ["MSFT"], "timeout_sec": 2},
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            http_get=fake_get,
            environ={"FINNHUB_API_KEY": "test-key"},
        )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(2, len(calls))
        self.assertEqual(["MSFT", "MSFT"], [call[1]["symbol"] for call in calls])
        self.assertEqual(["stock/profile2", "stock/metric"], [call[0].rsplit("/api/v1/", 1)[1] for call in calls])
        self.assertEqual(["test-key", "test-key"], [call[1]["token"] for call in calls])

        extra = payload["items"][0]["extra"]
        self.assertEqual("stock/profile2", extra["source"])
        self.assertEqual("stock/metric", extra["metrics_source"])
        self.assertEqual("finnhub_basic_financials_metric_all_v1", extra["metrics_provider_payload_version"])
        self.assertEqual("ok", extra["metric_status"])
        self.assertEqual(0.91, extra["beta"])
        self.assertEqual(31.2, extra["avg_volume_10d_provider"])
        self.assertEqual(24.5, extra["avg_volume_3m_provider"])
        self.assertEqual(468.35, extra["fifty_two_week_high"])
        self.assertEqual(344.79, extra["fifty_two_week_low"])
        self.assertEqual(7_390_000_000, extra["float_shares_provider"])
        self.assertEqual(65_000_000, extra["short_interest_provider"])
        self.assertEqual(8, extra["metric_field_count"])
        self.assertIn("beta", extra["metric_keys_sample"])
        self.assertNotIn("metric", pb.created[0][1]["raw_profile"])
        self.assertNotIn("test-key", str(payload))

    def test_target_upsert_rejects_manual_non_current_market_date(self):
        pb = _MinimalPB()
        payload, status_code = build_target_upsert_response(
            pb,
            payload={"symbol": "AAPL", "date": "2026-04-22", "environment": "live", "source": "manual_page_add"},
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            time_strings=lambda: {"us": "2026-04-23 09:30:00", "cn": "2026-04-23 21:30:00", "date": "2026-04-23"},
            request_json_request=lambda *args, **kwargs: {
                "status_code": 200,
                "payload": {"market_date": "2026-04-23"},
                "target_url": "http://127.0.0.1:5100/ibkr/status",
            },
            compute_base_url="http://127.0.0.1:5100",
        )
        self.assertEqual(400, status_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("manual_target_only_current_market_date", payload["error"])
        self.assertEqual("2026-04-23", payload["current_market_date"])

    def test_target_upsert_rejects_market_context_symbol_as_trade_target(self):
        pb = _MinimalPB()
        pb._records["watchlist"] = [
            {"symbol": "SPY", "environment": "global", "symbol_role": "market_monitor"},
        ]
        reconcile_calls = []

        payload, status_code = build_target_upsert_response(
            pb,
            payload={"symbol": "SPY", "date": "2026-04-23", "environment": "live", "status": "active"},
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
            time_strings=lambda: {"us": "2026-04-23 09:30:00", "cn": "2026-04-23 21:30:00", "date": "2026-04-23"},
            request_json_request=lambda *args, **kwargs: reconcile_calls.append((args, kwargs))
            or {
                "status_code": 200,
                "payload": {"market_date": "2026-04-23"},
                "target_url": "http://127.0.0.1:5100/ibkr/status",
            },
            compute_base_url="http://127.0.0.1:5100",
        )

        self.assertEqual(400, status_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("market_context_symbol_not_trade_target", payload["error"])
        self.assertEqual("market_monitor", payload["symbol_role"])
        self.assertEqual([], pb.created)
        self.assertEqual([], pb.updated)
        self.assertEqual(1, len(reconcile_calls))

    def test_screener_targets_upsert_counts_created_records(self):
        pb = _MinimalPB()
        payload, status_code = build_screener_targets_upsert_response(
            pb,
            payload={
                "environment": "live",
                "market_date": "2026-04-23",
                "items": [
                    {"symbol": "aapl", "score": 12, "direction_bias": "long"},
                    {"symbol": "msft", "tradability_score": 66, "target_status": "active"},
                ],
            },
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
        )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(2, payload["created"])
        self.assertEqual(["AAPL", "MSFT"], payload["symbols"])

    def test_screener_targets_upsert_paper_writes_shared_live_targets(self):
        pb = _MinimalPB()
        payload, status_code = build_screener_targets_upsert_response(
            pb,
            payload={
                "environment": "paper",
                "market_date": "2026-05-18",
                "items": [{"symbol": "aapl", "score": 12, "direction_bias": "long"}],
            },
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            escape_filter_string=lambda value: str(value or "").replace('"', '\\"'),
        )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("paper", payload["environment"])
        self.assertEqual("live", payload["data_environment"])
        self.assertEqual("live", pb.created[0][1]["environment"])

    def test_today_targets_builds_signal_workflow_payload(self):
        pb = _MinimalPB()
        pb._records["ibkr_targets"] = [
            {
                "id": "target-1",
                "symbol": "AAPL",
                "environment": "live",
                "date": "2026-04-23",
                "status": "active",
                "direction_bias": "long",
                "score": 12,
                "scan_reason": "earnings_breakout",
                "exchange": "NASDAQ",
                "updated": "2026-04-23 09:36:00",
                "extra": {
                    "screener_snapshot": {
                        "premarket_volume": 650000,
                        "today_volume": 800000,
                        "operable_reasons": ["盘前量能>50万"],
                    }
                },
            }
        ]
        pb._records["watchlist"] = [
            {"symbol": "AAPL", "environment": "live", "exchange": "NASDAQ", "industry": "Technology", "note": "Leader"}
        ]
        def et_ms(text: str) -> int:
            return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET).timestamp() * 1000)
        pb._all_records["ibkr_bars"] = [
            {"symbol": "AAPL", "environment": "live", "interval": "1d", "bar_time_ms": et_ms("2026-04-18 00:00:00"), "close": 95, "volume": 1200000, "us_time": "2026-04-18 16:00:00"},
            {"symbol": "AAPL", "environment": "live", "interval": "1d", "bar_time_ms": et_ms("2026-04-21 00:00:00"), "close": 96, "volume": 1300000, "us_time": "2026-04-21 16:00:00"},
            {"symbol": "AAPL", "environment": "live", "interval": "1d", "bar_time_ms": et_ms("2026-04-22 00:00:00"), "close": 98, "volume": 1500000, "us_time": "2026-04-22 16:00:00"},
            {"symbol": "AAPL", "environment": "live", "interval": "5m", "bar_time_ms": et_ms("2026-04-23 09:35:00"), "close": 100, "volume": 800000, "us_time": "2026-04-23 09:35:00", "exchange": "NASDAQ", "session_type": "regular"},
        ]
        pb._all_records["ibkr_indicators"] = [
            {
                "symbol": "AAPL",
                "environment": "live",
                "interval": "5",
                "bar_time_ms": et_ms("2026-04-23 09:35:00"),
                "atr_pct": 3.5,
                "ema_bullish": True,
                "crsi_bull_div": True,
                "fractal_bull": True,
                "trend_dir": 1,
                "vwap_bullish": True,
            }
        ]
        pb._all_records["ibkr_signals"] = [
            {
                "symbol": "AAPL",
                "environment": "live",
                "signal_id": "sig-1",
                "direction": "long",
                "status": "awaiting_confirm",
                "bar_time_ms": et_ms("2026-04-23 09:35:00"),
                "us_time": "2026-04-23 09:35:00",
                "created": "2026-04-23 09:35:01",
                "updated": "2026-04-23 09:35:02",
                "note": "manual confirmation required",
            }
        ]
        pb._states[("ibkr_daily_scan_state", "live", "global")] = {
            "data": {"status": "completed", "result": {"symbols": ["AAPL"]}}
        }

        with mock.patch("ibkr_api.universe.today_targets.time.time", return_value=et_ms("2026-04-23 09:40:00") / 1000):
            payload, status_code = build_today_targets_response(
                pb,
                payload={"environment": "live", "market_date": "2026-04-23"},
                normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
                time_strings=lambda: {"us": "2026-04-23 09:40:00", "cn": "2026-04-23 21:40:00", "date": "2026-04-23"},
            )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["summary"]["active_count"])
        self.assertEqual(1, payload["summary"]["awaiting_confirm_count"])
        self.assertEqual(1, payload["filtered_total"])
        self.assertEqual("AAPL", payload["items"][0]["symbol"])
        self.assertEqual("awaiting_confirm", payload["items"][0]["workflow_stage"])
        self.assertEqual("信号待确认", payload["items"][0]["workflow_label"])
        self.assertTrue(payload["items"][0]["has_signal_today"])
        self.assertEqual("completed", payload["daily_scan"]["status"])
        ready_definition = payload["workflow"]["ready_definition"]
        self.assertEqual(2, ready_definition["required_aligned_flags"])
        self.assertEqual(500000, ready_definition["thresholds"]["avg_10d_volume_gte"])
        self.assertEqual(60, ready_definition["thresholds"]["tradability_score_gte"])
        self.assertEqual(90, ready_definition["thresholds"]["freshness_lte_min"])
        ready_explanation = payload["items"][0]["ready_explanation"]
        self.assertTrue(ready_explanation["ready"])
        self.assertIn("方向一致技术条件 5/2", ready_explanation["passed"])
        self.assertEqual([], ready_explanation["missing"])

    def test_today_targets_explains_missing_ready_conditions(self):
        pb = _MinimalPB()
        pb._records["ibkr_targets"] = [
            {
                "id": "target-2",
                "symbol": "MSFT",
                "environment": "live",
                "date": "2026-04-23",
                "status": "candidate",
                "direction_bias": "long",
                "score": 6,
                "scan_reason": "manual_review",
                "exchange": "NASDAQ",
                "updated": "2026-04-23 09:36:00",
                "extra": {},
            }
        ]

        def et_ms(text: str) -> int:
            return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET).timestamp() * 1000)

        pb._all_records["ibkr_bars"] = [
            {"symbol": "MSFT", "environment": "live", "interval": "1d", "bar_time_ms": et_ms("2026-04-22 00:00:00"), "close": 25, "volume": 100000, "us_time": "2026-04-22 16:00:00"},
            {"symbol": "MSFT", "environment": "live", "interval": "5m", "bar_time_ms": et_ms("2026-04-23 09:35:00"), "close": 26, "volume": 5000, "us_time": "2026-04-23 09:35:00", "exchange": "NASDAQ", "session_type": "regular"},
        ]
        pb._all_records["ibkr_indicators"] = [
            {
                "symbol": "MSFT",
                "environment": "live",
                "interval": "5",
                "bar_time_ms": et_ms("2026-04-23 09:35:00"),
                "atr_pct": 0.4,
                "ema_bullish": True,
                "trend_dir": 0,
                "vwap_bullish": False,
            }
        ]

        with mock.patch("ibkr_api.universe.today_targets.time.time", return_value=et_ms("2026-04-23 09:40:00") / 1000):
            payload, status_code = build_today_targets_response(
                pb,
                payload={"environment": "live", "market_date": "2026-04-23"},
                normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
                time_strings=lambda: {"us": "2026-04-23 09:40:00", "cn": "2026-04-23 21:40:00", "date": "2026-04-23"},
            )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("watch", payload["items"][0]["technical_state"])
        ready_explanation = payload["items"][0]["ready_explanation"]
        self.assertFalse(ready_explanation["ready"])
        self.assertIn("当日 5m bar", ready_explanation["passed"])
        self.assertIn("方向一致技术条件 1/2", ready_explanation["missing"])
        self.assertTrue(any(item.startswith("10D均量") for item in ready_explanation["missing"]))

    def test_cancel_sync_wraps_group_cancel_shape(self):
        with mock.patch("ibkr_api.orders.cancel_sync.build_order_cancel_group_response", return_value=({"ok": True, "action": "cancel_group"}, 200)):
            payload, status_code = build_order_cancel_sync_response(
                object(),
                payload={"id": "abc"},
                normalize_environment=lambda value, default="live": default,
                escape_filter_string=lambda value: str(value or ""),
                cancel_broker_order=lambda environment, order_id, payload: {"ok": True},
            )
        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("cancel_sync", payload["action"])
        self.assertEqual("主单与系统订单已同步取消", payload["message"])


if __name__ == "__main__":
    unittest.main()
