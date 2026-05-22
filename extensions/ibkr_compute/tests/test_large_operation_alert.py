import sys
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.core.large_operation_alert import emit_large_operation_alert, large_operation_reasons


class _FakePB:
    def __init__(self):
        self.states = {}
        self.records = []

    def get_state(self, state_key, environment, date="global"):
        return self.states.get((state_key, environment, date))

    def upsert_state(self, state_key, environment, data, date="global"):
        record = {"data": dict(data or {})}
        self.states[(state_key, environment, date)] = record
        return record

    def create_record(self, collection, data):
        self.records.append((collection, dict(data or {})))
        return {"id": f"rec_{len(self.records)}", **dict(data or {})}


class _Config:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_for_environment(self, key, _environment, default=""):
        return self.values.get(key, default)

    def get_bool_for_environment(self, key, _environment, default=False):
        return bool(self.values.get(key, default))

    def get_int_for_environment(self, key, _environment, default=0):
        return int(self.values.get(key, default))


class LargeOperationAlertTest(unittest.TestCase):
    def test_reasons_cover_planned_and_runtime_thresholds(self):
        reasons = large_operation_reasons(
            {
                "symbols_total": 26,
                "intervals": ["5m", "1d"],
                "period": "2y",
                "request_count": 120,
                "retry_count": 11,
                "throttle_count": 51,
                "written": 10001,
                "duration_s": 121,
            }
        )

        self.assertIn("symbols>=25:26", reasons)
        self.assertIn("tasks>=50:52", reasons)
        self.assertIn("high_interval:1d", reasons)
        self.assertIn("period_days>=120:730", reasons)
        self.assertIn("requests>=100:120", reasons)
        self.assertIn("retry>=10:11", reasons)
        self.assertIn("throttle>=50:51", reasons)
        self.assertIn("written>=10000:10001", reasons)

    def test_emit_deduplicates_start_progress_and_terminal(self):
        pb = _FakePB()
        operation = {
            "operation_id": "op-large",
            "operation_type": "history_backfill",
            "job_id": "unit_test",
            "symbols_total": 25,
            "intervals": ["5m"],
            "data_environment": "live",
            "broker_mode": "paper",
        }

        with mock.patch("ibkr_compute.core.large_operation_alert.time.time", return_value=100.0):
            first = emit_large_operation_alert(pb, operation, config=_Config(), stage="start", broker_mode="paper")
            duplicate = emit_large_operation_alert(pb, operation, config=_Config(), stage="start", broker_mode="paper")
            progress = emit_large_operation_alert(
                pb,
                {**operation, "duration_s": 130},
                config=_Config({"ibkr_large_operation_progress_cooldown_s": 300}),
                stage="progress",
                broker_mode="paper",
            )
            progress_duplicate = emit_large_operation_alert(
                pb,
                {**operation, "duration_s": 140},
                config=_Config({"ibkr_large_operation_progress_cooldown_s": 300}),
                stage="progress",
                broker_mode="paper",
            )
            done = emit_large_operation_alert(pb, operation, config=_Config(), stage="completed", broker_mode="paper")
            done_duplicate = emit_large_operation_alert(pb, operation, config=_Config(), stage="completed", broker_mode="paper")

        self.assertTrue(first["ok"])
        self.assertEqual(duplicate["reason"], "start_already_notified")
        self.assertTrue(progress["ok"])
        self.assertEqual(progress_duplicate["reason"], "progress_cooldown")
        self.assertTrue(done["ok"])
        self.assertEqual(done_duplicate["reason"], "terminal_already_notified")
        self.assertEqual(len([row for row in pb.records if row[0] == "system_events"]), 3)

    def test_duration_gate_suppresses_auto_topup_until_threshold(self):
        pb = _FakePB()
        operation = {
            "operation_id": "op-topup",
            "operation_type": "history_backfill",
            "job_id": "watchlist_idle_topup",
            "source": "watchlist_idle_topup",
            "symbols_total": 119,
            "intervals": ["5m"],
            "data_environment": "live",
            "broker_mode": "paper",
        }

        with mock.patch("ibkr_compute.core.large_operation_alert.time.time", return_value=100.0):
            start = emit_large_operation_alert(pb, operation, config=_Config(), stage="start", broker_mode="paper")
            progress_short = emit_large_operation_alert(
                pb,
                {**operation, "duration_s": 60},
                config=_Config(),
                stage="progress",
                broker_mode="paper",
            )
            completed_short = emit_large_operation_alert(
                pb,
                {**operation, "duration_s": 60},
                config=_Config(),
                stage="completed",
                broker_mode="paper",
            )
            progress = emit_large_operation_alert(
                pb,
                {**operation, "duration_s": 130},
                config=_Config({"ibkr_large_operation_progress_cooldown_s": 300}),
                stage="progress",
                broker_mode="paper",
            )
            progress_duplicate = emit_large_operation_alert(
                pb,
                {**operation, "duration_s": 140},
                config=_Config({"ibkr_large_operation_progress_cooldown_s": 300}),
                stage="progress",
                broker_mode="paper",
            )
            completed = emit_large_operation_alert(
                pb,
                {**operation, "duration_s": 130},
                config=_Config(),
                stage="completed",
                broker_mode="paper",
            )

        self.assertEqual(start["reason"], "duration_gate_pending")
        self.assertEqual(progress_short["reason"], "duration_below_gate")
        self.assertEqual(completed_short["reason"], "duration_below_gate")
        self.assertTrue(progress["ok"])
        self.assertEqual(progress["stage"], "progress")
        self.assertEqual(progress_duplicate["reason"], "progress_cooldown")
        self.assertTrue(completed["ok"])
        self.assertEqual(len([row for row in pb.records if row[0] == "system_events"]), 2)
        detail = pb.records[0][1]["detail"]
        self.assertTrue(detail["duration_gate"])
        self.assertEqual(detail["duration_gate_source"], "watchlist_idle_topup")
        self.assertEqual(detail["duration_gate_threshold_s"], 120)
        self.assertEqual(detail["duration_gate_elapsed_s"], 130)

    def test_duration_gate_keeps_failed_and_deferred_alerts_immediate(self):
        pb = _FakePB()
        operation = {
            "operation_type": "history_backfill",
            "job_id": "watchlist_idle_topup",
            "source": "watchlist_idle_topup",
            "symbols_total": 119,
            "intervals": ["5m"],
            "duration_s": 30,
            "data_environment": "live",
            "broker_mode": "paper",
        }

        failed = emit_large_operation_alert(
            pb,
            {**operation, "operation_id": "op-topup-failed", "error": "broker_timeout"},
            config=_Config(),
            stage="failed",
            broker_mode="paper",
        )
        deferred = emit_large_operation_alert(
            pb,
            {**operation, "operation_id": "op-topup-deferred"},
            config=_Config(),
            stage="deferred",
            broker_mode="paper",
        )

        self.assertTrue(failed["ok"])
        self.assertTrue(deferred["ok"])
        self.assertEqual(len([row for row in pb.records if row[0] == "system_events"]), 2)
        self.assertEqual(pb.records[0][1]["level"], "error")
        self.assertEqual(pb.records[1][1]["detail"]["alert_stage"], "deferred")


if __name__ == "__main__":
    unittest.main()
