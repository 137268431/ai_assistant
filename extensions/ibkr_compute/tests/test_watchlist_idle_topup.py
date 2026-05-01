import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration import market_universe as market_universe_mod
from ibkr_compute.orchestration.market_universe import TradingServiceMarketUniverseMixin


class DummyConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_int_for_environment(self, key, environment, default):
        return int(self.values.get(key, default))

    def get_bool_for_environment(self, key, environment, default):
        return bool(self.values.get(key, default))

    def get_for_environment(self, key, environment, default=None):
        return self.values.get(key, default)


class DummyWriter:
    def __init__(self, *, pending=0, inflight=0):
        self.pending = int(pending)
        self.inflight = int(inflight)
        self.flush_calls = 0

    def status(self):
        return {
            "pending_batch": self.pending,
            "inflight_batch": self.inflight,
        }

    def flush(self):
        self.flush_calls += 1
        return True


class DummySessionKeeper:
    is_authenticated = True


class DummyWebSocketClient:
    def __init__(self, connected=True, ready=True):
        self.connected = bool(connected)
        self.ready = bool(ready)

    def status(self):
        return {"connected": self.connected, "ready": self.ready}


class DummyDataBackfill:
    def __init__(self, latest_by_symbol):
        self.latest_by_symbol = latest_by_symbol
        self.backfill_all_calls = []

    def get_latest_stored_bar_ms(self, symbol, interval):
        return int(self.latest_by_symbol.get(str(symbol or "").strip().upper()) or 0)

    def backfill_all(self, conid_map, symbol_meta=None, intervals=None, repair_symbols=None, period_overrides=None, trace_source=""):
        self.backfill_all_calls.append(
            {
                "conid_map": dict(conid_map or {}),
                "symbol_meta": dict(symbol_meta or {}),
                "intervals": list(intervals or []),
                "repair_symbols": list(repair_symbols or []),
                "period_overrides": {
                    symbol: dict(payload or {})
                    for symbol, payload in (period_overrides or {}).items()
                },
                "trace_source": trace_source,
            }
        )
        for symbol in (conid_map or {}).keys():
            self.latest_by_symbol[str(symbol or "").strip().upper()] = 9_999_999
        return {symbol: {"5m": 1} for symbol in (conid_map or {})}


class DummyConidResolver:
    def __init__(self, conid_map):
        self.conid_map = dict(conid_map or {})
        self.calls = []

    def resolve_bulk(self, symbols):
        normalized = [str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()]
        self.calls.append(normalized)
        return {symbol: int(self.conid_map.get(symbol) or 0) for symbol in normalized}


class DummyBarRepairCoordinator:
    def __init__(self, *, pending=0, inflight=0):
        self.pending = int(pending)
        self.inflight = int(inflight)

    def status(self):
        return {
            "ok": True,
            "pending": self.pending,
            "inflight": self.inflight,
            "failed": 0,
        }


class DummyWatchlistIdleTopup(TradingServiceMarketUniverseMixin):
    def __init__(self):
        self.config = DummyConfig({"ibkr_watchlist_active_due_guard_sec": 180})
        self._compute_queue = queue.Queue()
        self._compute_thread = mock.Mock()
        self._compute_thread.is_alive.return_value = True
        self._last_realtime_compute_at = 1_000.0
        self._last_realtime_compute_started_at = 0.0
        self._official_5m_state = {
            "enabled": True,
            "pending_symbols_total": 0,
            "pending_symbols": [],
            "last_due_bucket_ms": 1_000_000,
            "last_completed_bucket_ms": 1_000_000,
        }
        self.bar_writer = DummyWriter()
        self.data_writer = self.bar_writer
        self.market_data_writer = self.bar_writer
        self.bar_repair_coordinator = DummyBarRepairCoordinator()
        self._resource_governor = {
            "status": "green",
            "health": "ok",
            "admission": {"watchlist_idle_topup": {"admit": True, "blockers": []}},
        }
        self._seconds_until_active_5m_due = 600
        self._active_5m_seconds_until_due = 600
        self._watchlist_active_due_seconds = 600
        self.session_keeper = DummySessionKeeper()
        self.ws_client = DummyWebSocketClient()
        self._subscription_lock = threading.RLock()
        self._active_subscription_symbols = {"AAPL"}
        self._active_trade_symbols = {"AAPL"}
        self._watchlist_symbols = ["AAPL", "MSFT", "NVDA", "TSLA"]
        self._watchlist_records = {symbol: {"symbol": symbol} for symbol in self._watchlist_symbols}
        self._watchlist_idle_topup_cursor = 0
        self._watchlist_idle_topup_lock = threading.RLock()
        self._watchlist_idle_observations = {}
        self._watchlist_idle_topup_state = self._initial_watchlist_idle_topup_state()
        self._latest_5m_bar_ms = {"AAPL": 5000, "MSFT": 3000, "TSLA": 1000}
        self.data_backfill = DummyDataBackfill(self._latest_5m_bar_ms)
        self.conid_resolver = DummyConidResolver({"MSFT": 2, "NVDA": 3, "TSLA": 4, "META": 5})
        self._symbol_meta = {
            "MSFT": {"exchange": "NASDAQ"},
            "NVDA": {"exchange": "NASDAQ"},
            "TSLA": {"exchange": "NASDAQ"},
            "META": {"exchange": "NASDAQ"},
        }
        self._running = True
        self._last_backfill_at = 0.0
        self._last_backfill_symbols = []

    def _is_warmup_active(self):
        return False

    def _now_iso(self):
        return "2026-04-30T10:15:00-04:00"

    def _copy_official_5m_state(self, source=None):
        return dict(self._official_5m_state if source is None else source)

    def _resource_governor_snapshot(self):
        return dict(self._resource_governor)

    def _active_5m_due_in_seconds(self):
        return self._seconds_until_active_5m_due

    def _seconds_until_next_active_5m_due(self):
        return self._seconds_until_active_5m_due

    def _watchlist_seconds_until_active_5m_due(self):
        return self._seconds_until_active_5m_due

    def _refresh_watchlist_pool(self, force=False):
        return None

    def _normalize_symbol_list(self, symbols):
        return [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]

    def _latest_stored_5m_bar_ms(self, symbol):
        return self._latest_5m_bar_ms.get(str(symbol or "").strip().upper())

    def _latest_watchlist_5m_bar_ms(self, symbol):
        return self._latest_stored_5m_bar_ms(symbol)

    def _latest_5m_bar_time_ms_for_symbol(self, symbol):
        return self._latest_stored_5m_bar_ms(symbol)

    def _watchlist_latest_5m_bar_map(self, symbols):
        return {symbol: self._latest_stored_5m_bar_ms(symbol) for symbol in symbols}


class WatchlistIdleTopupAdmissionTest(unittest.TestCase):
    def setUp(self):
        self.service = DummyWatchlistIdleTopup()
        self.service_mod = mock.Mock()
        self.service_mod.ENVIRONMENT = "live"
        self.service_mod.logger = mock.Mock()
        self.service_mod.build_market_session_snapshot.return_value = {"kind": "regular"}
        self.patcher = mock.patch.object(market_universe_mod, "_service_mod", return_value=self.service_mod)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _admission(self):
        return self.service._watchlist_idle_topup_admission()

    def _admitted(self, result):
        if isinstance(result, bool):
            return result
        if isinstance(result, tuple):
            return bool(result[0])
        if isinstance(result, dict):
            for key in ("admit", "admitted", "allowed", "ok"):
                if key in result:
                    return bool(result[key])
        self.fail(f"Unsupported admission result shape: {result!r}")

    def _reason_text(self, result):
        if isinstance(result, tuple):
            return " ".join(str(part) for part in result[1:])
        if not isinstance(result, dict):
            return str(result)
        parts = []
        for key in ("reason", "code", "message"):
            if result.get(key):
                parts.append(str(result[key]))
        blockers = result.get("blockers") or result.get("reasons") or []
        if isinstance(blockers, dict):
            blockers = [blockers]
        for blocker in blockers:
            if isinstance(blocker, dict):
                parts.extend(str(blocker.get(key) or "") for key in ("code", "reason", "message"))
            else:
                parts.append(str(blocker))
        return " ".join(parts).lower()

    def assertBlockedBy(self, expected_text):
        result = self._admission()
        self.assertFalse(self._admitted(result), result)
        self.assertIn(expected_text, self._reason_text(result), result)

    def test_all_idle_allows_watchlist_idle_topup(self):
        result = self._admission()

        self.assertTrue(self._admitted(result), result)

    def test_blocks_when_official_5m_has_pending_symbols(self):
        self.service._official_5m_state.update(
            {
                "pending_symbols_total": 1,
                "pending_symbols": ["MSFT"],
            }
        )

        self.assertBlockedBy("official")

    def test_blocks_when_compute_queue_has_pending_or_inflight_work(self):
        cases = [
            ("pending", lambda: self.service._compute_queue.put({"symbol": "MSFT"}), "compute"),
            ("inflight", lambda: setattr(self.service, "_last_realtime_compute_started_at", 2_000.0), "compute"),
        ]
        for name, arrange, reason in cases:
            with self.subTest(name=name):
                self.service = DummyWatchlistIdleTopup()
                arrange()
                self.assertBlockedBy(reason)

    def test_blocks_when_bar_writer_has_pending_or_inflight_work(self):
        for name, writer in (
            ("pending", DummyWriter(pending=1, inflight=0)),
            ("inflight", DummyWriter(pending=0, inflight=1)),
        ):
            with self.subTest(name=name):
                self.service = DummyWatchlistIdleTopup()
                self.service.bar_writer = writer
                self.service.data_writer = writer
                self.service.market_data_writer = writer
                self.assertBlockedBy("writer")

    def test_blocks_when_bar_repair_has_pending_or_inflight_work(self):
        for name, coordinator in (
            ("pending", DummyBarRepairCoordinator(pending=1, inflight=0)),
            ("inflight", DummyBarRepairCoordinator(pending=0, inflight=1)),
        ):
            with self.subTest(name=name):
                self.service = DummyWatchlistIdleTopup()
                self.service.bar_repair_coordinator = coordinator
                self.assertBlockedBy("repair")

    def test_blocks_when_resource_governor_denies_watchlist_admission(self):
        self.service._resource_governor = {
            "status": "warning",
            "health": "degraded",
            "admission": {
                "watchlist_idle_topup": {
                    "admit": False,
                    "blockers": [{"code": "watchlist_cpu_over_limit"}],
                }
            },
        }

        self.assertBlockedBy("watchlist_cpu_over_limit")

    def test_blocks_when_active_5m_due_window_is_too_close(self):
        self.service._seconds_until_active_5m_due = 30
        self.service._active_5m_seconds_until_due = 30
        self.service._watchlist_active_due_seconds = 30

        self.assertBlockedBy("due")

    def test_closed_session_allows_history_compensation_without_live_ws_or_official_freshness(self):
        self.service.ws_client = DummyWebSocketClient(connected=False, ready=False)
        self.service._official_5m_state.update(
            {
                "pending_symbols_total": 1,
                "pending_symbols": ["AAPL"],
                "last_due_bucket_ms": 2_000_000,
                "last_completed_bucket_ms": 1_000_000,
            }
        )
        self.service._seconds_until_active_5m_due = 10
        self.service_mod.build_market_session_snapshot.return_value = {"kind": "closed"}

        admitted, admission = self.service._watchlist_idle_topup_admission()

        self.assertTrue(admitted, admission)
        self.assertFalse(admission["active_due_guard_required"])
        self.assertEqual(admission["official_5m_pending_symbols"], ["AAPL"])


class WatchlistIdleTopupCandidateSelectionTest(unittest.TestCase):
    def setUp(self):
        self.service = DummyWatchlistIdleTopup()
        self.service_mod = mock.Mock()
        self.service_mod.ENVIRONMENT = "live"
        self.service_mod.logger = mock.Mock()
        self.service_mod.build_market_session_snapshot.return_value = {"kind": "regular"}
        self.patcher = mock.patch.object(market_universe_mod, "_service_mod", return_value=self.service_mod)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _candidate_symbols(self, result):
        rows = result.get("candidates", result) if isinstance(result, dict) else result
        symbols = []
        for item in rows:
            if isinstance(item, dict):
                symbols.append(str(item.get("symbol") or "").upper())
            else:
                symbols.append(str(item or "").upper())
        return symbols

    def test_candidate_selection_excludes_active_subscriptions_and_orders_missing_then_oldest_5m(self):
        if not hasattr(self.service, "_watchlist_idle_topup_candidates"):
            self.skipTest("_watchlist_idle_topup_candidates helper is not implemented yet")

        result = self.service._watchlist_idle_topup_candidates()

        self.assertEqual(self._candidate_symbols(result), ["NVDA", "TSLA", "MSFT"])


class WatchlistIdleTopupCycleTest(unittest.TestCase):
    def setUp(self):
        self.service = DummyWatchlistIdleTopup()
        self.service_mod = mock.Mock()
        self.service_mod.ENVIRONMENT = "live"
        self.service_mod.logger = mock.Mock()
        self.service_mod.build_market_session_snapshot.return_value = {"kind": "regular"}
        self.patcher = mock.patch.object(market_universe_mod, "_service_mod", return_value=self.service_mod)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_no_active_targets_full_loads_all_stale_watchlist_in_batches(self):
        self.service._active_subscription_symbols = set()
        self.service._active_trade_symbols = set()
        self.service._watchlist_symbols = ["MSFT", "NVDA", "TSLA", "META"]
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_batch_size": 2,
                "ibkr_watchlist_idle_topup_max_symbols_per_cycle": 0,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["mode"], "full_load_no_active_targets")
        self.assertEqual(state["last_attempted_symbols_total"], 4)
        self.assertEqual(state["last_processed_symbols_total"], 4)
        self.assertEqual(state["last_loaded_bars"], 4)
        self.assertEqual(len(self.service.data_backfill.backfill_all_calls), 2)
        self.assertEqual(
            [call["trace_source"] for call in self.service.data_backfill.backfill_all_calls],
            ["watchlist_idle_topup", "watchlist_idle_topup"],
        )
        self.assertEqual(self.service.bar_writer.flush_calls, 2)

    def test_active_targets_pause_when_next_due_budget_is_too_close(self):
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_batch_size": 2,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )
        self.service._seconds_until_active_5m_due = 50

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "skipped")
        self.assertEqual(state["skip_reason"], "active_5m_due_guard")
        self.assertEqual(self.service.data_backfill.backfill_all_calls, [])

    def test_active_targets_continue_across_batches_until_candidates_done(self):
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_batch_size": 1,
                "ibkr_watchlist_idle_topup_max_symbols_per_cycle": 3,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )
        self.service._seconds_until_active_5m_due = 600

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["mode"], "continuous_until_active_due")
        self.assertEqual(state["last_stop_reason"], "max_symbols_per_cycle")
        self.assertEqual(state["last_attempted_symbols_total"], 3)
        self.assertEqual(state["last_processed_symbols_total"], 3)
        self.assertEqual(len(self.service.data_backfill.backfill_all_calls), 3)

    def test_closed_session_full_loads_even_when_active_targets_exist(self):
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_batch_size": 2,
                "ibkr_watchlist_idle_topup_max_symbols_per_cycle": 0,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )
        self.service.ws_client = DummyWebSocketClient(connected=False, ready=False)
        self.service._official_5m_state.update(
            {
                "pending_symbols_total": 1,
                "pending_symbols": ["AAPL"],
                "last_due_bucket_ms": 2_000_000,
                "last_completed_bucket_ms": 1_000_000,
            }
        )
        self.service._seconds_until_active_5m_due = 10
        self.service_mod.build_market_session_snapshot.return_value = {"kind": "closed"}

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["mode"], "full_load_off_active_window")
        self.assertEqual(state["last_processed_symbols_total"], 3)
        self.assertGreaterEqual(len(self.service.data_backfill.backfill_all_calls), 2)


if __name__ == "__main__":
    unittest.main()
