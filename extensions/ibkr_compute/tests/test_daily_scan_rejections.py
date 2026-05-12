import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
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


def context_engines(symbol: str, *, environment: str = "live", side: str = "long", hot: bool = False) -> dict:
    bullish = side == "long"
    snapshot = (
        {"ema_bullish": True, "trend_dir": 1}
        if bullish
        else {"ema_bearish": True, "trend_dir": -1}
    )
    trigger = dict(snapshot)
    if bullish:
        trigger["orb_breakout_up"] = True
        trigger["vwap_alignment"] = "above"
        if hot:
            trigger["rvol_20"] = 3.2
    else:
        trigger["orb_breakout_down"] = True
        trigger["vwap_alignment"] = "below"
        if hot:
            trigger["rvol_20"] = 3.2
    return {
        (environment, symbol, "1d"): FakeEngine(snapshot),
        (environment, symbol, "4h"): FakeEngine(snapshot),
        (environment, symbol, "1h"): FakeEngine(snapshot),
        (environment, symbol, "30m"): FakeEngine(snapshot),
        (environment, symbol, "15m"): FakeEngine(trigger),
        (environment, symbol, "5m"): FakeEngine(trigger),
    }


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
            "active_target_limit": 24,
            "active_min_score": 0,
            "day_gain_trigger_enabled": True,
            "day_gain_trigger_pct": 4.0,
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
        }
        engines.update(context_engines("NVDA"))
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

    def test_topup_only_adds_new_symbols_and_keeps_existing_targets(self):
        watchlist = [
            {"symbol": "AAPL", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
            {"symbol": "NVDA", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
        ]
        existing_targets = [
            {
                "id": "target-aapl",
                "symbol": "AAPL",
                "environment": "live",
                "date": "2026-04-21",
                "status": "active",
                "extra": {
                    "source": "daily_scan",
                    "scan_stage": "early_expansion_seed",
                    "active_gate_passed": True,
                },
            }
        ]
        pb_client = DummyPBClient(watchlist=watchlist, existing_targets=existing_targets)
        engines = {("live", "AAPL", "5m"): FakeEngine({"ema_bullish": True})}
        engines.update(context_engines("NVDA"))
        scanner = DailyScanner(pb_client=pb_client, engines=engines)
        scanner._build_metric_rows = lambda date, environment, symbols: {
            symbol: {
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 2.5,
                "exchange": "SMART",
            }
            for symbol in symbols
        }

        result = scanner.run_scan("2026-04-21", environments=["live"], mode="topup")

        self.assertEqual([row["symbol"] for row in pb_client.upserts], ["NVDA"])
        self.assertEqual(pb_client.upserts[0]["status"], "active")
        self.assertEqual(pb_client.upserts[0]["extra"]["scan_stage"], "early_expansion_topup")
        self.assertEqual(result["mode"], "topup")
        self.assertEqual(result["active"], 2)
        self.assertEqual(result["new_active"], 1)
        self.assertEqual(result["new_targets"][0]["symbol"], "NVDA")
        self.assertEqual(pb_client.updated, [])

    def test_topup_skips_new_candidate_when_active_budget_is_full(self):
        self.settings["active_target_limit"] = 1
        daily_scanner_mod._load_scan_settings.return_value = dict(self.settings)
        watchlist = [
            {"symbol": "AAPL", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
            {"symbol": "NVDA", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
        ]
        existing_targets = [
            {
                "id": "target-aapl",
                "symbol": "AAPL",
                "environment": "live",
                "date": "2026-04-21",
                "status": "active",
                "extra": {
                    "source": "daily_scan",
                    "scan_stage": "early_expansion_seed",
                    "active_gate_passed": True,
                },
            }
        ]
        pb_client = DummyPBClient(watchlist=watchlist, existing_targets=existing_targets)
        engines = {("live", "AAPL", "5m"): FakeEngine({"ema_bullish": True})}
        engines.update(context_engines("NVDA"))
        scanner = DailyScanner(pb_client=pb_client, engines=engines)
        scanner._build_metric_rows = lambda date, environment, symbols: {
            symbol: {
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 2.5,
                "exchange": "SMART",
            }
            for symbol in symbols
        }

        result = scanner.run_scan("2026-04-21", environments=["live"], mode="topup")

        self.assertEqual(pb_client.upserts, [])
        self.assertEqual(result["new_active"], 0)
        self.assertEqual(result["new_candidates"], 0)
        self.assertEqual(result["rejection_summary"]["topup_active_budget_full"], 1)

    def test_topup_adds_context_active_even_below_active_score(self):
        self.settings["active_min_score"] = 35
        daily_scanner_mod._load_scan_settings.return_value = dict(self.settings)
        watchlist = [
            {"symbol": "AAPL", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
        ]
        pb_client = DummyPBClient(watchlist=watchlist)
        engines = context_engines("AAPL")
        scanner = DailyScanner(pb_client=pb_client, engines=engines)
        scanner._build_metric_rows = lambda date, environment, symbols: {
            symbol: {
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 2.5,
                "exchange": "SMART",
            }
            for symbol in symbols
        }

        result = scanner.run_scan("2026-04-21", environments=["live"], mode="topup")

        self.assertEqual(pb_client.upserts[0]["status"], "active")
        self.assertTrue(pb_client.upserts[0]["extra"]["context_active"])
        self.assertTrue(pb_client.upserts[0]["extra"]["context_gate_passed"])
        self.assertEqual(pb_client.upserts[0]["extra"]["setup_family"], "trend_follow")
        self.assertEqual(pb_client.upserts[0]["extra"]["allowed_sides"], ["long"])
        self.assertGreater(pb_client.upserts[0]["extra"]["context_score"], 0)
        self.assertEqual(result["new_active"], 1)
        self.assertEqual(result["new_candidates"], 0)

    def test_topup_skips_symbol_without_context_gate(self):
        self.settings["active_min_score"] = 0
        daily_scanner_mod._load_scan_settings.return_value = dict(self.settings)
        watchlist = [
            {"symbol": "AAPL", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
        ]
        pb_client = DummyPBClient(watchlist=watchlist)
        engines = {
            ("live", "AAPL", "5m"): FakeEngine({"ema_bullish": True}),
        }
        scanner = DailyScanner(pb_client=pb_client, engines=engines)
        scanner._build_metric_rows = lambda date, environment, symbols: {
            symbol: {
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 2.5,
                "exchange": "SMART",
            }
            for symbol in symbols
        }

        result = scanner.run_scan("2026-04-21", environments=["live"], mode="topup")

        self.assertEqual(pb_client.upserts, [])
        self.assertEqual(result["new_active"], 0)
        self.assertEqual(result["new_candidates"], 0)
        self.assertEqual(result["rejection_summary"]["topup_context_gate_not_passed"], 1)

    def test_seed_skips_symbol_when_quality_passes_but_context_gate_fails(self):
        self.settings["active_target_limit"] = 2
        self.settings["active_min_score"] = 35
        daily_scanner_mod._load_scan_settings.return_value = dict(self.settings)
        watchlist = [
            {"symbol": "AAPL", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
            {"symbol": "NVDA", "environment": "live", "symbol_role": "trade", "exchange": "SMART"},
        ]
        pb_client = DummyPBClient(watchlist=watchlist)
        engines = {
            ("live", "AAPL", "5m"): FakeEngine({"ema_bullish": True}),
        }
        engines.update(context_engines("NVDA", hot=True))
        scanner = DailyScanner(pb_client=pb_client, engines=engines)
        scanner._build_metric_rows = lambda date, environment, symbols: {
            symbol: {
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 4.5 if symbol == "NVDA" else 2.5,
                "exchange": "SMART",
            }
            for symbol in symbols
        }

        result = scanner.run_scan("2026-04-21", environments=["live"])

        statuses = {row["symbol"]: row["status"] for row in pb_client.upserts}
        self.assertEqual(statuses["NVDA"], "active")
        self.assertEqual(result["new_active"], 1)
        self.assertEqual(result["new_candidates"], 0)
        self.assertNotIn("AAPL", statuses)
        self.assertEqual(result["rejection_summary"]["context_gate_not_passed"], 1)
        self.assertEqual(
            next(row for row in pb_client.upserts if row["symbol"] == "NVDA")["extra"]["setup_family"],
            "hot_momentum",
        )

    def test_evaluate_symbol_adds_stocks_in_play_bonus_fields(self):
        pb_client = DummyPBClient(watchlist=[])
        engines = {
            (
                "live",
                "NVDA",
                "5m",
            ): FakeEngine(
                {
                    "ema_bullish": True,
                    "sd_regime": "breakout_up",
                    "orb_breakout_up": True,
                    "vwap_alignment": "above",
                    "rvol_20": 2.0,
                    "dollar_volume": 15_000_000,
                }
            ),
        }
        scanner = DailyScanner(pb_client=pb_client, engines=engines)

        result = scanner.evaluate_symbol(
            "NVDA",
            "2026-04-21",
            "live",
            metrics={
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 2.5,
                "data_quality": {"needs_repair": False, "status": "ready"},
            },
            settings=dict(self.settings),
        )

        self.assertTrue(result["quality_gate_passed"])
        self.assertIn("sd_regime=breakout_up", result["reason"])
        self.assertIn("orb_breakout=up", result["reason"])
        self.assertEqual(result["extra"]["stocks_in_play_score"], 13)
        self.assertEqual(result["extra"]["sd_regime"], "breakout_up")
        self.assertEqual(result["extra"]["data_quality"]["status"], "ready")

    def test_day_gain_trigger_does_not_override_technical_direction(self):
        pb_client = DummyPBClient(watchlist=[])
        scanner = DailyScanner(pb_client=pb_client, engines={})

        result = scanner.evaluate_symbol(
            "APP",
            "2026-04-21",
            "live",
            metrics={
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 4.2,
                "exchange": "NASDAQ",
            },
            settings=dict(self.settings),
            stored_snapshots={"5m": {"ema_bearish": True}},
        )

        self.assertTrue(result["quality_gate_passed"])
        self.assertEqual(result["direction_bias"], "short")
        self.assertIn("day_gain>=4%", result["reason"])
        self.assertTrue(result["extra"]["day_gain_triggered"])
        self.assertEqual(result["extra"]["short_votes"], 1)

        result = scanner.evaluate_symbol(
            "APP",
            "2026-04-21",
            "live",
            metrics={
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 4.2,
                "exchange": "NASDAQ",
            },
            settings={**dict(self.settings), "day_gain_trigger_pct": 4.0},
            stored_snapshots={"5m": {"ema_bullish": True}},
        )

        self.assertTrue(result["quality_gate_passed"])
        self.assertEqual(result["direction_bias"], "long")
        self.assertIn("day_gain>=4%", result["reason"])
        self.assertTrue(result["extra"]["day_gain_triggered"])
        self.assertEqual(result["extra"]["long_votes"], 1)

    def test_day_gain_trigger_can_select_symbol_without_sd_touch(self):
        pb_client = DummyPBClient(watchlist=[])
        scanner = DailyScanner(pb_client=pb_client, engines={})

        result = scanner.evaluate_symbol(
            "APP",
            "2026-04-21",
            "live",
            metrics={
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 4.2,
                "exchange": "NASDAQ",
            },
            settings=dict(self.settings),
            stored_snapshots={"5m": {}},
        )

        self.assertTrue(result["quality_gate_passed"])
        self.assertEqual(result["direction_bias"], "neutral")
        self.assertIn("day_gain>=4%", result["reason"])
        self.assertEqual(result["extra"]["selection_triggers"], ["day_gain"])
        self.assertEqual(result["strategy_policy"]["selection_trigger"], "day_gain")
        self.assertEqual(result["strategy_policy"]["allowed_sides"], ["long", "short"])
        self.assertEqual(result["strategy_policy"]["entry_style"], "wait_for_pullback_or_exhaustion")

    def test_long_cycle_up_can_context_activate_exhaustion_short(self):
        pb_client = DummyPBClient(watchlist=[])
        scanner = DailyScanner(pb_client=pb_client, engines={})

        result = scanner.evaluate_symbol(
            "APP",
            "2026-04-21",
            "live",
            metrics={
                "avg_10d_volume": 3500000,
                "premarket_volume": 25000,
                "atr_pct": 0.8,
                "day_change_pct": 4.8,
                "exchange": "NASDAQ",
            },
            settings=dict(self.settings),
            stored_snapshots={
                "1d": {"ema_bullish": True, "trend_dir": 1},
                "4h": {"ema_bullish": True, "trend_dir": 1},
                "1h": {"sd_upper": True, "crsi_ob": True},
                "30m": {"sd_upper": True, "fractal_bear": True},
                "15m": {"sd_upper": True, "crsi_ob": True, "orb_breakout_down": True},
                "5m": {"sd_upper": True, "fractal_bear": True, "vwap_alignment": "below"},
            },
        )

        self.assertTrue(result["quality_gate_passed"])
        self.assertTrue(result["context_gate_passed"])
        self.assertEqual(result["setup_family"], "exhaustion_reversal")
        self.assertEqual(result["allowed_sides"], ["short"])


if __name__ == "__main__":
    unittest.main()
