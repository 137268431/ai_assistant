import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("PB_RETRY_ATTEMPTS", "1")
os.environ.setdefault("PB_RETRY_BACKOFF_SECONDS", "0.01")

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    fake_flask = types.ModuleType("flask")
    fake_flask.request = types.SimpleNamespace(get_json=lambda silent=True: {}, args={}, values={})
    sys.modules["flask"] = fake_flask

from ibkr_compute.api.compute import request as compute_request
from ibkr_compute.api.compute import rollup as compute_rollup


class ComputeRollupPlanTest(unittest.TestCase):
    def test_canonical_close_uses_incremental_rollup_for_targeted_symbols(self):
        fake_app = mock.Mock()
        fake_app.SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
        fake_app.DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]
        fake_app.INTERVALS = ["5m", "15m", "30m", "1h", "4h", "1d"]
        fake_app.SIGNAL_SUPPRESSED_COMPUTE_SOURCES = {
            "history_repair",
            "history_rebuild",
            "recompute",
            "targeted_recompute",
        }
        fake_app.normalize_symbols.return_value = ["AAPL", "MSFT"]
        fake_app.cfg.has_environment_override.return_value = False
        fake_app.cfg.get_bool_for_environment.return_value = True

        with mock.patch.object(compute_request, "_api_app", return_value=fake_app):
            plan = compute_request.build_compute_execution_plan(
                {
                    "source": "canonical_close",
                    "environments": ["live"],
                    "symbols": ["AAPL", "MSFT"],
                }
            )

        self.assertTrue(plan["targeted_rollup"])
        self.assertTrue(plan["force_rollup"])
        self.assertTrue(plan["incremental_rollup"])

    def test_history_repair_keeps_full_targeted_rollup(self):
        fake_app = mock.Mock()
        fake_app.SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
        fake_app.DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]
        fake_app.INTERVALS = ["5m", "15m", "30m", "1h", "4h", "1d"]
        fake_app.SIGNAL_SUPPRESSED_COMPUTE_SOURCES = {
            "history_repair",
            "history_rebuild",
            "recompute",
            "targeted_recompute",
        }
        fake_app.normalize_symbols.return_value = ["AAPL"]
        fake_app.cfg.has_environment_override.return_value = False
        fake_app.cfg.get_bool_for_environment.return_value = True

        with mock.patch.object(compute_request, "_api_app", return_value=fake_app):
            plan = compute_request.build_compute_execution_plan(
                {
                    "source": "history_repair",
                    "environments": ["live"],
                    "symbols": ["AAPL"],
                }
            )

        self.assertTrue(plan["targeted_rollup"])
        self.assertTrue(plan["force_rollup"])
        self.assertFalse(plan["incremental_rollup"])


class IncrementalRollupWindowTest(unittest.TestCase):
    def test_recent_rollup_uses_two_max_interval_windows(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.last_interval_fetch_ms = {("live", "5m"): 1776278100000}

        with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app):
            since_ms = compute_rollup._recent_rollup_since_ms(
                "live",
                ["AAPL", "MSFT"],
                intervals=fake_app.HIGHER_INTERVALS,
            )

        self.assertEqual(since_ms, 1776278100000 - (2 * 24 * 60 * 60 * 1000))


if __name__ == "__main__":
    unittest.main()
