import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.order_flow import OrderFlowManager


class ScopedConfig:
    def __init__(self):
        self.values = {
            ("ibkr_order_flow_enabled", "paper"): "true",
            ("ibkr_order_flow_enabled", "live"): "false",
            ("ibkr_order_flow_execution_pool_size", "paper"): "4",
            ("ibkr_order_flow_execution_pool_size", "live"): "2",
        }

    def get_bool_for_environment(self, key, environment, default=False):
        return str(self.values.get((key, environment), str(default))).lower() in {"true", "1", "yes"}

    def get_int_for_environment(self, key, environment, default=0):
        return int(self.values.get((key, environment), default))

    def get_float_for_environment(self, key, environment, default=0.0):
        return float(self.values.get((key, environment), default))

    def get_for_environment(self, key, environment, default=""):
        return str(self.values.get((key, environment), default))


def test_order_flow_manager_uses_broker_environment_for_config_and_data_environment_for_market_data():
    manager = OrderFlowManager(config=ScopedConfig(), environment="paper", data_environment="live")

    assert manager.enabled() is True
    assert manager.execution_pool.max_symbols == 4
    status = manager.status()
    assert status["broker_environment"] == "paper"
    assert status["data_environment"] == "live"

    live_manager = OrderFlowManager(config=ScopedConfig(), environment="live", data_environment="live")
    assert live_manager.enabled() is False
    assert live_manager.execution_pool.max_symbols == 2
