import sys
import threading
import unittest
from pathlib import Path

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


class WarmupRetryStateTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
