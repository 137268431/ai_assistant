import sys
import unittest
from datetime import datetime
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.backtest.constants import ET
from ibkr_compute.market.backtest_preload import BacktestPreloadCoordinator
from ibkr_compute.market.timeframe_utils import interval_to_ms


class DummyConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_bool_for_environment(self, key, _environment, fallback=False):
        value = self.values.get(key, fallback)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def get_int_for_environment(self, key, _environment, fallback=0):
        return int(self.values.get(key, fallback))

    def get_float_for_environment(self, key, _environment, fallback=0.0):
        return float(self.values.get(key, fallback))


class FakeBacktestService:
    def __init__(self):
        self.calls = []
        self.persisted_rows = []

    def _backfill_symbol_history(self, symbol, environment, start_ms, end_ms, interval="5m", **kwargs):
        self.calls.append(
            {
                "symbol": symbol,
                "environment": environment,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "interval": interval,
                "kwargs": kwargs,
            }
        )
        return {
            "ok": True,
            "reason": "ok",
            "batches": 1,
            "raw_points": 1,
            "rows": [
                {
                    "symbol": symbol,
                    "environment": environment,
                    "exchange": "SMART",
                    "interval": "5m",
                    "open": 1,
                    "high": 2,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": 100,
                    "bar_time_ms": start_ms,
                    "us_time": "2026-04-24 00:00:00",
                    "cn_time": "2026-04-24 12:00:00",
                    "session_type": "regular",
                    "extra": {"source": "ibkr_history_backfill"},
                }
            ],
        }

    def _dedupe_backfill_rows(self, rows):
        return list(rows or [])

    def _persist_backfill_rows(self, rows):
        self.persisted_rows.extend(list(rows or []))
        return len(rows or [])


class BacktestPreloadCoordinatorTest(unittest.TestCase):
    def test_default_range_uses_latest_complete_et_date_and_14_day_lookback(self):
        coordinator = BacktestPreloadCoordinator(config=DummyConfig(), environment="live", autostart_workers=False)

        date_from, date_to, lookback_days = coordinator._resolve_date_range(
            environment="live",
            now=datetime(2026, 5, 9, 12, 0, tzinfo=ET),
        )

        self.assertEqual("2026-05-08", date_to)
        self.assertEqual("2026-04-24", date_from)
        self.assertEqual(14, lookback_days)

    def test_enqueue_dedupes_by_symbol_environment_range_and_warmup(self):
        coordinator = BacktestPreloadCoordinator(config=DummyConfig(), environment="live", autostart_workers=False)

        first = coordinator.enqueue("aapl", date_to="2026-05-08")
        second = coordinator.enqueue(["AAPL"], date_to="2026-05-08")

        self.assertEqual(1, len(first["queued"]))
        self.assertEqual("2026-04-24", first["date_from"])
        self.assertEqual("2026-05-08", first["date_to"])
        self.assertEqual(1, len(second["deduped"]))
        self.assertEqual(1, coordinator.status()["pending"])

    def test_run_job_preloads_warmup_buffer_window_and_marks_rows(self):
        service = FakeBacktestService()
        coordinator = BacktestPreloadCoordinator(
            config=DummyConfig({"ibkr_backtest_preload_request_spacing": 0}),
            environment="live",
            backtest_service=service,
            autostart_workers=False,
        )
        result = coordinator.enqueue(["AAPL"], environment="live", date_from="2026-04-24", date_to="2026-05-08")
        job = result["queued"][0]

        coordinator._run_job(job["job_key"])

        status = coordinator.status(include_jobs=True)
        self.assertEqual(1, status["succeeded"])
        call = service.calls[0]
        self.assertEqual("AAPL", call["symbol"])
        self.assertEqual("live", call["environment"])
        self.assertEqual("5m", call["interval"])
        self.assertEqual(job["end_ms"], call["end_ms"])
        self.assertEqual(job["preload_start_ms"], call["start_ms"])
        self.assertEqual(
            interval_to_ms("5m") * (job["warmup_bars"] + job["buffer_bars"]),
            job["start_ms"] - job["preload_start_ms"],
        )
        self.assertEqual(1, len(service.persisted_rows))
        extra = service.persisted_rows[0]["extra"]
        self.assertEqual("ibkr_backtest_preload", extra["source"])
        self.assertTrue(extra["canonical"])
        self.assertEqual("default_backtest_range", extra["backfill_scope"])


if __name__ == "__main__":
    unittest.main()
