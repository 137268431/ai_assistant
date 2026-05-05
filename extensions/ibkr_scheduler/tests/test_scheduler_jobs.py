import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_scheduler" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

os.environ.setdefault("IBKR_SCHEDULER_AUTOSTART", "false")

if "flask" not in sys.modules:
    import types

    flask_stub = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, name):
            self.name = name

        def route(self, _path, methods=None):
            def decorator(func):
                return func
            return decorator

    flask_stub.Flask = _FakeFlask
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = SimpleNamespace(
        args={},
        headers={},
        method="GET",
        get_json=lambda silent=True: {},
    )
    sys.modules["flask"] = flask_stub

from ibkr_scheduler import scheduler_app as scheduler_app_mod
from ibkr_scheduler.schedule import cron_matches_minute
from ibkr_scheduler.scheduler_app import (
    BAR_INGEST_CURSOR_STATE_KEY,
    COMPUTE_DISPATCH_CURSOR_STATE_KEY,
    SCHEDULER_JOB_STATE_PREFIX,
    SchedulerService,
)


class _FakeConfig:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}

    def refresh(self):
        return None

    def get_for_environment(self, key, environment, default=None):
        return self.overrides.get((key, environment), self.overrides.get(key, (default if default is not None else "")))


class _FakePB:
    def __init__(self):
        self.states = {}
        self.records = []

    def get_state(self, state_key, environment, date="global"):
        return self.states.get((state_key, environment, date))

    def upsert_state(self, state_key, environment, data, date="global"):
        record = {"data": data}
        self.states[(state_key, environment, date)] = record
        return record

    def create_record(self, collection, data):
        self.records.append((collection, data))
        return data


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.content = b"{}"
        self.headers = {}

    def json(self):
        return self._payload


class SchedulerJobsTest(unittest.TestCase):
    def test_cron_matches_minute_supports_ranges_steps_and_weekdays(self):
        monday = datetime(2026, 4, 20, 9, 40, tzinfo=timezone.utc)
        weekend = datetime(2026, 4, 19, 9, 40, tzinfo=timezone.utc)

        self.assertTrue(cron_matches_minute("40 9 * * 1-5", monday))
        self.assertTrue(cron_matches_minute("*/5 * * * *", monday))
        self.assertTrue(
            cron_matches_minute(
                "20 8 * * 1-5",
                datetime(2026, 1, 5, 13, 20, tzinfo=timezone.utc),
                "America/New_York",
            )
        )
        self.assertTrue(
            cron_matches_minute(
                "20 8 * * 1-5",
                datetime(2026, 4, 20, 12, 20, tzinfo=timezone.utc),
                "America/New_York",
            )
        )
        self.assertFalse(cron_matches_minute("41 9 * * 1-5", monday))
        self.assertFalse(cron_matches_minute("40 9 * * 1-5", weekend))
        self.assertFalse(
            cron_matches_minute(
                "20 8 * * 1-5",
                datetime(2026, 1, 5, 12, 20, tzinfo=timezone.utc),
                "America/New_York",
            )
        )

    def test_scan_runtime_cron_matches_0920_et_only(self):
        definition = next(item for item in scheduler_app_mod.CRON_DEFINITIONS if item["id"] == "ibkr_scan_runtime")

        self.assertEqual(definition["cron_expr"], "20 9 * * 1-5")
        self.assertEqual(definition["cron_timezone"], "America/New_York")
        self.assertTrue(
            cron_matches_minute(
                definition["cron_expr"],
                datetime(2026, 4, 20, 13, 20, tzinfo=timezone.utc),
                definition["cron_timezone"],
            )
        )
        self.assertTrue(
            cron_matches_minute(
                definition["cron_expr"],
                datetime(2026, 1, 5, 14, 20, tzinfo=timezone.utc),
                definition["cron_timezone"],
            )
        )
        self.assertFalse(
            cron_matches_minute(
                definition["cron_expr"],
                datetime(2026, 4, 20, 9, 55, tzinfo=timezone.utc),
                definition["cron_timezone"],
            )
        )

    def test_early_expansion_topup_crons_match_0930_to_1030_et(self):
        early = next(item for item in scheduler_app_mod.CRON_DEFINITIONS if item["id"] == "ibkr_early_expansion_topup")
        late = next(item for item in scheduler_app_mod.CRON_DEFINITIONS if item["id"] == "ibkr_early_expansion_topup_late")

        self.assertEqual(early["cron_expr"], "30,40,50 9 * * 1-5")
        self.assertEqual(late["cron_expr"], "0,10,20,30 10 * * 1-5")
        self.assertEqual(early["cron_timezone"], "America/New_York")
        self.assertEqual(late["cron_timezone"], "America/New_York")
        self.assertTrue(cron_matches_minute(early["cron_expr"], datetime(2026, 4, 20, 13, 30, tzinfo=timezone.utc), early["cron_timezone"]))
        self.assertTrue(cron_matches_minute(late["cron_expr"], datetime(2026, 4, 20, 14, 30, tzinfo=timezone.utc), late["cron_timezone"]))
        self.assertFalse(cron_matches_minute(early["cron_expr"], datetime(2026, 4, 20, 13, 20, tzinfo=timezone.utc), early["cron_timezone"]))
        self.assertFalse(cron_matches_minute(late["cron_expr"], datetime(2026, 4, 20, 14, 40, tzinfo=timezone.utc), late["cron_timezone"]))

    def test_intraday_window_admission_cron_matches_regular_session_et(self):
        definition = next(item for item in scheduler_app_mod.CRON_DEFINITIONS if item["id"] == "ibkr_intraday_window_admission")

        self.assertEqual(definition["cron_expr"], "*/5 9-15 * * 1-5")
        self.assertEqual(definition["cron_timezone"], "America/New_York")
        self.assertEqual(
            scheduler_app_mod.NATIVE_API_HTTP_JOB_ENDPOINTS["ibkr_intraday_window_admission"],
            ("POST", "/api/custom/system/jobs/intraday_window_admission"),
        )
        self.assertTrue(
            cron_matches_minute(
                definition["cron_expr"],
                datetime(2026, 4, 20, 13, 35, tzinfo=timezone.utc),
                definition["cron_timezone"],
            )
        )
        self.assertTrue(
            cron_matches_minute(
                definition["cron_expr"],
                datetime(2026, 1, 5, 20, 55, tzinfo=timezone.utc),
                definition["cron_timezone"],
            )
        )
        self.assertFalse(
            cron_matches_minute(
                definition["cron_expr"],
                datetime(2026, 4, 20, 20, 0, tzinfo=timezone.utc),
                definition["cron_timezone"],
            )
        )

    def test_compute_dispatch_updates_cursor_from_latest_persisted_bars(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713797100000}}}
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.compute_dispatch.requests.get",
            return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post", return_value=_FakeResponse({"ok": True, "processed": 3})) as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", "live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertEqual(result["latest_dispatched_bar_time_ms"], 1713797100000)
        self.assertEqual(result["dispatch_cursor"]["intervals"]["5m"]["latest_bar_time_ms"], 1713797100000)
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713797100000)
        self.assertEqual(post_mock.call_args.kwargs["json"]["intervals"], ["5m"])
        self.assertEqual(post_mock.call_args.kwargs["json"]["rollup_intervals"], [])
        job_state = pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}ibkr_compute_runtime", "live", "global")]["data"]
        self.assertEqual(job_state["status"], "ok")
        self.assertGreater(int(job_state["last_success_at_ms"]), 0)
        self.assertTrue(any(collection == "system_events" for collection, _ in pb.records))

    def test_compute_dispatch_skips_backfill_only_ingest_cursor(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797400000,
                        "latest_sources": ["ibkr_history_backfill"],
                    }
                }
            }
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.get") as get_mock:
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post") as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", "live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "non_compute_ingest_source")
        get_mock.assert_not_called()
        post_mock.assert_not_called()
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        dispatch_5m = dispatch["intervals"]["5m"]
        self.assertEqual(dispatch_5m["latest_bar_time_ms"], 1713796800000)
        self.assertEqual(dispatch_5m["latest_non_compute_ingest_bar_time_ms"], 1713797400000)
        self.assertEqual(dispatch_5m["dispatch_skip_reason"], "non_compute_ingest_source")

    def test_compute_dispatch_uses_compute_eligible_cursor_when_backfill_is_newer(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797400000,
                        "latest_sources": ["ibkr_history_backfill"],
                        "latest_compute_ingest_bar_time_ms": 1713797100000,
                        "latest_compute_ingest_bar_us": "2026-04-22 09:35:00",
                        "latest_compute_ingest_bar_cn": "2026-04-22 21:35:00",
                        "latest_compute_ingest_symbols": ["AAPL"],
                        "latest_compute_ingest_sources": ["ibkr_history_close"],
                    }
                }
            }
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.compute_dispatch.requests.get",
            return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post", return_value=_FakeResponse({"ok": True, "processed": 3})) as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", "live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        post_mock.assert_called_once()
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        dispatch_5m = dispatch["intervals"]["5m"]
        self.assertEqual(dispatch_5m["latest_bar_time_ms"], 1713797100000)
        self.assertEqual(dispatch_5m["latest_sources"], ["ibkr_history_close"])

    def test_compute_dispatch_skips_while_compute_startup_preload_running(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713797100000}}}
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.compute_dispatch.requests.get",
            return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "running", "running": True}}),
        ) as get_mock:
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post") as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", "live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "compute_startup_preload_running")
        get_mock.assert_called_once()
        self.assertTrue(str(get_mock.call_args.args[0]).endswith("/health"))
        post_mock.assert_not_called()
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713796800000)
        job_state = pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}ibkr_compute_runtime", "live", "global")]["data"]
        self.assertEqual(job_state["status"], "idle")

    def test_compute_dispatch_runs_after_compute_startup_preload_completes(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713797100000}}}
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.compute_dispatch.requests.get",
            return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post", return_value=_FakeResponse({"ok": True, "processed": 3})) as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", "live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        post_mock.assert_called_once()
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713797100000)

    def test_native_api_job_persists_success_state_and_manual_event(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse({"ok": True, "expired_count": 2}),
        ) as request_mock:
            result = scheduler.run_job(
                "signal_expiry_check",
                "live",
                trigger_source="api_manual",
                scheduled_slot="2026-04-23T10:00Z",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"]["expired_count"], 2)
        request_mock.assert_called_once()
        job_state = pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}signal_expiry_check", "live", "global")]["data"]
        self.assertEqual(job_state["status"], "ok")
        self.assertEqual(job_state["last_scheduled_slot"], "2026-04-23T10:00Z")
        self.assertTrue(any(collection == "system_events" for collection, _ in pb.records))

    def test_premarket_truth_audit_cron_scans_full_watchlist(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse({"ok": True, "summary": {"audit_mode": "bar_only"}}),
        ) as request_mock:
            result = scheduler.run_job(
                "ibkr_data_quality_premarket_truth_audit",
                "live",
                trigger_source="api_manual",
                scheduled_slot="2026-04-23T12:20Z",
            )

        self.assertTrue(result["ok"])
        request_mock.assert_called_once()
        request_payload = request_mock.call_args.kwargs["json"]
        self.assertEqual(request_payload["environment"], "live")
        self.assertEqual(request_payload["source"], "ibkr_scheduler")
        self.assertEqual(request_payload["scan_scope"], "watchlist_full")
        self.assertTrue(request_payload["persist"])
        self.assertIn("market_date", request_payload)

    def test_postmarket_truth_audit_cron_scans_full_watchlist(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse({"ok": True, "summary": {"audit_mode": "bar_only"}}),
        ) as request_mock:
            result = scheduler.run_job(
                "ibkr_data_quality_truth_audit",
                "live",
                trigger_source="api_manual",
                scheduled_slot="2026-04-23T20:20Z",
            )

        self.assertTrue(result["ok"])
        request_mock.assert_called_once()
        request_payload = request_mock.call_args.kwargs["json"]
        self.assertEqual(request_payload["scan_scope"], "watchlist_full")
        self.assertTrue(request_payload["persist"])
        self.assertIn("market_date", request_payload)

    def test_duplicate_slot_is_skipped_without_second_upstream_call(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())
        pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}signal_expiry_check", "live", "global")] = {
            "data": {
                "status": "ok",
                "last_result": {"ok": True},
                "last_scheduled_slot": "2026-04-23T10:00Z",
            }
        }

        with mock.patch("ibkr_scheduler.jobs.upstream_http.requests.request") as request_mock:
            result = scheduler.run_job(
                "signal_expiry_check",
                "live",
                trigger_source="scheduler_loop",
                scheduled_slot="2026-04-23T10:00Z",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "already_triggered_for_slot")
        request_mock.assert_not_called()

    def test_failed_slot_can_retry_within_same_minute(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())
        pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}signal_expiry_check", "live", "global")] = {
            "data": {
                "status": "error",
                "last_result": {"ok": False, "error": "upstream_down"},
                "last_scheduled_slot": "2026-04-23T10:00Z",
            }
        }

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse({"ok": True, "expired_count": 1}),
        ) as request_mock:
            result = scheduler.run_job(
                "signal_expiry_check",
                "live",
                trigger_source="api_manual",
                scheduled_slot="2026-04-23T10:00Z",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"]["expired_count"], 1)
        request_mock.assert_called_once()

    def test_disabled_job_records_disabled_state(self):
        pb = _FakePB()
        cfg = _FakeConfig({"pb_cron_signal_expiry_enabled": "FALSE"})
        scheduler = SchedulerService(pb, cfg)

        result = scheduler.run_job(
            "signal_expiry_check",
            "live",
            trigger_source="scheduler_loop",
            scheduled_slot="2026-04-23T10:00Z",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "disabled")
        job_state = pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}signal_expiry_check", "live", "global")]["data"]
        self.assertEqual(job_state["status"], "disabled")
        self.assertEqual(job_state["last_scheduled_slot"], "2026-04-23T10:00Z")

    def test_run_due_jobs_dispatches_all_matching_native_jobs_with_shared_slot(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())
        when_utc = datetime(2026, 4, 20, 9, 40, tzinfo=timezone.utc)

        with mock.patch.object(scheduler, "run_job", return_value={"ok": True}) as run_job_mock:
            results = scheduler.run_due_jobs(when_utc, "live")

        self.assertEqual(len(results), run_job_mock.call_count)
        called_job_ids = {call.args[0] for call in run_job_mock.call_args_list}
        expected_job_ids = {
            str(definition.get("id") or "")
            for definition in scheduler_app_mod.CRON_DEFINITIONS
            if str(definition.get("runner_kind") or "").strip().lower().startswith("native_")
            and cron_matches_minute(
                str(definition.get("cron_expr") or ""),
                when_utc,
                str(definition.get("cron_timezone") or "UTC"),
            )
        }
        self.assertEqual(called_job_ids, expected_job_ids)
        called_slots = {call.kwargs["scheduled_slot"] for call in run_job_mock.call_args_list}
        self.assertEqual(called_slots, {"2026-04-20T09:40Z"})

    def test_manual_run_route_uses_scheduler_service(self):
        sentinel = {"ok": True, "job_id": "signal_expiry_check"}
        with mock.patch.object(scheduler_app_mod.request, "get_json", return_value={"environment": "paper", "scheduled_slot": "2026-04-23T10:00Z"}):
            with mock.patch.object(scheduler_app_mod.scheduler, "run_job", return_value=sentinel) as run_job_mock:
                payload, status_code = scheduler_app_mod.run_job("signal_expiry_check")

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["job_id"], "signal_expiry_check")
        run_job_mock.assert_called_once_with(
            "signal_expiry_check",
            "paper",
            trigger_source="api_manual",
            scheduled_slot="2026-04-23T10:00Z",
        )


if __name__ == "__main__":
    unittest.main()
