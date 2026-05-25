import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.order_flow import CandidateQueueManager, ExecutionPoolManager, OrderFlowAggregator, OrderFlowTick


class OrderFlowAggregatorTest(unittest.TestCase):
    def test_aggregates_cvd_bars_and_confirms_direction(self):
        agg = OrderFlowAggregator(intervals=(10, 60))
        base = 1_700_000_000_000

        agg.update(OrderFlowTick("AAPL", base, 100.0, 10, "buy", bid=99.99, ask=100.0))
        agg.update(OrderFlowTick("AAPL", base + 1_000, 100.01, 20, "buy", bid=100.0, ask=100.01))
        closed = agg.update(OrderFlowTick("AAPL", base + 61_000, 100.02, 5, "buy", bid=100.01, ask=100.02))

        bar = next((item for item in closed if item.interval_seconds == 60), None)
        self.assertIsNotNone(bar)
        self.assertEqual(30, bar.buy_volume)
        self.assertGreater(bar.cvd_close, 0)


class CandidateQueueManagerTest(unittest.TestCase):
    def test_merges_same_direction_and_blocks_opposite(self):
        queue = CandidateQueueManager(max_candidates=10)
        first = queue.add_signal(
            {
                "symbol": "MSFT",
                "direction": "long",
                "signal_id": "s1",
                "extra": {"setup": "sd_squeeze_breakout_long", "quality_score": 82},
            },
            at_ms=1_000,
        )
        merged = queue.add_signal(
            {
                "symbol": "MSFT",
                "direction": "long",
                "signal_id": "s2",
                "extra": {"setup": "vwap_trend_pullback_long", "quality_score": 84},
            },
            at_ms=2_000,
        )
        blocked = queue.add_signal(
            {
                "symbol": "MSFT",
                "direction": "short",
                "signal_id": "s3",
                "extra": {"setup": "mean_reversion_short", "quality_score": 90},
            },
            at_ms=3_000,
        )

        self.assertEqual(first.key, merged.key)
        self.assertIn("vwap_trend_pullback_long", merged.confluence)
        self.assertEqual("blocked", blocked.status)
        self.assertEqual(1, queue.status()["active_count"])
        self.assertEqual(1, queue.status()["blocked_count"])

    def test_expires_by_ttl(self):
        queue = CandidateQueueManager(max_candidates=10, ttl_by_setup={"breakout": 2})
        queue.add_signal(
            {"symbol": "NVDA", "direction": "long", "extra": {"setup": "breakout", "quality_score": 80}},
            at_ms=1_000,
        )
        expired = queue.expire(at_ms=3_500)
        self.assertEqual(1, len(expired))
        self.assertEqual(0, queue.status()["active_count"])


class ExecutionPoolManagerTest(unittest.TestCase):
    def test_limits_entry_slots_and_releases_position_watch(self):
        queue = CandidateQueueManager()
        first = queue.add_signal({"symbol": "A", "direction": "long", "extra": {"setup": "breakout", "quality_score": 90}}, at_ms=1_000)
        second = queue.add_signal({"symbol": "B", "direction": "long", "extra": {"setup": "breakout", "quality_score": 90}}, at_ms=2_000)
        third = queue.add_signal({"symbol": "C", "direction": "long", "extra": {"setup": "breakout", "quality_score": 90}}, at_ms=3_000)
        pool = ExecutionPoolManager(max_symbols=3, max_position_slots=1, entry_watch_after_fill_sec=1)

        self.assertTrue(pool.allocate_candidate(first, conid=1, at_ms=1_000)[0])
        self.assertTrue(pool.allocate_candidate(second, conid=2, at_ms=2_000)[0])
        ok, _slot, reason = pool.allocate_candidate(third, conid=3, at_ms=3_000)
        self.assertFalse(ok)
        self.assertEqual("entry_slots_full", reason)

        pool.mark_filled_watch("A", conid=1, at_ms=4_000)
        self.assertEqual(1, pool.status()["position_slot_count"])
        pool.release_expired_watches(6_000)
        self.assertEqual(0, pool.status()["position_slot_count"])


if __name__ == "__main__":
    unittest.main()
