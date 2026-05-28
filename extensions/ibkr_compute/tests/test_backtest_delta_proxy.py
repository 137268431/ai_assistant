import copy
import sys
import unittest
from datetime import datetime
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest import request_utils
from ibkr_compute.backtest import runtime_service as service_mod
from ibkr_compute.backtest.delta_proxy import (
    PROXY_DELTA_FILTER_REASON,
    compute_proxy_5m_delta_v1,
    evaluate_proxy_delta_gate,
)
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
                "open": 100.0,
                "high": 101.0,
                "low": 98.0,
                "close": 100.0,
                "volume": 10000,
                "session_type": "regular",
            }
        )
    return bars


class BacktestDeltaProxyTests(unittest.TestCase):
    def setUp(self):
        self.original_engine = service_mod.IndicatorEngine
        self.original_signal_generator = service_mod.SignalGenerator
        service_mod.IndicatorEngine = FakeIndicatorEngine
        service_mod.SignalGenerator = FakeSignalGenerator
        FakeSignalGenerator.signals_by_symbol_ms = {}
        self.service = BacktestService(None)
        self.service._load_symbol_warmup_bars = lambda *args, **kwargs: []
        self.service._load_daily_close_lookup = lambda *args, **kwargs: []

    def tearDown(self):
        service_mod.IndicatorEngine = self.original_engine
        service_mod.SignalGenerator = self.original_signal_generator

    def _request(self, **overrides):
        payload = {
            "name": "delta proxy test",
            "symbol_source": "manual",
            "symbols": "AAPL",
            "date_from": "2026-04-01",
            "date_to": "2026-04-01",
            "source_environment": "live",
            "session_mode": "regular",
            "initial_capital": 10000,
            "execution_model": "symbol_independent",
            "compare_with_tv": False,
            "compare_tv_signals": False,
            "persist_backtest_indicators": False,
            "preflight_backfill": False,
            "backtest_require_truth_proof": False,
        }
        payload.update(overrides)
        return request_utils.normalize_request(payload)

    def _seed_failing_long_signal(self, bars: list[dict]):
        signal_index = 5
        signal_bar = bars[signal_index]
        signal_bar.update({"open": 101.0, "high": 102.0, "low": 98.0, "close": 99.0, "volume": 10000})
        FakeSignalGenerator.signals_by_symbol_ms = {
            (
                "AAPL",
                int(signal_bar["bar_time_ms"]),
            ): {
                "direction": "long",
                "signal": "mr_L",
                "entry": 99.0,
                "stop_loss": 95.0,
                "take_profit": 105.0,
                "shares": 10,
                "rr": 1.5,
                "reason": "unit-test",
                "extra": {"strategy_profile": "core_two_setup_v1"},
            }
        }

    def test_request_defaults_delta_mode_off_and_threshold(self):
        request = self._request()

        self.assertEqual(request["backtest_delta_mode"], "off")
        self.assertAlmostEqual(request["backtest_delta_proxy_threshold"], 0.12)
        self.assertEqual(request["params"]["backtest_delta_mode"], "off")
        self.assertAlmostEqual(request["params"]["backtest_delta_proxy_threshold"], 0.12)

    def test_proxy_delta_computes_directional_ratio_from_ohlcv(self):
        proxy = compute_proxy_5m_delta_v1(
            {"open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0, "volume": 1000}
        )
        gate = evaluate_proxy_delta_gate(
            {"open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0, "volume": 1000},
            "long",
            mode="proxy_shadow",
            threshold=0.12,
        )

        self.assertEqual(proxy["version"], "proxy_5m_delta_v1")
        self.assertAlmostEqual(proxy["close_location"], 0.5)
        self.assertAlmostEqual(proxy["body_bias"], 0.25)
        self.assertAlmostEqual(proxy["delta_ratio"], 0.425)
        self.assertAlmostEqual(proxy["delta"], 425.0)
        self.assertTrue(gate["passed"])
        self.assertFalse(gate["filtered"])

    def test_proxy_shadow_records_gate_without_changing_signal_status_or_trades(self):
        start_ms = int(datetime(2026, 4, 1, 9, 35, tzinfo=ET).timestamp() * 1000)
        bars = build_bars("AAPL", start_ms)
        self._seed_failing_long_signal(bars)

        off_result = self.service._run_symbol_backtest("AAPL", copy.deepcopy(bars), self._request())
        shadow_result = self.service._run_symbol_backtest(
            "AAPL",
            copy.deepcopy(bars),
            self._request(backtest_delta_mode="proxy_shadow"),
        )
        off_trades, _off_quality, _off_tv, _off_indicators, off_signals, _off_reverses, _off_count = off_result
        shadow_trades, _quality, _tv, _indicators, shadow_signals, _reverses, _count = shadow_result

        self.assertEqual(len(shadow_trades), len(off_trades))
        self.assertEqual(shadow_signals[0]["status"], off_signals[0]["status"])
        self.assertEqual(shadow_signals[0]["status"], "executed")
        gate = shadow_signals[0]["extra"]["delta_gate"]
        self.assertFalse(gate["passed"])
        self.assertTrue(gate["would_filter"])
        self.assertFalse(gate["filtered"])

    def test_proxy_filter_skips_failing_generated_signal_before_pending_entry(self):
        start_ms = int(datetime(2026, 4, 1, 9, 35, tzinfo=ET).timestamp() * 1000)
        bars = build_bars("AAPL", start_ms)
        self._seed_failing_long_signal(bars)

        trades, _quality, _tv, _indicators, signals, _reverses, _count = self.service._run_symbol_backtest(
            "AAPL",
            copy.deepcopy(bars),
            self._request(backtest_delta_mode="proxy_filter"),
        )

        self.assertEqual(trades, [])
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["status"], "skipped")
        self.assertEqual(signals[0]["extra"]["signal_status_reason"], PROXY_DELTA_FILTER_REASON)
        self.assertFalse(signals[0]["extra"]["delta_gate"]["passed"])
        self.assertTrue(signals[0]["extra"]["delta_gate"]["filtered"])

    def test_execute_run_adds_delta_ab_summary_metrics(self):
        request = self._request(backtest_delta_mode="proxy_filter")
        gate = evaluate_proxy_delta_gate(
            {"open": 101.0, "high": 102.0, "low": 98.0, "close": 99.0, "volume": 10000},
            "long",
            mode="proxy_filter",
            threshold=0.12,
        )
        gate = {**gate, "filtered": True, "filter_reason": PROXY_DELTA_FILTER_REASON}
        signal_row = {
            "symbol": "AAPL",
            "direction": "long",
            "signal": "mr_L",
            "entry": 99.0,
            "stop_loss": 95.0,
            "take_profit": 105.0,
            "rr": "1.5",
            "shares": 10,
            "signal_id": "AAPL_delta_proxy_test",
            "us_time": "2026-04-01 10:00:00",
            "cn_time": "",
            "date": "2026-04-01",
            "bar_time_ms": 1,
            "status": "skipped",
            "extra": {
                "delta_gate": gate,
                "signal_status_reason": PROXY_DELTA_FILTER_REASON,
                "status_history": [
                    {"status": "generated", "reason": "signal_generated", "bar_time_ms": 1},
                    {"status": "skipped", "reason": PROXY_DELTA_FILTER_REASON, "bar_time_ms": 1},
                ],
            },
        }

        self.service._update_run = lambda *args, **kwargs: None
        self.service._preflight_backfill_symbols = lambda *args, **kwargs: {"enabled": False}
        self.service._load_symbol_bars = lambda *args, **kwargs: build_bars("AAPL", 1, count=40)
        self.service._run_symbol_backtest = lambda *args, **kwargs: (
            [],
            {"symbol": "AAPL", "bar_count": 40, "gap_count": 0, "status": "ok"},
            None,
            [],
            [copy.deepcopy(signal_row)],
            [],
            0,
        )
        self.service._build_benchmark_curve = lambda *args, **kwargs: []

        result = self.service._execute_run("delta_proxy_metrics", request)

        summary = result["metrics"]["delta_ab_summary"]
        self.assertEqual(summary["mode"], "proxy_filter")
        self.assertEqual(summary["evaluated_count"], 1)
        self.assertEqual(summary["pass_count"], 0)
        self.assertEqual(summary["fail_count"], 1)
        self.assertEqual(summary["filter_count"], 1)


if __name__ == "__main__":
    unittest.main()
