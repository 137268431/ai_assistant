import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.workflows import daily_scanner as daily_scanner_mod
from ibkr_compute.workflows.daily_scanner import DailyScanner


class FakeEngine:
    def __init__(self, snapshot: dict, ready: bool = True):
        self._snapshot = dict(snapshot)
        self._ready = bool(ready)

    def is_ready(self) -> bool:
        return self._ready

    def get_snapshot(self) -> dict:
        return dict(self._snapshot)


class DummyPBClient:
    def __init__(self, watchlist: list[dict], existing_targets: list[dict] | None = None):
        self.watchlist = list(watchlist)
        self.existing_targets = list(existing_targets or [])
        self.upserts: list[dict] = []
        self.updated: list[tuple[str, str, dict]] = []

    def get_records(self, collection: str, **kwargs):
        if collection != "watchlist":
            raise AssertionError(f"unexpected collection: {collection}")
        return list(self.watchlist)

    def get_all_records(self, collection: str, **kwargs):
        if collection != "ibkr_targets":
            raise AssertionError(f"unexpected collection: {collection}")
        return list(self.existing_targets)

    def upsert_scan(self, payload: dict):
        self.upserts.append(dict(payload))

    def update_record(self, collection: str, record_id: str, payload: dict):
        self.updated.append((collection, record_id, dict(payload)))


class DailyScanRejectionSummaryTest(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "scan_time_et": "09:20",
            "min_avg_10d_volume": 100000,
            "min_premarket_volume": 5000,
            "min_atr_pct": 0.15,
            "min_abs_day_change_pct": 1.0,
            "monitor_count": 0,
            "target_subscription_limit": 80,
            "total_subscription_limit": 80,
            "trade_subscription_budget": 80,
        }
        self.settings_patch = mock.patch.object(daily_scanner_mod, "_load_scan_settings", return_value=dict(self.settings))
        self.api_app_patch = mock.patch.object(daily_scanner_mod, "get_api_app", return_value=object())
        self.settings_patch.start()
        self.api_app_patch.start()

    def tearDown(self):
        self.settings_patch.stop()
        self.api_app_patch.stop()

    def _build_scanner(self):
        watchlist = [
            {"symbol": "AAPL", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
            {"symbol": "MSFT", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
            {"symbol": "TSLA", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
            {"symbol": "NVDA", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
        ]
        pb_client = DummyPBClient(watchlist=watchlist)
        engines = {
            ("live", "MSFT", "5m"): FakeEngine({"ema_bullish": True, "ema_bearish": True}),
            ("live", "TSLA", "5m"): FakeEngine({"ema_bullish": True}),
            ("live", "NVDA", "5m"): FakeEngine({"ema_bullish": True}),
        }
        scanner = DailyScanner(pb_client=pb_client, engines=engines)
        scanner._build_metric_rows = lambda date, environment, symbols: {
            "TSLA": {
                "avg_10d_volume": 50000,
                "premarket_volume": 1500,
                "atr_pct": 0.05,
                "day_change_pct": 0.4,
                "exchange": "SMART",
            },
            "NVDA": {
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 2.5,
                "exchange": "SMART",
            },
        }
        return scanner, pb_client

    def test_run_scan_aggregates_rejection_reasons_and_keeps_eligible_symbol(self):
        scanner, pb_client = self._build_scanner()

        result = scanner.run_scan("2026-04-21", environments=["live"])

        self.assertEqual(result["active"], 1)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["rejection_summary"]["no_snapshot"], 1)
        self.assertEqual(result["rejection_summary"]["vote_tie"], 1)
        self.assertEqual(result["rejection_summary"]["avg_10d_volume_below_threshold"], 1)
        self.assertEqual(result["rejection_summary"]["premarket_volume_below_threshold"], 1)
        self.assertEqual(result["rejection_summary"]["atr_pct_below_threshold"], 1)
        self.assertEqual(result["rejection_summary"]["day_change_below_threshold"], 1)
        self.assertEqual(len(pb_client.upserts), 1)
        self.assertEqual(pb_client.upserts[0]["symbol"], "NVDA")
        example_buckets = {row["bucket"] for row in result["rejection_examples"]}
        self.assertIn("no_snapshot", example_buckets)
        self.assertIn("vote_tie", example_buckets)
        self.assertIn("premarket_volume_below_threshold", example_buckets)

    def test_zero_target_day_still_reports_rejection_summary(self):
        scanner, pb_client = self._build_scanner()
        scanner._build_metric_rows = lambda date, environment, symbols: {
            "TSLA": {
                "avg_10d_volume": 50000,
                "premarket_volume": 1500,
                "atr_pct": 0.05,
                "day_change_pct": 0.4,
                "exchange": "SMART",
            },
        }

        result = scanner.run_scan("2026-04-21", environments=["live"])

        self.assertEqual(result["active"], 0)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(len(pb_client.upserts), 0)
        self.assertEqual(result["rejection_summary"]["no_snapshot"], 1)
        self.assertEqual(result["rejection_summary"]["vote_tie"], 1)
        self.assertEqual(result["rejection_summary"]["avg_10d_volume_below_threshold"], 2)
        self.assertEqual(result["rejection_summary"]["premarket_volume_below_threshold"], 2)
        self.assertEqual(result["rejection_summary"]["atr_pct_below_threshold"], 2)
        self.assertEqual(result["rejection_summary"]["day_change_below_threshold"], 2)
        self.assertTrue(result["rejection_examples"])


if __name__ == "__main__":
    unittest.main()
