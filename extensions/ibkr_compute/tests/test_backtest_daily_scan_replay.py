import os
import re
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
from ibkr_compute.backtest.constants import BACKTEST_WARMUP_BARS
from ibkr_compute.core.indicator_engine import indicator_ready_bar_count
from ibkr_compute.market.timeframe_utils import ET


class FakeEngine:
    def __init__(self, snapshot: dict):
        self.snapshot = dict(snapshot)

    def is_ready(self):
        return True

    def get_snapshot(self):
        return dict(self.snapshot)


class FakeDailySelectionCachePB:
    def __init__(self):
        self.rows = {}
        self.counter = 0

    @staticmethod
    def _filter_value(filter_text: str, field: str) -> str:
        match = re.search(rf'{field}\s*=\s*"([^"]*)"', filter_text or "")
        return match.group(1) if match else ""

    def get_first_record(self, collection, filter=None, sort=None):
        if collection != "ibkr_backtest_daily_selection_cache":
            return None
        key = self._filter_value(filter or "", "cache_key")
        date = self._filter_value(filter or "", "market_date")
        row = self.rows.get((key, date))
        return dict(row) if row else None

    def create_record(self, collection, data):
        if collection != "ibkr_backtest_daily_selection_cache":
            raise AssertionError(collection)
        self.counter += 1
        row = dict(data)
        row["id"] = f"cache_{self.counter}"
        self.rows[(row["cache_key"], row["market_date"])] = row
        return dict(row)

    def update_record(self, collection, record_id, data):
        if collection != "ibkr_backtest_daily_selection_cache":
            raise AssertionError(collection)
        row = dict(data)
        row["id"] = record_id
        self.rows[(row["cache_key"], row["market_date"])] = row
        return dict(row)


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

    def _cache_request(self):
        return {
            **self.request,
            "symbol_source": "daily_scan_replay",
            "execution_model": "portfolio_stream",
            "daily_selected_only": True,
            "daily_selection_cache_enabled": True,
            "daily_selection_cache_mode": "use_or_build",
            "daily_selection_cache_force_rebuild": False,
            "daily_selection_require_sd_trigger": False,
            "daily_selection_reuse_live_admission": False,
            "daily_selection_candidate_limit": 20,
            "session_mode": "extended",
            "trade_window_start_time": "09:35",
            "trade_window_end_time": "15:30",
            "strategy_tag": "TEST",
            "params": {"strategy_params": {}},
        }

    def _install_cache_scan_fakes(self, service, dates=None):
        dates = dates or ["2026-04-22", "2026-04-23"]
        service.pb = FakeDailySelectionCachePB()
        service._load_trading_dates = lambda request: list(dates)
        service._load_scan_universe = lambda request, as_of_date="": [{"symbol": "NVDA", "exchange": "SMART"}]
        service._load_historical_scan_settings = lambda environment: {
            "scan_time_et": "09:25",
            "min_avg_10d_volume": 100000,
            "min_premarket_volume": 5000,
            "min_atr_pct": 0.15,
            "min_abs_day_change_pct": 1.0,
        }
        service._build_daily_selection_input_fingerprint = lambda trade_date, universe_rows, request: {
            "usable": True,
            "hash": f"clean-{trade_date}",
            "reason": "coverage_clean",
            "row_count": len(universe_rows),
        }
        return service.pb

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

    def test_daily_selection_cache_reuses_prebuilt_targets_for_same_request(self):
        pb = self._install_cache_scan_fakes(self.service)
        calls = []

        def evaluate(symbol, trade_date, request, settings=None):
            calls.append((trade_date, symbol))
            return {
                "symbol": symbol,
                "score": 8,
                "direction_bias": "long",
                "quality_gate_passed": True,
                "reason": "quality ok",
                "extra": {"scan_cutoff_ms": self.service._build_scan_cutoff_ms(trade_date, "09:25")},
            }

        self.service._evaluate_historical_scan_symbol = evaluate
        request = self._cache_request()

        first = self.service._build_daily_scan_replay_plan(request)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(pb.rows), 2)
        self.assertEqual(first["summary"]["daily_selection_cache"]["rebuilt_days"], 2)
        self.assertEqual(first["summary"]["daily_selection_cache"]["written_days"], 2)

        calls.clear()
        self.service._evaluate_historical_scan_symbol = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cache hit should skip historical scan")
        )

        second = self.service._build_daily_scan_replay_plan(request)

        self.assertEqual(calls, [])
        self.assertEqual(second["symbols"], ["NVDA"])
        self.assertEqual(second["selection_plan"]["2026-04-22"], ["NVDA"])
        self.assertEqual(len(second["target_rows"]), 2)
        cache = second["summary"]["daily_selection_cache"]
        self.assertEqual(cache["hit_days"], 2)
        self.assertEqual(cache["rebuilt_days"], 0)
        self.assertEqual(cache["hit_rate"], 100.0)
        self.assertTrue(all(day["cache_hit"] for day in second["summary"]["daily"]))
        self.assertTrue(all(row["extra"]["daily_selection_cache_hit"] for row in second["target_rows"]))

    def test_daily_selection_cache_force_rebuild_ignores_existing_rows(self):
        self._install_cache_scan_fakes(self.service)
        calls = []
        self.service._evaluate_historical_scan_symbol = lambda symbol, trade_date, request, settings=None: {
            "symbol": symbol,
            "score": 8,
            "direction_bias": "long",
            "quality_gate_passed": True,
            "reason": "quality ok",
            "extra": {"scan_cutoff_ms": self.service._build_scan_cutoff_ms(trade_date, "09:25")},
        }
        request = self._cache_request()
        self.service._build_daily_scan_replay_plan(request)

        def evaluate(symbol, trade_date, request, settings=None):
            calls.append((trade_date, symbol))
            return {
                "symbol": symbol,
                "score": 9,
                "direction_bias": "long",
                "quality_gate_passed": True,
                "reason": "rebuilt",
                "extra": {"scan_cutoff_ms": self.service._build_scan_cutoff_ms(trade_date, "09:25")},
            }

        self.service._evaluate_historical_scan_symbol = evaluate
        rebuilt_request = {**request, "daily_selection_cache_force_rebuild": True}
        plan = self.service._build_daily_scan_replay_plan(rebuilt_request)

        self.assertEqual(len(calls), 2)
        self.assertEqual(plan["summary"]["daily_selection_cache"]["rebuilt_days"], 2)
        self.assertEqual(plan["summary"]["daily_selection_cache"]["stale_days"], 2)
        self.assertEqual(plan["target_rows"][0]["score"], 9)

    def test_daily_selection_cache_trust_existing_ignores_input_hash_drift(self):
        self._install_cache_scan_fakes(self.service)
        self.service._evaluate_historical_scan_symbol = lambda symbol, trade_date, request, settings=None: {
            "symbol": symbol,
            "score": 8,
            "direction_bias": "long",
            "quality_gate_passed": True,
            "reason": "quality ok",
            "extra": {"scan_cutoff_ms": self.service._build_scan_cutoff_ms(trade_date, "09:25")},
        }
        request = self._cache_request()
        self.service._build_daily_scan_replay_plan(request)
        self.service._build_daily_selection_input_fingerprint = lambda trade_date, universe_rows, request: {
            "usable": True,
            "hash": f"changed-{trade_date}",
            "reason": "bars_aggregate",
            "row_count": len(universe_rows),
        }
        self.service._evaluate_historical_scan_symbol = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("trusted cache hit should skip historical scan")
        )

        plan = self.service._build_daily_scan_replay_plan({**request, "daily_selection_cache_trust_existing": True})

        cache = plan["summary"]["daily_selection_cache"]
        self.assertEqual(cache["hit_days"], 2)
        self.assertEqual(cache["rebuilt_days"], 0)
        self.assertEqual(plan["symbols"], ["NVDA"])

    def test_daily_selection_cache_requires_usable_input_fingerprint(self):
        self._install_cache_scan_fakes(self.service)
        self.service._evaluate_historical_scan_symbol = lambda symbol, trade_date, request, settings=None: {
            "symbol": symbol,
            "score": 8,
            "direction_bias": "long",
            "quality_gate_passed": True,
            "reason": "quality ok",
            "extra": {"scan_cutoff_ms": self.service._build_scan_cutoff_ms(trade_date, "09:25")},
        }
        request = self._cache_request()
        self.service._build_daily_scan_replay_plan(request)

        calls = []
        self.service._build_daily_selection_input_fingerprint = lambda trade_date, universe_rows, request: {
            "usable": False,
            "hash": "",
            "reason": "missing_daily_coverage",
            "row_count": 0,
        }

        def evaluate(symbol, trade_date, request, settings=None):
            calls.append((trade_date, symbol))
            return {
                "symbol": symbol,
                "score": 7,
                "direction_bias": "long",
                "quality_gate_passed": True,
                "reason": "recomputed",
                "extra": {"scan_cutoff_ms": self.service._build_scan_cutoff_ms(trade_date, "09:25")},
            }

        self.service._evaluate_historical_scan_symbol = evaluate
        plan = self.service._build_daily_scan_replay_plan(request)

        self.assertEqual(len(calls), 2)
        cache = plan["summary"]["daily_selection_cache"]
        self.assertEqual(cache["hit_days"], 0)
        self.assertEqual(cache["input_unusable_days"], 2)
        self.assertEqual(cache["reason_counts"]["missing_daily_coverage"], 2)
        self.assertEqual(plan["target_rows"][0]["score"], 7)

    def test_daily_selection_cache_read_only_does_not_write_rows(self):
        pb = self._install_cache_scan_fakes(self.service)
        calls = []

        def evaluate(symbol, trade_date, request, settings=None):
            calls.append((trade_date, symbol))
            return {
                "symbol": symbol,
                "score": 8,
                "direction_bias": "long",
                "quality_gate_passed": True,
                "reason": "quality ok",
                "extra": {"scan_cutoff_ms": self.service._build_scan_cutoff_ms(trade_date, "09:25")},
            }

        self.service._evaluate_historical_scan_symbol = evaluate
        request = {**self._cache_request(), "daily_selection_cache_mode": "read_only"}
        plan = self.service._build_daily_scan_replay_plan(request)

        self.assertEqual(len(calls), 2)
        self.assertEqual(pb.rows, {})
        cache = plan["summary"]["daily_selection_cache"]
        self.assertEqual(cache["mode"], "read_only")
        self.assertEqual(cache["rebuilt_days"], 2)
        self.assertEqual(cache["written_days"], 0)

    def test_daily_selection_cache_key_changes_when_rules_change(self):
        service = BacktestService(None)
        base = self._cache_request()
        settings = {
            "scan_time_et": "09:25",
            "min_avg_10d_volume": 100000,
            "min_premarket_volume": 5000,
            "min_atr_pct": 0.15,
            "min_abs_day_change_pct": 1.0,
        }

        first = service._build_daily_selection_cache_base_fingerprint(base, settings)
        changed_cutoff = service._build_daily_selection_cache_base_fingerprint(
            {**base, "premarket_cutoff_time": "09:15"},
            settings,
        )
        changed_strategy = service._build_daily_selection_cache_base_fingerprint(
            {**base, "params": {"strategy_params": {"signal_window_max_bars": 20}}},
            settings,
        )

        self.assertNotEqual(first["cache_key"], changed_cutoff["cache_key"])
        self.assertNotEqual(first["cache_key"], changed_strategy["cache_key"])

    def test_effective_warmup_covers_indicator_and_signal_window(self):
        service = BacktestService(None)
        request = {
            **self._cache_request(),
            "warmup_bars": 160,
            "scan_warmup_bars": 160,
            "params": {"strategy_params": {"signal_window_max_bars": 36}},
        }

        expected_floor = indicator_ready_bar_count(request["params"]["strategy_params"]) + 44

        self.assertGreaterEqual(service._effective_indicator_warmup_bars(request, "warmup_bars"), BACKTEST_WARMUP_BARS)
        self.assertGreaterEqual(service._effective_indicator_warmup_bars(request, "warmup_bars"), expected_floor)
        self.assertGreaterEqual(service._effective_indicator_warmup_bars(request, "scan_warmup_bars"), expected_floor)

    def test_backtest_scan_setting_overrides_are_request_scoped(self):
        settings = self.service._apply_historical_scan_setting_overrides(
            {
                "min_avg_10d_volume": 100000,
                "min_premarket_volume": 5000,
                "min_atr_pct": 0.15,
                "min_abs_day_change_pct": 1.0,
            },
            {
                "daily_scan_min_avg_10d_volume": 50000,
                "daily_scan_min_premarket_volume": 2000,
                "daily_scan_min_abs_day_change_pct": 0.5,
            },
        )

        self.assertEqual(settings["min_avg_10d_volume"], 50000)
        self.assertEqual(settings["min_premarket_volume"], 2000)
        self.assertEqual(settings["min_abs_day_change_pct"], 0.5)
        self.assertEqual(settings["min_atr_pct"], 0.15)
        self.assertEqual(settings["override_source"], "backtest_request")

    def test_daily_selection_sd_rank_keeps_rejected_candidates(self):
        self.service._build_historical_sd_admission = lambda symbol, trade_date, request, candidate=None: {
            "passed": symbol == "NVDA",
            "reason": "sd_window_admitted" if symbol == "NVDA" else "no_window",
            "sd_admitted_at_ms": 123 if symbol == "NVDA" else 0,
        }
        candidates = [
            {"symbol": "AMD", "score": 10, "extra": {}},
            {"symbol": "NVDA", "score": 9, "extra": {}},
        ]
        request = {
            **self._cache_request(),
            "daily_selection_reuse_live_admission": True,
            "daily_selection_sd_mode": "rank",
            "daily_selection_candidate_limit": 20,
        }

        ranked, summary = self.service._apply_historical_sd_admission_to_candidates("2026-04-22", candidates, request)

        self.assertEqual([item["symbol"] for item in ranked], ["NVDA", "AMD"])
        self.assertEqual(summary["mode"], "rank")
        self.assertEqual(summary["sd_admitted_count"], 1)
        self.assertEqual(summary["sd_rejected_count"], 1)

    def test_scan_history_check_uses_recent_bar_count_not_calendar_window(self):
        service = BacktestService(None)
        cutoff_ms = service._build_scan_cutoff_ms("2025-05-12", "09:20")
        interval_ms = 5 * 60 * 1000
        rows = [
            {
                "bar_time_ms": cutoff_ms - interval_ms * (index + 1),
                "us_time": datetime.fromtimestamp((cutoff_ms - interval_ms * (index + 1)) / 1000, ET).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
            for index in range(BACKTEST_WARMUP_BARS + 30)
        ]

        def fake_load(symbol, environment, **kwargs):
            self.assertTrue(kwargs.get("descending"))
            self.assertGreaterEqual(int(kwargs.get("limit") or 0), BACKTEST_WARMUP_BARS)
            return list(rows)

        service._load_bar_rows_from_sqlite = fake_load
        service._backfill_symbol_history = mock.Mock(side_effect=AssertionError("unexpected backfill"))

        summary = service._ensure_scan_history_available(
            "NVDA",
            {"source_environment": "live", "scan_warmup_bars": BACKTEST_WARMUP_BARS, "preflight_backfill": True},
            cutoff_ms,
        )

        self.assertTrue(summary["ok"])
        self.assertFalse(summary["needed"])
        service._backfill_symbol_history.assert_not_called()

    def test_scan_history_check_honors_preflight_backfill_false(self):
        service = BacktestService(None)
        cutoff_ms = service._build_scan_cutoff_ms("2025-05-12", "09:20")
        service._load_bar_rows_from_sqlite = lambda *args, **kwargs: []
        service._backfill_symbol_history = mock.Mock(side_effect=AssertionError("unexpected backfill"))

        summary = service._ensure_scan_history_available(
            "NVDA",
            {"source_environment": "live", "scan_warmup_bars": BACKTEST_WARMUP_BARS, "preflight_backfill": False},
            cutoff_ms,
        )

        self.assertFalse(summary["ok"])
        self.assertTrue(summary["needed"])
        self.assertTrue(summary["backfill_skipped"])
        self.assertEqual(summary["reason"], "no_rows")
        service._backfill_symbol_history.assert_not_called()


if __name__ == "__main__":
    unittest.main()
