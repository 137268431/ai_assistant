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
from ibkr_compute.api.compute.runtime_state import timing as compute_timing


class ComputeRollupPlanTest(unittest.TestCase):
    def test_canonical_close_uses_incremental_rollup_for_targeted_symbols(self):
        fake_app = mock.Mock()
        fake_app.SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
        fake_app.DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
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
        self.assertEqual(plan["rollup_intervals"], ["15m", "30m", "1h", "4h"])

    def test_history_repair_keeps_full_targeted_rollup(self):
        fake_app = mock.Mock()
        fake_app.SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
        fake_app.DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
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
        self.assertEqual(plan["rollup_intervals"], ["15m", "30m", "1h", "4h", "1d"])

    def test_scheduler_compute_defaults_to_5m_without_rollup(self):
        fake_app = mock.Mock()
        fake_app.SUPPORTED_COMPUTE_ENVIRONMENTS = ["live", "paper", "backtest"]
        fake_app.DEFAULT_COMPUTE_ENVIRONMENTS = ["live", "paper"]
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.INTERVALS = ["5m", "15m", "30m", "1h", "4h", "1d"]
        fake_app.SIGNAL_SUPPRESSED_COMPUTE_SOURCES = set()
        fake_app.normalize_symbols.return_value = []
        fake_app.cfg.has_environment_override.return_value = False
        fake_app.cfg.get_bool_for_environment.return_value = True

        with mock.patch.object(compute_request, "_api_app", return_value=fake_app):
            plan = compute_request.build_compute_execution_plan(
                {
                    "source": "ibkr_scheduler",
                    "environments": ["live"],
                }
            )

        self.assertEqual(plan["intervals"], ["5m"])
        self.assertEqual(plan["rollup_intervals"], [])

    def test_empty_rollup_interval_override_stays_empty(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]

        self.assertEqual(compute_rollup._normalize_target_intervals(fake_app, []), [])
        self.assertEqual(
            compute_rollup._normalize_target_intervals(fake_app, None),
            ["15m", "30m", "1h", "4h", "1d"],
        )


class IncrementalRollupWindowTest(unittest.TestCase):
    def test_fetch_since_falls_back_to_processed_cursor_when_interval_fetch_empty(self):
        fake_app = mock.Mock()
        fake_app.last_interval_fetch_ms = {}
        fake_app.last_processed_ms = {
            ("live", "AAPL", "5m"): 1777056600000,
            ("live", "MSFT", "5m"): 1777056300000,
            ("paper", "AAPL", "5m"): 1777057200000,
            ("live", "AAPL", "15m"): 1777057200000,
        }

        with mock.patch.object(compute_timing, "_api_app", return_value=fake_app):
            since_ms = compute_timing.get_fetch_since_ms("live", "5m")

        self.assertEqual(since_ms, 1777056600000 - (2 * 5 * 60 * 1000))

    def test_recent_rollup_respects_selected_intervals(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.last_interval_fetch_ms = {("live", "5m"): 1776278100000}

        with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app):
            since_ms = compute_rollup._recent_rollup_since_ms(
                "live",
                ["AAPL", "MSFT"],
                intervals=["15m", "30m", "1h", "4h"],
            )

        self.assertEqual(since_ms, 1776278100000 - (2 * 4 * 60 * 60 * 1000))

    def test_incremental_due_intervals_only_keep_closed_higher_timeframes(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]

        with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app):
            self.assertEqual(
                compute_rollup._incremental_due_intervals(
                    1776794700000,
                    intervals=["15m", "30m", "1h", "4h"],
                ),
                [],
            )
            self.assertEqual(
                compute_rollup._incremental_due_intervals(
                    1776795300000,
                    intervals=["15m", "30m", "1h", "4h"],
                ),
                ["15m"],
            )
            self.assertEqual(
                compute_rollup._incremental_due_intervals(
                    1776798000000,
                    intervals=["15m", "30m", "1h", "4h"],
                ),
                ["15m", "30m", "1h"],
            )

    def test_incremental_targeted_rollup_skips_when_no_higher_interval_closes(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.normalize_symbols.return_value = ["AAPL"]

        with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_rollup, "_latest_targeted_5m_bar_ms", return_value=1776794700000), \
                mock.patch.object(compute_rollup, "rebuild_higher_timeframe_bars") as rebuild:
            results = compute_rollup.ensure_higher_timeframe_bars(
                ["live"],
                force=True,
                symbols=["AAPL"],
                incremental=True,
                intervals=["15m", "30m", "1h", "4h"],
            )

        self.assertEqual(
            results["live"],
            {
                "skipped": True,
                "reason": "no_due_intervals",
                "written": 0,
                "errors": 0,
                "intervals": [],
            },
        )
        rebuild.assert_not_called()

    def test_incremental_targeted_rollup_only_rebuilds_due_intervals(self):
        fake_app = mock.Mock()
        fake_app.HIGHER_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]
        fake_app.normalize_symbols.return_value = ["AAPL", "MSFT"]

        with mock.patch.object(compute_rollup, "_api_app", return_value=fake_app), \
                mock.patch.object(compute_rollup, "_latest_targeted_5m_bar_ms", return_value=1776795300000), \
                mock.patch.object(compute_rollup, "_recent_rollup_since_ms", return_value=1776793500000) as since_mock, \
                mock.patch.object(
                    compute_rollup,
                    "rebuild_higher_timeframe_bars",
                    return_value={"processed_5m": 24, "written": 48, "errors": 0},
                ) as rebuild:
            results = compute_rollup.ensure_higher_timeframe_bars(
                ["live"],
                force=True,
                symbols=["AAPL", "MSFT"],
                incremental=True,
                intervals=["15m", "30m", "1h", "4h"],
            )

        since_mock.assert_called_once_with(
            "live",
            ["AAPL", "MSFT"],
            intervals=["15m"],
        )
        rebuild.assert_called_once_with(
            "live",
            symbols=["AAPL", "MSFT"],
            intervals=["15m"],
            since_ms=1776793500000,
        )
        self.assertTrue(results["live"]["targeted"])
        self.assertTrue(results["live"]["incremental"])


if __name__ == "__main__":
    unittest.main()
