import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.system.monitor_support import build_system_monitor_payload
from ibkr_api.system.monitor_support import derive_monitor_service_map
from ibkr_api.system.scheduler_support import build_scheduler_summary
from ibkr_api.system.scheduler_support import scheduler_status
from ibkr_api.system.service_state import derive_compute_state


def _raise(message):
    raise RuntimeError(message)


def _backtest_topology(status="peer"):
    return {
        "services": {
            "ibkr-backtest": {
                "service_name": "ibkr-backtest",
                "kind": "backtest_plane",
                "fault_domain": "backtest_plane",
                "owner": "ibkr-backtest",
                "status": status,
                "internal_url": "http://backtest.internal:5105",
                "restart_independent": True,
            }
        }
    }


def _healthy_split_stack_payload(backtest_status):
    return {
        "status": "ok",
        "runtime": {
            "status": "running",
            "runtime_phase": "running",
            "gateway": {"running": True, "reachable": True, "managed_by": "ibkr-runtime", "pid": 42},
            "session": {"authenticated": True},
            "websocket": {"connected": True, "ready": True},
        },
        "compute": {
            "status": "running",
            "total_engines": 10,
            "ready_engines": 10,
        },
        "backtest": backtest_status,
        "service_topology": _backtest_topology(),
    }


class SystemMonitorSupportTest(unittest.TestCase):
    def test_compute_partial_optional_engines_still_reports_running(self):
        state = derive_compute_state(
            {"status": "running", "total_engines": 712, "ready_engines": 490},
            observed_at="2026-04-29T00:00:00+00:00",
        )

        self.assertEqual(state["status"], "running")
        self.assertTrue(state["ready"])
        self.assertEqual(state["readiness_phase"], "ready")

    def test_monitor_includes_backtest_and_treats_idle_worker_as_running(self):
        service_monitor = derive_monitor_service_map(
            "live",
            _healthy_split_stack_payload(
                {
                    "ok": True,
                    "status": "idle",
                    "worker_status": "idle",
                    "worker": {"status": "idle"},
                    "ib_gateway_client_id": 81,
                    "active_runs": 0,
                    "queued_runs": 0,
                    "error": "",
                }
            ),
            {"status": "running", "loop_interval_seconds": 30, "job_count": 12, "jobs": {}},
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: _backtest_topology(),
        )

        backtest = service_monitor["services"]["ibkr-backtest"]
        self.assertEqual(backtest["service_name"], "ibkr-backtest")
        self.assertEqual(backtest["status"], "running")
        self.assertTrue(backtest["ready"])
        self.assertEqual(backtest["readiness_phase"], "ready")
        self.assertEqual(backtest["ib_gateway_client_id"], 81)
        self.assertIn("client 81", backtest["detail"])
        self.assertEqual(0, service_monitor["status_counts"].get("degraded", 0))

    def test_monitor_marks_backtest_failure_offline(self):
        service_monitor = derive_monitor_service_map(
            "live",
            _healthy_split_stack_payload(
                {
                    "ok": False,
                    "status": "failed",
                    "worker_status": "failed",
                    "worker": {"status": "failed"},
                    "error": "backtest worker crashed",
                }
            ),
            {"status": "running", "loop_interval_seconds": 30, "job_count": 12, "jobs": {}},
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: _backtest_topology(),
        )

        backtest = service_monitor["services"]["ibkr-backtest"]
        self.assertEqual(backtest["status"], "degraded")
        self.assertFalse(backtest["ready"])
        self.assertEqual(backtest["readiness_phase"], "failed")

    def test_scheduler_lag_is_not_degraded_while_compute_preload_active(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                },
                "compute": {
                    "status": "running",
                    "total_engines": 10,
                    "ready_engines": 10,
                    "compute_startup_preload": {"status": "running", "running": True},
                },
                "service_topology": {"services": {}},
            },
            {
                "status": "running",
                "dispatch_lag_min": 15.0,
                "latest_ingested_bar_time_ms": 1713797100000,
                "loop_interval_seconds": 30,
                "job_count": 12,
            },
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        scheduler = service_monitor["services"]["ibkr-scheduler"]
        self.assertEqual(scheduler["status"], "running")
        self.assertIn("deferred by compute preload", scheduler["detail"])

    def test_scheduler_empty_status_payload_is_unknown_not_offline(self):
        scheduler_payload = scheduler_status(
            "paper",
            request_json=lambda *args, **kwargs: {
                "ok": False,
                "status_code": 200,
                "error": "empty scheduler status payload",
                "payload": {},
            },
            scheduler_base_url="http://scheduler.internal:5103",
        )
        scheduler_summary = build_scheduler_summary("paper", scheduler_payload)
        service_monitor = derive_monitor_service_map(
            "paper",
            {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                },
                "compute": {
                    "status": "running",
                    "total_engines": 10,
                    "ready_engines": 10,
                },
                "service_topology": {"services": {}},
            },
            scheduler_summary,
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        scheduler = service_monitor["services"]["ibkr-scheduler"]
        self.assertEqual(scheduler_payload["status"], "unknown")
        self.assertEqual(scheduler["status"], "unknown")
        self.assertEqual(0, service_monitor["status_counts"].get("offline", 0))
        self.assertIn("status unavailable", scheduler["detail"])
        self.assertIn("awaiting bars", scheduler["detail"])
        self.assertIn("jobs 0", scheduler["detail"])

    def test_scheduler_status_lite_adds_lite_query_param(self):
        calls = []

        payload = scheduler_status(
            "live",
            request_json=lambda *args, **kwargs: calls.append((args, kwargs)) or {
                "ok": True,
                "status_code": 200,
                "payload": {"ok": True, "status": "running", "jobs": {}},
            },
            scheduler_base_url="http://scheduler.internal:5103",
            broker_mode="paper",
            market_data_mode="live",
            lite=True,
        )

        self.assertTrue(payload["ok"])
        self.assertIn(("lite", "1"), calls[0][1]["params"])

    def test_scheduler_lag_is_not_degraded_when_compute_job_deferred_by_preload(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                },
                "compute": {
                    "status": "running",
                    "total_engines": 10,
                    "ready_engines": 10,
                },
                "service_topology": {"services": {}},
            },
            {
                "status": "running",
                "dispatch_lag_min": 15.0,
                "latest_ingested_bar_time_ms": 1713797100000,
                "loop_interval_seconds": 30,
                "job_count": 12,
                "jobs": {
                    "ibkr_compute_runtime": {
                        "status": "idle",
                        "last_result": {
                            "ok": True,
                            "skipped": True,
                            "reason": "compute_startup_preload_running",
                        },
                    }
                },
            },
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        scheduler = service_monitor["services"]["ibkr-scheduler"]
        self.assertEqual(scheduler["status"], "running")
        self.assertIn("deferred by compute preload", scheduler["detail"])

    def test_scheduler_lag_is_not_degraded_while_close_compute_inflight(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                },
                "compute": {
                    "status": "running",
                    "total_engines": 10,
                    "ready_engines": 10,
                    "compute_startup_preload": {"status": "completed", "running": False},
                },
                "service_topology": {"services": {}},
            },
            {
                "status": "running",
                "dispatch_lag_min": 15.0,
                "latest_ingested_bar_time_ms": 1713797100000,
                "loop_interval_seconds": 30,
                "job_count": 12,
                "dispatch_lag_reason": "close_compute_inflight",
                "compute_in_progress": True,
                "compute_in_progress_stalled": False,
                "inflight_age_s": 45.0,
                "inflight_timeout_threshold_s": 900.0,
                "missing_indicator_symbol_count": 2,
            },
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        scheduler = service_monitor["services"]["ibkr-scheduler"]
        self.assertEqual(scheduler["status"], "running")
        self.assertIn("close compute in progress", scheduler["detail"])
        self.assertIn("missing indicators 2", scheduler["detail"])

    def test_scheduler_lag_is_not_degraded_while_official_close_inflight(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                },
                "compute": {
                    "status": "running",
                    "total_engines": 10,
                    "ready_engines": 10,
                    "compute_startup_preload": {"status": "completed", "running": False},
                },
                "service_topology": {"services": {}},
            },
            {
                "status": "running",
                "dispatch_lag_min": 15.0,
                "latest_ingested_bar_time_ms": 1713797100000,
                "loop_interval_seconds": 30,
                "job_count": 12,
                "dispatch_lag_reason": "official_5m_close_inflight",
                "compute_in_progress": True,
                "official_5m_close_in_progress": True,
                "official_5m_close_stalled": False,
                "official_5m_close_age_s": 45.0,
                "missing_indicator_symbol_count": 2,
                "canonical_5m": {"current_due_bucket_ms": 1713797100000},
            },
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        scheduler = service_monitor["services"]["ibkr-scheduler"]
        self.assertEqual(scheduler["status"], "running")
        self.assertIn("official close in progress", scheduler["detail"])
        self.assertIn("missing indicators 2", scheduler["detail"])

    def test_monitor_marks_compute_starting_when_root_preload_active(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                },
                "compute": {
                    "status": "running",
                    "total_engines": 0,
                    "ready_engines": 0,
                },
                "compute_startup_preload": {"status": "running", "running": True},
                "service_topology": {"services": {}},
            },
            {"status": "running", "loop_interval_seconds": 30, "job_count": 12},
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        compute = service_monitor["services"]["ibkr-compute"]
        self.assertEqual(compute["status"], "starting")
        self.assertEqual(compute["readiness_phase"], "preload")
        self.assertFalse(compute["ready"])

    def test_monitor_compute_detail_includes_history_backfill_pressure(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                },
                "compute": {
                    "status": "running",
                    "total_engines": 10,
                    "ready_engines": 10,
                    "data_backfill": {
                        "active_requests": 4,
                        "active_symbols_total": 12,
                        "last_trace": {
                            "source": "backfill_all",
                            "duration_s": 130,
                            "request_count": 101,
                            "retry_count": 3,
                            "throttle_count": 20,
                        },
                    },
                },
                "service_topology": {"services": {}},
            },
            {"status": "running", "loop_interval_seconds": 30, "job_count": 12},
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        compute = service_monitor["services"]["ibkr-compute"]
        self.assertIn("history active 4/12", compute["detail"])
        self.assertIn("history last backfill_all 130s req 101 retry 3 throttle 20", compute["detail"])

    def test_scheduler_lag_degrades_after_compute_preload_completes(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                },
                "compute": {
                    "status": "running",
                    "total_engines": 10,
                    "ready_engines": 10,
                    "compute_startup_preload": {"status": "completed", "running": False},
                },
                "service_topology": {"services": {}},
            },
            {
                "status": "running",
                "dispatch_lag_min": 15.0,
                "latest_ingested_bar_time_ms": 1713797100000,
                "loop_interval_seconds": 30,
                "job_count": 12,
            },
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        scheduler = service_monitor["services"]["ibkr-scheduler"]
        self.assertEqual(scheduler["status"], "degraded")
        self.assertNotIn("deferred by compute preload", scheduler["detail"])

    def test_scheduler_backfill_only_lag_stays_running(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                },
                "compute": {
                    "status": "running",
                    "total_engines": 10,
                    "ready_engines": 10,
                    "compute_startup_preload": {"status": "completed", "running": False},
                },
                "service_topology": {"services": {}},
            },
            {
                "status": "running",
                "dispatch_lag_min": 0.0,
                "raw_dispatch_lag_min": 20.0,
                "latest_ingested_bar_time_ms": 1713798000000,
                "latest_compute_ingested_bar_time_ms": 1713796800000,
                "dispatch_lag_compute_relevant": False,
                "dispatch_lag_reason": "non_compute_ingest_source",
                "loop_interval_seconds": 30,
                "job_count": 12,
            },
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        scheduler = service_monitor["services"]["ibkr-scheduler"]
        self.assertEqual(scheduler["status"], "running")
        self.assertIn("non-compute ingest", scheduler["detail"])

    def test_scheduler_summary_filters_legacy_backfill_only_cursor(self):
        summary = build_scheduler_summary(
            "live",
            {
                "ok": True,
                "status": "running",
                "environment": "live",
                "loop_interval_seconds": 30,
                "ingest_cursor": {
                    "intervals": {
                        "5m": {
                            "latest_bar_time_ms": 1713798000000,
                            "latest_sources": ["ibkr_history_backfill"],
                        }
                    }
                },
                "compute_dispatch_cursor": {
                    "intervals": {
                        "5m": {
                            "latest_bar_time_ms": 1713796800000,
                        }
                    }
                },
                "jobs": {},
            },
        )

        self.assertEqual(summary["raw_dispatch_lag_min"], 20.0)
        self.assertEqual(summary["dispatch_lag_min"], 0.0)
        self.assertFalse(summary["dispatch_lag_compute_relevant"])
        self.assertEqual(summary["dispatch_lag_reason"], "non_compute_ingest_source")

    def test_scheduler_summary_uses_compute_ingest_cursor_when_present(self):
        summary = build_scheduler_summary(
            "live",
            {
                "ok": True,
                "status": "running",
                "environment": "live",
                "loop_interval_seconds": 30,
                "ingest_cursor": {
                    "intervals": {
                        "5m": {
                            "latest_bar_time_ms": 1713798000000,
                            "latest_sources": ["ibkr_history_backfill"],
                            "latest_compute_ingest_bar_time_ms": 1713797400000,
                            "latest_compute_ingest_sources": ["ibkr_history_close"],
                        }
                    }
                },
                "compute_dispatch_cursor": {
                    "intervals": {
                        "5m": {
                            "latest_bar_time_ms": 1713796800000,
                        }
                    }
                },
                "jobs": {},
            },
        )

        self.assertEqual(summary["raw_dispatch_lag_min"], 20.0)
        self.assertEqual(summary["dispatch_lag_min"], 10.0)
        self.assertTrue(summary["dispatch_lag_compute_relevant"])
        self.assertEqual(summary["latest_compute_ingest_sources"], ["ibkr_history_close"])

    def test_scheduler_summary_exposes_close_compute_inflight_result(self):
        summary = build_scheduler_summary(
            "live",
            {
                "ok": True,
                "status": "running",
                "environment": "live",
                "loop_interval_seconds": 30,
                "ingest_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713797400000}}},
                "compute_dispatch_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}},
                "jobs": {
                    "ibkr_compute_runtime": {
                        "status": "idle",
                        "last_result": {
                            "ok": True,
                            "skipped": True,
                            "reason": "close_compute_inflight",
                            "indicator_coverage_status": "missing",
                            "missing_indicator_symbols": ["AAPL", "MSFT"],
                            "missing_indicator_symbol_count": 2,
                            "compute_in_progress": True,
                            "inflight_age_s": 30,
                            "inflight_timeout_threshold_s": 900,
                            "realtime_compute": {"inflight": True, "stalled": False},
                        },
                    }
                },
            },
        )

        self.assertEqual(summary["dispatch_lag_min"], 10.0)
        self.assertEqual(summary["dispatch_lag_reason"], "close_compute_inflight")
        self.assertTrue(summary["compute_in_progress"])
        self.assertFalse(summary["compute_in_progress_stalled"])
        self.assertEqual(summary["missing_indicator_symbols"], ["AAPL", "MSFT"])

    def test_scheduler_summary_exposes_official_close_inflight_result(self):
        summary = build_scheduler_summary(
            "live",
            {
                "ok": True,
                "status": "running",
                "environment": "live",
                "loop_interval_seconds": 30,
                "ingest_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713797400000}}},
                "compute_dispatch_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}},
                "jobs": {
                    "ibkr_compute_runtime": {
                        "status": "idle",
                        "last_result": {
                            "ok": True,
                            "skipped": True,
                            "reason": "official_5m_close_inflight",
                            "indicator_coverage_status": "missing",
                            "missing_indicator_symbols": ["AAPL"],
                            "missing_indicator_symbol_count": 1,
                            "compute_in_progress": True,
                            "official_5m_close_in_progress": True,
                            "official_5m_close_age_s": 45,
                            "official_5m_close_timeout_threshold_s": 600,
                            "canonical_5m": {
                                "running": True,
                                "phase": "fetching",
                                "current_due_bucket_ms": 1713797400000,
                            },
                        },
                    }
                },
            },
        )

        self.assertEqual(summary["dispatch_lag_reason"], "official_5m_close_inflight")
        self.assertTrue(summary["compute_in_progress"])
        self.assertTrue(summary["official_5m_close_in_progress"])
        self.assertEqual(summary["official_5m_close_age_s"], 45.0)
        self.assertEqual(summary["inflight_age_s"], 45.0)

    def test_scheduler_exact_lag_threshold_stays_running(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True},
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                },
                "compute": {
                    "status": "running",
                    "total_engines": 10,
                    "ready_engines": 10,
                    "compute_startup_preload": {"status": "completed", "running": False},
                },
                "service_topology": {"services": {}},
            },
            {
                "status": "running",
                "dispatch_lag_min": 10.0,
                "latest_ingested_bar_time_ms": 1713797100000,
                "loop_interval_seconds": 30,
                "job_count": 12,
            },
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        scheduler = service_monitor["services"]["ibkr-scheduler"]
        self.assertEqual(scheduler["status"], "running")
        self.assertIn("lag 10.00m", scheduler["detail"])

    def test_runtime_auth_recovery_reports_starting_instead_of_degraded(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {
                    "starting": True,
                    "startup_complete": False,
                    "runtime_phase": "running",
                    "gateway": {"running": True, "reachable": True, "managed_by": "systemd", "pid": 123},
                    "session": {"authenticated": False},
                    "websocket": {"connected": False, "ready": False},
                    "auth_recovery": {
                        "recovery_phase": "resume_waiting_manual",
                        "recovery_class": "manual_auth_required",
                        "probe_result": "pending",
                    },
                },
                "compute": {"status": "running", "total_engines": 1, "ready_engines": 1},
                "service_topology": {"services": {}},
            },
            {"status": "running", "loop_interval_seconds": 30, "jobs": {}},
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        runtime = service_monitor["services"]["ibkr-runtime"]
        self.assertEqual(runtime["status"], "starting")
        self.assertEqual(runtime["readiness_phase"], "auth_pending")
        self.assertFalse(runtime["ready"])

    def test_canonical_monitor_clears_ready_when_probe_marks_service_offline(self):
        service_monitor = derive_monitor_service_map(
            "live",
            {
                "status": "ok",
                "runtime": {},
                "compute": {},
                "service_topology": {
                    "services": {
                        "ibkr-console": {"status": "running", "ready": True},
                        "pocketbase": {"status": "running", "ready": True},
                    },
                },
            },
            {"status": "running", "loop_interval_seconds": 30, "jobs": {}},
            console_probe={"ok": False, "status_code": 0, "error": "connection refused"},
            pb_health={"ok": False, "status_code": 0, "error": "connection refused"},
            build_service_topology=lambda: {"services": {}},
        )

        console = service_monitor["services"]["ibkr-console"]
        pocketbase = service_monitor["services"]["pocketbase"]
        self.assertEqual(console["status"], "offline")
        self.assertFalse(console["ready"])
        self.assertEqual(pocketbase["status"], "offline")
        self.assertFalse(pocketbase["ready"])

    def test_monitor_source_unavailable_keeps_ibkr_link_unknown_not_offline(self):
        service_monitor = derive_monitor_service_map(
            "paper",
            {
                "ok": False,
                "status": "warning",
                "monitor_source_unavailable": True,
                "runtime": {},
                "compute": {},
                "service_topology": {"services": {}},
            },
            {"status": "running", "loop_interval_seconds": 30, "job_count": 12},
            console_probe={"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html"},
            pb_health={"ok": True, "status_code": 200},
            build_service_topology=lambda: {"services": {}},
        )

        services = service_monitor["services"]
        self.assertEqual(services["ibkr-compute"]["status"], "unknown")
        self.assertEqual(services["ibkr-runtime"]["status"], "unknown")
        self.assertEqual(services["ibkr-gateway"]["status"], "unknown")
        self.assertIn("monitor source unavailable", services["ibkr-compute"]["detail"])
        self.assertEqual(0, service_monitor["status_counts"].get("offline", 0))

    def test_monitor_payload_marks_empty_compute_monitor_timeout_as_source_unavailable(self):
        payload = build_system_monitor_payload(
            "paper",
            normalize_environment=lambda value, default="paper": str(value or default).strip().lower() or default,
            fetch_compute_monitor=lambda environment: {
                "ok": False,
                "status_code": 0,
                "payload": {},
                "error": "Read timed out",
                "target_url": "http://compute.internal:5100/ibkr/monitor",
                "elapsed_ms": 20001.0,
                "timeout_s": 20.0,
            },
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            config_refresh=lambda: None,
            scheduler_status=lambda environment: {"ok": True, "status": "running", "environment": environment, "jobs": {}},
            build_cron_payload=lambda config, environment, jobs: [],
            config=object(),
            build_scheduler_summary=lambda environment, scheduler_payload: {
                "status": "running",
                "loop_interval_seconds": 30,
                "job_count": 0,
            },
            augment_scheduler_summary=lambda summary, items: summary,
            request_json=lambda *args, **kwargs: {"ok": True, "status_code": 200, "payload": {}},
            pb_base_url="http://127.0.0.1:8090",
            console_base_url="https://quant.lzw-glory.top",
            probe_console_status=lambda *_args, **_kwargs: {
                "ok": True,
                "status_code": 200,
                "target_url": "https://quant.lzw-glory.top/index.html",
                "error": "",
            },
            load_effective_config_map=lambda *args, **kwargs: {},
            monitor_config_keys=("ibkr_target_refresh_sec",),
            load_recent_system_events=lambda *args, **kwargs: [],
            enrich_monitor_payload_with_pocketbase_disk=lambda payload: payload,
            derive_monitor_service_map=derive_monitor_service_map,
            merge_service_topology=lambda *payloads: {"services": {}},
            build_service_topology=lambda: {"services": {}},
            service_profile="api",
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "warning")
        self.assertTrue(payload["monitor_source_unavailable"])
        self.assertEqual(payload["upstream_monitor"]["elapsed_ms"], 20001.0)
        self.assertEqual(payload["upstream_monitor"]["timeout_s"], 20.0)
        compute = payload["service_monitor"]["services"]["ibkr-compute"]
        runtime = payload["service_monitor"]["services"]["ibkr-runtime"]
        gateway = payload["service_monitor"]["services"]["ibkr-gateway"]
        self.assertEqual(compute["status"], "unknown")
        self.assertEqual(runtime["status"], "unknown")
        self.assertEqual(gateway["status"], "unknown")
        errors = {item["stage"]: item for item in payload["monitor_builder_errors"]}
        self.assertEqual(errors["compute_monitor"]["severity"], "warning")

    def test_monitor_payload_accepts_zero_arg_console_probe_wrapper(self):
        payload = build_system_monitor_payload(
            "live",
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            fetch_compute_monitor=lambda environment: {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "status": "ok",
                    "environment": environment,
                    "flags": [],
                    "runtime": {},
                    "compute": {},
                    "api_utilization": {},
                    "samples": {},
                    "host": {},
                    "service_topology": {
                        "services": {
                            "ibkr-api": {"service_name": "ibkr-api", "status": "running"},
                            "ibkr-console": {"service_name": "ibkr-console", "status": "peer"},
                        }
                    },
                },
            },
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            config_refresh=lambda: None,
            scheduler_status=lambda environment: {"ok": True, "status": "running", "environment": environment, "jobs": {}},
            build_cron_payload=lambda config, environment, jobs: [],
            config=object(),
            build_scheduler_summary=lambda environment, scheduler_payload: {"status": "running", "job_count": 0},
            augment_scheduler_summary=lambda summary, items: summary,
            request_json=lambda *args, **kwargs: {"ok": True, "status_code": 200, "payload": {}},
            pb_base_url="http://127.0.0.1:8090",
            console_base_url="https://quant.lzw-glory.top",
            probe_console_status=lambda: {"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html", "error": ""},
            load_effective_config_map=lambda *args, **kwargs: {},
            monitor_config_keys=("ibkr_target_refresh_sec",),
            load_recent_system_events=lambda *args, **kwargs: [],
            enrich_monitor_payload_with_pocketbase_disk=lambda payload: payload,
            derive_monitor_service_map=lambda *args, **kwargs: {"environment": "live", "services": {}, "status_counts": {}},
            merge_service_topology=lambda *payloads: {
                "services": {
                    "ibkr-api": {"service_name": "ibkr-api", "status": "running"},
                    "ibkr-console": {"service_name": "ibkr-console", "status": "peer"},
                }
            },
            build_service_topology=lambda: {
                "services": {
                    "ibkr-api": {"service_name": "ibkr-api", "status": "running"},
                    "ibkr-console": {"service_name": "ibkr-console", "status": "peer"},
                }
            },
            service_profile="api",
        )

        self.assertNotIn("monitor_builder_errors", payload)
        self.assertTrue(payload["ok"])

    def test_monitor_payload_degrades_instead_of_raising(self):
        payload = build_system_monitor_payload(
            "live",
            normalize_environment=lambda value, default="live": str(value or default).strip().lower() or default,
            fetch_compute_monitor=lambda environment: {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "status": "ok",
                    "environment": environment,
                    "flags": [],
                    "runtime": {},
                    "compute": {},
                    "api_utilization": {},
                    "samples": {},
                    "host": {},
                    "service_topology": {
                        "services": {
                            "ibkr-api": {"service_name": "ibkr-api", "status": "running"},
                        }
                    },
                },
            },
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            config_refresh=lambda: _raise("config refresh failed"),
            scheduler_status=lambda environment: _raise("scheduler unavailable"),
            build_cron_payload=lambda config, environment, jobs: [],
            config=object(),
            build_scheduler_summary=lambda environment, scheduler_payload: {"status": "running", "job_count": 1},
            augment_scheduler_summary=lambda summary, items: summary,
            request_json=lambda *args, **kwargs: _raise("pb health failed"),
            pb_base_url="http://127.0.0.1:8090",
            console_base_url="https://quant.lzw-glory.top",
            probe_console_status=lambda *_args, **_kwargs: _raise("console probe failed"),
            load_effective_config_map=lambda *args, **kwargs: _raise("config map failed"),
            monitor_config_keys=("ibkr_target_refresh_sec",),
            load_recent_system_events=lambda *args, **kwargs: _raise("events failed"),
            enrich_monitor_payload_with_pocketbase_disk=lambda payload: _raise("disk failed"),
            derive_monitor_service_map=lambda *args, **kwargs: _raise("service map failed"),
            merge_service_topology=lambda *payloads: {"services": {"ibkr-api": {"service_name": "ibkr-api", "status": "running"}}},
            build_service_topology=lambda: {
                "services": {
                    "ibkr-api": {"service_name": "ibkr-api", "status": "running"},
                    "ibkr-runtime": {"service_name": "ibkr-runtime", "status": "peer"},
                }
            },
            service_profile="api",
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "warning")
        self.assertIn("monitor_builder_errors", payload)
        self.assertIn("service_monitor", payload)
        self.assertIn("ibkr-api", payload["service_monitor"]["services"])

        stages = {item["stage"] for item in payload["monitor_builder_errors"]}
        self.assertIn("config_refresh", stages)
        self.assertIn("scheduler_status", stages)
        self.assertIn("pocketbase_health", stages)
        self.assertIn("console_probe", stages)
        self.assertIn("config_map", stages)
        self.assertIn("recent_events", stages)
        self.assertIn("pocketbase_disk", stages)
        self.assertIn("service_monitor", stages)

        flag_codes = {item["code"] for item in payload["flags"]}
        self.assertIn("monitor_builder_config_refresh", flag_codes)
        self.assertIn("monitor_builder_service_monitor", flag_codes)


if __name__ == "__main__":
    unittest.main()
