import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration.warmup import TradingServiceWarmupMixin


class DummyWarmupRetryState(TradingServiceWarmupMixin):
    def __init__(self):
        self._warmup_lock = threading.Lock()
        self._warmup_state = self._initial_warmup_state()

    def _now_iso(self) -> str:
        return "2026-04-21T09:00:00Z"


class _DummyConfig:
    def get_bool_for_environment(self, key: str, environment: str, default=False):
        del key, environment
        return default

    def get_for_environment(self, key: str, environment: str, default=None):
        del environment
        if key == "ibkr_market_ws_symbols":
            return "SPY,VIX"
        return default


class _DummyConidResolver:
    def __init__(self):
        self.calls = []

    def resolve_bulk(self, symbols):
        normalized = [str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()]
        self.calls.append(normalized)
        return {"VIX": 3}


class DummyWarmupSnapshot(TradingServiceWarmupMixin):
    def __init__(self):
        self._subscription_lock = threading.Lock()
        self._active_target_date = "2026-04-21"
        self._current_market_date = "2026-04-21"
        self._active_trade_symbols = ["AAPL"]
        self._active_subscription_map = {"AAPL": 1, "SPY": 2}
        self._symbol_meta = {
            "AAPL": {"exchange": "NASDAQ"},
            "SPY": {"exchange": "ARCA"},
        }
        self._watchlist_symbols = ["AAPL"]
        self._watchlist_trade_symbols = ["AAPL"]
        self._watchlist_monitor_symbols = []
        self.config = _DummyConfig()
        self.conid_resolver = _DummyConidResolver()

    def _market_date(self) -> str:
        return "2026-04-21"


class WarmupRetryStateTest(unittest.TestCase):
    def test_warmup_snapshot_resolves_market_monitor_conids(self):
        service = DummyWarmupSnapshot()

        state = service._warmup_snapshot_from_subscriptions()

        self.assertEqual(state["symbols"], ["AAPL", "SPY", "VIX"])
        self.assertEqual(state["trade_symbols"], ["AAPL"])
        self.assertEqual(state["monitor_symbols"], ["SPY", "VIX"])
        self.assertEqual(state["conid_map"], {"AAPL": 1, "SPY": 2, "VIX": 3})
        self.assertEqual(service.conid_resolver.calls, [["VIX"]])

    def test_retry_preserves_ready_snapshot_when_scope_is_still_ready(self):
        service = DummyWarmupRetryState()
        previous = service._initial_warmup_state()
        previous.update(
            {
                "phase": "ready",
                "started_at": "2026-04-21T08:55:00Z",
                "finished_at": "2026-04-21T08:59:00Z",
                "last_success_at": "2026-04-21T08:59:00Z",
                "ready_symbols_list": ["AAPL", "QQQ", "SPY", "VIX"],
                "symbols": ["AAPL", "QQQ", "SPY", "VIX"],
                "scan_symbols": ["AAPL"],
                "subscription_symbols": ["QQQ", "SPY", "VIX"],
                "trade_symbols": [],
                "monitor_symbols": ["QQQ", "SPY", "VIX"],
                "symbol_status": [
                    {"symbol": "AAPL", "role": "scan", "ready": True},
                    {"symbol": "QQQ", "role": "monitor", "ready": True},
                ],
            }
        )
        snapshot = {
            "target_date": "2026-04-21",
            "symbols": ["AAPL", "QQQ", "SPY", "VIX"],
            "scan_symbols": ["AAPL"],
            "subscription_symbols": ["QQQ", "SPY", "VIX"],
            "trade_symbols": [],
            "monitor_symbols": ["QQQ", "SPY", "VIX"],
            "symbols_total": 4,
            "scan_symbols_total": 1,
            "subscription_symbols_total": 3,
            "trade_symbols_total": 0,
            "monitor_symbols_total": 3,
        }

        state = service._build_transient_warmup_retry_state(
            snapshot,
            previous,
            reason="pb_unavailable_retry",
            requested_at="2026-04-21T09:00:00Z",
            started_at="2026-04-21T09:00:00Z",
        )

        self.assertEqual(state["phase"], "ready")
        self.assertEqual(state["pending_symbols"], [])
        self.assertEqual(state["ready_symbols_list"], ["AAPL", "QQQ", "SPY", "VIX"])
        self.assertEqual(state["started_at"], "2026-04-21T08:55:00Z")
        self.assertEqual(state["finished_at"], "2026-04-21T08:59:00Z")
        self.assertEqual(state["last_success_at"], "2026-04-21T08:59:00Z")
        self.assertEqual(state["trading_gate_reason"], "no_trade_symbols")

    def test_retry_keeps_partial_readiness_when_new_trade_symbol_is_added(self):
        service = DummyWarmupRetryState()
        previous = service._initial_warmup_state()
        previous.update(
            {
                "phase": "ready",
                "started_at": "2026-04-21T08:00:00Z",
                "finished_at": "2026-04-21T08:05:00Z",
                "last_success_at": "2026-04-21T08:05:00Z",
                "ready_symbols_list": ["AAPL", "QQQ"],
                "symbols": ["AAPL", "QQQ"],
                "scan_symbols": ["AAPL"],
                "subscription_symbols": ["QQQ"],
                "trade_symbols": [],
                "monitor_symbols": ["QQQ"],
            }
        )
        snapshot = {
            "target_date": "2026-04-21",
            "symbols": ["AAPL", "MSFT", "QQQ"],
            "scan_symbols": ["AAPL", "MSFT"],
            "subscription_symbols": ["MSFT", "QQQ"],
            "trade_symbols": ["MSFT"],
            "monitor_symbols": ["QQQ"],
            "symbols_total": 3,
            "scan_symbols_total": 2,
            "subscription_symbols_total": 2,
            "trade_symbols_total": 1,
            "monitor_symbols_total": 1,
        }

        state = service._build_transient_warmup_retry_state(
            snapshot,
            previous,
            reason="pb_unavailable_retry",
            requested_at="2026-04-21T09:00:00Z",
            started_at="2026-04-21T09:00:00Z",
        )

        self.assertEqual(state["phase"], "running")
        self.assertEqual(state["ready_symbols_list"], ["AAPL", "QQQ"])
        self.assertEqual(state["pending_symbols"], ["MSFT"])
        self.assertEqual(state["started_at"], "2026-04-21T09:00:00Z")
        self.assertIsNone(state["finished_at"])
        self.assertEqual(state["last_success_at"], "2026-04-21T08:05:00Z")
        self.assertFalse(state["trading_gate_open"])
        self.assertEqual(state["trading_gate_reason"], "pb_unavailable_retry")

    def test_set_warmup_state_keeps_ready_and_pending_disjoint_during_refresh(self):
        service = DummyWarmupRetryState()
        service._warmup_state.update(
            {
                "phase": "ready",
                "ready_symbols_list": ["AAPL", "QQQ", "SPY", "VIX", "OLD"],
                "pending_symbols": [],
                "symbols": ["AAPL", "QQQ", "SPY", "VIX", "OLD"],
                "scan_symbols": ["AAPL"],
                "subscription_symbols": ["QQQ", "SPY", "VIX", "OLD"],
                "trade_symbols": [],
                "monitor_symbols": ["QQQ", "SPY", "VIX", "OLD"],
                "last_success_at": "2026-04-21T08:59:00Z",
            }
        )

        state = service._set_warmup_state(
            phase="running",
            reason="poll",
            started_at="2026-04-21T09:05:00Z",
            finished_at=None,
            symbols=["AAPL", "QQQ", "SPY", "VIX"],
            scan_symbols=["AAPL"],
            subscription_symbols=["QQQ", "SPY", "VIX"],
            trade_symbols=[],
            monitor_symbols=["QQQ", "SPY", "VIX"],
            pending_symbols=["AAPL", "QQQ", "SPY", "VIX"],
            integrity_pending_symbols=["OLD", "QQQ"],
        )

        self.assertEqual(state["phase"], "running")
        self.assertEqual(state["ready_symbols_list"], ["AAPL", "QQQ", "SPY", "VIX"])
        self.assertEqual(state["ready_symbols"], 4)
        self.assertEqual(state["pending_symbols"], [])
        self.assertEqual(state["integrity_pending_symbols"], ["QQQ"])
        self.assertEqual(state["symbols_total"], 4)
        self.assertEqual(state["monitor_symbols_total"], 3)

    def test_trade_readiness_uses_current_compute_when_startup_snapshot_is_stale(self):
        service = DummyWarmupRetryState()
        service._running = True
        service.session_keeper = type("Session", (), {"is_authenticated": True})()
        service._warmup_state.update(
            {
                "phase": "degraded",
                "trading_gate_open": False,
                "trading_gate_reason": "history_repair_pending",
                "symbols": ["AAPL"],
                "subscription_symbols": ["AAPL"],
                "trade_symbols": ["AAPL"],
                "monitor_symbols": [],
                "trade_symbols_total": 1,
                "ready_trade_symbols": 0,
            }
        )
        service._warmup_uses_remote_compute_service = lambda: True
        readiness = {
            "environment": "live",
            "status": "ready",
            "hard_gate_interval": "5m",
            "symbols_total": 1,
            "intervals": {
                "5m": {
                    "status": "ready",
                    "missing_ready_symbols": [],
                    "missing_ready_symbols_total": 0,
                }
            },
        }

        with mock.patch("ibkr_compute.api.compute_status_client.get_remote_compute_status", return_value={"multi_timeframe_readiness": readiness}):
            state = service._trade_readiness_snapshot()

        self.assertTrue(state["open"])
        self.assertEqual(state["reason"], "ready")
        self.assertEqual(state["source"], "runtime_multi_timeframe_readiness")

    def test_trade_readiness_reports_missing_trade_symbol_reason(self):
        service = DummyWarmupRetryState()
        service._running = True
        service.session_keeper = type("Session", (), {"is_authenticated": True})()
        service._warmup_state.update(
            {
                "phase": "degraded",
                "trading_gate_open": False,
                "trading_gate_reason": "history_repair_pending",
                "symbols": ["AAPL", "SPY"],
                "subscription_symbols": ["AAPL", "SPY"],
                "trade_symbols": ["AAPL"],
                "monitor_symbols": ["SPY"],
                "trade_symbols_total": 1,
                "ready_trade_symbols": 0,
            }
        )
        service._warmup_uses_remote_compute_service = lambda: True
        readiness = {
            "environment": "live",
            "status": "blocked",
            "hard_gate_interval": "5m",
            "symbols_total": 2,
            "intervals": {
                "5m": {
                    "status": "blocked",
                    "missing_ready_symbols": ["AAPL"],
                    "missing_ready_symbols_total": 1,
                }
            },
        }

        with mock.patch("ibkr_compute.api.compute_status_client.get_remote_compute_status", return_value={"multi_timeframe_readiness": readiness}):
            state = service._trade_readiness_snapshot()

        self.assertFalse(state["open"])
        self.assertEqual(state["reason"], "missing_trade_symbols")
        self.assertEqual(state["symbols"], ["AAPL"])


if __name__ == "__main__":
    unittest.main()
