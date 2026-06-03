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
        self.latest_map_calls = []
        self.latest_single_calls = []
        self.last_trace = {}
        self.recent_traces = []
        self.write_new_bars = True

    def get_latest_stored_bar_ms(self, symbol, interval):
        self.latest_single_calls.append((str(symbol or "").strip().upper(), interval))
        return int(self.latest_by_symbol.get(str(symbol or "").strip().upper()) or 0)

    def get_latest_stored_bar_ms_map(self, symbols, interval):
        normalized = [str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()]
        self.latest_map_calls.append((normalized, interval))
        return {symbol: int(self.latest_by_symbol.get(symbol) or 0) for symbol in normalized}

    def backfill_all(
        self,
        conid_map,
        symbol_meta=None,
        intervals=None,
        repair_symbols=None,
        period_overrides=None,
        trace_source="",
        trace_context=None,
    ):
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
                "trace_context": dict(trace_context or {}),
            }
        )
        if self.write_new_bars:
            for symbol in (conid_map or {}).keys():
                self.latest_by_symbol[str(symbol or "").strip().upper()] = 9_999_999
        return {symbol: {"5m": 1 if self.write_new_bars else 0} for symbol in (conid_map or {})}

    def status(self):
        return {
            "last_trace": dict(self.last_trace or {}),
            "recent_traces": list(self.recent_traces or []),
        }


class DummyConidResolver:
    def __init__(self, conid_map):
        self.conid_map = dict(conid_map or {})
        self.calls = []

    def resolve_bulk(self, symbols):
        normalized = [str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()]
        self.calls.append(normalized)
        return {symbol: int(self.conid_map.get(symbol) or 0) for symbol in normalized}


class DummyPB:
    def __init__(self, target_rows=None):
        self.target_rows = list(target_rows or [])
        self.calls = []
        self.notifications = []

    def get_all_records(self, collection, **kwargs):
        self.calls.append({"collection": collection, **kwargs})
        if collection != "ibkr_targets":
            return []
        return list(self.target_rows)

    def notify_system_event(self, title, detail, **kwargs):
        payload = {"title": title, "detail": dict(detail or {}), **dict(kwargs or {})}
        self.notifications.append(payload)
        return {"ok": True, "message_id": f"msg_{len(self.notifications)}"}


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
        self._current_market_date = "2026-04-30"
        self._watchlist_symbols = ["AAPL", "MSFT", "NVDA", "TSLA"]
        self._watchlist_records = {symbol: {"symbol": symbol} for symbol in self._watchlist_symbols}
        self._watchlist_idle_topup_cursor = 0
        self._watchlist_idle_topup_lock = threading.RLock()
        self._watchlist_idle_observations = {}
        self._watchlist_topup_wakeup = threading.Event()
        self._watchlist_topup_force_until = 0.0
        self._watchlist_idle_topup_state = self._initial_watchlist_idle_topup_state()
        self._latest_5m_bar_ms = {"AAPL": 5000, "MSFT": 3000, "TSLA": 1000}
        self.data_backfill = DummyDataBackfill(self._latest_5m_bar_ms)
        self.conid_resolver = DummyConidResolver({"MSFT": 2, "NVDA": 3, "TSLA": 4, "META": 5})
        self.realtime_compute_calls = []
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

    def _watchlist_idle_topup_force_catchup_active(self):
        return self._watchlist_topup_force_until > 1_000.0

    def _trigger_realtime_compute(
        self,
        source="bar_close",
        symbols=None,
        persist_signals=None,
        persist_signal_symbols=None,
        intervals=None,
        rollup_intervals=None,
    ):
        self.realtime_compute_calls.append(
            {
                "source": source,
                "symbols": [str(symbol or "").strip().upper() for symbol in (symbols or [])],
                "persist_signals": persist_signals,
                "persist_signal_symbols": [
                    str(symbol or "").strip().upper()
                    for symbol in (persist_signal_symbols or [])
                ],
                "intervals": list(intervals or []),
                "rollup_intervals": list(rollup_intervals or []),
            }
        )
        return {"ok": True, "processed": 0, "signals": 0, "errors": 0}


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

    def test_allows_when_compute_queue_has_pending_or_inflight_work(self):
        cases = [
            ("pending", lambda: self.service._compute_queue.put({"symbol": "MSFT"})),
            ("inflight", lambda: setattr(self.service, "_last_realtime_compute_started_at", 2_000.0)),
        ]
        for name, arrange in cases:
            with self.subTest(name=name):
                self.service = DummyWatchlistIdleTopup()
                arrange()
                admitted, admission = self.service._watchlist_idle_topup_admission()
                self.assertTrue(admitted, admission)
                blocker_codes = [item["code"] for item in admission["blockers"]]
                self.assertNotIn("compute_queue_busy", blocker_codes)
                self.assertNotIn("compute_inflight", blocker_codes)

    def test_post_close_catchup_allows_topup_while_compute_is_busy(self):
        self.service._compute_queue.put({"symbol": "AAPL"})
        self.service._last_realtime_compute_started_at = 2_000.0
        self.service._watchlist_topup_force_until = 2_000.0

        admitted, admission = self.service._watchlist_idle_topup_admission()

        self.assertTrue(admitted, admission)
        self.assertTrue(admission["post_close_catchup"])
        blocker_codes = [item["code"] for item in admission["blockers"]]
        self.assertNotIn("compute_queue_busy", blocker_codes)
        self.assertNotIn("compute_inflight", blocker_codes)

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

    def test_no_active_targets_allows_topup_when_bar_repair_is_busy(self):
        self.service._active_trade_symbols = set()
        self.service._active_subscription_symbols = set()
        self.service.bar_repair_coordinator = DummyBarRepairCoordinator(pending=10, inflight=2)

        admitted, admission = self.service._watchlist_idle_topup_admission()

        self.assertTrue(admitted, admission)
        self.assertFalse(admission["active_due_guard_required"])
        blocker_codes = [item["code"] for item in admission["blockers"]]
        self.assertNotIn("bar_repair_busy", blocker_codes)

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

    def test_allows_watchlist_topup_when_active_5m_due_window_is_close(self):
        self.service._seconds_until_active_5m_due = 30
        self.service._active_5m_seconds_until_due = 30
        self.service._watchlist_active_due_seconds = 30

        admitted, admission = self.service._watchlist_idle_topup_admission()

        self.assertTrue(admitted, admission)
        self.assertTrue(admission["active_due_guard_required"])
        blocker_codes = [item["code"] for item in admission["blockers"]]
        self.assertNotIn("active_5m_due_guard", blocker_codes)

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
        self.assertEqual(len(self.service.data_backfill.latest_map_calls), 1)
        self.assertEqual(self.service.data_backfill.latest_single_calls, [])

    def test_dynamic_candidate_scan_limits_active_first_selection(self):
        if not hasattr(self.service, "_watchlist_idle_topup_candidates"):
            self.skipTest("_watchlist_idle_topup_candidates helper is not implemented yet")
        self.service._watchlist_symbols = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]
        self.service.data_backfill.latest_by_symbol.update(
            {
                "MSFT": 3_000,
                "NVDA": 0,
                "TSLA": 1_000,
                "META": 0,
            }
        )
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_candidate_scan_size": 2,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )

        first = self.service._watchlist_idle_topup_candidates(scan_all=False)
        second = self.service._watchlist_idle_topup_candidates(scan_all=False)

        self.assertEqual(self._candidate_symbols(first), ["NVDA", "MSFT"])
        self.assertEqual(self._candidate_symbols(second), ["META", "TSLA"])
        self.assertEqual(self.service._watchlist_idle_topup_cursor, 0)

    def test_static_candidate_scan_all_ignores_dynamic_scan_window(self):
        if not hasattr(self.service, "_watchlist_idle_topup_candidates"):
            self.skipTest("_watchlist_idle_topup_candidates helper is not implemented yet")
        self.service._watchlist_symbols = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]
        self.service.data_backfill.latest_by_symbol.update(
            {
                "MSFT": 3_000,
                "NVDA": 0,
                "TSLA": 1_000,
                "META": 0,
            }
        )
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_candidate_scan_size": 2,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )

        result = self.service._watchlist_idle_topup_candidates(scan_all=True)

        self.assertEqual(self._candidate_symbols(result), ["NVDA", "META", "TSLA", "MSFT"])
        self.assertEqual(self.service._watchlist_idle_topup_cursor, 0)

    def test_no_data_cooldown_skips_inactive_symbol_candidate(self):
        self.service._watchlist_idle_topup_state["no_data_cooldowns"] = {
            "NVDA": {
                "symbol": "NVDA",
                "reason": "extended_hours_no_data",
                "cooldown_until_ms": 9_999_999_999_999,
                "inactive_candidate": True,
            }
        }

        result = self.service._watchlist_idle_topup_candidates(scan_all=True)

        self.assertNotIn("NVDA", self._candidate_symbols(result))
        self.assertEqual(self._candidate_symbols(result), ["TSLA", "MSFT"])

    def test_deactivated_target_is_excluded_from_watchlist_idle_topup_candidates(self):
        class PB:
            def get_all_records(self, collection, **_kwargs):
                if collection != "ibkr_targets":
                    return []
                return [
                    {
                        "symbol": "NVDA",
                        "date": "2026-04-30",
                        "environment": "live",
                        "status": "candidate",
                        "extra": {"deactivated_after_close": True},
                    }
                ]

        self.service.pb = PB()

        result = self.service._watchlist_idle_topup_candidates(scan_all=True)

        self.assertNotIn("NVDA", self._candidate_symbols(result))
        self.assertEqual(self._candidate_symbols(result), ["TSLA", "MSFT"])


class WatchlistIdleTopupSchedulerTest(unittest.TestCase):
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

    def test_next_wait_fast_retries_when_writer_is_temporarily_busy(self):
        wait_s = self.service._watchlist_idle_topup_next_wait_sec(
            {
                "status": "skipped",
                "last_stop_reason": "data_writer_busy",
                "last_admission": {"blockers": [{"code": "data_writer_busy"}]},
            }
        )

        self.assertLess(wait_s, 1.0)

    def test_next_wait_fast_retries_when_more_watchlist_symbols_remain(self):
        with mock.patch.object(
            self.service,
            "_watchlist_idle_topup_completion_snapshot",
            return_value={"stale": 1, "missing": 0, "unobserved": 2},
        ):
            wait_s = self.service._watchlist_idle_topup_next_wait_sec(
                {
                    "status": "completed",
                    "last_stop_reason": "max_symbols_per_cycle",
                    "last_written_bars": 3,
                    "last_admission": {"blockers": []},
                }
            )

        self.assertLess(wait_s, 1.0)

    def test_next_wait_uses_actionable_completion_when_no_data_is_explained(self):
        with mock.patch.object(
            self.service,
            "_watchlist_idle_topup_completion_snapshot",
            return_value={
                "stale": 2,
                "missing": 0,
                "unobserved": 0,
                "actionable_stale": 0,
                "actionable_missing": 0,
                "actionable_unobserved": 0,
                "explained_no_data_count": 2,
            },
        ):
            wait_s = self.service._watchlist_idle_topup_next_wait_sec(
                {
                    "status": "completed",
                    "last_stop_reason": "completed",
                    "last_written_bars": 1,
                    "last_admission": {"blockers": []},
                }
            )

        self.assertEqual(wait_s, 2.0)

    def test_next_wait_uses_normal_loop_for_hard_blockers(self):
        wait_s = self.service._watchlist_idle_topup_next_wait_sec(
            {
                "status": "skipped",
                "last_stop_reason": "session_unauthenticated",
                "last_admission": {"blockers": [{"code": "session_unauthenticated"}]},
            }
        )

        self.assertEqual(wait_s, 2.0)

    def test_completion_snapshot_separates_explained_no_data_from_actionable_stale(self):
        self.service._watchlist_idle_observations = {
            "NVDA": {"latest_ms": 0, "observed_at": "now"},
            "TSLA": {"latest_ms": 1_000, "observed_at": "now"},
        }
        self.service._watchlist_idle_topup_state["no_data_cooldowns"] = {
            "NVDA": {
                "symbol": "NVDA",
                "reason": "extended_hours_no_data",
                "cooldown_until_ms": 20_000_000,
                "inactive_candidate": True,
            }
        }

        snapshot = self.service._watchlist_idle_topup_completion_snapshot(now_ms=10_000_000)

        self.assertEqual(snapshot["explained_no_data_count"], 1)
        self.assertEqual(snapshot["explained_no_data_symbols_sample"], ["NVDA"])
        self.assertEqual(snapshot["missing"], 0)
        self.assertEqual(snapshot["stale"], 1)
        self.assertEqual(snapshot["actionable_stale"], 1)


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

    def _called_symbols(self):
        symbols = []
        for call in self.service.data_backfill.backfill_all_calls:
            symbols.extend(list((call.get("conid_map") or {}).keys()))
        return symbols

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
        self.assertGreaterEqual(len(self.service.data_backfill.backfill_all_calls), 1)
        self.assertEqual(self._called_symbols(), state["last_attempted_symbols"])
        self.assertEqual(
            [call["trace_source"] for call in self.service.data_backfill.backfill_all_calls],
            ["watchlist_idle_topup"] * len(self.service.data_backfill.backfill_all_calls),
        )
        self.assertEqual(self.service.bar_writer.flush_calls, len(self.service.data_backfill.backfill_all_calls))

    def test_legacy_bar_pipeline_disabled_skips_without_backfill_or_flush(self):
        self.service.config.values["ibkr_legacy_bar_pipeline_enabled"] = False

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual("skipped", state["status"])
        self.assertEqual("legacy_bar_pipeline_disabled", state["skip_reason"])
        self.assertEqual([], self.service.data_backfill.backfill_all_calls)
        self.assertEqual(0, self.service.bar_writer.flush_calls)

    def test_legacy_bar_pipeline_disabled_skips_active_repair_scan(self):
        self.service.config.values["ibkr_legacy_bar_pipeline_enabled"] = False
        self.service.scan_bar_integrity = mock.Mock(side_effect=AssertionError("scan should not run"))

        self.service._run_active_repair_cycle()

        self.service.scan_bar_integrity.assert_not_called()

    def test_idle_topup_default_compute_payload_persists_signals_only_for_active_targets(self):
        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(self.service.realtime_compute_calls), 1)
        call = self.service.realtime_compute_calls[0]
        self.assertEqual(call["source"], "watchlist_idle_topup")
        self.assertCountEqual(call["symbols"], ["AAPL", "MSFT", "NVDA", "TSLA"])
        self.assertEqual(call["persist_signals"], True)
        self.assertEqual(call["persist_signal_symbols"], ["AAPL"])
        self.assertEqual(call["intervals"], ["5m"])
        self.assertEqual(call["rollup_intervals"], ["15m", "30m", "1h", "4h", "1d"])
        for symbol in ("MSFT", "NVDA", "TSLA"):
            self.assertNotIn(symbol, call["persist_signal_symbols"])

    def test_idle_topup_passes_trigger_context_to_backfill(self):
        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        trace_context = self.service.data_backfill.backfill_all_calls[0]["trace_context"]
        self.assertEqual(trace_context["trigger_type"], "scheduled_auto_topup")
        self.assertEqual(trace_context["trigger_reason"], "watchlist 5m bars missing/stale")
        self.assertEqual(trace_context["bar_interval"], "5m")
        self.assertEqual(trace_context["request_period"], "1d")
        self.assertIn("watchlist_completion_before", trace_context)

    def test_hmds_no_data_marks_extended_hours_cooldown_and_batch_explanation(self):
        self.service._active_subscription_symbols = {"AAPL"}
        self.service._active_trade_symbols = {"AAPL"}
        self.service._watchlist_symbols = ["AAPL", "TKO"]
        self.service._watchlist_records = {"AAPL": {"symbol": "AAPL"}, "TKO": {"symbol": "TKO"}}
        self.service._latest_5m_bar_ms = {"AAPL": 5000, "TKO": 1000}
        self.service.data_backfill = DummyDataBackfill(self.service._latest_5m_bar_ms)
        self.service.data_backfill.write_new_bars = False
        self.service.data_backfill.last_trace = {
            "source": "watchlist_idle_topup",
            "symbol_outcomes": [
                {
                    "symbol": "TKO",
                    "hmds_no_data": True,
                    "last_error": "HMDS query returned no data: TKO@SMART Trades",
                    "request_count": 1,
                    "rows": 0,
                }
            ],
        }
        self.service.conid_resolver = DummyConidResolver({"TKO": 987})
        self.service_mod.build_market_session_snapshot.return_value = {"kind": "afterhours"}

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["last_no_data_symbols"], ["TKO"])
        self.assertEqual(state["last_no_data_reason"], "extended_hours_no_data")
        self.assertIn("TKO", state["no_data_cooldowns"])
        self.assertEqual(state["no_data_cooldowns"]["TKO"]["reason"], "extended_hours_no_data")
        self.assertEqual(state["last_batches"][0]["no_data_symbols"], ["TKO"])
        self.assertEqual(state["last_batches"][0]["explained_no_data_symbols"], ["TKO"])

        candidates = self.service._watchlist_idle_topup_candidates(scan_all=True)
        self.assertEqual([item["symbol"] for item in candidates], [])

    def test_active_trade_no_data_is_not_silently_cooled(self):
        self.service._active_subscription_symbols = set()
        self.service._active_trade_symbols = {"TKO"}
        self.service._watchlist_symbols = ["TKO"]
        self.service._watchlist_records = {"TKO": {"symbol": "TKO"}}
        self.service._latest_5m_bar_ms = {"TKO": 1000}
        self.service.data_backfill = DummyDataBackfill(self.service._latest_5m_bar_ms)
        self.service.data_backfill.write_new_bars = False
        self.service.data_backfill.last_trace = {
            "source": "watchlist_idle_topup",
            "symbol_outcomes": [
                {
                    "symbol": "TKO",
                    "hmds_no_data": True,
                    "last_error": "HMDS query returned no data: TKO@SMART Trades",
                    "request_count": 1,
                    "rows": 0,
                }
            ],
        }
        self.service.conid_resolver = DummyConidResolver({"TKO": 987})
        self.service_mod.build_market_session_snapshot.return_value = {"kind": "afterhours"}

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertNotIn("TKO", state["no_data_cooldowns"])
        self.assertEqual(state["last_batches"][0]["active_no_data_symbols"], ["TKO"])

    def test_repeated_no_data_emits_hygiene_candidate_alert_without_auto_remove(self):
        self.service.pb = DummyPB()
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_inactive_no_data_count_threshold": 1,
                "ibkr_watchlist_inactive_days_threshold": 3,
                "ibkr_watchlist_hygiene_notify_cooldown_hours": 24,
            }
        )

        self.service._watchlist_idle_topup_record_no_data_symbols(
            {"WELL": {"last_error": "HMDS query returned no data: WELL@SMART Trades"}},
            "afterhours",
        )

        state = self.service._watchlist_idle_topup_status()
        self.assertEqual([item["symbol"] for item in state["removal_candidates"]], ["WELL"])
        self.assertFalse(state["removal_candidates"][0]["auto_remove_allowed"])
        self.assertEqual(len(self.service.pb.notifications), 1)
        self.assertIn("Watchlist hygiene", self.service.pb.notifications[0]["title"])

    def test_idle_topup_triggers_rollup_even_when_backfill_writes_no_new_5m_bars(self):
        def zero_write_backfill(
            conid_map,
            symbol_meta=None,
            intervals=None,
            repair_symbols=None,
            period_overrides=None,
            trace_source="",
            trace_context=None,
        ):
            self.service.data_backfill.backfill_all_calls.append(
                {
                    "conid_map": dict(conid_map or {}),
                    "symbol_meta": dict(symbol_meta or {}),
                    "intervals": list(intervals or []),
                    "repair_symbols": list(repair_symbols or []),
                    "period_overrides": dict(period_overrides or {}),
                    "trace_source": trace_source,
                    "trace_context": dict(trace_context or {}),
                }
            )
            return {symbol: {"5m": 0} for symbol in (conid_map or {})}

        self.service.data_backfill.backfill_all = zero_write_backfill

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["last_loaded_bars"], 0)
        self.assertEqual(len(self.service.realtime_compute_calls), 1)
        self.assertEqual(self.service.realtime_compute_calls[0]["source"], "watchlist_idle_topup")
        self.assertEqual(self.service.realtime_compute_calls[0]["rollup_intervals"], ["15m", "30m", "1h", "4h", "1d"])

    def test_idle_topup_persists_signals_for_unsubscribed_active_targets(self):
        self.service.pb = DummyPB(
            [
                {
                    "symbol": "MSFT",
                    "status": "active",
                    "extra": {"source": "daily_scan", "context_gate_passed": True},
                },
                {
                    "symbol": "NVDA",
                    "status": "active",
                    "extra": {"source": "daily_scan", "context_gate_passed": False},
                },
                {
                    "symbol": "TSLA",
                    "status": "candidate",
                    "extra": {"source": "daily_scan", "context_gate_passed": True},
                },
            ]
        )

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(self.service.realtime_compute_calls), 1)
        call = self.service.realtime_compute_calls[0]
        self.assertCountEqual(call["symbols"], ["AAPL", "MSFT", "NVDA", "TSLA"])
        self.assertEqual(call["persist_signal_symbols"], ["AAPL", "MSFT"])
        self.assertNotIn("NVDA", call["persist_signal_symbols"])
        self.assertNotIn("TSLA", call["persist_signal_symbols"])

    def test_active_targets_continue_when_next_due_budget_is_close(self):
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_batch_size": 2,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )
        self.service._seconds_until_active_5m_due = 50

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["mode"], "continuous_until_active_due")
        self.assertGreater(state["last_processed_symbols_total"], 0)
        self.assertGreater(len(self.service.data_backfill.backfill_all_calls), 0)

    def test_active_targets_continue_between_dynamic_batches_when_due_budget_tightens(self):
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_batch_size": 2,
                "ibkr_watchlist_idle_topup_max_symbols_per_cycle": 0,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )

        with mock.patch.object(
            self.service,
            "_seconds_until_next_active_5m_due",
            side_effect=[600, 600, 600, 50],
        ):
            state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["mode"], "continuous_until_active_due")
        self.assertEqual(state["last_attempted_symbols"], ["NVDA", "TSLA", "MSFT"])
        self.assertEqual(state["last_processed_symbols"], ["NVDA", "TSLA", "MSFT"])
        self.assertEqual(len(self.service.data_backfill.backfill_all_calls), 1)

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
        self.assertEqual(state["last_request_count"], 3)
        self.assertEqual(self._called_symbols(), state["last_attempted_symbols"])
        self.assertGreaterEqual(len(self.service.data_backfill.backfill_all_calls), 1)

    def test_dynamic_active_first_selects_multiple_symbols_per_batch_and_preserves_order(self):
        self.service._watchlist_symbols = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]
        self.service.data_backfill.latest_by_symbol.update(
            {
                "MSFT": 3_000,
                "NVDA": 0,
                "TSLA": 1_000,
                "META": 0,
            }
        )
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_batch_size": 2,
                "ibkr_watchlist_idle_topup_max_symbols_per_cycle": 4,
                "ibkr_watchlist_idle_topup_candidate_scan_size": 2,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["mode"], "continuous_until_active_due")
        self.assertEqual(state["last_stop_reason"], "max_symbols_per_cycle")
        self.assertEqual(self._called_symbols(), ["NVDA", "META", "TSLA", "MSFT"])
        self.assertEqual(state["last_attempted_symbols"], ["NVDA", "META", "TSLA", "MSFT"])
        self.assertEqual(state["last_processed_symbols"], ["NVDA", "META", "TSLA", "MSFT"])
        self.assertEqual(state["last_request_count"], 4)

    def test_static_full_load_preserves_multi_symbol_batches_when_active_guard_disabled(self):
        self.service._watchlist_symbols = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]
        self.service.data_backfill.latest_by_symbol.update(
            {
                "MSFT": 3_000,
                "NVDA": 0,
                "TSLA": 1_000,
                "META": 0,
            }
        )
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_batch_size": 3,
                "ibkr_watchlist_idle_topup_dynamic_enabled": False,
                "ibkr_watchlist_idle_topup_candidate_scan_size": 1,
                "ibkr_watchlist_idle_topup_max_symbols_per_cycle": 0,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )
        self.service_mod.build_market_session_snapshot.return_value = {"kind": "closed"}

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["mode"], "full_load_off_active_window")
        self.assertEqual(
            [call["conid_map"] for call in self.service.data_backfill.backfill_all_calls],
            [{"NVDA": 3, "META": 5, "TSLA": 4}, {"MSFT": 2}],
        )
        self.assertEqual(state["last_attempted_symbols"], ["NVDA", "META", "TSLA", "MSFT"])
        self.assertEqual(state["last_processed_symbols"], ["NVDA", "META", "TSLA", "MSFT"])

    def test_dynamic_symbol_budget_truncates_batch_when_exposed(self):
        if not hasattr(self.service, "_watchlist_idle_topup_max_symbols_per_cycle"):
            self.skipTest("watchlist idle topup symbol budget is not exposed")
        self.service._watchlist_symbols = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]
        self.service.data_backfill.latest_by_symbol.update(
            {
                "MSFT": 3_000,
                "NVDA": 0,
                "TSLA": 1_000,
                "META": 0,
            }
        )
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_batch_size": 4,
                "ibkr_watchlist_idle_topup_max_symbols_per_cycle": 3,
                "ibkr_watchlist_idle_topup_candidate_scan_size": 4,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["last_stop_reason"], "max_symbols_per_cycle")
        self.assertEqual(state["last_attempted_symbols"], ["NVDA", "META", "TSLA"])
        self.assertEqual(state["last_processed_symbols"], ["NVDA", "META", "TSLA"])
        self.assertEqual(self._called_symbols(), ["NVDA", "META", "TSLA"])

    def test_resource_governor_recommended_budget_overrides_idle_topup_limits(self):
        self.service._watchlist_symbols = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]
        self.service.data_backfill.latest_by_symbol.update(
            {
                "MSFT": 3_000,
                "NVDA": 0,
                "TSLA": 1_000,
                "META": 0,
            }
        )
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_batch_size": 4,
                "ibkr_watchlist_idle_topup_max_symbols_per_cycle": 4,
                "ibkr_watchlist_idle_topup_candidate_scan_size": 4,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )
        self.service._resource_governor = {
            "status": "green",
            "health": "ok",
            "shedding_mode": "green",
            "recommended_limits": {
                "watchlist_idle_topup": {
                    "enabled": True,
                    "max_symbols_per_cycle": 2,
                    "batch_size": 1,
                    "history_concurrency": 2,
                    "request_spacing_s": 0.25,
                }
            },
            "admission": {"watchlist_idle_topup": {"admit": True, "blockers": []}},
        }

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["last_stop_reason"], "max_symbols_per_cycle")
        self.assertEqual(state["last_attempted_symbols"], ["NVDA", "META"])
        self.assertEqual([list(call["conid_map"].keys()) for call in self.service.data_backfill.backfill_all_calls], [["NVDA"], ["META"]])
        self.assertEqual(state["applied_budget"]["max_symbols_per_cycle"], 2)
        self.assertEqual(state["applied_budget"]["batch_size"], 1)
        self.assertEqual(state["applied_budget"]["history_concurrency"], 2)
        self.assertEqual(state["applied_budget"]["request_spacing_s"], 0.25)
        trace_context = self.service.data_backfill.backfill_all_calls[0]["trace_context"]
        self.assertEqual(trace_context["applied_budget"]["max_symbols_per_cycle"], 2)
        self.assertEqual(trace_context["batch_size"], 1)

    def test_resource_governor_critical_shedding_disables_idle_topup(self):
        self.service._resource_governor = {
            "status": "critical",
            "health": "degraded",
            "shedding_mode": "critical",
            "recommended_limits": {
                "watchlist_idle_topup": {
                    "enabled": True,
                    "max_symbols_per_cycle": 10,
                    "batch_size": 4,
                }
            },
            "admission": {"watchlist_idle_topup": {"admit": True, "blockers": []}},
        }

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "skipped")
        self.assertEqual(state["skip_reason"], "disabled")
        self.assertFalse(state["applied_budget"]["enabled"])
        self.assertEqual(state["applied_budget"]["max_symbols_per_cycle"], 0)

    def test_dynamic_estimated_bar_budget_truncates_batch_when_exposed(self):
        self.service._watchlist_symbols = ["AAPL", "MSFT", "NVDA", "TSLA", "META"]
        self.service.data_backfill.latest_by_symbol.update(
            {
                "MSFT": 3_000,
                "NVDA": 0,
                "TSLA": 1_000,
                "META": 0,
            }
        )
        self.service.config = DummyConfig(
            {
                "ibkr_watchlist_idle_topup_dynamic_max_symbols_per_cycle": 4,
                "ibkr_watchlist_idle_topup_max_estimated_bars_per_cycle": 300,
                "ibkr_watchlist_idle_topup_candidate_scan_size": 4,
                "ibkr_watchlist_active_due_guard_sec": 45,
            }
        )

        state = self.service._run_watchlist_idle_topup_cycle()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["last_stop_reason"], "estimated_bars_budget")
        self.assertEqual(state["last_attempted_symbols"], ["NVDA", "META"])
        self.assertEqual(state["estimated_bars_selected"], 300)
        self.assertEqual(self._called_symbols(), ["NVDA", "META"])

    def test_due_budget_helper_allows_watchlist_batches_when_active_due_is_close(self):
        if not hasattr(self.service, "_watchlist_idle_topup_due_budget_allows_batch"):
            self.skipTest("watchlist idle topup due-budget helper is not exposed")

        allowed, reason = self.service._watchlist_idle_topup_due_budget_allows_batch(
            {
                "active_due_guard_required": True,
                "active_target_count": 1,
                "seconds_until_next_active_5m_due": 64,
                "active_due_guard_sec": 45,
            },
            20,
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "")

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
        self.assertGreaterEqual(len(self.service.data_backfill.backfill_all_calls), 1)


if __name__ == "__main__":
    unittest.main()
