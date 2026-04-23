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
from ibkr_api.universe.screener import build_screener_proxy_response
from ibkr_api.universe.today_targets import build_today_targets_response
from ibkr_api.universe.targets import build_screener_targets_upsert_response, build_target_upsert_response
from ibkr_api.universe.watchlist import build_watchlist_upsert_response
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
