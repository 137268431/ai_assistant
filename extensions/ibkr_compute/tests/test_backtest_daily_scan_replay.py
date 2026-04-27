import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

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
