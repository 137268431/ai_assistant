import json
import plistlib
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

OPS_VALIDATE_ROOT = Path(__file__).resolve().parents[3] / "ops" / "validate"
if str(OPS_VALIDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(OPS_VALIDATE_ROOT))

from manage_today_tv_replay_stress_launchd import (  # noqa: E402
    audit_retry_artifacts,
    build_watchdog_plist,
    classify_retry_summary,
    compact_retry_summary,
    ensure_decision,
    finalize,
    latest_terminal_artifact_item,
    labels_from_plists,
    parse_launchctl_print,
    readiness_from_components,
    select_current_runner_item,
)

ET = ZoneInfo("America/New_York")


class TodayTvReplayLaunchdManagerTest(unittest.TestCase):
    def test_parse_launchctl_print_extracts_runner_state(self):
        text = """
        state = running
        runs = 1
        pid = 5181
        last exit code = (never exited)
        """

        result = parse_launchctl_print(text)

        self.assertEqual("running", result["state"])
        self.assertEqual(1, result["runs"])
        self.assertEqual(5181, result["pid"])
        self.assertEqual("(never exited)", result["last_exit_code"])

    def test_classify_retry_summary_success_and_market_end(self):
        success = classify_retry_summary(
            {"ok": True, "reason": "stable_success_reached", "attempts": [{}], "updated_at_et": "2026-06-08T10:10:00-04:00"},
            launch_state="",
            now_et=datetime(2026, 6, 8, 10, 11, tzinfo=ET),
        )
        abandoned = classify_retry_summary(
            {"ok": False, "reason": "market_end_reached", "attempts": [{}], "updated_at_et": "2026-06-08T14:30:00-04:00"},
            launch_state="",
            now_et=datetime(2026, 6, 8, 14, 31, tzinfo=ET),
        )

        self.assertEqual("success", success["phase"])
        self.assertTrue(success["terminal"])
        self.assertTrue(success["goal_complete"])
        self.assertEqual("abandoned_market_end", abandoned["phase"])
        self.assertTrue(abandoned["terminal"])
        self.assertTrue(abandoned["abandoned"])

    def test_classify_retry_summary_marks_running_wait_as_stale_after_three_minutes(self):
        current = datetime(2026, 6, 5, 16, 20, tzinfo=ET)
        result = classify_retry_summary(
            {
                "ok": False,
                "reason": "waiting_market_window",
                "attempts": [],
                "updated_at_et": (current - timedelta(seconds=181)).isoformat(),
            },
            launch_state="running",
            now_et=current,
        )

        self.assertEqual("waiting_market_window", result["phase"])
        self.assertTrue(result["stale"])
        self.assertFalse(result["terminal"])

    def test_compact_retry_summary_includes_phase_and_last_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "retry_summary.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "retrying_until_market_end",
                        "updated_at_et": "2026-06-08T10:00:00-04:00",
                        "attempts": [{"attempt": 1, "ok": False, "error": "boom"}],
                    }
                )
            )

            result = compact_retry_summary(tmp, launch={"state": "running"})

        self.assertEqual("retrying_intraday", result["phase"])
        self.assertEqual(1, result["attempts"])
        self.assertEqual(1, result["last_attempt"]["attempt"])

    def test_audit_retry_artifacts_proves_success_attempt_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt_dir = root / "attempt-1"
            attempt_dir.mkdir()
            (root / "retry_summary.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "reason": "stable_success_reached",
                        "updated_at_et": "2026-06-08T10:15:00-04:00",
                        "attempts": [{"attempt": 1, "ok": True, "artifact_dir": "attempt-1"}],
                    }
                )
            )
            summary = {
                "ok": True,
                "run_id": "SIMTV_TEST_R01",
                "market_date": "2026-06-08",
                "selected_chains": 3,
                "selected_full_chains": 3,
                "flow_requirements": {
                    "ok": True,
                    "counts": {
                        "selected_chains": 3,
                        "full_chains": 3,
                        "bracket_chains": 3,
                        "filled_entry_chains": 1,
                        "routed_exit_chains": 1,
                        "closed_reverse_chains": 1,
                    },
                    "thresholds": {
                        "min_bracket_chains": 3,
                        "min_filled_entry_chains": 1,
                        "min_routed_exit_chains": 1,
                        "min_closed_reverse_chains": 1,
                    },
                    "failures": [],
                },
                "account_flat": {"before": {"ok": True}, "after": {"ok": True}},
                "stability": {"before": {"ok": True}, "after": {"ok": True}},
                "burst_results": {"total": 6, "failed": 0},
                "cleanup_results": {"total": 3, "failed": 0},
                "post_cleanup_results": {"total": 3, "failed": 0},
                "chains": [
                    {"synthetic_signal_id": "sim-1", "symbol": "AAPL", "checks": [{"name": "entry_processed", "ok": True}]},
                    {"synthetic_signal_id": "sim-2", "symbol": "MSFT", "checks": [{"name": "entry_processed", "ok": True}]},
                    {"synthetic_signal_id": "sim-3", "symbol": "NVDA", "checks": [{"name": "entry_processed", "ok": True}]},
                ],
            }
            (attempt_dir / "summary.json").write_text(json.dumps(summary))
            (attempt_dir / "health_and_metrics.json").write_text(json.dumps({"before": {}, "after": {}}))
            (attempt_dir / "chains.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"synthetic_signal_id": "sim-1"}),
                        json.dumps({"synthetic_signal_id": "sim-2"}),
                        json.dumps({"synthetic_signal_id": "sim-3"}),
                    ]
                )
                + "\n"
            )

            result = audit_retry_artifacts(root)

        self.assertTrue(result["ok"])
        self.assertEqual("success", result["outcome"])
        self.assertTrue(result["goal_complete"])
        self.assertEqual("complete", result["objective_status"])
        self.assertEqual(3, result["evidence_summary"]["selected_chains"])
        self.assertEqual(3, result["evidence_summary"]["flow_counts"]["bracket_chains"])
        self.assertTrue(result["evidence_summary"]["account_flat_after_ok"])

    def test_audit_retry_artifacts_marks_market_end_abandoned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "retry_summary.json").write_text(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "market_end_reached",
                        "updated_at_et": "2026-06-08T14:30:00-04:00",
                        "attempts": [{"attempt": 1, "ok": False, "error": "not stable"}],
                    }
                )
            )

            result = audit_retry_artifacts(root)

        self.assertTrue(result["ok"])
        self.assertEqual("abandoned", result["outcome"])
        self.assertTrue(result["abandoned"])
        self.assertEqual("abandoned", result["objective_status"])

    def test_audit_retry_artifacts_rejects_success_with_too_few_chains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt_dir = root / "attempt-1"
            attempt_dir.mkdir()
            (root / "retry_summary.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "reason": "stable_success_reached",
                        "updated_at_et": "2026-06-08T10:15:00-04:00",
                        "attempts": [{"attempt": 1, "ok": True, "artifact_dir": "attempt-1"}],
                    }
                )
            )
            summary = {
                "ok": True,
                "selected_chains": 2,
                "selected_full_chains": 2,
                "flow_requirements": {"ok": True, "counts": {"selected_chains": 2, "full_chains": 2}, "failures": []},
                "account_flat": {"before": {"ok": True}, "after": {"ok": True}},
                "stability": {"before": {"ok": True}, "after": {"ok": True}},
                "burst_results": {"total": 4, "failed": 0},
                "cleanup_results": {"total": 2, "failed": 0},
                "post_cleanup_results": {"total": 2, "failed": 0},
                "chains": [
                    {"synthetic_signal_id": "sim-1", "symbol": "AAPL", "checks": [{"name": "entry_processed", "ok": True}]},
                    {"synthetic_signal_id": "sim-2", "symbol": "MSFT", "checks": [{"name": "entry_processed", "ok": True}]},
                ],
            }
            (attempt_dir / "summary.json").write_text(json.dumps(summary))
            (attempt_dir / "health_and_metrics.json").write_text(json.dumps({"before": {}, "after": {}}))
            (attempt_dir / "chains.jsonl").write_text(json.dumps({"synthetic_signal_id": "sim-1"}) + "\n")

            result = audit_retry_artifacts(root)

        self.assertFalse(result["ok"])
        self.assertEqual("attention", result["outcome"])
        self.assertFalse(result["goal_complete"])
        self.assertIn("multi_signal_selected_chains", {item["name"] for item in result["checks"] if not item["ok"]})

    def test_ensure_decision_keeps_healthy_runner(self):
        status = {
            "items": [
                {
                    "loaded": True,
                    "launch": {"state": "running"},
                    "retry_summary": {"phase": "waiting_market_window", "stale": False},
                }
            ]
        }

        result = ensure_decision(status)

        self.assertTrue(result["ok"])
        self.assertEqual("already_running", result["action"])

    def test_ensure_decision_does_not_restart_terminal_result(self):
        status = {
            "items": [
                {
                    "loaded": False,
                    "launch": {"state": "not running"},
                    "retry_summary": {"phase": "success", "goal_complete": True},
                }
            ]
        }

        result = ensure_decision(status)

        self.assertTrue(result["ok"])
        self.assertEqual("terminal_no_restart", result["action"])

    def test_ensure_decision_requires_force_for_stale_running_runner(self):
        status = {
            "items": [
                {
                    "loaded": True,
                    "launch": {"state": "running"},
                    "retry_summary": {"phase": "waiting_market_window", "stale": True},
                }
            ]
        }

        result = ensure_decision(status)

        self.assertFalse(result["ok"])
        self.assertEqual("stale_running_requires_force_restart", result["action"])

    def test_ensure_decision_starts_when_no_runner_is_known(self):
        result = ensure_decision({"items": []})

        self.assertTrue(result["ok"])
        self.assertEqual("start_required", result["action"])

    def test_ensure_decision_does_not_restart_latest_terminal_artifact(self):
        terminal_item = {
            "loaded": False,
            "launch": {"state": "artifact_terminal"},
            "retry_summary": {"phase": "success", "goal_complete": True},
        }

        result = ensure_decision({"items": []}, latest_terminal_item=terminal_item)

        self.assertTrue(result["ok"])
        self.assertEqual("terminal_no_restart", result["action"])
        self.assertEqual("latest_terminal_artifact", result["source"])

    def test_latest_terminal_artifact_item_finds_terminal_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "SIMTV_AUTO_DONE"
            run_dir.mkdir()
            (run_dir / "retry_summary.json").write_text(
                json.dumps({"ok": False, "reason": "market_end_reached", "updated_at_et": "2026-06-08T14:30:00-04:00"})
            )
            (run_dir / "runner.launchd.meta.json").write_text(json.dumps({"label": "done-label"}))

            with mock.patch("manage_today_tv_replay_stress_launchd.ARTIFACT_ROOT", root):
                result = latest_terminal_artifact_item()

        self.assertEqual("done-label", result["label"])
        self.assertTrue(result["retry_summary"]["abandoned"])

    def test_finalize_refuses_pending_artifact_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "retry_summary.json").write_text(
                json.dumps({"ok": False, "reason": "waiting_market_window", "updated_at_et": "2026-06-05T16:00:00-04:00"})
            )

            result = finalize(Namespace(artifact_dir=str(root), force=False, dry_run=False))

        self.assertFalse(result["ok"])
        self.assertEqual("not_terminal_noop", result["action"])

    def test_finalize_allows_terminal_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "retry_summary.json").write_text(
                json.dumps({"ok": False, "reason": "market_end_reached", "updated_at_et": "2026-06-08T14:30:00-04:00"})
            )

            result = finalize(Namespace(artifact_dir=str(root), force=False, dry_run=True))

        self.assertTrue(result["ok"])
        self.assertEqual("would_finalize", result["action"])
        self.assertEqual("abandoned", result["audit"]["outcome"])

    def test_build_watchdog_plist_runs_ensure_on_interval(self):
        plist = build_watchdog_plist(10, stdout_path=Path("/tmp/watchdog.out"), stderr_path=Path("/tmp/watchdog.err"))

        self.assertEqual("com.lzwglory.ai-assistant.today-tv-replay-stress.watchdog", plist["Label"])
        self.assertEqual(30, plist["StartInterval"])
        self.assertIn("ensure", plist["ProgramArguments"])
        self.assertIn("--text", plist["ProgramArguments"])
        self.assertEqual(str(Path("/tmp/watchdog.out")), plist["StandardOutPath"])

    def test_labels_from_plists_excludes_watchdog_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = root / "com.lzwglory.ai-assistant.today-tv-replay-stress.simtv-auto-test.plist"
            watchdog = root / "com.lzwglory.ai-assistant.today-tv-replay-stress.watchdog.plist"
            runner.write_bytes(plistlib.dumps({"Label": "com.lzwglory.ai-assistant.today-tv-replay-stress.simtv-auto-test"}))
            watchdog.write_bytes(plistlib.dumps({"Label": "com.lzwglory.ai-assistant.today-tv-replay-stress.watchdog"}))

            with mock.patch("manage_today_tv_replay_stress_launchd.launch_agents_dir", return_value=root):
                labels = labels_from_plists()

        self.assertEqual(["com.lzwglory.ai-assistant.today-tv-replay-stress.simtv-auto-test"], labels)

    def test_readiness_from_components_accepts_pending_healthy_runner(self):
        result = readiness_from_components(
            status={"running_count": 1, "count": 1},
            watchdog={"loaded": True},
            audit_payload={"ok": True, "outcome": "pending", "objective_status": "pending"},
            replay_readiness={"ok": True, "failures": []},
        )

        self.assertTrue(result["ok"])

    def test_readiness_from_components_rejects_missing_watchdog_or_runner(self):
        result = readiness_from_components(
            status={"running_count": 0, "count": 0},
            watchdog={"loaded": False},
            audit_payload={"ok": True, "outcome": "pending", "objective_status": "pending"},
            replay_readiness={"ok": True, "failures": []},
        )

        self.assertFalse(result["ok"])
        self.assertIn("watchdog_loaded", {item["name"] for item in result["checks"] if not item["ok"]})
        self.assertIn("single_runner_running", {item["name"] for item in result["checks"] if not item["ok"]})

    def test_select_current_runner_item_prefers_healthy_running_runner(self):
        items = [
            {
                "label": "newer-terminal",
                "loaded": False,
                "launch": {"state": "not running"},
                "retry_summary": {"available": True, "terminal": True, "updated_at_et": "2026-06-08T14:30:00-04:00"},
            },
            {
                "label": "running",
                "loaded": True,
                "launch": {"state": "running"},
                "retry_summary": {"available": True, "stale": False, "updated_at_et": "2026-06-05T16:00:00-04:00"},
            },
        ]

        result = select_current_runner_item(items)

        self.assertEqual("running", result["label"])

    def test_select_current_runner_item_uses_latest_summary_without_running_runner(self):
        items = [
            {
                "label": "old",
                "loaded": False,
                "launch": {"state": "not running"},
                "retry_summary": {"available": True, "updated_at_et": "2026-06-05T16:00:00-04:00"},
            },
            {
                "label": "new",
                "loaded": False,
                "launch": {"state": "not running"},
                "retry_summary": {"available": True, "updated_at_et": "2026-06-05T17:00:00-04:00"},
            },
        ]

        result = select_current_runner_item(items)

        self.assertEqual("new", result["label"])


if __name__ == "__main__":
    unittest.main()
