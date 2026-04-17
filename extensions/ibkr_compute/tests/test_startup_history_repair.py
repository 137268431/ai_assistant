import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration.lifecycle import TradingServiceLifecycleMixin


ET = ZoneInfo("America/New_York")


def _bucket_start_ms(value_ms: int, interval: str) -> int:
    if interval != "5m":
        raise ValueError(f"unsupported interval: {interval}")
    bucket_ms = 5 * 60 * 1000
    return int(value_ms // bucket_ms) * bucket_ms


def _bar_row(dt: datetime, session_type: str = "regular") -> dict:
    bar_time_ms = int(dt.timestamp() * 1000)
    return {
        "symbol": "AAPL",
        "interval": "5m",
        "bar_time_ms": bar_time_ms,
        "us_time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "session_type": session_type,
    }


class _FakePocketBase:
    def __init__(self, rows):
        self._rows = list(rows)

    def get_all_records(self, *_args, **_kwargs):
        return list(self._rows)


class DummyLifecycle(TradingServiceLifecycleMixin):
    def __init__(self, rows):
        self.pb = _FakePocketBase(rows)

    def _build_bar_environment_filter(self) -> str:
        return 'environment = "live"'

    def _official_5m_close_delay_sec(self) -> int:
        return 8

    def _trade_window_start(self) -> tuple[int, int]:
        return 9, 30

    def _trade_window_end(self) -> tuple[int, int]:
        return 16, 0


class StartupHistoryRepairTest(unittest.TestCase):
    def test_missing_latest_safe_regular_bar_requires_repair_during_startup(self):
        rows = []
        previous_day_start = datetime(2026, 4, 16, 11, 25, tzinfo=ET)
        for idx in range(49):
            rows.append(_bar_row(previous_day_start + timedelta(minutes=5 * idx)))

        today_start = datetime(2026, 4, 17, 9, 30, tzinfo=ET)
        for idx in range(11):
            rows.append(_bar_row(today_start + timedelta(minutes=5 * idx)))

        rows = sorted(rows, key=lambda row: int(row["bar_time_ms"]), reverse=True)
        lifecycle = DummyLifecycle(rows)
        service_mod = SimpleNamespace(
            ET=ET,
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
            interval_to_ms=lambda value: 5 * 60 * 1000 if value == "5m" else 0,
            bucket_start_ms=_bucket_start_ms,
            indicator_ready_bar_count=lambda: 60,
            _regular_session_gap_summary=lambda rows, interval, same_day_only=False, example_limit=4: {
                "gap_count": 0,
                "gap_examples": [],
            },
        )

        with mock.patch("ibkr_compute.orchestration.lifecycle._service_mod", return_value=service_mod):
            snapshot = lifecycle._collect_startup_history_repair_snapshot(
                "AAPL",
                et_now=datetime(2026, 4, 17, 10, 31, 44, tzinfo=ET),
            )

        self.assertTrue(snapshot["needs_history_fetch"])
        self.assertTrue(snapshot["needs_pipeline_repair"])
        self.assertEqual(snapshot["today_regular_latest_ms"], int(datetime(2026, 4, 17, 10, 20, tzinfo=ET).timestamp() * 1000))
        self.assertEqual(snapshot["expected_today_regular_ms"], int(datetime(2026, 4, 17, 10, 25, tzinfo=ET).timestamp() * 1000))
        self.assertIn("today_regular_incomplete=1", snapshot["repair_reason"])


if __name__ == "__main__":
    unittest.main()
