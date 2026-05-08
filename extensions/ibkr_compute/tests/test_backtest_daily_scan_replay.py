import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest.runtime_service import BacktestService
from ibkr_compute.market.timeframe_utils import ET


class FakeEngine:
    def __init__(self, snapshot: dict):
        self.snapshot = dict(snapshot)

    def is_ready(self):
        return True

    def get_snapshot(self):
        return dict(self.snapshot)


class BacktestDailyScanReplayTests(unittest.TestCase):
    def setUp(self):
        self.service = BacktestService(None)
        self.request = {
            "source_environment": "live",
            "symbols": [],
            "max_symbols": 2,
            "premarket_cutoff_time": "09:25",
            "scan_session_mode": "extended",
            "scan_warmup_bars": 320,
        }

    def test_scan_replay_falls_back_to_current_watchlist_snapshot_for_missing_history(self):
        self.service._load_trading_dates = lambda request: ["2026-04-22", "2026-04-23"]

        def fake_universe(request, as_of_date=""):
            if as_of_date:
                return []
            return [{"symbol": "NVDA", "exchange": "SMART"}]

        self.service._load_scan_universe = fake_universe
        self.service._evaluate_historical_scan_symbol = lambda symbol, trade_date, request, settings=None: {
            "symbol": symbol,
            "score": 8,
            "direction_bias": "long",
            "quality_gate_passed": True,
            "reason": "quality ok",
            "extra": {"scan_cutoff_ms": self.service._build_scan_cutoff_ms(trade_date, "09:25")},
        }

        plan = self.service._build_daily_scan_replay_plan(self.request)

        self.assertEqual(plan["symbols"], ["NVDA"])
        self.assertEqual(plan["selection_plan"]["2026-04-22"], ["NVDA"])
        self.assertEqual(plan["selection_plan"]["2026-04-23"], ["NVDA"])
        self.assertEqual(len(plan["target_rows"]), 2)
        self.assertTrue(all(day["universe_snapshot_fallback"] for day in plan["summary"]["daily"]))
        self.assertTrue(all(row["extra"]["universe_snapshot_fallback"] for row in plan["target_rows"]))

    def test_backtest_history_broker_uses_dedicated_client_id(self):
        with mock.patch.dict(
            os.environ,
            {"IBGW_CLIENT_ID": "31", "IBGW_BACKTEST_CLIENT_ID": "71"},
            clear=False,
        ):
            service = BacktestService(object())

        self.assertEqual(service._history_broker.client_id, 71)
        self.assertIs(service.data_backfill.broker, service._history_broker)
        self.assertIs(service.conid_resolver.broker, service._history_broker)

    def test_backfill_history_failure_returns_repair_summary_without_raising(self):
        service = BacktestService(None)
        service.pb = object()
        service.conid_resolver = SimpleNamespace(resolve=lambda symbol: 123)

        def fail_history(*_args, **_kwargs):
            raise RuntimeError("history_fetch_failed_after_retries:F:5m:123:historical_timeout")

        service.data_backfill = SimpleNamespace(_request_history_json=fail_history)

        result = service._backfill_symbol_history(
            "F",
            "live",
            self.service._build_scan_cutoff_ms("2026-04-22", "09:20"),
            self.service._build_scan_cutoff_ms("2026-04-23", "09:20"),
            interval="5m",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "history_fetch_failed")
        self.assertIn("historical_timeout", result["error"])
        self.assertEqual(result["rows"], [])

    def test_scan_replay_applies_daily_scanner_quality_gate(self):
        self.service._load_trading_dates = lambda request: ["2026-04-22"]
        self.service._load_scan_universe = lambda request, as_of_date="": [
            {"symbol": "NVDA", "exchange": "SMART"}
        ]
        self.service._evaluate_historical_scan_symbol = lambda symbol, trade_date, request, settings=None: {
            "symbol": symbol,
            "score": 8,
            "direction_bias": "long",
            "quality_gate_passed": False,
            "reason": "premarket too low",
            "rejection_examples": [{"bucket": "premarket_volume_below_threshold"}],
            "extra": {"scan_cutoff_ms": self.service._build_scan_cutoff_ms(trade_date, "09:25")},
        }

        plan = self.service._build_daily_scan_replay_plan(self.request)

        self.assertEqual(plan["symbols"], [])
        self.assertEqual(plan["target_rows"], [])
        daily = plan["summary"]["daily"][0]
        self.assertEqual(daily["ready_symbol_count"], 1)
        self.assertEqual(daily["quality_rejected_count"], 1)
        self.assertEqual(daily["candidate_count"], 0)
        self.assertEqual(daily["selected_count"], 0)
        self.assertEqual(daily["rejection_summary"]["premarket_volume_below_threshold"], 1)

    def test_scan_replay_reuses_sd_admission_as_hard_gate_when_enabled(self):
        self.service._load_trading_dates = lambda request: ["2026-04-22"]
        self.service._load_scan_universe = lambda request, as_of_date="": [
            {"symbol": "AAPL", "exchange": "SMART"},
            {"symbol": "NVDA", "exchange": "SMART"},
        ]

        def evaluate(symbol, trade_date, request, settings=None):
            return {
                "symbol": symbol,
                "score": 10 if symbol == "AAPL" else 9,
                "direction_bias": "long",
                "quality_gate_passed": True,
                "reason": "quality ok",
                "extra": {"scan_cutoff_ms": self.service._build_scan_cutoff_ms(trade_date, "09:25")},
            }

        self.service._evaluate_historical_scan_symbol = evaluate
        self.service._build_historical_sd_admission = lambda symbol, trade_date, request, candidate=None: {
            "passed": symbol == "NVDA",
            "reason": "sd_window_admitted" if symbol == "NVDA" else "no_window",
            "sd_admitted_at_ms": self.service._build_scan_cutoff_ms(trade_date, "10:00") if symbol == "NVDA" else 0,
            "sd_window_status": "lower_active" if symbol == "NVDA" else "no_window",
            "sd_lower_valid": symbol == "NVDA",
            "sd_upper_valid": False,
        }
        request = {
            **self.request,
            "daily_selection_require_sd_trigger": True,
            "daily_selection_reuse_live_admission": True,
            "daily_selection_candidate_limit": 10,
        }

        plan = self.service._build_daily_scan_replay_plan(request)

        self.assertEqual(plan["symbols"], ["NVDA"])
        self.assertEqual(plan["selection_plan"]["2026-04-22"], ["NVDA"])
        target_extra = plan["target_rows"][0]["extra"]
        self.assertTrue(target_extra["sd_selection_passed"])
        self.assertEqual(target_extra["sd_window_status"], "lower_active")
        daily = plan["summary"]["daily"][0]
        self.assertEqual(daily["candidate_count"], 2)
        self.assertEqual(daily["sd_scanned_count"], 2)
        self.assertEqual(daily["sd_admitted_count"], 1)
        self.assertEqual(daily["sd_rejected_count"], 1)

    def test_daily_scan_match_diagnostics_counts_selected_day_hits_and_misses(self):
        target_rows = [
            {"symbol": "NVDA", "date": "2026-04-22"},
            {"symbol": "TSLA", "date": "2026-04-22"},
            {"symbol": "AAPL", "date": "2026-04-23"},
        ]
        signal_rows = [
            {
                "symbol": "NVDA",
                "date": "2026-04-22",
                "status": "executed",
                "direction": "long",
                "us_time": "2026-04-22 10:00:00",
                "extra": {"signal_status_reason": "entry_limit_filled"},
            },
            {
                "symbol": "TSLA",
                "date": "2026-04-22",
                "status": "skipped",
                "direction": "short",
                "us_time": "2026-04-22 09:25:00",
                "extra": {"signal_status_reason": "outside_trade_window"},
            },
            {
                "symbol": "AMD",
                "date": "2026-04-22",
                "status": "skipped",
                "direction": "long",
                "us_time": "2026-04-22 10:15:00",
                "extra": {"signal_status_reason": "symbol_not_selected_for_day"},
            },
            {
                "symbol": "AAPL",
                "date": "2026-04-24",
                "status": "skipped",
                "direction": "long",
                "us_time": "2026-04-24 11:00:00",
                "extra": {"signal_status_reason": "symbol_not_selected_for_day"},
            },
        ]

        diagnostics = self.service._build_daily_scan_match_diagnostics(target_rows, signal_rows)

        self.assertTrue(diagnostics["enabled"])
        self.assertEqual(diagnostics["selected_pair_count"], 3)
        self.assertEqual(diagnostics["generated_signal_count"], 4)
        self.assertEqual(diagnostics["selected_day_signal_count"], 2)
        self.assertEqual(diagnostics["not_selected_signal_count"], 2)
        self.assertEqual(diagnostics["selected_day_executed_signal_count"], 1)
        self.assertEqual(diagnostics["selected_day_signal_rate_pct"], 50.0)
        self.assertEqual(diagnostics["selected_pair_hit_rate_pct"], 66.6667)
        self.assertEqual(diagnostics["top_not_selected_symbols"][0], {"key": "AAPL", "count": 1})
        self.assertEqual(diagnostics["skipped_not_selected_samples"][0]["symbol"], "AMD")

    def test_backtest_funnel_metrics_counts_daily_opens_and_target_entry_rate(self):
        metrics = self.service._build_backtest_funnel_metrics(
            {"symbol_source": "daily_scan_replay"},
            [
                {"symbol": "NVDA", "date": "2026-04-22"},
                {"symbol": "TSLA", "date": "2026-04-22"},
                {"symbol": "AAPL", "date": "2026-04-23"},
            ],
            [
                {"symbol": "NVDA", "date": "2026-04-22", "status": "executed"},
                {"symbol": "TSLA", "date": "2026-04-22", "status": "skipped"},
                {"symbol": "AMD", "date": "2026-04-22", "status": "skipped", "extra": {"signal_status_reason": "symbol_not_selected_for_day"}},
                {"symbol": "AAPL", "date": "2026-04-23", "status": "generated"},
            ],
            [
                {"symbol": "NVDA", "entry_us_time": "2026-04-22 10:05:00"},
                {"symbol": "AAPL", "entry_us_time": "2026-04-23 10:10:00"},
            ],
        )

        self.assertTrue(metrics["target_funnel_enabled"])
        self.assertEqual(
            metrics["daily_open_counts"],
            [
                {"date": "2026-04-22", "open_count": 1},
                {"date": "2026-04-23", "open_count": 1},
            ],
        )
        self.assertEqual(metrics["target_to_entry_rate"], 66.6667)
        self.assertEqual(metrics["target_to_signal_rate"], 100.0)
        self.assertEqual(metrics["signal_to_entry_rate"], 33.3333)
        self.assertEqual(metrics["funnel_signal_count"], 3)
        self.assertEqual(metrics["funnel_executed_signal_count"], 1)
        self.assertEqual(metrics["target_entry_count"], 2)
        self.assertEqual(metrics["target_signal_count"], 3)
        self.assertEqual(metrics["daily_funnel"][0]["target_count"], 2)
        self.assertEqual(metrics["daily_funnel"][0]["signal_count"], 2)
        self.assertEqual(metrics["daily_funnel"][0]["target_signal_count"], 2)
        self.assertEqual(metrics["daily_funnel"][0]["open_count"], 1)
        self.assertEqual(metrics["daily_funnel"][0]["target_to_entry_rate"], 50.0)

    def test_backtest_funnel_metrics_disables_target_rates_for_manual_symbols(self):
        metrics = self.service._build_backtest_funnel_metrics(
            {"symbol_source": "manual"},
            [{"symbol": "NVDA", "date": "2026-04-22"}],
            [{"symbol": "NVDA", "date": "2026-04-22", "status": "executed"}],
            [{"symbol": "NVDA", "entry_us_time": "2026-04-22 10:05:00"}],
        )

        self.assertFalse(metrics["target_funnel_enabled"])
        self.assertEqual(metrics["target_to_entry_rate"], 0.0)
        self.assertEqual(metrics["daily_funnel"][0]["target_count"], 0)
        self.assertEqual(metrics["daily_funnel"][0]["signal_to_entry_rate"], 100.0)

    def test_historical_scan_metric_row_uses_cutoff_limited_premarket_data(self):
        trade_date = "2026-04-24"
        day_start = datetime.strptime(trade_date, "%Y-%m-%d").replace(tzinfo=ET)

        def bar_ms(hour: int, minute: int = 0):
            return int(day_start.replace(hour=hour, minute=minute).timestamp() * 1000)

        intraday_rows = [
            {
                "symbol": "NVDA",
                "exchange": "SMART",
                "interval": "5m",
                "close": 101.0,
                "volume": 3000,
                "session_type": "premarket",
                "bar_time_ms": bar_ms(4, 0),
            },
            {
                "symbol": "NVDA",
                "exchange": "SMART",
                "interval": "5m",
                "close": 102.5,
                "volume": 2500,
                "session_type": "premarket",
                "bar_time_ms": bar_ms(9, 20),
            },
        ]
        daily_rows = []
        for index in range(10, 0, -1):
            daily_rows.append(
                {
                    "symbol": "NVDA",
                    "exchange": "SMART",
                    "interval": "1d",
                    "close": 100.0,
                    "volume": 200000,
                    "session_type": "regular",
                    "bar_time_ms": int((day_start - timedelta(days=index)).timestamp() * 1000),
                }
            )

        self.service._load_bar_rows_from_sqlite = lambda symbol, environment, **kwargs: (
            list(intraday_rows) if kwargs.get("interval") == "5m" else list(daily_rows)
        )
        cutoff_ms = self.service._build_scan_cutoff_ms(trade_date, "09:25")

        metrics = self.service._build_historical_scan_metric_row(
            "NVDA",
            trade_date,
            self.request,
            cutoff_ms,
            {("live", "NVDA", "5m"): FakeEngine({"atr_pct": 0.25})},
        )

        self.assertEqual(metrics["premarket_volume"], 5500)
        self.assertEqual(metrics["avg_10d_volume"], 200000)
        self.assertEqual(metrics["atr_pct"], 0.25)
        self.assertEqual(metrics["day_change_pct"], 2.5)
        self.assertEqual(metrics["latest_bar_time_ms"], bar_ms(9, 20))

    def test_metric_prefilter_skips_indicator_engine_build_for_obvious_rejection(self):
        self.service._build_historical_scan_metric_row = lambda symbol, trade_date, request, cutoff_ms, engines=None: {
            "symbol": symbol,
            "avg_10d_volume": 1,
            "premarket_volume": 1,
            "atr_pct": 0.25,
            "day_change_pct": 0.1,
        }

        def fail_engine_build(*args, **kwargs):
            raise AssertionError("indicator engines should be skipped")

        self.service._build_historical_scan_engines = fail_engine_build

        result = self.service._evaluate_historical_scan_symbol(
            "NVDA",
            "2026-04-24",
            self.request,
            settings={
                "min_avg_10d_volume": 100000,
                "min_premarket_volume": 5000,
                "min_atr_pct": 0.15,
                "min_abs_day_change_pct": 1.0,
            },
        )

        self.assertFalse(result["quality_gate_passed"])
        self.assertEqual(result["reason"], "historical_metric_prefilter")
        self.assertTrue(result["extra"]["prefiltered_before_indicators"])
        buckets = {row["bucket"] for row in result["rejection_examples"]}
        self.assertIn("avg_10d_volume_below_threshold", buckets)
        self.assertIn("premarket_volume_below_threshold", buckets)
        self.assertIn("day_change_below_threshold", buckets)


if __name__ == "__main__":
    unittest.main()
