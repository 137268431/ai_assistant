import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration.integrity import TradingServiceIntegrityMixin
from ibkr_compute.orchestration.warmup import TradingServiceWarmupMixin
from ibkr_compute.orchestration.warmup_cycle import TradingServiceWarmupCycleMixin


class _FakeConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_for_environment(self, key, environment, default=None):
        del environment
        return self.values.get(key, default)

    def get_int_for_environment(self, key, environment, default=0):
        del environment
        return int(self.values.get(key, default))

    def get_float_for_environment(self, key, environment, default=0.0):
        del environment
        return float(self.values.get(key, default))

    def get_bool_for_environment(self, key, environment, default=False):
        del environment
        return str(self.values.get(key, str(default).lower())).lower() in ("true", "1", "yes")


class _DummyWarmup(TradingServiceWarmupCycleMixin, TradingServiceWarmupMixin, TradingServiceIntegrityMixin):
    def __init__(self, values=None):
        self.config = _FakeConfig(values)

    def _warmup_uses_remote_compute_service(self):
        return True


def _service_mod():
    return SimpleNamespace(
        ENVIRONMENT="live",
        DEFAULT_WARMUP_REQUIRED_INTERVAL="5m",
        STARTUP_BACKGROUND_PRIME_INTERVALS=("15m", "30m", "1h"),
        STARTUP_HISTORY_REPAIR_SHORT_PERIOD="1d",
        indicator_ready_bar_count=lambda: 212,
    )


class MultiTimeframeWarmupPeriodTest(unittest.TestCase):
    def test_warmup_days_uses_longest_indicator_timeframe(self):
        warmup = _DummyWarmup()

        with mock.patch("ibkr_compute.orchestration.warmup._service_mod", return_value=_service_mod()):
            self.assertEqual(warmup._multi_timeframe_warmup_days(), 51)
            self.assertEqual(warmup._multi_timeframe_warmup_period(), "51d")

    def test_startup_history_repair_keeps_5m_refresh_short_for_latest_gap(self):
        warmup = _DummyWarmup()
        snapshot = {"repair_reason": "today_regular_incomplete=1"}

        with mock.patch("ibkr_compute.orchestration.integrity._service_mod", return_value=_service_mod()):
            period = warmup._startup_history_repair_period(snapshot)

        self.assertEqual(period, "1d")

    def test_period_overrides_keep_pending_5m_backfill_bounded(self):
        warmup = _DummyWarmup({"ibkr_warmup_required_5m_period": "3d"})

        with mock.patch("ibkr_compute.orchestration.warmup._service_mod", return_value=_service_mod()):
            overrides = warmup._multi_timeframe_5m_period_overrides(["aapl", "MSFT"])

        self.assertEqual(overrides, {"AAPL": {"5m": "3d"}, "MSFT": {"5m": "3d"}})

    def test_indicator_backfill_plan_uses_remote_missing_higher_timeframe_symbols(self):
        warmup = _DummyWarmup({"ibkr_warmup_indicator_backfill_intervals": "15m,30m,1h"})
        snapshot = {
            "symbols": ["AAPL", "MSFT", "VIX"],
            "conid_map": {"AAPL": 1, "VIX": 2},
        }
        payload = {
            "service_profile": "compute",
            "multi_timeframe_readiness": {
                "intervals": {
                    "15m": {"status": "ready"},
                    "1h": {
                        "status": "degraded",
                        "missing_indicator_symbols": ["aapl", "MSFT"],
                        "missing_ready_symbols": ["VIX"],
                    },
                }
            },
        }

        with (
            mock.patch("ibkr_compute.orchestration.warmup_cycle._service_mod", return_value=_service_mod()),
            mock.patch(
                "ibkr_compute.api.compute_status_client.get_remote_compute_status",
                return_value=payload,
            ),
        ):
            plan = warmup._collect_warmup_indicator_backfill_plan(snapshot)

        self.assertEqual(plan, {"1h": ["AAPL"]})


if __name__ == "__main__":
    unittest.main()
