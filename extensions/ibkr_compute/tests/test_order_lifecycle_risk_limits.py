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


if __name__ == "__main__":
    unittest.main()
