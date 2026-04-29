import sys
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.timeframe_utils import format_us_time
from ibkr_compute.orchestration.runtime_pipeline import TradingServiceRuntimePipelineMixin


ET = ZoneInfo("America/New_York")
STEP_MS = 5 * 60 * 1000


def _bar(symbol: str, bar_time_ms: int) -> dict:
    return {
        "symbol": symbol,
        "interval": "5m",
        "bar_time_ms": bar_time_ms,
        "us_time": format_us_time(bar_time_ms),
        "cn_time": "",
        "session_type": "regular",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000.0,
        "extra": {},
    }


class _FakeWriter:
    def __init__(self):
        self.pending_rows = []
        self.flushed_rows = []
        self.flush_calls = 0

    def write_bar(self, payload: dict) -> bool:
        self.pending_rows.append(dict(payload))
        return True

    def flush(self) -> bool:
        self.flush_calls += 1
        if self.pending_rows:
            self.flushed_rows.extend(self.pending_rows)
            self.pending_rows = []
        return True


class _FakeBackfill:
    def __init__(self, writer: _FakeWriter, initial_rows=None, incremental_rows=None, repair_rows=None):
        self.writer = writer
        self.initial_rows = [dict(row) for row in (initial_rows or [])]
        self.incremental_rows = [dict(row) for row in (incremental_rows or [])]
        self.repair_rows = [dict(row) for row in (repair_rows or [])]
        self.repair_fetch_calls = 0

    def _visible_rows(self, symbol: str) -> list[dict]:
        return [
            dict(row)
            for row in (self.initial_rows + self.writer.flushed_rows)
            if str(row.get("symbol") or "").upper() == symbol
        ]

    def get_latest_stored_bar_ms(self, symbol: str, interval: str) -> int:
        rows = self._visible_rows(symbol)
        if not rows:
            return 0
        return max(int(row.get("bar_time_ms", 0) or 0) for row in rows)

    def fetch_history(
        self,
        conid: int,
        symbol: str,
        interval: str = "5m",
        exchange: str = "",
        repair: bool = False,
        request_period: str | None = None,
    ) -> list[dict]:
        if repair:
            self.repair_fetch_calls += 1
            return [dict(row) for row in self.repair_rows]
        return [dict(row) for row in self.incremental_rows]

    def get_required_sequence_snapshot(
        self,
        symbol: str,
        interval: str = "5m",
        start_ms: int = 0,
        end_ms: int = 0,
        example_limit: int = 4,
    ) -> dict:
        expected = list(range(int(start_ms), int(end_ms) + STEP_MS, STEP_MS))
        stored = {
            int(row.get("bar_time_ms", 0) or 0)
            for row in self._visible_rows(symbol)
        }
        missing = [bar_time_ms for bar_time_ms in expected if bar_time_ms not in stored]
        return {
            "symbol": symbol,
            "interval": interval,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "expected_count": len(expected),
            "stored_count": len(stored),
            "missing_count": len(missing),
            "missing_bar_times": missing,
            "missing_us_times": [format_us_time(value) for value in missing[: max(1, int(example_limit or 0))]],
            "query_error": "",
        }


class _FakePB:
    def __init__(self, cursor_map=None):
        self.cursor_map = dict(cursor_map or {})

    def get_state(self, key: str, environment: str, date: str = "global") -> dict:
        del environment, date
        if key != "compute_cursors":
            return {}
        return {
            "data": {
                "cursors": dict(self.cursor_map),
            }
        }


class _DummyPipeline(TradingServiceRuntimePipelineMixin):
    def __init__(
        self,
        due_bucket_ms: int,
        last_completed_bucket_ms: int,
        data_writer: _FakeWriter,
        data_backfill: _FakeBackfill,
        pb=None,
        snapshot=None,
    ):
        self._running = True
        self._starting = False
        self._official_5m_lock = threading.Lock()
        self._official_5m_state = {
            "enabled": True,
            "driver": "ibkr_history_close",
            "close_delay_sec": 8,
            "request_period": "1d",
            "last_run": "",
            "last_due_bucket_ms": 0,
            "last_due_bucket_us": "",
            "last_completed_bucket_ms": last_completed_bucket_ms,
            "last_completed_bucket_us": format_us_time(last_completed_bucket_ms) if last_completed_bucket_ms > 0 else "",
            "last_written_bars": 0,
            "written_symbols": [],
            "written_symbols_total": 0,
            "pending_symbols": [],
            "pending_symbols_total": 0,
            "pending_symbol_details": [],
            "sequence_gap_count": 0,
            "missing_required_bars_total": 0,
            "last_error": "",
        }
        self._official_5m_last_cycle_at = 0.0
        self._last_bar_close_at = 0.0
        self._due_bucket_ms = due_bucket_ms
        self.data_writer = data_writer
        self.data_backfill = data_backfill
        self.pb = pb or _FakePB()
        self.snapshot = snapshot
        self.compute_events = []

    def _official_5m_enabled(self) -> bool:
        return True

    def _official_5m_close_delay_sec(self) -> int:
        return 8

    def _official_5m_request_period(self) -> str:
        return "1d"

    def _latest_safe_closed_5m_ms(self) -> int:
        return self._due_bucket_ms

    def _warmup_snapshot_from_subscriptions(self) -> dict:
        if self.snapshot is not None:
            return dict(self.snapshot)
        return {
            "symbols": ["AAPL"],
            "monitor_symbols": [],
            "conid_map": {"AAPL": 1},
            "symbol_meta": {"AAPL": {"exchange": "NASDAQ"}},
        }

    def _normalize_symbol_list(self, symbols) -> list[str]:
        return sorted({str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()})

    def _queue_compute_event(self, source: str, bar_count: int = 0, symbols=None):
        self.compute_events.append(
            {
                "source": source,
                "bar_count": bar_count,
                "symbols": list(symbols or []),
            }
        )

    def _now_iso(self) -> str:
        return "2026-04-17T15:00:00Z"

    def _non_monitor_pending_symbols(self, symbols, monitor_symbols):
        return list(symbols or [])


class Official5mCloseFlushTest(unittest.TestCase):
    def _service_mod(self):
        logger = SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )
        return SimpleNamespace(ENVIRONMENT="live", logger=logger)

    def test_flushes_incremental_write_before_sequence_validation(self):
        due_bucket_ms = int(datetime(2026, 4, 17, 10, 50, tzinfo=ET).timestamp() * 1000)
        writer = _FakeWriter()
        backfill = _FakeBackfill(
            writer,
            initial_rows=[],
            incremental_rows=[_bar("AAPL", due_bucket_ms)],
            repair_rows=[],
        )
        pipeline = _DummyPipeline(
            due_bucket_ms=due_bucket_ms,
            last_completed_bucket_ms=0,
            data_writer=writer,
            data_backfill=backfill,
        )

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_official_5m_close_cycle()

        state = pipeline._copy_official_5m_state()
        self.assertGreaterEqual(writer.flush_calls, 1)
        self.assertEqual(backfill.repair_fetch_calls, 0)
        self.assertEqual(state["pending_symbols_total"], 0)
        self.assertEqual(state["last_completed_bucket_ms"], due_bucket_ms)
        self.assertEqual(len(writer.flushed_rows), 1)

    def test_flushes_repair_write_before_revalidation(self):
        due_bucket_ms = int(datetime(2026, 4, 17, 10, 45, tzinfo=ET).timestamp() * 1000)
        initial_rows = [
            _bar("AAPL", int(datetime(2026, 4, 17, 10, 35, tzinfo=ET).timestamp() * 1000)),
        ]
        writer = _FakeWriter()
        backfill = _FakeBackfill(
            writer,
            initial_rows=initial_rows,
            incremental_rows=[_bar("AAPL", due_bucket_ms)],
            repair_rows=[_bar("AAPL", int(datetime(2026, 4, 17, 10, 40, tzinfo=ET).timestamp() * 1000))],
        )
        pipeline = _DummyPipeline(
            due_bucket_ms=due_bucket_ms,
            last_completed_bucket_ms=int(datetime(2026, 4, 17, 10, 30, tzinfo=ET).timestamp() * 1000),
            data_writer=writer,
            data_backfill=backfill,
        )

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_official_5m_close_cycle()

        state = pipeline._copy_official_5m_state()
        self.assertGreaterEqual(writer.flush_calls, 2)
        self.assertEqual(backfill.repair_fetch_calls, 1)
        self.assertEqual(state["pending_symbols_total"], 0)
        self.assertEqual(state["last_completed_bucket_ms"], due_bucket_ms)
        self.assertEqual(
            sorted(int(row["bar_time_ms"]) for row in writer.flushed_rows),
            sorted(
                [
                    int(datetime(2026, 4, 17, 10, 40, tzinfo=ET).timestamp() * 1000),
                    due_bucket_ms,
                ]
            ),
        )

    def test_dispatches_compute_when_persisted_due_bucket_is_ahead_of_compute_cursor(self):
        due_bucket_ms = int(datetime(2026, 4, 17, 10, 50, tzinfo=ET).timestamp() * 1000)
        previous_bucket_ms = due_bucket_ms - STEP_MS
        writer = _FakeWriter()
        backfill = _FakeBackfill(
            writer,
            initial_rows=[_bar("AAPL", due_bucket_ms)],
            incremental_rows=[],
            repair_rows=[],
        )
        pipeline = _DummyPipeline(
            due_bucket_ms=due_bucket_ms,
            last_completed_bucket_ms=previous_bucket_ms,
            data_writer=writer,
            data_backfill=backfill,
            pb=_FakePB(cursor_map={"AAPL|5m": previous_bucket_ms}),
        )

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_official_5m_close_cycle()

        self.assertEqual(
            pipeline.compute_events,
            [
                {
                    "source": "canonical_close",
                    "bar_count": 0,
                    "symbols": ["AAPL"],
                }
            ],
        )

    def test_default_close_cycle_only_tracks_trade_symbols(self):
        due_bucket_ms = int(datetime(2026, 4, 17, 10, 50, tzinfo=ET).timestamp() * 1000)
        previous_bucket_ms = due_bucket_ms - STEP_MS
        writer = _FakeWriter()
        backfill = _FakeBackfill(
            writer,
            initial_rows=[_bar("AAPL", previous_bucket_ms)],
            incremental_rows=[_bar("AAPL", due_bucket_ms)],
            repair_rows=[],
        )
        pipeline = _DummyPipeline(
            due_bucket_ms=due_bucket_ms,
            last_completed_bucket_ms=previous_bucket_ms,
            data_writer=writer,
            data_backfill=backfill,
            snapshot={
                "symbols": ["AAPL", "MSFT"],
                "trade_symbols": ["AAPL"],
                "monitor_symbols": ["MSFT"],
                "conid_map": {"AAPL": 1, "MSFT": 2},
                "symbol_meta": {
                    "AAPL": {"exchange": "NASDAQ"},
                    "MSFT": {"exchange": "NASDAQ"},
                },
            },
        )

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_official_5m_close_cycle()

        state = pipeline._copy_official_5m_state()
        self.assertEqual(state["pending_symbols"], [])
        self.assertEqual(state["written_symbols"], ["AAPL"])
        self.assertEqual(len(writer.flushed_rows), 1)
        self.assertEqual(writer.flushed_rows[0]["symbol"], "AAPL")

    def test_startup_cycle_fetches_missing_official_bars_instead_of_restoring_only(self):
        session_start_ms = int(datetime(2026, 4, 17, 9, 30, tzinfo=ET).timestamp() * 1000)
        previous_bucket_ms = int(datetime(2026, 4, 17, 10, 30, tzinfo=ET).timestamp() * 1000)
        due_bucket_ms = int(datetime(2026, 4, 17, 10, 40, tzinfo=ET).timestamp() * 1000)
        initial_rows = [
            _bar("AAPL", bar_time_ms)
            for bar_time_ms in range(session_start_ms, previous_bucket_ms + STEP_MS, STEP_MS)
        ]
        missing_rows = [
            _bar("AAPL", previous_bucket_ms + STEP_MS),
            _bar("AAPL", due_bucket_ms),
        ]
        writer = _FakeWriter()
        backfill = _FakeBackfill(
            writer,
            initial_rows=initial_rows,
            incremental_rows=missing_rows,
            repair_rows=[],
        )
        pipeline = _DummyPipeline(
            due_bucket_ms=due_bucket_ms,
            last_completed_bucket_ms=0,
            data_writer=writer,
            data_backfill=backfill,
        )
        pipeline._starting = True

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_official_5m_close_cycle()

        state = pipeline._copy_official_5m_state()
        self.assertEqual(state["pending_symbols_total"], 0)
        self.assertEqual(state["last_completed_bucket_ms"], due_bucket_ms)
        self.assertEqual(
            sorted(int(row["bar_time_ms"]) for row in writer.flushed_rows),
            sorted(int(row["bar_time_ms"]) for row in missing_rows),
        )


if __name__ == "__main__":
    unittest.main()
