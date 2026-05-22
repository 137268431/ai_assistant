import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.timeframe_utils import format_us_time, normalize_interval
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
    def __init__(self, writer: _FakeWriter, initial_rows=None, incremental_rows=None, repair_rows=None, fetch_delays=None):
        self.writer = writer
        self.initial_rows = [dict(row) for row in (initial_rows or [])]
        self.incremental_rows_by_symbol = {
            str(symbol or "").upper(): [dict(row) for row in rows]
            for symbol, rows in (incremental_rows or {}).items()
        } if isinstance(incremental_rows, dict) else {}
        self.repair_rows_by_symbol = {
            str(symbol or "").upper(): [dict(row) for row in rows]
            for symbol, rows in (repair_rows or {}).items()
        } if isinstance(repair_rows, dict) else {}
        self.incremental_rows = [] if isinstance(incremental_rows, dict) else [dict(row) for row in (incremental_rows or [])]
        self.repair_rows = [] if isinstance(repair_rows, dict) else [dict(row) for row in (repair_rows or [])]
        self.repair_fetch_calls = 0
        self.backfill_all_calls = []
        self.fetch_delays = {
            str(symbol or "").upper(): float(delay or 0.0)
            for symbol, delay in (fetch_delays or {}).items()
        }
        self.fetch_started_symbols = set()
        self.fetch_finished_symbols = set()
        self.fetch_lock = threading.Lock()

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
        trace=None,
    ) -> list[dict]:
        if repair:
            self.repair_fetch_calls += 1
            if self.repair_rows_by_symbol:
                return [dict(row) for row in self.repair_rows_by_symbol.get(str(symbol or "").upper(), [])]
            return [dict(row) for row in self.repair_rows]
        normalized_symbol = str(symbol or "").upper()
        with self.fetch_lock:
            self.fetch_started_symbols.add(normalized_symbol)
        try:
            delay = float(self.fetch_delays.get(normalized_symbol, 0.0) or 0.0)
            if delay > 0:
                time.sleep(delay)
            if self.incremental_rows_by_symbol:
                return [dict(row) for row in self.incremental_rows_by_symbol.get(normalized_symbol, [])]
            return [dict(row) for row in self.incremental_rows]
        finally:
            with self.fetch_lock:
                self.fetch_finished_symbols.add(normalized_symbol)

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

    def backfill_all(self, conid_map, symbol_meta=None, intervals=None, repair_symbols=None, period_overrides=None, trace_source=""):
        requested_intervals = []
        for interval in (intervals or ["5m"]):
            normalized = normalize_interval(interval)
            if normalized and normalized not in requested_intervals:
                requested_intervals.append(normalized)
        self.backfill_all_calls.append(
            {
                "conid_map": dict(conid_map or {}),
                "symbol_meta": dict(symbol_meta or {}),
                "intervals": list(requested_intervals),
                "repair_symbols": list(repair_symbols or []),
                "period_overrides": {
                    symbol: dict(payload or {})
                    for symbol, payload in (period_overrides or {}).items()
                },
                "trace_source": str(trace_source or ""),
            }
        )
        return {symbol: {interval: 1 for interval in requested_intervals} for symbol in (conid_map or {})}


class _FakeConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def _get(self, key: str, default=None):
        return self.values.get(key, default)

    def get_bool_for_environment(self, key: str, environment: str, default=False):
        del environment
        value = self._get(key, default)
        return str(value).strip().lower() in {"true", "1", "yes", "on"}

    def get_for_environment(self, key: str, environment: str, default=None):
        del environment
        return self._get(key, default)

    def get_int_for_environment(self, key: str, environment: str, default=0):
        del environment
        return int(self._get(key, default))

    def get_float_for_environment(self, key: str, environment: str, default=0.0):
        del environment
        return float(self._get(key, default))


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
        watchlist_completion=None,
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
        self.watchlist_completion = watchlist_completion
        self.config = _FakeConfig()
        self._direct_topup_lock = threading.Lock()
        self._direct_topup_state = {
            "enabled": False,
            "driver": "ibkr_history_direct_topup",
            "intervals": ["15m", "30m", "1h", "4h", "1d"],
            "close_delay_sec": 30,
            "loop_interval_s": 5,
            "parallel_enabled": False,
            "interval_priority": ["4h", "1h", "30m", "15m", "1d"],
            "last_run": "",
            "last_error": "",
            "total_written_bars": 0,
            "last_completed_interval": "",
            "intervals_state": {},
        }
        self._direct_topup_due_ms = due_bucket_ms
        self.compute_events = []
        self.compute_event_snapshots = []
        self.compute_triggers = []
        self.watchlist_topup_requests = []

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
        self.compute_event_snapshots.append(
            {
                "source": source,
                "bar_count": bar_count,
                "symbols": list(symbols or []),
                "fetch_finished_symbols": sorted(getattr(self.data_backfill, "fetch_finished_symbols", set())),
            }
        )
        self.compute_events.append(
            {
                "source": source,
                "bar_count": bar_count,
                "symbols": list(symbols or []),
            }
        )

    def _trigger_realtime_compute(
        self,
        source: str = "bar_close",
        symbols=None,
        persist_signals=None,
        intervals=None,
        rollup_intervals=None,
    ):
        self.compute_triggers.append(
            {
                "source": source,
                "symbols": list(symbols or []),
                "persist_signals": persist_signals,
                "intervals": list(intervals or []),
                "rollup_intervals": list(rollup_intervals or []),
            }
        )
        return {"ok": True, "processed": 1, "signals": 0, "errors": 0}

    def _runtime_direct_topup_latest_due_ms(self, interval: str) -> int:
        del interval
        return self._direct_topup_due_ms

    def _watchlist_idle_topup_completion_snapshot(self) -> dict:
        if self.watchlist_completion is None:
            return {
                "total": 0,
                "fresh": 0,
                "stale": 0,
                "missing": 0,
                "unobserved": 0,
            }
        return dict(self.watchlist_completion)

    def _request_watchlist_idle_topup_now(self, *, ttl_s: float = 15.0) -> None:
        self.watchlist_topup_requests.append(
            {
                "ttl_s": ttl_s,
                "official_completed_ms": self._copy_official_5m_state().get("last_completed_bucket_ms"),
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
        return SimpleNamespace(
            ENVIRONMENT="live",
            DATA_ENVIRONMENT="live",
            logger=logger,
            DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS=("15m", "30m", "1h", "4h", "1d"),
            DEFAULT_RUNTIME_DIRECT_TOPUP_CLOSE_DELAY_SECONDS=30,
            DEFAULT_RUNTIME_DIRECT_TOPUP_LOOP_INTERVAL_SECONDS=5.0,
            DEFAULT_RUNTIME_DIRECT_TOPUP_PARALLEL_ENABLED=False,
            DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVAL_PRIORITY=("4h", "1h", "30m", "15m", "1d"),
            DEFAULT_RUNTIME_DIRECT_TOPUP_PERIODS={
                "15m": "2d",
                "30m": "3d",
                "1h": "5d",
                "4h": "20d",
                "1d": "60d",
            },
        )

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

    def test_default_close_cycle_refreshes_trade_and_monitor_but_computes_trade_only(self):
        due_bucket_ms = int(datetime(2026, 4, 17, 10, 50, tzinfo=ET).timestamp() * 1000)
        previous_bucket_ms = due_bucket_ms - STEP_MS
        writer = _FakeWriter()
        backfill = _FakeBackfill(
            writer,
            initial_rows=[
                _bar("AAPL", previous_bucket_ms),
                _bar("MSFT", previous_bucket_ms),
            ],
            incremental_rows={
                "AAPL": [_bar("AAPL", due_bucket_ms)],
                "MSFT": [_bar("MSFT", due_bucket_ms)],
            },
            repair_rows=[],
        )
        pipeline = _DummyPipeline(
            due_bucket_ms=due_bucket_ms,
            last_completed_bucket_ms=previous_bucket_ms,
            data_writer=writer,
            data_backfill=backfill,
            pb=_FakePB(cursor_map={"AAPL|5m": previous_bucket_ms, "MSFT|5m": previous_bucket_ms}),
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
        self.assertEqual(state["written_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(sorted(row["symbol"] for row in writer.flushed_rows), ["AAPL", "MSFT"])
        self.assertEqual(len(pipeline.compute_events), 1)
        self.assertEqual(pipeline.compute_events[0]["source"], "canonical_close")
        self.assertEqual(pipeline.compute_events[0]["symbols"], ["AAPL"])
        self.assertGreaterEqual(pipeline.compute_events[0]["bar_count"], 1)

    def test_streaming_close_queues_fast_symbol_before_slow_symbol_finishes(self):
        due_bucket_ms = int(datetime(2026, 4, 17, 10, 50, tzinfo=ET).timestamp() * 1000)
        previous_bucket_ms = due_bucket_ms - STEP_MS
        writer = _FakeWriter()
        backfill = _FakeBackfill(
            writer,
            initial_rows=[
                _bar("AAPL", previous_bucket_ms),
                _bar("MSFT", previous_bucket_ms),
            ],
            incremental_rows={
                "AAPL": [_bar("AAPL", due_bucket_ms)],
                "MSFT": [_bar("MSFT", due_bucket_ms)],
            },
            repair_rows={},
            fetch_delays={"MSFT": 0.1},
        )
        pipeline = _DummyPipeline(
            due_bucket_ms=due_bucket_ms,
            last_completed_bucket_ms=previous_bucket_ms,
            data_writer=writer,
            data_backfill=backfill,
            pb=_FakePB(cursor_map={"AAPL|5m": previous_bucket_ms, "MSFT|5m": previous_bucket_ms}),
            snapshot={
                "symbols": ["AAPL", "MSFT"],
                "trade_symbols": ["AAPL", "MSFT"],
                "monitor_symbols": [],
                "conid_map": {"AAPL": 1, "MSFT": 2},
                "symbol_meta": {
                    "AAPL": {"exchange": "NASDAQ"},
                    "MSFT": {"exchange": "NASDAQ"},
                },
            },
        )

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_official_5m_close_cycle()

        self.assertGreaterEqual(len(pipeline.compute_event_snapshots), 2)
        self.assertEqual(pipeline.compute_event_snapshots[0]["symbols"], ["AAPL"])
        self.assertIn("AAPL", pipeline.compute_event_snapshots[0]["fetch_finished_symbols"])
        self.assertNotIn("MSFT", pipeline.compute_event_snapshots[0]["fetch_finished_symbols"])
        self.assertEqual([event["symbols"] for event in pipeline.compute_events], [["AAPL"], ["MSFT"]])

    def test_close_cycle_fetches_multiple_trade_symbols_with_worker_trace(self):
        due_bucket_ms = int(datetime(2026, 4, 17, 10, 50, tzinfo=ET).timestamp() * 1000)
        previous_bucket_ms = due_bucket_ms - STEP_MS
        writer = _FakeWriter()
        backfill = _FakeBackfill(
            writer,
            initial_rows=[
                _bar("AAPL", previous_bucket_ms),
                _bar("MSFT", previous_bucket_ms),
            ],
            incremental_rows={
                "AAPL": [_bar("AAPL", due_bucket_ms)],
                "MSFT": [_bar("MSFT", due_bucket_ms)],
            },
            repair_rows={},
        )
        pipeline = _DummyPipeline(
            due_bucket_ms=due_bucket_ms,
            last_completed_bucket_ms=previous_bucket_ms,
            data_writer=writer,
            data_backfill=backfill,
            snapshot={
                "symbols": ["AAPL", "MSFT"],
                "trade_symbols": ["AAPL", "MSFT"],
                "monitor_symbols": [],
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
        self.assertEqual(state["pending_symbols_total"], 0)
        self.assertEqual(state["written_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(state["fetch_workers"], 2)
        self.assertTrue(str(state["last_trace_id"]).startswith("official5m_"))
        self.assertEqual(
            sorted(item["symbol"] for item in state["last_symbol_timings"]),
            ["AAPL", "MSFT"],
        )
        self.assertEqual(sorted(row["symbol"] for row in writer.flushed_rows), ["AAPL", "MSFT"])

    def test_close_cycle_queues_ready_symbol_compute_before_repaired_symbol_finishes(self):
        due_bucket_ms = int(datetime(2026, 4, 17, 10, 40, tzinfo=ET).timestamp() * 1000)
        previous_bucket_ms = due_bucket_ms - (2 * STEP_MS)
        middle_bucket_ms = due_bucket_ms - STEP_MS
        writer = _FakeWriter()
        backfill = _FakeBackfill(
            writer,
            initial_rows=[
                _bar("AAPL", previous_bucket_ms),
                _bar("MSFT", previous_bucket_ms),
            ],
            incremental_rows={
                "AAPL": [_bar("AAPL", middle_bucket_ms), _bar("AAPL", due_bucket_ms)],
                "MSFT": [_bar("MSFT", due_bucket_ms)],
            },
            repair_rows={
                "MSFT": [_bar("MSFT", middle_bucket_ms)],
            },
        )
        pipeline = _DummyPipeline(
            due_bucket_ms=due_bucket_ms,
            last_completed_bucket_ms=previous_bucket_ms,
            data_writer=writer,
            data_backfill=backfill,
            pb=_FakePB(cursor_map={"AAPL|5m": previous_bucket_ms, "MSFT|5m": previous_bucket_ms}),
            snapshot={
                "symbols": ["AAPL", "MSFT"],
                "trade_symbols": ["AAPL", "MSFT"],
                "monitor_symbols": [],
                "conid_map": {"AAPL": 1, "MSFT": 2},
                "symbol_meta": {
                    "AAPL": {"exchange": "NASDAQ"},
                    "MSFT": {"exchange": "NASDAQ"},
                },
            },
        )

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_official_5m_close_cycle()

        self.assertEqual(
            [event["symbols"] for event in pipeline.compute_events],
            [["AAPL"], ["MSFT"]],
        )
        self.assertEqual(backfill.repair_fetch_calls, 1)
        state = pipeline._copy_official_5m_state()
        self.assertEqual(state["pending_symbols_total"], 0)

    def test_close_cycle_wakes_watchlist_after_completed_state_is_visible(self):
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
        )

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_official_5m_close_cycle()

        self.assertEqual(
            pipeline.watchlist_topup_requests,
            [
                {
                    "ttl_s": 20.0,
                    "official_completed_ms": due_bucket_ms,
                }
            ],
        )

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

    def test_runtime_direct_topup_fetches_higher_interval_from_ibkr_and_triggers_compute_only_for_interval(self):
        due_5m_ms = int(datetime(2026, 4, 17, 10, 45, tzinfo=ET).timestamp() * 1000)
        due_15m_ms = int(datetime(2026, 4, 17, 10, 30, tzinfo=ET).timestamp() * 1000)
        writer = _FakeWriter()
        backfill = _FakeBackfill(writer)
        pipeline = _DummyPipeline(
            due_bucket_ms=due_5m_ms,
            last_completed_bucket_ms=due_5m_ms,
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
        pipeline.config = _FakeConfig({"ibkr_runtime_direct_topup_enabled": "true"})
        pipeline._direct_topup_due_ms = due_15m_ms

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_runtime_direct_topup_cycle(["15m"])

        self.assertEqual(len(backfill.backfill_all_calls), 1)
        self.assertEqual(backfill.backfill_all_calls[0]["conid_map"], {"AAPL": 1})
        self.assertEqual(backfill.backfill_all_calls[0]["intervals"], ["15m"])
        self.assertEqual(backfill.backfill_all_calls[0]["repair_symbols"], [])
        self.assertEqual(backfill.backfill_all_calls[0]["period_overrides"], {"AAPL": {"15m": "2d"}})
        self.assertEqual(backfill.backfill_all_calls[0]["trace_source"], "runtime_direct_topup")
        self.assertEqual(
            pipeline.compute_triggers,
            [
                {
                    "source": "direct_history_topup",
                    "symbols": ["AAPL"],
                    "persist_signals": False,
                    "intervals": ["15m"],
                    "rollup_intervals": [],
                }
            ],
        )
        state = pipeline._copy_direct_topup_state()
        self.assertEqual(state["last_completed_interval"], "15m")
        self.assertEqual(state["total_written_bars"], 1)
        self.assertEqual(state["intervals_state"]["15m"]["status"], "completed")
        self.assertEqual(state["intervals_state"]["15m"]["last_completed_bucket_ms"], due_15m_ms)
        self.assertEqual(state["intervals_state"]["15m"]["written_symbols"], ["AAPL"])
        self.assertEqual(writer.flush_calls, 1)

    def test_runtime_direct_topup_batches_all_due_intervals_in_priority_order(self):
        due_5m_ms = int(datetime(2026, 4, 17, 10, 45, tzinfo=ET).timestamp() * 1000)
        due_15m_ms = int(datetime(2026, 4, 17, 10, 30, tzinfo=ET).timestamp() * 1000)
        writer = _FakeWriter()
        backfill = _FakeBackfill(writer)
        pipeline = _DummyPipeline(
            due_bucket_ms=due_5m_ms,
            last_completed_bucket_ms=due_5m_ms,
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
        pipeline.config = _FakeConfig(
            {
                "ibkr_runtime_direct_topup_enabled": "true",
                "ibkr_runtime_direct_topup_parallel_enabled": "true",
            }
        )
        pipeline._direct_topup_due_ms = due_15m_ms

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_runtime_direct_topup_cycle(["15m", "30m", "1h", "4h"])

        self.assertEqual(len(backfill.backfill_all_calls), 1)
        self.assertEqual(backfill.backfill_all_calls[0]["intervals"], ["4h", "1h", "30m", "15m"])
        self.assertEqual(backfill.backfill_all_calls[0]["trace_source"], "runtime_direct_topup_parallel")
        self.assertEqual(
            backfill.backfill_all_calls[0]["period_overrides"],
            {"AAPL": {"4h": "20d", "1h": "5d", "30m": "3d", "15m": "2d"}},
        )
        self.assertEqual(
            pipeline.compute_triggers,
            [
                {
                    "source": "direct_history_topup",
                    "symbols": ["AAPL"],
                    "persist_signals": False,
                    "intervals": ["4h", "1h", "30m", "15m"],
                    "rollup_intervals": [],
                }
            ],
        )
        state = pipeline._copy_direct_topup_state()
        self.assertEqual(state["last_completed_interval"], "4h,1h,30m,15m")
        self.assertEqual(state["total_written_bars"], 4)
        for interval in ["4h", "1h", "30m", "15m"]:
            self.assertEqual(state["intervals_state"][interval]["status"], "completed")
            self.assertEqual(state["intervals_state"][interval]["last_completed_bucket_ms"], due_15m_ms)
            self.assertEqual(state["intervals_state"][interval]["written_symbols"], ["AAPL"])
        self.assertEqual(writer.flush_calls, 1)

    def test_runtime_direct_topup_skips_when_canonical_5m_is_not_current(self):
        due_5m_ms = int(datetime(2026, 4, 17, 10, 45, tzinfo=ET).timestamp() * 1000)
        writer = _FakeWriter()
        backfill = _FakeBackfill(writer)
        pipeline = _DummyPipeline(
            due_bucket_ms=due_5m_ms,
            last_completed_bucket_ms=due_5m_ms - STEP_MS,
            data_writer=writer,
            data_backfill=backfill,
        )
        pipeline.config = _FakeConfig({"ibkr_runtime_direct_topup_enabled": "true"})

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_runtime_direct_topup_cycle(["15m"])

        self.assertEqual(backfill.backfill_all_calls, [])
        self.assertEqual(pipeline.compute_triggers, [])
        state = pipeline._copy_direct_topup_state()
        self.assertEqual(state["last_error"], "canonical_5m_not_current")

    def test_runtime_direct_topup_can_yield_when_watchlist_5m_guard_is_enabled(self):
        due_5m_ms = int(datetime(2026, 4, 17, 10, 45, tzinfo=ET).timestamp() * 1000)
        due_15m_ms = int(datetime(2026, 4, 17, 10, 30, tzinfo=ET).timestamp() * 1000)
        writer = _FakeWriter()
        backfill = _FakeBackfill(writer)
        pipeline = _DummyPipeline(
            due_bucket_ms=due_5m_ms,
            last_completed_bucket_ms=due_5m_ms,
            data_writer=writer,
            data_backfill=backfill,
            watchlist_completion={
                "total": 3,
                "fresh": 2,
                "stale": 1,
                "missing": 0,
                "unobserved": 0,
            },
        )
        pipeline.config = _FakeConfig(
            {
                "ibkr_runtime_direct_topup_enabled": "true",
                "ibkr_runtime_direct_topup_wait_for_watchlist_5m_enabled": "true",
            }
        )
        pipeline._direct_topup_due_ms = due_15m_ms

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_runtime_direct_topup_cycle(["15m"])

        self.assertEqual(backfill.backfill_all_calls, [])
        self.assertEqual(pipeline.compute_triggers, [])
        state = pipeline._copy_direct_topup_state()
        self.assertEqual(state["last_error"], "watchlist_5m_pending")

    def test_runtime_direct_topup_does_not_wait_for_non_active_watchlist_5m_by_default(self):
        due_5m_ms = int(datetime(2026, 4, 17, 10, 45, tzinfo=ET).timestamp() * 1000)
        due_15m_ms = int(datetime(2026, 4, 17, 10, 30, tzinfo=ET).timestamp() * 1000)
        writer = _FakeWriter()
        backfill = _FakeBackfill(writer)
        pipeline = _DummyPipeline(
            due_bucket_ms=due_5m_ms,
            last_completed_bucket_ms=due_5m_ms,
            data_writer=writer,
            data_backfill=backfill,
            watchlist_completion={
                "total": 3,
                "fresh": 2,
                "stale": 1,
                "missing": 0,
                "unobserved": 0,
            },
            snapshot={
                "symbols": ["AAPL", "MSFT"],
                "trade_symbols": ["AAPL"],
                "monitor_symbols": ["MSFT"],
                "conid_map": {"AAPL": 1, "MSFT": 2},
                "symbol_meta": {"AAPL": {"exchange": "NASDAQ"}, "MSFT": {"exchange": "NASDAQ"}},
            },
        )
        pipeline.config = _FakeConfig({"ibkr_runtime_direct_topup_enabled": "true"})
        pipeline._direct_topup_due_ms = due_15m_ms

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_runtime_direct_topup_cycle(["15m"])

        self.assertEqual(len(backfill.backfill_all_calls), 1)
        self.assertEqual(backfill.backfill_all_calls[0]["conid_map"], {"AAPL": 1})
        state = pipeline._copy_direct_topup_state()
        self.assertEqual(state["last_error"], "")
        self.assertEqual(state["intervals_state"]["15m"]["status"], "completed")

    def test_runtime_direct_topup_is_disabled_by_default(self):
        due_5m_ms = int(datetime(2026, 4, 17, 10, 45, tzinfo=ET).timestamp() * 1000)
        writer = _FakeWriter()
        backfill = _FakeBackfill(writer)
        pipeline = _DummyPipeline(
            due_bucket_ms=due_5m_ms,
            last_completed_bucket_ms=due_5m_ms,
            data_writer=writer,
            data_backfill=backfill,
        )

        with mock.patch("ibkr_compute.orchestration.runtime_pipeline._service_mod", return_value=self._service_mod()):
            pipeline._run_runtime_direct_topup_cycle(["15m"])

        self.assertEqual(backfill.backfill_all_calls, [])
        self.assertEqual(pipeline.compute_triggers, [])
        state = pipeline._copy_direct_topup_state()
        self.assertFalse(state["enabled"])
        self.assertFalse(state["parallel_enabled"])


if __name__ == "__main__":
    unittest.main()
