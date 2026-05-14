import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest import request_utils
from ibkr_compute.backtest import runtime_service as service_mod
from ibkr_compute.backtest.runtime_service import BacktestService
from ibkr_compute.market.timeframe_utils import ET


class FakeIndicatorEngine:
    def __init__(self, symbol: str, interval: str, params=None):
        self.symbol = symbol
        self.interval = interval
        self.params = params or {}
        self.bar_count = 0

    def update(self, bar: dict) -> dict:
        self.bar_count += 1
        return {
            "bar_time_ms": int(bar.get("bar_time_ms", 0) or 0),
            "close": float(bar.get("close", 0) or 0),
            "atr": 1.0,
            "atr_raw": 1.0,
            "atr_pct": 1.0,
            "crsi": 50,
            "obv_rsi": 50,
            "vwap_dist": 0,
        }

    def is_ready(self):
        return True


class FakeSignalGenerator:
    signals_by_symbol_ms = {}

    def __init__(self, symbol: str, interval: str, params=None):
        self.symbol = symbol
        self.interval = interval
        self.params = params or {}

    def update(self, snapshot: dict):
        signal = self.signals_by_symbol_ms.get((self.symbol, int(snapshot.get("bar_time_ms", 0) or 0)))
        return copy.deepcopy(signal) if signal else None

    def daily_reset(self):
        return None


def build_bars(symbol: str, start_ms: int, count: int = 45) -> list[dict]:
    bars = []
    for index in range(count):
        bar_ms = start_ms + index * 5 * 60 * 1000
        et_time = datetime.fromtimestamp(bar_ms / 1000, tz=ET)
        bars.append(
            {
                "symbol": symbol,
                "exchange": "SMART",
                "interval": "5m",
                "bar_time_ms": bar_ms,
                "us_time": et_time.strftime("%Y-%m-%d %H:%M:%S"),
                "cn_time": "",
                "open": 101.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 100000 + index,
                "session_type": "regular",
            }
        )
    return bars


class BacktestPortfolioStreamTests(unittest.TestCase):
    def setUp(self):
        self.original_engine = service_mod.IndicatorEngine
        self.original_signal_generator = service_mod.SignalGenerator
        service_mod.IndicatorEngine = FakeIndicatorEngine
        service_mod.SignalGenerator = FakeSignalGenerator
        FakeSignalGenerator.signals_by_symbol_ms = {}
        self.service = BacktestService(None)

    def tearDown(self):
        service_mod.IndicatorEngine = self.original_engine
        service_mod.SignalGenerator = self.original_signal_generator

    def _request(self, **overrides):
        payload = {
            "name": "portfolio stream test",
            "symbol_source": "manual",
            "symbols": "AAPL,NVDA",
            "date_from": "2026-04-01",
            "date_to": "2026-04-01",
            "source_environment": "live",
            "session_mode": "regular",
            "initial_capital": 10000,
            "execution_model": "portfolio_stream",
            "borrow_limit_mode": "fixed",
            "max_borrow_amount": 0,
            "position_limit_max": 3,
            "signal_validity_minutes": 30,
            "trade_window_start_time": "09:35",
            "trade_window_end_time": "15:30",
            "order_window_end_time": "15:00",
            "simultaneous_signal_priority": "daily_target_rank",
            "compare_with_tv": False,
            "compare_tv_signals": False,
            "persist_backtest_indicators": False,
        }
        payload.update(overrides)
        return request_utils.normalize_request(payload)

    def test_simultaneous_signals_use_daily_rank_before_quality_and_capital_rejects_second(self):
        start = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        start_ms = int(start.timestamp() * 1000)
        signal_ms = start_ms + 40 * 5 * 60 * 1000
        bars_by_symbol = {
            "AAPL": build_bars("AAPL", start_ms),
            "NVDA": build_bars("NVDA", start_ms),
        }
        base_signal = {
            "direction": "long",
            "signal": "mr_L",
            "entry": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "shares": 100,
            "reason": "unit-test",
            "extra": {},
        }
        FakeSignalGenerator.signals_by_symbol_ms = {
            ("AAPL", signal_ms): {**base_signal, "rr": 1.5},
            ("NVDA", signal_ms): {**base_signal, "rr": 9.0},
        }
        self.service._load_symbol_bars = lambda symbol, *args, **kwargs: list(bars_by_symbol[symbol])
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []
        request = self._request()
        target_rows = [
            {"symbol": "AAPL", "date": "2026-04-01", "rank": 1, "score": 7.0},
            {"symbol": "NVDA", "date": "2026-04-01", "rank": 2, "score": 9.0},
        ]

        result = self.service._run_portfolio_stream_backtest(
            ["AAPL", "NVDA"],
            request,
            allowed_trade_days_by_symbol={"AAPL": {"2026-04-01"}, "NVDA": {"2026-04-01"}},
            target_rows=target_rows,
        )

        signals = {row["symbol"]: row for row in result["signal_rows"]}
        self.assertEqual(signals["AAPL"]["status"], "executed")
        self.assertEqual(signals["NVDA"]["status"], "skipped")
        self.assertEqual(signals["NVDA"]["extra"]["signal_status_reason"], "buying_power_exceeded")
        self.assertEqual(result["portfolio_metrics"]["portfolio_rejection_counts"]["buying_power_exceeded"], 1)
        self.assertEqual(result["portfolio_metrics"]["portfolio_candidate_samples"][0]["symbol"], "AAPL")

    def test_zero_position_limit_allows_more_than_three_daily_entries(self):
        start = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        start_ms = int(start.timestamp() * 1000)
        signal_ms = start_ms + 5 * 60 * 1000
        symbols = ["AAPL", "NVDA", "MSFT", "TSLA"]
        bars_by_symbol = {symbol: build_bars(symbol, start_ms) for symbol in symbols}
        base_signal = {
            "direction": "long",
            "signal": "mr_L",
            "entry": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "shares": 10,
            "reason": "unit-test",
            "rr": 1.5,
            "extra": {},
        }
        FakeSignalGenerator.signals_by_symbol_ms = {
            (symbol, signal_ms): dict(base_signal) for symbol in symbols
        }
        self.service._load_symbol_bars = lambda symbol, *args, **kwargs: list(bars_by_symbol[symbol])
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []
        request = self._request(
            symbols=",".join(symbols),
            initial_capital=100000,
            position_limit_max=0,
        )

        result = self.service._run_portfolio_stream_backtest(
            symbols,
            request,
            allowed_trade_days_by_symbol={symbol: {"2026-04-01"} for symbol in symbols},
        )

        signals = {row["symbol"]: row for row in result["signal_rows"]}
        self.assertEqual([signals[symbol]["status"] for symbol in symbols], ["executed", "executed", "executed", "executed"])
        self.assertNotIn("position_limit_reached", result["portfolio_metrics"]["portfolio_rejection_counts"])
        self.assertEqual(result["portfolio_metrics"]["portfolio_risk"]["position_limit_max"], 0)

    def test_consecutive_stop_losses_stop_later_entries_for_same_day(self):
        start = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        start_ms = int(start.timestamp() * 1000)
        first_signal_ms = start_ms + 5 * 60 * 1000
        later_signal_ms = start_ms + 15 * 60 * 1000
        symbols = ["AAPL", "NVDA", "MSFT", "GOOG"]
        bars_by_symbol = {symbol: build_bars(symbol, start_ms) for symbol in symbols}
        stop_signal = {
            "direction": "long",
            "signal": "mr_L",
            "entry": 100.0,
            "stop_loss": 99.5,
            "take_profit": 110.0,
            "shares": 10,
            "reason": "unit-test",
            "rr": 1.5,
            "extra": {},
        }
        late_signal = {
            **stop_signal,
            "stop_loss": 95.0,
        }
        FakeSignalGenerator.signals_by_symbol_ms = {
            ("AAPL", first_signal_ms): dict(stop_signal),
            ("NVDA", first_signal_ms): dict(stop_signal),
            ("MSFT", first_signal_ms): dict(stop_signal),
            ("GOOG", later_signal_ms): dict(late_signal),
        }
        self.service._load_symbol_bars = lambda symbol, *args, **kwargs: list(bars_by_symbol[symbol])
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []
        request = self._request(
            symbols=",".join(symbols),
            initial_capital=100000,
            position_limit_max=0,
            consecutive_stop_loss_limit=3,
        )

        result = self.service._run_portfolio_stream_backtest(
            symbols,
            request,
            allowed_trade_days_by_symbol={symbol: {"2026-04-01"} for symbol in symbols},
        )

        signals = {row["symbol"]: row for row in result["signal_rows"]}
        self.assertEqual(signals["GOOG"]["status"], "skipped")
        self.assertEqual(signals["GOOG"]["extra"]["signal_status_reason"], "sl_circuit_breaker")
        self.assertEqual(result["portfolio_metrics"]["portfolio_rejection_counts"]["sl_circuit_breaker"], 1)
        self.assertEqual(result["portfolio_metrics"]["portfolio_risk"]["max_consecutive_stop_loss_count"], 3)

    def test_indicator_rows_stay_memory_only_when_persistence_disabled(self):
        start = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        start_ms = int(start.timestamp() * 1000)
        bars_by_symbol = {
            "AAPL": build_bars("AAPL", start_ms),
            "NVDA": build_bars("NVDA", start_ms),
        }
        self.service._load_symbol_bars = lambda symbol, *args, **kwargs: list(bars_by_symbol[symbol])
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []

        result = self.service._run_portfolio_stream_backtest(["AAPL", "NVDA"], self._request())

        self.assertEqual(result["indicator_rows"], [])
        self.assertEqual(result["indicator_count"], 90)

    def test_indicator_rows_can_be_captured_for_debug_runs(self):
        start = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        start_ms = int(start.timestamp() * 1000)
        bars_by_symbol = {
            "AAPL": build_bars("AAPL", start_ms),
            "NVDA": build_bars("NVDA", start_ms),
        }
        self.service._load_symbol_bars = lambda symbol, *args, **kwargs: list(bars_by_symbol[symbol])
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []

        result = self.service._run_portfolio_stream_backtest(
            ["AAPL", "NVDA"],
            self._request(persist_backtest_indicators=True),
        )

        self.assertEqual(len(result["indicator_rows"]), 90)
        self.assertEqual(result["indicator_count"], 90)

    def test_daily_selected_backtest_loads_only_selected_symbol_days(self):
        day1 = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        day2 = datetime(2026, 4, 2, 9, 35, tzinfo=ET)
        day1_ms = int(day1.timestamp() * 1000)
        day2_ms = int(day2.timestamp() * 1000)
        bars_by_key = {
            ("AAPL", "2026-04-01"): build_bars("AAPL", day1_ms),
            ("NVDA", "2026-04-02"): build_bars("NVDA", day2_ms),
        }
        load_calls = []

        def load_symbol_bars(symbol, environment, date_from, date_to, session_mode, allow_backfill=True):
            load_calls.append((symbol, date_from, date_to))
            return list(bars_by_key[(symbol, date_from)])

        self.service._load_symbol_bars = load_symbol_bars
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []
        request = self._request(
            symbol_source="daily_scan_replay",
            symbols="",
            date_from="2026-04-01",
            date_to="2026-04-02",
            daily_selected_only=True,
        )
        selection_plan = {
            "2026-04-01": ["AAPL"],
            "2026-04-02": ["NVDA"],
        }

        result = self.service._run_portfolio_daily_selected_backtest(
            ["AAPL", "NVDA"],
            request,
            selection_plan,
            target_rows=[
                {"symbol": "AAPL", "date": "2026-04-01", "rank": 1, "score": 9.0, "extra": {}},
                {"symbol": "NVDA", "date": "2026-04-02", "rank": 1, "score": 8.0, "extra": {}},
            ],
        )

        self.assertEqual(load_calls, [("AAPL", "2026-04-01", "2026-04-01"), ("NVDA", "2026-04-02", "2026-04-02")])
        self.assertEqual(result["indicator_count"], 90)
        profile = result["portfolio_metrics"]["daily_selected_profile"]
        self.assertTrue(profile["enabled"])
        self.assertEqual(profile["trade_dates"], 2)
        self.assertEqual(profile["selected_symbol_days"], 2)

    def test_daily_scan_replay_execute_uses_daily_selected_runner(self):
        request = self._request(
            symbol_source="daily_scan_replay",
            symbols="",
            date_from="2026-04-01",
            date_to="2026-04-02",
            daily_selected_only=False,
        )
        request["daily_selected_only"] = False
        request["params"]["daily_selected_only"] = False
        selection_plan = {"2026-04-01": ["AAPL"], "2026-04-02": ["NVDA"]}
        target_rows = [
            {"symbol": "AAPL", "date": "2026-04-01", "rank": 1, "score": 9.0, "extra": {}},
            {"symbol": "NVDA", "date": "2026-04-02", "rank": 1, "score": 8.0, "extra": {}},
        ]
        calls = []
        updates = []

        self.service._prepare_backtest_account_model = lambda *_args, **_kwargs: {}
        self.service._update_run = lambda run_id, patch: updates.append((run_id, patch))
        self.service._build_daily_scan_replay_plan = lambda *_args, **_kwargs: {
            "symbols": ["AAPL", "NVDA"],
            "target_rows": target_rows,
            "summary": {"mode": "daily_scan_replay"},
            "selection_plan": selection_plan,
        }
        self.service._persist_backtest_targets = lambda run_id, rows: {
            "collection": "ibkr_backtest_targets",
            "run_id": run_id,
            "attempted_count": len(rows),
            "saved_count": len(rows),
            "error_count": 0,
            "status": "ok",
            "errors": [],
        }
        self.service._preflight_backfill_symbols = lambda *_args, **_kwargs: {"enabled": False, "symbols": ["AAPL", "NVDA"]}
        self.service._build_benchmark_curve = lambda *_args, **_kwargs: []
        self.service._persist_backtest_signals = lambda run_id, rows: self.service._empty_capture_summary("ibkr_backtest_signals", run_id)
        self.service._persist_backtest_reverse_signals = lambda run_id, rows: self.service._empty_capture_summary("ibkr_backtest_reverse_signals", run_id)
        self.service._persist_trades = lambda *_args, **_kwargs: None

        def daily_selected_runner(symbols, runner_request, runner_selection_plan, **_kwargs):
            calls.append((symbols, runner_request["daily_selected_only"], runner_selection_plan))
            return {
                "trades": [],
                "indicator_rows": [],
                "indicator_count": 0,
                "signal_rows": [],
                "reverse_rows": [],
                "skipped_symbols": [],
                "data_quality": [],
                "tv_symbol_reports": [],
                "portfolio_metrics": {"execution_model": "portfolio_stream"},
            }

        self.service._run_portfolio_daily_selected_backtest = daily_selected_runner
        self.service._run_portfolio_stream_backtest = lambda *_args, **_kwargs: self.fail(
            "daily_scan_replay should not run the full-union portfolio stream"
        )

        result = self.service._execute_run("daily_selected_route", request)

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [(["AAPL", "NVDA"], True, selection_plan)])
        self.assertTrue(request["daily_selected_only"])
        self.assertTrue(any(patch.get("status") == "completed" for _run_id, patch in updates))

    def test_preflight_refreshes_daily_coverage_after_repair(self):
        request = self._request(
            symbol_source="manual",
            symbols="AAPL,NVDA",
            date_from="2026-04-01",
            date_to="2026-04-01",
            backfill_concurrency=1,
        )
        start_ms, _end_ms = self.service._date_to_ms_range("2026-04-01", "2026-04-01")
        calls = []

        def fake_coverage(symbol, *_args, refresh_daily_coverage=False, **_kwargs):
            calls.append((symbol, refresh_daily_coverage))
            if len(calls) <= 2:
                if symbol == "AAPL":
                    return {
                        "symbol": symbol,
                        "needs_backfill": True,
                        "repair_windows": [
                            {
                                "start_ms": start_ms,
                                "end_ms": start_ms + 5 * 60 * 1000,
                                "reason": "bad_bar",
                            }
                        ],
                    }
                return {"symbol": symbol, "needs_backfill": False, "repair_windows": []}
            return {"symbol": symbol, "needs_backfill": False, "repair_windows": []}

        self.service._symbol_range_coverage_summary = fake_coverage
        self.service._backfill_symbol_history = lambda *_args, **_kwargs: {
            "ok": True,
            "reason": "ok",
            "batches": 1,
            "rows": [{"symbol": "AAPL", "bar_time_ms": start_ms}],
        }
        self.service._dedupe_backfill_rows = lambda rows: list(rows)
        self.service._persist_backfill_rows = lambda rows: len(rows)
        self.service._rollup_symbol_history = lambda *_args, **_kwargs: 1

        summary = self.service._preflight_backfill_symbols(["AAPL", "NVDA"], request)

        self.assertEqual(summary["needed_symbols"], ["AAPL"])
        self.assertEqual(summary["results"]["AAPL"]["persisted_rows"], 1)
        self.assertIn(("AAPL", True), calls)
        self.assertIn(("NVDA", False), calls)
        self.assertNotIn(("NVDA", True), calls)

    def test_daily_selected_preflight_skips_bad_bar_only_repairs(self):
        request = self._request(
            symbol_source="daily_scan_replay",
            symbols="AAPL,NVDA",
            date_from="2026-04-01",
            date_to="2026-04-02",
            daily_selected_only=True,
        )
        request["daily_selected_only"] = True
        request["params"]["daily_selected_only"] = True
        request["symbol_source"] = "daily_scan_replay"
        start_ms, _end_ms = self.service._date_to_ms_range("2026-04-01", "2026-04-01")

        def fake_coverage(symbol, *_args, **_kwargs):
            if symbol == "AAPL":
                return {
                    "symbol": symbol,
                    "needs_backfill": True,
                    "repair_windows": [
                        {
                            "start_ms": start_ms,
                            "end_ms": start_ms + 5 * 60 * 1000,
                            "reason": "bad_bar",
                        }
                    ],
                }
            return {"symbol": symbol, "needs_backfill": False, "repair_windows": []}

        self.service._symbol_range_coverage_summary = fake_coverage
        self.service._backfill_symbol_history = lambda *_args, **_kwargs: self.fail("bad_bar-only windows should not be backfilled")

        summary = self.service._preflight_backfill_symbols(["AAPL", "NVDA"], request)

        self.assertEqual(summary["needed_symbols"], [])
        self.assertEqual(summary["skipped_bad_bar_symbols"], ["AAPL"])
        self.assertIn("AAPL", summary["skipped_bad_bar_windows"])
        self.assertEqual(summary["results"], {})

    def test_daily_selected_reuses_daily_close_lookup_cache(self):
        request = self._request(
            symbol_source="daily_scan_replay",
            symbols="AAPL",
            date_from="2026-04-01",
            date_to="2026-04-02",
            daily_selected_only=True,
        )
        selection_plan = {"2026-04-01": ["AAPL"], "2026-04-02": ["AAPL"]}
        lookup_calls = []
        seen_cache_ids = []
        progress_windows = []

        def load_lookup(symbol, *_args, **_kwargs):
            lookup_calls.append(symbol)
            return [{"date": "2026-03-31", "close": 100.0, "bar_time_ms": 1}]

        def daily_runner(symbols, runner_request, *_args, **kwargs):
            self.assertEqual(symbols, ["AAPL"])
            cache = runner_request.get("_daily_close_lookup_cache")
            self.assertIn("AAPL", cache)
            seen_cache_ids.append(id(cache))
            progress_windows.append(kwargs.get("progress_context") or {})
            return {
                "trades": [],
                "indicator_rows": [],
                "indicator_count": 0,
                "signal_rows": [],
                "reverse_rows": [],
                "skipped_symbols": [],
                "data_quality": [],
                "tv_symbol_reports": [],
                "portfolio_metrics": {
                    "portfolio_profile": {"bars_loaded": 0, "bar_times": 0},
                    "portfolio_rejection_counts": {},
                    "portfolio_realized_pnl": 0,
                },
            }

        self.service._load_daily_close_lookup = load_lookup
        self.service._run_portfolio_stream_backtest = daily_runner

        result = self.service._run_portfolio_daily_selected_backtest(["AAPL"], request, selection_plan)

        self.assertEqual(lookup_calls, ["AAPL"])
        self.assertEqual(len(set(seen_cache_ids)), 1)
        self.assertEqual([(item.get("start"), item.get("end")) for item in progress_windows], [(16, 50), (50, 85)])
        profile = result["portfolio_metrics"]["daily_selected_profile"]
        self.assertEqual(profile["daily_close_lookup_cache"]["symbols"], 1)
        self.assertEqual(profile["daily_close_lookup_cache"]["rows"], 1)

    def test_running_progress_does_not_regress_for_same_run(self):
        self.service._active_run_id = "run_1"
        self.service._active_batch_id = ""
        self.service._progress = {
            "status": "queued",
            "run_id": "run_1",
            "batch_id": "",
            "mode": "single",
            "progress": 0,
            "stage": "queued",
            "message": "",
            "updated_at_ms": 0,
        }

        self.service._set_progress("running", "daily_selected_stream", "day 10", 70)
        self.service._set_progress("running", "loading", "loading nested day", 20)

        self.assertEqual(self.service._progress["progress"], 70)
        self.assertEqual(self.service._progress["stage"], "loading")

        self.service._progress["status"] = "cancelling"
        self.service._progress["progress"] = 80
        self.service._set_progress("running", "preflight_backfill", "late worker update", 13)
        self.assertEqual(self.service._progress["status"], "cancelling")
        self.assertEqual(self.service._progress["progress"], 80)

        self.service._active_run_id = "run_2"
        self.service._set_progress("running", "bootstrap", "new run", 5)
        self.assertEqual(self.service._progress["progress"], 5)

    def test_portfolio_target_filters_skip_low_rank_score_and_direction_mismatch(self):
        start = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        start_ms = int(start.timestamp() * 1000)
        signal_ms = start_ms + 5 * 60 * 1000
        symbols = ["AAPL", "NVDA", "MSFT", "TSLA"]
        bars_by_symbol = {symbol: build_bars(symbol, start_ms) for symbol in symbols}
        base_signal = {
            "direction": "long",
            "signal": "mr_L",
            "entry": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "shares": 10,
            "reason": "unit-test",
            "extra": {},
        }
        FakeSignalGenerator.signals_by_symbol_ms = {
            ("AAPL", signal_ms): {**base_signal, "rr": 1.5},
            ("NVDA", signal_ms): {**base_signal, "rr": 1.5},
            ("MSFT", signal_ms): {**base_signal, "rr": 1.5},
            ("TSLA", signal_ms): {**base_signal, "direction": "short", "signal": "mr_U", "rr": 1.5},
        }
        self.service._load_symbol_bars = lambda symbol, *args, **kwargs: list(bars_by_symbol[symbol])
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []
        request = self._request(
            symbols="AAPL,NVDA,MSFT,TSLA",
            initial_capital=50000,
            portfolio_require_target_direction_alignment=True,
            portfolio_max_target_rank=1,
            portfolio_min_target_score=5,
        )
        target_rows = [
            {"symbol": "AAPL", "date": "2026-04-01", "rank": 1, "score": 9.0, "direction_bias": "long"},
            {"symbol": "NVDA", "date": "2026-04-01", "rank": 2, "score": 9.0, "direction_bias": "long"},
            {"symbol": "MSFT", "date": "2026-04-01", "rank": 1, "score": 4.0, "direction_bias": "long"},
            {"symbol": "TSLA", "date": "2026-04-01", "rank": 1, "score": 8.0, "direction_bias": "long"},
        ]

        result = self.service._run_portfolio_stream_backtest(
            symbols,
            request,
            allowed_trade_days_by_symbol={symbol: {"2026-04-01"} for symbol in symbols},
            target_rows=target_rows,
        )

        signals = {row["symbol"]: row for row in result["signal_rows"]}
        self.assertEqual(signals["AAPL"]["status"], "executed")
        self.assertEqual(signals["NVDA"]["status"], "skipped")
        self.assertEqual(signals["NVDA"]["extra"]["signal_status_reason"], "target_rank_over_limit")
        self.assertEqual(signals["MSFT"]["status"], "skipped")
        self.assertEqual(signals["MSFT"]["extra"]["signal_status_reason"], "target_score_below_min")
        self.assertEqual(signals["TSLA"]["status"], "skipped")
        self.assertEqual(signals["TSLA"]["extra"]["signal_status_reason"], "target_direction_mismatch")
        self.assertEqual(result["portfolio_metrics"]["portfolio_rejection_counts"]["target_rank_over_limit"], 1)
        self.assertEqual(result["portfolio_metrics"]["portfolio_rejection_counts"]["target_score_below_min"], 1)
        self.assertEqual(result["portfolio_metrics"]["portfolio_rejection_counts"]["target_direction_mismatch"], 1)

    def test_portfolio_blocks_overextended_mean_reversion_when_enabled(self):
        start = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        start_ms = int(start.timestamp() * 1000)
        signal_ms = start_ms + 5 * 60 * 1000
        symbols = ["AAPL", "NVDA", "MSFT"]
        bars_by_symbol = {symbol: build_bars(symbol, start_ms) for symbol in symbols}
        base_signal = {
            "entry": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "shares": 10,
            "reason": "unit-test",
            "rr": 1.5,
        }
        FakeSignalGenerator.signals_by_symbol_ms = {
            (
                "AAPL",
                signal_ms,
            ): {
                **base_signal,
                "direction": "long",
                "signal": "mr_sdLower",
                "extra": {"signal_mode": "mr", "crsi_state": "overbought", "sd_zone": "normal"},
            },
            (
                "NVDA",
                signal_ms,
            ): {
                **base_signal,
                "direction": "short",
                "signal": "mr_sdUpper",
                "extra": {"signal_mode": "mr", "crsi_state": "normal", "sd_zone": "oversold"},
            },
            (
                "MSFT",
                signal_ms,
            ): {
                **base_signal,
                "direction": "long",
                "signal": "mr_sdLower",
                "extra": {"signal_mode": "mr", "crsi_state": "normal", "sd_zone": "normal"},
            },
        }
        self.service._load_symbol_bars = lambda symbol, *args, **kwargs: list(bars_by_symbol[symbol])
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []
        request = self._request(
            symbols="AAPL,NVDA,MSFT",
            initial_capital=50000,
            portfolio_block_mr_overextended_state=True,
        )
        target_rows = [
            {"symbol": "AAPL", "date": "2026-04-01", "rank": 1, "score": 9.0, "direction_bias": "long"},
            {"symbol": "NVDA", "date": "2026-04-01", "rank": 2, "score": 9.0, "direction_bias": "short"},
            {"symbol": "MSFT", "date": "2026-04-01", "rank": 3, "score": 9.0, "direction_bias": "long"},
        ]

        result = self.service._run_portfolio_stream_backtest(
            symbols,
            request,
            allowed_trade_days_by_symbol={symbol: {"2026-04-01"} for symbol in symbols},
            target_rows=target_rows,
        )

        signals = {row["symbol"]: row for row in result["signal_rows"]}
        self.assertEqual(signals["AAPL"]["status"], "skipped")
        self.assertEqual(signals["AAPL"]["extra"]["signal_status_reason"], "mr_long_overextended_state")
        self.assertEqual(signals["NVDA"]["status"], "skipped")
        self.assertEqual(signals["NVDA"]["extra"]["signal_status_reason"], "mr_short_overextended_state")
        self.assertEqual(signals["MSFT"]["status"], "executed")
        self.assertEqual(result["portfolio_metrics"]["portfolio_rejection_counts"]["mr_long_overextended_state"], 1)
        self.assertEqual(result["portfolio_metrics"]["portfolio_rejection_counts"]["mr_short_overextended_state"], 1)

    def test_portfolio_blocks_early_trend_without_ema_touch_when_enabled(self):
        start = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        start_ms = int(start.timestamp() * 1000)
        signal_ms = start_ms + 5 * 60 * 1000
        symbols = ["AAPL", "MSFT"]
        bars_by_symbol = {symbol: build_bars(symbol, start_ms) for symbol in symbols}
        base_signal = {
            "direction": "long",
            "signal": "trend_sdUpper",
            "entry": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "shares": 10,
            "reason": "unit-test",
            "rr": 1.5,
        }
        FakeSignalGenerator.signals_by_symbol_ms = {
            (
                "AAPL",
                signal_ms,
            ): {
                **base_signal,
                "extra": {"signal_mode": "trend", "dtp_phase": "early", "ema_touch_line": "none"},
            },
            (
                "MSFT",
                signal_ms,
            ): {
                **base_signal,
                "extra": {"signal_mode": "trend", "dtp_phase": "confirmed", "ema_touch_line": "none"},
            },
        }
        self.service._load_symbol_bars = lambda symbol, *args, **kwargs: list(bars_by_symbol[symbol])
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []
        request = self._request(
            symbols="AAPL,MSFT",
            initial_capital=50000,
            portfolio_block_early_trend_without_ema_touch=True,
        )
        target_rows = [
            {"symbol": "AAPL", "date": "2026-04-01", "rank": 1, "score": 9.0, "direction_bias": "long"},
            {"symbol": "MSFT", "date": "2026-04-01", "rank": 2, "score": 9.0, "direction_bias": "long"},
        ]

        result = self.service._run_portfolio_stream_backtest(
            symbols,
            request,
            allowed_trade_days_by_symbol={symbol: {"2026-04-01"} for symbol in symbols},
            target_rows=target_rows,
        )

        signals = {row["symbol"]: row for row in result["signal_rows"]}
        self.assertEqual(signals["AAPL"]["status"], "skipped")
        self.assertEqual(signals["AAPL"]["extra"]["signal_status_reason"], "trend_early_without_ema_touch")
        self.assertEqual(signals["MSFT"]["status"], "executed")
        self.assertEqual(result["portfolio_metrics"]["portfolio_rejection_counts"]["trend_early_without_ema_touch"], 1)

    def test_account_buying_power_sets_borrow_limit_from_snapshot(self):
        service = BacktestService(
            None,
            account_snapshot_provider=lambda: {
                "summary": {
                    "account_code": "DU123",
                    "buying_power": 25000,
                    "available_funds": 12000,
                    "net_liquidation": 15000,
                }
            },
        )
        request = self._request(borrow_limit_mode="account_buying_power", initial_capital=10000)

        limits = service._resolve_portfolio_risk_limits(request)

        self.assertEqual(limits["borrow_limit_mode"], "account_buying_power")
        self.assertEqual(limits["total_exposure_limit"], 25000)
        self.assertEqual(limits["max_borrow_amount"], 15000)
        self.assertEqual(limits["account_summary"]["account_code"], "DU123")

    def test_signal_expiry_matches_live_greater_than_validity_semantics(self):
        request = self._request(signal_validity_minutes=30)
        signal_bar_ms = int(datetime(2026, 4, 1, 9, 35, tzinfo=ET).timestamp() * 1000)
        signal = {"signal_bar_ms": signal_bar_ms}

        self.assertFalse(self.service._portfolio_signal_expired(signal, signal_bar_ms + 30 * 60 * 1000, request))
        self.assertTrue(self.service._portfolio_signal_expired(signal, signal_bar_ms + 31 * 60 * 1000, request))

    def test_signal_expiry_uses_per_signal_validity_override(self):
        request = self._request(signal_validity_minutes=30)
        signal_bar_ms = int(datetime(2026, 4, 1, 9, 35, tzinfo=ET).timestamp() * 1000)
        signal = {"signal_bar_ms": signal_bar_ms, "extra": {"validity_minutes": 5}}

        self.assertFalse(self.service._portfolio_signal_expired(signal, signal_bar_ms + 5 * 60 * 1000, request))
        self.assertTrue(self.service._portfolio_signal_expired(signal, signal_bar_ms + 6 * 60 * 1000, request))

    def test_setup_level_metrics_split_independent_setups(self):
        metrics = self.service._build_setup_level_metrics(
            [
                {
                    "signal_id": "sig-squeeze",
                    "signal": "sd_squeeze_breakout_long",
                    "direction": "long",
                    "status": "executed",
                    "extra": {"setup": "sd_squeeze_breakout_long", "signal_mode": "breakout"},
                },
                {
                    "signal_id": "sig-vwap",
                    "signal": "vwap_trend_pullback_short",
                    "direction": "short",
                    "status": "dropped",
                    "extra": {
                        "setup": "vwap_trend_pullback_short",
                        "signal_mode": "trend_pullback",
                        "signal_status_reason": "active_target_exists",
                    },
                },
            ],
            [
                {
                    "signal_id": "sig-squeeze",
                    "signal": "sd_squeeze_breakout_long",
                    "direction": "long",
                    "pnl": 25.0,
                    "exit_reason": "take_profit",
                    "extra": {"mfe": 3.0, "mae": 0.5},
                }
            ],
            [
                {
                    "signal_id": "sig-squeeze",
                    "signal": "sd_squeeze_breakout_long",
                    "direction": "long",
                    "action_type": "adjust_sl",
                    "extra": {},
                }
            ],
        )

        rows = {row["setup"]: row for row in metrics["setup_stats"]}
        self.assertEqual(metrics["setup_summary"]["setup_count"], 2)
        self.assertEqual(metrics["setup_summary"]["top_contributor"], "sd_squeeze_breakout_long")
        self.assertIn("sd_squeeze_breakout_long", rows)
        self.assertIn("vwap_trend_pullback_short", rows)
        self.assertEqual(rows["sd_squeeze_breakout_long"]["setup_family"], "breakout")
        self.assertEqual(rows["sd_squeeze_breakout_long"]["trade_count"], 1)
        self.assertEqual(rows["sd_squeeze_breakout_long"]["net_pnl"], 25.0)
        self.assertEqual(rows["sd_squeeze_breakout_long"]["reverse_action_breakdown"]["adjust_sl"], 1)
        self.assertEqual(rows["vwap_trend_pullback_short"]["signal_rejection_breakdown"]["active_target_exists"], 1)
        self.assertEqual(rows["vwap_trend_pullback_short"]["setup_family"], "trend_pullback")

    def test_marketable_limit_pending_fill_uses_signal_entry_price(self):
        signal_bar_ms = int(datetime(2026, 4, 1, 10, 0, tzinfo=ET).timestamp() * 1000)
        pending = {
            "symbol": "AAPL",
            "direction": "long",
            "signal": "intraday_sd_v1",
            "signal_id": "sig-market-limit",
            "entry_price": 102.0,
            "take_profit": 108.0,
            "stop_loss": 98.0,
            "shares": 10,
            "signal_bar_ms": signal_bar_ms,
            "extra": {"entry_order_type": "marketable_limit", "setup": "orb_vwap"},
            "entry_order_type": "marketable_limit",
            "setup": "orb_vwap",
        }
        bar = {
            "bar_time_ms": signal_bar_ms + 5 * 60 * 1000,
            "us_time": "2026-04-01 10:05:00",
            "cn_time": "",
            "open": 101.0,
            "high": 103.0,
            "low": 100.0,
            "close": 102.0,
            "session_type": "regular",
        }

        position = self.service._check_pending_entry_fill("AAPL", bar, pending, 0.0, 0.0)

        self.assertIsNotNone(position)
        self.assertEqual(position["entry_limit_price"], 102.0)
        self.assertEqual(position["entry_price"], 101.0)
        self.assertEqual(position["entry_order_type"], "passive")
        self.assertEqual(position["setup"], "orb_vwap")

    def test_passive_pending_limit_does_not_fill_until_price_touched(self):
        signal_bar_ms = int(datetime(2026, 4, 1, 10, 0, tzinfo=ET).timestamp() * 1000)
        cases = [
            (
                "long",
                "passive",
                100.0,
                {
                    "open": 100.5,
                    "high": 101.25,
                    "low": 100.01,
                    "close": 100.75,
                },
            ),
            (
                "short",
                "limit",
                100.0,
                {
                    "open": 99.5,
                    "high": 99.99,
                    "low": 98.75,
                    "close": 99.25,
                },
            ),
            (
                "long",
                "marketable_limit",
                100.0,
                {
                    "open": 100.5,
                    "high": 101.0,
                    "low": 100.01,
                    "close": 100.8,
                },
            ),
        ]

        for direction, entry_order_type, entry_price, prices in cases:
            with self.subTest(direction=direction, entry_order_type=entry_order_type):
                pending = {
                    "symbol": "AAPL",
                    "direction": direction,
                    "signal": "intraday_sd_v1",
                    "signal_id": f"sig-no-touch-{direction}-{entry_order_type}",
                    "entry_price": entry_price,
                    "take_profit": 108.0 if direction == "long" else 92.0,
                    "stop_loss": 98.0 if direction == "long" else 102.0,
                    "shares": 10,
                    "signal_bar_ms": signal_bar_ms,
                    "extra": {"entry_order_type": entry_order_type, "setup": "orb_vwap"},
                    "entry_order_type": entry_order_type,
                    "setup": "orb_vwap",
                }
                bar = {
                    "bar_time_ms": signal_bar_ms + 5 * 60 * 1000,
                    "us_time": "2026-04-01 10:05:00",
                    "cn_time": "",
                    **prices,
                    "session_type": "regular",
                }

                position = self.service._check_pending_entry_fill("AAPL", bar, pending, 0.0, 0.0)

                self.assertIsNone(position)

    def test_order_window_rejects_late_new_signals(self):
        request = self._request(order_window_end_time="15:00")
        before_cutoff = int(datetime(2026, 4, 1, 15, 0, tzinfo=ET).timestamp() * 1000)
        after_cutoff = int(datetime(2026, 4, 1, 15, 5, tzinfo=ET).timestamp() * 1000)

        self.assertTrue(self.service._portfolio_bar_in_order_window(before_cutoff, request))
        self.assertFalse(self.service._portfolio_bar_in_order_window(after_cutoff, request))

    def test_audit_trail_links_target_signal_fill_and_exit(self):
        start = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        start_ms = int(start.timestamp() * 1000)
        signal_ms = start_ms + 5 * 60 * 1000
        bars_by_symbol = {"AAPL": build_bars("AAPL", start_ms)}
        FakeSignalGenerator.signals_by_symbol_ms = {
            ("AAPL", signal_ms): {
                "direction": "long",
                "signal": "mr_L",
                "entry": 100.0,
                "stop_loss": 95.0,
                "take_profit": 102.0,
                "shares": 10,
                "reason": "unit-test",
                "rr": 1.5,
                "extra": {},
            }
        }
        self.service._load_symbol_bars = lambda symbol, *args, **kwargs: list(bars_by_symbol[symbol])
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []
        request = self._request(symbols="AAPL", initial_capital=50000)
        target_rows = [
            {"symbol": "AAPL", "date": "2026-04-01", "rank": 1, "score": 9.0, "direction_bias": "long"},
        ]

        result = self.service._run_portfolio_stream_backtest(
            ["AAPL"],
            request,
            allowed_trade_days_by_symbol={"AAPL": {"2026-04-01"}},
            target_rows=target_rows,
        )
        audit = self.service._build_backtest_audit_trail(
            request,
            target_rows,
            result["signal_rows"],
            result["trades"],
            result["reverse_rows"],
        )

        signal_history = result["signal_rows"][0]["extra"]["status_history"]
        self.assertEqual([item["status"] for item in signal_history], ["generated", "pending", "executed"])
        self.assertEqual(audit["focus_date"], "2026-04-01")
        self.assertEqual(audit["focus_symbols"], ["AAPL"])
        for event_type in ("target_selected", "signal_generated", "signal_pending", "entry_filled", "trade_opened", "trade_closed"):
            self.assertGreaterEqual(audit["event_type_counts"].get(event_type, 0), 1)
        closed_events = [item for item in audit["focus_day"]["timeline"] if item["event_type"] == "trade_closed"]
        self.assertEqual(closed_events[0]["status"], "take_profit")
        self.assertEqual(closed_events[0]["take_profit"], 102.0)

    def test_audit_trail_records_stop_price_adjustments(self):
        start = datetime(2026, 4, 1, 9, 35, tzinfo=ET)
        start_ms = int(start.timestamp() * 1000)
        signal_ms = start_ms + 5 * 60 * 1000
        bars_by_symbol = {"AAPL": build_bars("AAPL", start_ms)}
        FakeSignalGenerator.signals_by_symbol_ms = {
            ("AAPL", signal_ms): {
                "direction": "long",
                "signal": "mr_L",
                "entry": 100.0,
                "stop_loss": 95.0,
                "take_profit": 110.0,
                "shares": 10,
                "reason": "unit-test",
                "rr": 2.0,
                "extra": {},
            }
        }
        self.service._load_symbol_bars = lambda symbol, *args, **kwargs: list(bars_by_symbol[symbol])
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []
        request = self._request(symbols="AAPL", initial_capital=50000, atr_stop_min_profit_r=0.0)
        request["params"]["strategy_params"]["sl_atr_mult"] = 1.0

        result = self.service._run_portfolio_stream_backtest(["AAPL"], request)
        audit = self.service._build_backtest_audit_trail(
            request,
            [],
            result["signal_rows"],
            result["trades"],
            result["reverse_rows"],
        )

        adjustments = result["trades"][0]["extra"]["risk_adjustments"]
        self.assertGreaterEqual(len(adjustments), 1)
        self.assertEqual(adjustments[0]["event_type"], "atr_stop_adjust")
        self.assertEqual(adjustments[0]["old_sl"], 95.0)
        self.assertGreater(adjustments[0]["new_sl"], 95.0)
        risk_events = [item for item in audit["timeline"] if item["event_type"] == "atr_stop_adjust"]
        self.assertEqual(len(risk_events), 1)
        self.assertEqual(risk_events[0]["old_sl"], 95.0)
        self.assertGreater(risk_events[0]["new_sl"], 95.0)

    def test_backtest_capture_persists_signals_directly_to_sqlite(self):
        self.service.pb = SimpleNamespace(
            create_records=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("sqlite capture should avoid PocketBase batch writes")
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "data.db")
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE ibkr_backtest_signals (
                      id TEXT PRIMARY KEY,
                      run_id TEXT,
                      symbol TEXT,
                      signal_id TEXT,
                      bar_time_ms INTEGER,
                      extra TEXT,
                      created TEXT,
                      updated TEXT
                    )
                    """
                )
                conn.commit()

            with mock.patch("ibkr_compute.backtest.runtime_service.BACKTEST_SQLITE_PATH", db_path):
                summary = self.service._persist_backtest_signals(
                    "run_sqlite",
                    [
                        {
                            "symbol": "NVDA",
                            "signal_id": "sig_1",
                            "bar_time_ms": 123,
                            "extra": {"source": "unit"},
                        }
                    ],
                )

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT run_id, symbol, signal_id, extra FROM ibkr_backtest_signals"
                ).fetchone()

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["storage_source"], "sqlite")
        self.assertEqual(summary["saved_count"], 1)
        self.assertEqual(row[0], "run_sqlite")
        self.assertEqual(row[1], "NVDA")
        self.assertEqual(row[2], "sig_1")
        self.assertEqual(json.loads(row[3])["backtest_run_id"], "run_sqlite")


if __name__ == "__main__":
    unittest.main()
