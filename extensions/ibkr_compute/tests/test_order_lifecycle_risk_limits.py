import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.order.order_lifecycle import OrderLifecycle


class _FakeConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_for_environment(self, key, environment, default):
        return self.values.get(key, default)

    def get_int_for_environment(self, key, environment, default):
        return int(self.values.get(key, default))


class _FakeBroker:
    def __init__(self, positions=None):
        self.positions = list(positions or [])

    def list_positions(self):
        return list(self.positions)


class _FakeOrderTracker:
    def __init__(self, orders=None):
        self.orders = list(orders or [])

    def get_live_orders(self):
        return list(self.orders)


class OrderLifecycleRiskLimitTests(unittest.TestCase):
    def test_zero_position_limit_disables_daily_trade_count_cap(self):
        lifecycle = OrderLifecycle(config=_FakeConfig({"position_limit_max": 0}))

        for _ in range(25):
            lifecycle.increment_position_count()

        self.assertFalse(lifecycle.is_position_limit_reached)
        self.assertEqual(lifecycle.status()["position_limit_max"], 0)

    def test_positive_position_limit_still_blocks_after_count_reached(self):
        lifecycle = OrderLifecycle(config=_FakeConfig({"position_limit_max": 2}))

        lifecycle.increment_position_count()
        self.assertFalse(lifecycle.is_position_limit_reached)
        lifecycle.increment_position_count()

        self.assertTrue(lifecycle.is_position_limit_reached)

    def test_stop_loss_breaker_uses_consecutive_count(self):
        lifecycle = OrderLifecycle(config=_FakeConfig({"consecutive_stop_loss_limit": 3}))

        lifecycle.increment_sl_count()
        lifecycle.increment_sl_count()
        self.assertFalse(lifecycle.is_sl_circuit_breaker)
        lifecycle.reset_sl_count()
        self.assertFalse(lifecycle.is_sl_circuit_breaker)
        lifecycle.increment_sl_count()
        lifecycle.increment_sl_count()
        lifecycle.increment_sl_count()

        self.assertTrue(lifecycle.is_sl_circuit_breaker)

    def test_fixed_position_symbols_default_to_boxx_ibkr(self):
        lifecycle = OrderLifecycle(config=_FakeConfig({}))

        self.assertTrue(lifecycle.is_fixed_position_symbol("BOXX"))
        self.assertTrue(lifecycle.is_fixed_position_symbol("ibkr"))
        self.assertEqual(["BOXX", "IBKR"], lifecycle.status()["fixed_position_symbols"])

    def test_fixed_position_symbols_can_fallback_to_eod_keep_symbols(self):
        lifecycle = OrderLifecycle(config=_FakeConfig({"eod_keep_symbols": "SGOV, BIL"}))

        self.assertTrue(lifecycle.is_fixed_position_symbol("SGOV"))
        self.assertTrue(lifecycle.is_fixed_position_symbol("bil"))
        self.assertTrue(lifecycle.is_fixed_position_symbol("BOXX"))
        self.assertTrue(lifecycle.is_fixed_position_symbol("IBKR"))

    def test_strategy_capacity_excludes_fixed_positions_and_counts_open_entries(self):
        lifecycle = OrderLifecycle(
            config=_FakeConfig({
                "max_strategy_open_positions": 2,
                "fixed_position_symbols": "BOXX,IBKR",
            }),
            broker=_FakeBroker(
                [
                    {"ticker": "BOXX", "position": 100},
                    {"ticker": "AAPL", "position": 5},
                ]
            ),
        )
        tracker = _FakeOrderTracker(
            [
                {"orderId": "101", "ticker": "MSFT", "status": "Submitted", "cOID": "entry_MSFT_long_20260513_100000"},
                {"orderId": "102", "ticker": "MSFT", "status": "Submitted", "parentId": "101", "cOID": "tp_MSFT_long_20260513_100000"},
                {"orderId": "103", "ticker": "IBKR", "status": "Submitted", "cOID": "entry_IBKR_long_20260513_100000"},
            ]
        )

        snapshot = lifecycle.strategy_capacity_snapshot(order_tracker=tracker)

        self.assertTrue(snapshot["capacity_full"])
        self.assertEqual(2, snapshot["strategy_capacity_used"])
        self.assertEqual(["AAPL"], snapshot["strategy_open_position_symbols"])
        self.assertEqual(["MSFT"], snapshot["open_strategy_entry_order_symbols"])


if __name__ == "__main__":
    unittest.main()
