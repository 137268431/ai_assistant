import os
import re
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
from ibkr_scheduler.jobs import compute_dispatch as compute_dispatch_mod
from ibkr_scheduler.schedule import cron_matches_minute
from ibkr_scheduler.scheduler_app import (
    BAR_INGEST_CURSOR_STATE_KEY,
    COMPUTE_DISPATCH_CURSOR_STATE_KEY,
    SCHEDULER_JOB_STATE_PREFIX,
    SchedulerService,
)
from ibkr_compute.core.config import Config


class _FakeConfig:
    def __init__(self, overrides=None):
        self.overrides = {
            "ibkr_market_ws_symbols": "",
            "ibkr_scheduler_rollup_intervals": "",
        }
        self.overrides.update(overrides or {})

    def refresh(self):
        return None

    def get_for_environment(self, key, environment, default=None):
        return self.overrides.get((key, environment), self.overrides.get(key, (default if default is not None else "")))


class _FakePB:
    def __init__(self):
        self.states = {}
        self.records = []
        self.collection_rows = {}

    def get_state(self, state_key, environment, date="global"):
        return self.states.get((state_key, environment, date))

    def upsert_state(self, state_key, environment, data, date="global"):
        record = {"data": data}
        self.states[(state_key, environment, date)] = record
        return record

    def create_record(self, collection, data):
        self.records.append((collection, data))
        return data

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        rows = list(self.collection_rows.get(collection, []))
        if sort == "-bar_time_ms":
            rows.sort(key=lambda row: int((row or {}).get("bar_time_ms", 0) or 0), reverse=True)
        start = max(0, int(page - 1) * int(per_page))
        end = start + int(per_page)
        return rows[start:end]

    def get_all_records(self, collection, filter=None, sort=None, max_pages=10):
        rows = []
        for page in range(1, int(max_pages or 1) + 1):
            items = self.get_records(collection, filter=filter, sort=sort, per_page=200, page=page)
            rows.extend(items)
            if len(items) < 200:
                break
        return rows


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
    def test_status_lite_omits_heavy_items_and_families(self):
        class _FakeScheduler:
            def job_states_for_modes(self, broker_mode, market_data_mode):
                return {"system_heartbeat": {"status": "ok", "last_success_at_ms": 1}}

            def _get_ingest_cursor(self, market_data_mode):
                return {"latest_ingested_bar_time_ms": 1}

            def _get_dispatch_cursor(self, market_data_mode):
                return {"latest_dispatched_bar_time_ms": 1}

        fake_config = _FakeConfig()
        with mock.patch.object(scheduler_app_mod, "scheduler", _FakeScheduler()), mock.patch.object(
            scheduler_app_mod,
            "config",
            fake_config,
        ):
            if hasattr(scheduler_app_mod.app, "test_client"):
                with scheduler_app_mod.app.test_client() as client:
                    response = client.get("/status?broker_mode=paper&market_data_mode=live&lite=1")
                    payload = response.get_json()
            else:
                with mock.patch.object(
                    scheduler_app_mod,
                    "request",
                    SimpleNamespace(args={"broker_mode": "paper", "market_data_mode": "live", "lite": "1"}),
                ):
                    payload = scheduler_app_mod.status()

        self.assertTrue(payload["ok"])
        self.assertEqual("paper", payload["broker_mode"])
        self.assertEqual("live", payload["market_data_mode"])
        self.assertIn("jobs", payload)
        self.assertNotIn("items", payload)
        self.assertNotIn("families", payload)

    def test_long_running_native_jobs_use_short_async_submit_timeout(self):
        scheduler = SchedulerService(_FakePB(), _FakeConfig())

        with mock.patch.object(scheduler_app_mod, "run_upstream_http_job", return_value={"ok": True}) as run_mock:
            scheduler._run_native_http_job(
                "ibkr_scan_runtime",
                "live",
                broker_mode="paper",
                market_data_mode="live",
                mode_scope="market_data",
            )
            scheduler._run_native_http_job(
                "ibkr_data_quality_truth_audit_cycle",
                "live",
                broker_mode="paper",
                market_data_mode="live",
                mode_scope="market_data",
            )
            scheduler._run_native_api_job(
                "system_status_reminder",
                "live",
                broker_mode="paper",
                market_data_mode="live",
                mode_scope="market_data",
            )

        self.assertEqual(run_mock.call_args_list[0].kwargs["timeout_seconds"], scheduler_app_mod.DEFAULT_ASYNC_SUBMIT_TIMEOUT_SECONDS)
        self.assertEqual(run_mock.call_args_list[1].kwargs["timeout_seconds"], scheduler_app_mod.DEFAULT_ASYNC_SUBMIT_TIMEOUT_SECONDS)
        self.assertEqual(run_mock.call_args_list[2].kwargs["timeout_seconds"], 150)

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
        definition = next(item for item in scheduler_app_mod.CRON_DEFINITIONS if item["id"] == "ibkr_early_expansion_topup")
        schedules = {item["id"]: item for item in definition["schedules"]}
        early = schedules["early_0930_0950"]
        late = schedules["early_1000_1030"]

        self.assertEqual(early["cron_expr"], "30,40,50 9 * * 1-5")
        self.assertEqual(late["cron_expr"], "0,10,20,30 10 * * 1-5")
        self.assertEqual(early["cron_timezone"], "America/New_York")
        self.assertEqual(late["cron_timezone"], "America/New_York")
        self.assertIn("ibkr_early_expansion_topup_late", definition["deprecated_aliases"])
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

    def test_scheduler_loop_uses_configured_data_environment(self):
        scheduler = SchedulerService(_FakePB(), _FakeConfig())
        seen_environments = []
        scheduler.run_due_jobs = lambda _when_utc, **kwargs: seen_environments.append(kwargs.get("market_data_mode")) or []
        scheduler._stop_event = SimpleNamespace(is_set=lambda: False, wait=lambda _seconds: True)

        with mock.patch.dict(os.environ, {"IBKR_BROKER_MODE": "paper", "IBKR_MARKET_DATA_MODE": "live"}):
            scheduler._loop()

        self.assertEqual(seen_environments, ["live"])

    def test_storage_governor_cron_matches_low_peak_et(self):
        definition = next(item for item in scheduler_app_mod.CRON_DEFINITIONS if item["id"] == "ibkr_storage_governor")

        self.assertEqual(definition["cron_expr"], "20 3 * * *")
        self.assertEqual(definition["cron_timezone"], "America/New_York")
        self.assertEqual(
            scheduler_app_mod.NATIVE_HTTP_JOB_ENDPOINTS["ibkr_storage_governor"],
            ("POST", "/storage/cleanup"),
        )
        self.assertTrue(
            cron_matches_minute(
                definition["cron_expr"],
                datetime(2026, 4, 20, 7, 20, tzinfo=timezone.utc),
                definition["cron_timezone"],
            )
        )
        self.assertFalse(
            cron_matches_minute(
                definition["cron_expr"],
                datetime(2026, 4, 20, 7, 10, tzinfo=timezone.utc),
                definition["cron_timezone"],
            )
        )

    def test_data_quality_jobs_are_collapsed_to_multi_schedule_jobs(self):
        repair = next(item for item in scheduler_app_mod.CRON_DEFINITIONS if item["id"] == "ibkr_data_quality_repair_sweep")
        truth = next(item for item in scheduler_app_mod.CRON_DEFINITIONS if item["id"] == "ibkr_data_quality_truth_audit_cycle")

        self.assertNotIn("ibkr_data_quality_open_sweep", {item["id"] for item in scheduler_app_mod.CRON_DEFINITIONS})
        self.assertEqual({item["id"] for item in repair["schedules"]}, {"open_sweep", "close_sweep"})
        self.assertIn("ibkr_data_quality_open_sweep", repair["deprecated_aliases"])
        self.assertIn("ibkr_data_quality_close_sweep", repair["deprecated_aliases"])
        self.assertEqual(
            {item["payload_mode"] for item in truth["schedules"]},
            {"previous_business_day", "current_day"},
        )

    def test_scan_runtime_submits_async_job(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse(
                {
                    "ok": True,
                    "accepted": True,
                    "async": True,
                    "run_id": "scheduler:ibkr_scan_runtime:default:20260521T1320Z",
                    "status": "accepted",
                    "date": "2026-05-21",
                }
            ),
        ) as request_mock:
            result = scheduler.run_job(
                "ibkr_scan_runtime",
                market_data_mode="live",
                trigger_source="scheduler_loop",
                scheduled_slot="2026-05-21T13:20Z",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        self.assertEqual("pending", pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}ibkr_scan_runtime", "live", "global")]["data"]["status"])
        request_payload = request_mock.call_args.kwargs["json"]
        self.assertTrue(request_payload["async"])
        self.assertEqual("scheduler:ibkr_scan_runtime:default:20260521T1320Z", request_payload["run_id"])
        self.assertEqual("2026-05-21T13:20Z", request_payload["scheduled_slot"])

    def test_data_quality_repair_sweep_sends_watchlist_full_payload(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse(
                {
                    "ok": True,
                    "accepted": True,
                    "async": True,
                    "operation_id": "scheduler:ibkr_data_quality_repair_sweep:default:123",
                    "status": "accepted",
                }
            ),
        ) as request_mock:
            result = scheduler.run_job(
                "ibkr_data_quality_repair_sweep",
                market_data_mode="live",
                trigger_source="api_manual",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        request_mock.assert_called_once()
        self.assertEqual(request_mock.call_args.kwargs["timeout"], scheduler_app_mod.DEFAULT_ASYNC_SUBMIT_TIMEOUT_SECONDS)
        request_payload = request_mock.call_args.kwargs["json"]
        self.assertEqual(request_payload["scan_scope"], "watchlist_full")
        self.assertTrue(request_payload["persist"])
        self.assertTrue(request_payload["repair"])
        self.assertTrue(request_payload["async"])
        self.assertTrue(request_payload["allow_repair_defer"])
        self.assertTrue(str(request_payload["operation_id"]).startswith("scheduler:ibkr_data_quality_repair_sweep:"))
        self.assertEqual(request_payload["run_id"], request_payload["operation_id"])
        self.assertNotIn("repair_intervals", request_payload)
        self.assertNotIn("max_repair_symbols_per_run", request_payload)
        self.assertNotIn("repair_time_budget_s", request_payload)
        self.assertFalse(request_payload["force_repair_now"])
        self.assertEqual(request_payload["source"], "ibkr_scheduler")
        job_state = pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}ibkr_data_quality_repair_sweep", "live", "global")]["data"]
        self.assertEqual(job_state["status"], "pending")
        large_events = [
            payload for collection, payload in pb.records
            if collection == "system_events" and payload.get("source") == "ibkr_large_operation"
        ]
        self.assertEqual(len(large_events), 1)
        self.assertIn("大规模操作开始", large_events[0]["title"])

    def test_data_quality_repair_sweep_fails_empty_async_success_payload(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse(
                {
                    "ok": True,
                    "accepted": True,
                    "async": True,
                    "operation_id": "scheduler:ibkr_data_quality_repair_sweep:default:123",
                    "status": "accepted",
                }
            ),
        ):
            result = scheduler.run_job(
                "ibkr_data_quality_repair_sweep",
                market_data_mode="live",
                trigger_source="api_manual",
            )

        self.assertTrue(result["pending"])
        with mock.patch(
            "ibkr_scheduler.scheduler_app.requests.get",
            return_value=_FakeResponse(
                {
                    "ok": True,
                    "status": "completed",
                    "operation_id": result["async_operation"]["operation_id"],
                    "result": {
                        "ok": True,
                        "symbols": [],
                        "summary": {
                            "expected_symbols_total": 0,
                            "scanned_symbols_total": 0,
                        },
                    },
                }
            ),
        ):
            poll_results = scheduler.poll_pending_jobs(market_data_mode="live", force=True)

        self.assertEqual(1, len(poll_results))
        self.assertFalse(poll_results[0]["ok"])
        self.assertEqual(poll_results[0]["error"], "empty_data_quality_repair_sweep")
        self.assertTrue(poll_results[0]["empty_repair_sweep"])
        job_state = pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}ibkr_data_quality_repair_sweep", "live", "global")]["data"]
        self.assertEqual(job_state["status"], "error")

    def test_config_defaults_cover_seed_defaults_without_mismatch(self):
        seed_path = Path(__file__).resolve().parents[3] / "extensions" / "pocketbase" / "seeds" / "import.js"
        seed_text = seed_path.read_text()
        seed_defaults = {
            key: default
            for key, _value, default in re.findall(
                r"cfg\('([^']+)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'",
                seed_text,
            )
        }
        seed_only = sorted(set(seed_defaults) - set(Config.DEFAULTS))
        mismatches = [
            key
            for key in sorted(set(seed_defaults) & set(Config.DEFAULTS))
            if str(seed_defaults[key]).lower() != str(Config.DEFAULTS[key]).lower()
        ]

        self.assertEqual(seed_only, [])
        self.assertEqual(mismatches, [])
        self.assertIn("CONFIG_ALIAS_FALLBACKS", seed_text)
        self.assertIn("payload.value = aliasRecord.value ?? payload.value", seed_text)

    def test_config_deprecated_aliases_are_used_for_canonical_reads(self):
        cfg = Config()
        cfg._records_by_key = {
            "pb_cron_system_scan_summary_enabled": [
                {"key": "pb_cron_system_scan_summary_enabled", "value": "FALSE", "environment": "global"}
            ],
        }

        self.assertFalse(
            cfg.get_bool_for_environment(
                "pb_cron_system_market_open_reminder_enabled",
                "live",
                True,
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
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertEqual(result["latest_dispatched_bar_time_ms"], 1713797100000)
        self.assertEqual(result["dispatch_cursor"]["intervals"]["5m"]["latest_bar_time_ms"], 1713797100000)
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713797100000)
        self.assertEqual(post_mock.call_args.kwargs["json"]["intervals"], ["5m"])
        self.assertEqual(post_mock.call_args.kwargs["json"]["rollup_intervals"], [])
        self.assertNotIn("symbols", post_mock.call_args.kwargs["json"])
        job_state = pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}ibkr_compute_runtime", "live", "global")]["data"]
        self.assertEqual(job_state["status"], "ok")
        self.assertGreater(int(job_state["last_success_at_ms"]), 0)
        self.assertTrue(any(collection == "system_events" for collection, _ in pb.records))

    def test_compute_dispatch_includes_market_monitors_and_rollup_intervals(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713797100000}}}
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        scheduler = SchedulerService(
            pb,
            _FakeConfig(
                {
                    "ibkr_market_ws_symbols": "SPY, QQQ, VIX",
                    "ibkr_scheduler_rollup_intervals": "15m,30m,1h,4h",
                }
            ),
        )

        with mock.patch(
            "ibkr_scheduler.jobs.compute_dispatch.requests.get",
            return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post", return_value=_FakeResponse({"ok": True, "processed": 3})) as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        request_payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(request_payload["symbols"], ["QQQ", "SPY", "VIX"])
        self.assertEqual(request_payload["rollup_intervals"], ["15m", "30m", "1h", "4h"])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_compute_dispatch_symbols"], ["QQQ", "SPY", "VIX"])

    def test_compute_dispatch_retries_transient_compute_busy_before_success(self):
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
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.time.sleep") as sleep_mock:
                with mock.patch(
                    "ibkr_scheduler.jobs.compute_dispatch.requests.post",
                    side_effect=[
                        _FakeResponse({"ok": False, "error": "compute_busy", "retryable": True}, status_code=503),
                        _FakeResponse({"ok": True, "processed": 3}),
                    ],
                ) as post_mock:
                    result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["compute_dispatch_retry_count"], 1)
        self.assertEqual(result["compute_dispatch_attempts"], 2)
        self.assertEqual(post_mock.call_count, 2)
        sleep_mock.assert_called_once_with(compute_dispatch_mod.COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS[0])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713797100000)

    def test_compute_dispatch_stops_after_bounded_compute_busy_retries(self):
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
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.time.sleep") as sleep_mock:
                with mock.patch(
                    "ibkr_scheduler.jobs.compute_dispatch.requests.post",
                    return_value=_FakeResponse({"ok": False, "error": "compute_busy", "retryable": True}, status_code=503),
                ) as post_mock:
                    result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "compute_busy")
        self.assertEqual(result["status_code"], 503)
        self.assertEqual(result["compute_dispatch_retry_count"], len(compute_dispatch_mod.COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS))
        self.assertEqual(result["compute_dispatch_attempts"], len(compute_dispatch_mod.COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS) + 1)
        self.assertEqual(post_mock.call_count, len(compute_dispatch_mod.COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS) + 1)
        sleep_mock.assert_has_calls([mock.call(delay) for delay in compute_dispatch_mod.COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713796800000)

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
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

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

    def test_compute_dispatch_includes_target_candidates_from_backfill_cursor(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797400000,
                        "latest_sources": ["ibkr_history_backfill"],
                        "latest_batch_symbols": ["aapl", "MSFT", "QQQ"],
                    }
                }
            }
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        pb.collection_rows["ibkr_targets"] = [
            {"symbol": "AAPL", "environment": "live", "date": "2024-04-22", "status": "candidate", "score": 91},
            {"symbol": "MSFT", "environment": "live", "date": "2024-04-22", "status": "active", "score": 88},
            {"symbol": "QQQ", "environment": "live", "date": "2024-04-22", "status": "ignored", "score": 70},
        ]
        pb.collection_rows["ibkr_bars"] = [
            {"symbol": "AAPL", "environment": "live", "interval": "5m", "bar_time_ms": 1713797400000},
            {"symbol": "MSFT", "environment": "live", "interval": "5m", "bar_time_ms": 1713797100000},
            {"symbol": "QQQ", "environment": "live", "interval": "5m", "bar_time_ms": 1713797400000},
        ]
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.compute_dispatch.requests.get",
            return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post", return_value=_FakeResponse({"ok": True, "processed": 2})) as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["compute_dispatch_target_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(result["compute_dispatch_target_status_counts"], {"candidate": 1, "active": 1})
        post_mock.assert_called_once()
        request_payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(request_payload["symbols"], ["AAPL", "MSFT"])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        dispatch_5m = dispatch["intervals"]["5m"]
        self.assertEqual(dispatch_5m["latest_bar_time_ms"], 1713797400000)
        self.assertEqual(dispatch_5m["latest_target_dispatch_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(dispatch_5m["latest_target_dispatch_date"], "2024-04-22")

    def test_compute_dispatch_repairs_targets_when_global_cursor_already_advanced(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797400000,
                        "latest_compute_ingest_bar_time_ms": 1713797400000,
                        "latest_compute_ingest_symbols": ["aapl"],
                        "latest_sources": ["ibkr_history_backfill"],
                        "latest_batch_symbols": ["XPEV"],
                    }
                }
            }
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797400000,
                        "latest_batch_symbols": ["AAPL"],
                    }
                }
            }
        }
        pb.collection_rows["ibkr_targets"] = [
            {"symbol": "AAPL", "environment": "live", "date": "2024-04-22", "status": "active", "score": 91},
            {"symbol": "XPEV", "environment": "live", "date": "2024-04-22", "status": "candidate", "score": 88},
        ]
        pb.collection_rows["ibkr_bars"] = [
            {"symbol": "AAPL", "environment": "live", "interval": "5m", "bar_time_ms": 1713797400000},
            {"symbol": "XPEV", "environment": "live", "interval": "5m", "bar_time_ms": 1713797400000},
        ]
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.compute_dispatch.requests.get",
            return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post", return_value=_FakeResponse({"ok": True, "processed": 2})) as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["compute_dispatch_target_reason"], "target_symbols_not_dispatched")
        self.assertEqual(result["compute_dispatch_target_symbols"], ["XPEV"])
        request_payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(request_payload["symbols"], ["AAPL", "XPEV"])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        dispatch_5m = dispatch["intervals"]["5m"]
        self.assertEqual(dispatch_5m["latest_bar_time_ms"], 1713797400000)
        self.assertEqual(dispatch_5m["latest_target_dispatch_symbols"], ["XPEV"])
        self.assertEqual(dispatch_5m["latest_compute_dispatch_symbols"], ["AAPL", "XPEV"])

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
                        "latest_compute_ingest_symbols": ["msft", "AAPL", " aapl ", "", None],
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
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        post_mock.assert_called_once()
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        dispatch_5m = dispatch["intervals"]["5m"]
        self.assertEqual(dispatch_5m["latest_bar_time_ms"], 1713797100000)
        self.assertEqual(dispatch_5m["latest_sources"], ["ibkr_history_close"])
        request_payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(request_payload["source"], "ibkr_scheduler")
        self.assertEqual(request_payload["environments"], ["live"])
        self.assertEqual(request_payload["intervals"], ["5m"])
        self.assertEqual(request_payload["rollup_intervals"], [])
        self.assertEqual(request_payload["symbols"], ["AAPL", "MSFT"])

    def test_compute_dispatch_syncs_cursor_when_indicators_and_signals_already_current(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797100000,
                        "latest_compute_ingest_bar_time_ms": 1713797100000,
                        "latest_compute_ingest_symbols": ["aapl", "MSFT"],
                        "latest_compute_ingest_sources": ["ibkr_history_close"],
                    }
                }
            }
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713796800000,
                        "latest_signal_dispatch_bar_time_ms": 1713797100000,
                        "latest_signal_dispatch_symbols": ["AAPL", "MSFT"],
                    }
                }
            }
        }
        pb.collection_rows["ibkr_indicators"] = [
            {"symbol": "AAPL", "environment": "live", "interval": "5", "bar_time_ms": 1713797100000},
            {"symbol": "MSFT", "environment": "live", "interval": "5m", "bar_time_ms": 1713797100000},
        ]
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.compute_dispatch.requests.get",
            return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post") as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["reason"], "indicators_already_current")
        post_mock.assert_not_called()
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        dispatch_5m = dispatch["intervals"]["5m"]
        self.assertEqual(dispatch_5m["latest_bar_time_ms"], 1713797100000)
        self.assertEqual(dispatch_5m["dispatch_source"], "ibkr_scheduler_indicator_coverage")
        self.assertEqual(dispatch_5m["indicator_coverage_status"], "covered")
        self.assertEqual(dispatch_5m["signal_dispatch_status"], "covered")
        job_state = pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}ibkr_compute_runtime", "live", "global")]["data"]
        self.assertEqual(job_state["status"], "ok")

    def test_compute_dispatch_runs_when_indicators_current_but_signals_not_dispatched(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797100000,
                        "latest_compute_ingest_bar_time_ms": 1713797100000,
                        "latest_compute_ingest_symbols": ["aapl", "MSFT"],
                        "latest_compute_ingest_sources": ["ibkr_history_close"],
                    }
                }
            }
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        pb.collection_rows["ibkr_indicators"] = [
            {"symbol": "AAPL", "environment": "live", "interval": "5", "bar_time_ms": 1713797100000},
            {"symbol": "MSFT", "environment": "live", "interval": "5m", "bar_time_ms": 1713797100000},
        ]
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.compute_dispatch.requests.get",
            return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post", return_value=_FakeResponse({"ok": True, "processed": 2})) as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["signal_dispatch_status"], "missing")
        post_mock.assert_called_once()
        self.assertEqual(post_mock.call_args.kwargs["json"]["symbols"], ["AAPL", "MSFT"])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        dispatch_5m = dispatch["intervals"]["5m"]
        self.assertEqual(dispatch_5m["latest_bar_time_ms"], 1713797100000)
        self.assertEqual(dispatch_5m["dispatch_source"], "ibkr_scheduler")
        self.assertEqual(dispatch_5m["latest_signal_dispatch_bar_time_ms"], 1713797100000)
        self.assertEqual(dispatch_5m["latest_signal_dispatch_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(dispatch_5m["signal_dispatch_status"], "covered")

    def test_compute_dispatch_waits_when_close_compute_is_inflight(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797100000,
                        "latest_compute_ingest_symbols": ["AAPL"],
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
            side_effect=[
                _FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
                _FakeResponse(
                    {
                        "ok": True,
                        "realtime_compute": {
                            "inflight": True,
                            "inflight_age_s": 30,
                            "inflight_timeout_threshold_s": 900,
                            "stalled": False,
                        },
                    }
                ),
            ],
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post") as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "close_compute_inflight")
        self.assertTrue(result["compute_in_progress"])
        post_mock.assert_not_called()
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713796800000)

    def test_compute_dispatch_waits_when_official_close_is_inflight(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797100000,
                        "latest_compute_ingest_symbols": ["AAPL"],
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
            side_effect=[
                _FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
                _FakeResponse(
                    {
                        "ok": True,
                        "canonical_5m": {
                            "running": True,
                            "phase": "fetching",
                            "cycle_age_s": 45,
                            "current_due_bucket_ms": 1713797100000,
                        },
                    }
                ),
            ],
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post") as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "official_5m_close_inflight")
        self.assertTrue(result["compute_in_progress"])
        self.assertTrue(result["official_5m_close_in_progress"])
        post_mock.assert_not_called()
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713796800000)

    def test_compute_dispatch_repairs_only_missing_indicator_symbols(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797100000,
                        "latest_compute_ingest_symbols": ["AAPL", "MSFT"],
                    }
                }
            }
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713796800000,
                        "latest_signal_dispatch_bar_time_ms": 1713797100000,
                        "latest_signal_dispatch_symbols": ["AAPL"],
                    }
                }
            }
        }
        pb.collection_rows["ibkr_indicators"] = [
            {"symbol": "AAPL", "environment": "live", "interval": "5", "bar_time_ms": 1713797100000},
        ]
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.compute_dispatch.requests.get",
            return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
        ):
            with mock.patch("ibkr_scheduler.jobs.compute_dispatch.requests.post", return_value=_FakeResponse({"ok": True, "processed": 1})) as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["missing_indicator_symbols"], ["MSFT"])
        post_mock.assert_called_once()
        self.assertEqual(post_mock.call_args.kwargs["json"]["symbols"], ["MSFT"])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        dispatch_5m = dispatch["intervals"]["5m"]
        self.assertEqual(dispatch_5m["latest_bar_time_ms"], 1713797100000)
        self.assertEqual(dispatch_5m["missing_indicator_symbols"], ["MSFT"])

    def test_compute_dispatch_defers_busy_chunk_without_advancing_full_cursor(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797100000,
                        "latest_compute_ingest_symbols": ["AAPL", "MSFT"],
                    }
                }
            }
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch.dict(os.environ, {"IBKR_COMPUTE_DISPATCH_CHUNK_SIZE": "1"}):
            with mock.patch(
                "ibkr_scheduler.jobs.compute_dispatch.requests.get",
                return_value=_FakeResponse({"ok": True, "compute_startup_preload": {"status": "completed", "running": False}}),
            ):
                with mock.patch("ibkr_scheduler.jobs.compute_dispatch.time.sleep") as sleep_mock:
                    with mock.patch(
                        "ibkr_scheduler.jobs.compute_dispatch.requests.post",
                        side_effect=[
                            _FakeResponse({"ok": False, "error": "compute_busy", "retryable": True}, status_code=503),
                            _FakeResponse({"ok": False, "error": "compute_busy", "retryable": True}, status_code=503),
                            _FakeResponse({"ok": False, "error": "compute_busy", "retryable": True}, status_code=503),
                            _FakeResponse({"ok": True, "processed": 1}),
                        ],
                    ) as post_mock:
                        result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["reason"], "compute_busy_deferred")
        self.assertEqual(result["deferred_busy_symbols"], ["AAPL"])
        self.assertEqual(result["latest_dispatched_bar_time_ms"], 1713796800000)
        self.assertEqual(result["latest_partial_dispatched_bar_time_ms"], 1713797100000)
        self.assertEqual(post_mock.call_count, 4)
        self.assertEqual(post_mock.call_args_list[-1].kwargs["json"]["symbols"], ["MSFT"])
        sleep_mock.assert_has_calls([mock.call(delay) for delay in compute_dispatch_mod.COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        dispatch_5m = dispatch["intervals"]["5m"]
        self.assertEqual(dispatch_5m["latest_bar_time_ms"], 1713796800000)
        self.assertEqual(dispatch_5m["latest_partial_dispatched_bar_time_ms"], 1713797100000)
        self.assertEqual(dispatch_5m["deferred_busy_symbols"], ["AAPL"])

    def test_compute_dispatch_does_not_advance_cursor_when_compute_fails(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {
                "intervals": {
                    "5m": {
                        "latest_bar_time_ms": 1713797100000,
                        "latest_compute_ingest_symbols": ["aapl", "MSFT"],
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
            with mock.patch(
                "ibkr_scheduler.jobs.compute_dispatch.requests.post",
                return_value=_FakeResponse({"ok": False, "error": "compute_failed"}, status_code=500),
            ) as post_mock:
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "compute_failed")
        post_mock.assert_called_once()
        self.assertEqual(post_mock.call_args.kwargs["json"]["symbols"], ["AAPL", "MSFT"])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713796800000)

    def test_compute_dispatch_does_not_advance_cursor_when_compute_reports_errors(self):
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
            with mock.patch(
                "ibkr_scheduler.jobs.compute_dispatch.requests.post",
                return_value=_FakeResponse({"ok": True, "errors": 2, "processed": 1}),
            ):
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "compute_errors")
        self.assertEqual(result["compute_errors"], 2)
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713796800000)

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
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

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
                result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

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
                broker_mode="live",
                market_data_mode="live",
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

    def test_system_scan_summary_alias_runs_market_open_canonical_job(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse({"ok": True, "sent": True}),
        ) as request_mock:
            result = scheduler.run_job(
                "system_scan_summary",
                broker_mode="live",
                market_data_mode="live",
                trigger_source="api_manual",
                scheduled_slot="2026-04-23T13:30Z",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["canonical_job_id"], "system_market_open_reminder")
        self.assertEqual(result["alias_job_id"], "system_scan_summary")
        self.assertEqual(request_mock.call_args.kwargs["url"], "http://127.0.0.1:5102/api/custom/system/jobs/market_open_reminder")
        self.assertIn((f"{SCHEDULER_JOB_STATE_PREFIX}system_market_open_reminder", "live", "global"), pb.states)
        self.assertNotIn((f"{SCHEDULER_JOB_STATE_PREFIX}system_scan_summary", "live", "global"), pb.states)

    def test_late_topup_alias_uses_canonical_schedule_state(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse({"ok": True, "added": 1}),
        ) as request_mock:
            result = scheduler.run_job(
                "ibkr_early_expansion_topup_late",
                broker_mode="live",
                market_data_mode="live",
                trigger_source="api_manual",
                scheduled_slot="2026-04-23T14:30Z",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["canonical_job_id"], "ibkr_early_expansion_topup")
        self.assertEqual(result["alias_job_id"], "ibkr_early_expansion_topup_late")
        self.assertEqual(result["schedule_id"], "early_1000_1030")
        request_payload = request_mock.call_args.kwargs["json"]
        self.assertEqual(request_payload["schedule_id"], "early_1000_1030")
        job_state = pb.states[(f"{SCHEDULER_JOB_STATE_PREFIX}ibkr_early_expansion_topup", "live", "global")]["data"]
        self.assertIn("early_1000_1030", job_state["last_runs"])

    def test_premarket_truth_audit_cron_scans_full_watchlist(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse({"ok": True, "summary": {"audit_mode": "bar_only"}}),
        ) as request_mock:
            result = scheduler.run_job(
                "ibkr_data_quality_premarket_truth_audit",
                broker_mode="live",
                market_data_mode="live",
                trigger_source="api_manual",
                scheduled_slot="2026-04-23T12:20Z",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["canonical_job_id"], "ibkr_data_quality_truth_audit_cycle")
        self.assertEqual(result["alias_job_id"], "ibkr_data_quality_premarket_truth_audit")
        self.assertEqual(result["schedule_id"], "premarket_previous_business_day")
        request_mock.assert_called_once()
        request_payload = request_mock.call_args.kwargs["json"]
        self.assertNotIn("environment", request_payload)
        self.assertEqual(request_payload["market_data_mode"], "live")
        self.assertEqual(request_payload["selected_mode"], "live")
        self.assertEqual(request_payload["source"], "ibkr_scheduler")
        self.assertEqual(request_payload["scan_scope"], "watchlist_full")
        self.assertTrue(request_payload["persist"])
        self.assertEqual(request_payload["payload_mode"], "previous_business_day")
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
                broker_mode="live",
                market_data_mode="live",
                trigger_source="api_manual",
                scheduled_slot="2026-04-23T20:20Z",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["canonical_job_id"], "ibkr_data_quality_truth_audit_cycle")
        self.assertEqual(result["alias_job_id"], "ibkr_data_quality_truth_audit")
        self.assertEqual(result["schedule_id"], "postmarket_current_day")
        request_mock.assert_called_once()
        request_payload = request_mock.call_args.kwargs["json"]
        self.assertEqual(request_payload["scan_scope"], "watchlist_full")
        self.assertTrue(request_payload["persist"])
        self.assertEqual(request_payload["payload_mode"], "current_day")
        self.assertIn("market_date", request_payload)

    def test_storage_governor_dispatches_balanced_profile(self):
        pb = _FakePB()
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch(
            "ibkr_scheduler.jobs.upstream_http.requests.request",
            return_value=_FakeResponse({"ok": True, "total_deleted": 4}),
        ) as request_mock:
            result = scheduler.run_job(
                "ibkr_storage_governor",
                broker_mode="live",
                market_data_mode="live",
                trigger_source="api_manual",
                scheduled_slot="2026-04-23T07:20Z",
            )

        self.assertTrue(result["ok"])
        request_mock.assert_called_once()
        request_payload = request_mock.call_args.kwargs["json"]
        self.assertEqual(request_mock.call_args.kwargs["url"], "http://127.0.0.1:5100/storage/cleanup")
        self.assertNotIn("environment", request_payload)
        self.assertEqual(request_payload["market_data_mode"], "live")
        self.assertEqual(request_payload["selected_mode"], "live")
        self.assertEqual(request_payload["source"], "ibkr_scheduler")
        self.assertEqual(request_payload["profile"], "balanced_50g")
        self.assertFalse(request_payload["dry_run"])
        self.assertFalse(request_payload["force"])

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
                broker_mode="live",
                market_data_mode="live",
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
                broker_mode="live",
                market_data_mode="live",
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
            broker_mode="live",
            market_data_mode="live",
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
            results = scheduler.run_due_jobs(when_utc, market_data_mode="live")

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
        with mock.patch.object(scheduler_app_mod.request, "get_json", return_value={"broker_mode": "paper", "market_data_mode": "live", "scheduled_slot": "2026-04-23T10:00Z"}):
            with mock.patch.object(scheduler_app_mod.scheduler, "run_job", return_value=sentinel) as run_job_mock:
                payload, status_code = scheduler_app_mod.run_job("signal_expiry_check")

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["job_id"], "signal_expiry_check")
        run_job_mock.assert_called_once_with(
            "signal_expiry_check",
            broker_mode="paper",
            market_data_mode="live",
            trigger_source="api_manual",
            scheduled_slot="2026-04-23T10:00Z",
            schedule_id="",
        )


if __name__ == "__main__":
    unittest.main()
