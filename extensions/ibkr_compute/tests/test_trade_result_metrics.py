import logging
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.account.snapshot_builder.fetch import fetch_snapshot_sources
from ibkr_compute.observability.trade_result_metrics import refresh_trade_result_today_metrics
from ibkr_compute.orchestration import service_support
from ibkr_compute.orchestration.service_support import TradingServiceSupportMixin


class _Config:
    def get_bool_for_environment(self, _key, _environment, default=False):
        return default

    def get_float_for_environment(self, _key, _environment, default=0.0):
        return default


class _TradeResultService(TradingServiceSupportMixin):
    def __init__(self):
        self.config = _Config()
        self.pb = None
        self._running = False


class TradeResultMetricsTest(unittest.TestCase):
    def test_trade_result_refresh_reads_execution_fills_for_broker_environment(self):
        class _PB:
            def __init__(self):
                self.calls = []

            def get_all_records(self, collection, **kwargs):
                self.calls.append((collection, kwargs))
                return [
                    {"order_ref": "group-win", "realized_pnl_known": True, "realized_pnl": 10},
                    {"order_ref": "group-loss", "realized_pnl_known": True, "realized_pnl": -3},
                    {"order_ref": "group-loss", "realized_pnl_known": True, "realized_pnl": -2},
                ]

        pb = _PB()

        summary = refresh_trade_result_today_metrics(pb, environment="paper", service_name="ibkr-compute")

        self.assertEqual(5.0, summary["realized_pnl"])
        self.assertEqual({"win": 1, "loss": 1, "flat": 0, "unknown": 0}, summary["result_counts"])
        self.assertEqual("ibkr_execution_fills", pb.calls[0][0])
        self.assertIn('environment = "paper"', pb.calls[0][1]["filter"])

    def test_trade_result_summary_groups_partial_fills_by_order_ref(self):
        service = _TradeResultService()

        with mock.patch.object(
            service_support,
            "_service_mod",
            return_value=types.SimpleNamespace(ENVIRONMENT="paper", logger=logging.getLogger("test")),
        ):
            summary = service._build_trade_result_today_summary(
                [
                    {"order_ref": "group-loss", "exec_id": "loss-1", "realized_pnl_known": True, "realized_pnl": -5},
                    {"order_ref": "group-loss", "exec_id": "loss-2", "realized_pnl_known": True, "realized_pnl": -2},
                    {"order_id": "group-win", "exec_id": "win-1", "realized_pnl_known": True, "realized_pnl": 12},
                    {"order_id": "group-flat", "exec_id": "flat-1", "realized_pnl_known": True, "realized_pnl": 0},
                    {"order_id": "group-unknown", "exec_id": "unknown-1", "realized_pnl_known": False, "realized_pnl": 99},
                ]
            )

        self.assertEqual(5.0, summary["realized_pnl"])
        self.assertEqual({"win": 1, "loss": 1, "flat": 1, "unknown": 1}, summary["result_counts"])
        self.assertEqual(4, summary["group_count"])
        self.assertEqual(4, summary["known_fill_count"])
        self.assertEqual(1, summary["unknown_fill_count"])

    def test_account_snapshot_fetch_uses_all_open_orders_when_live_fetch_needed(self):
        class _Lifecycle:
            def get_account_snapshot(self, _account_id):
                return {"summary": {}, "positions": []}

            def get_account_pnl(self, _account_id):
                return {"ok": True}

        class _Tracker:
            def __init__(self):
                self.live_include_all = []

            def get_cached_live_orders(self, *, include_all=False):
                return []

            def get_live_orders(self, *, include_all=False):
                self.live_include_all.append(bool(include_all))
                return []

        service = types.SimpleNamespace(order_lifecycle=_Lifecycle(), order_tracker=_Tracker())

        fetch_snapshot_sources(service, "U123", include_pnl=False)

        self.assertEqual([True], service.order_tracker.live_include_all)


if __name__ == "__main__":
    unittest.main()
