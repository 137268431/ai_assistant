import copy
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

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

    def test_order_window_rejects_late_new_signals(self):
        request = self._request(order_window_end_time="15:00")
        before_cutoff = int(datetime(2026, 4, 1, 15, 0, tzinfo=ET).timestamp() * 1000)
        after_cutoff = int(datetime(2026, 4, 1, 15, 5, tzinfo=ET).timestamp() * 1000)

        self.assertTrue(self.service._portfolio_bar_in_order_window(before_cutoff, request))
        self.assertFalse(self.service._portfolio_bar_in_order_window(after_cutoff, request))


if __name__ == "__main__":
    unittest.main()
