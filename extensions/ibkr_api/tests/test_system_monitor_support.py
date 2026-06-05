import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.system.monitor_support import build_system_monitor_payload
from ibkr_api.system.monitor_support import build_tv_flow_monitor_summary
from ibkr_api.system.monitor_support import derive_monitor_service_map
from ibkr_api.system.scheduler_support import build_scheduler_summary
from ibkr_api.system.scheduler_support import scheduler_status
from ibkr_api.system.service_state import derive_compute_state
from ibkr_api.system.service_state import derive_runtime_state
from ibkr_api.system.service_state import canonicalize_topology


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


def _utc_minutes_ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _utc_ms(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp() * 1000)


class _FakePocketBase:
    def __init__(self, records):
        self.records = {collection: [dict(row) for row in rows] for collection, rows in records.items()}

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        rows = [dict(row) for row in self.records.get(collection, []) if self._matches(row, filter or "")]
        rows = self._sort(rows, sort or "")
        start = (int(page or 1) - 1) * int(per_page or 200)
        end = start + int(per_page or 200)
        return rows[start:end]

    def _matches(self, row, filter_expr):
        for raw_part in str(filter_expr or "").split("&&"):
            part = raw_part.strip()
            if not part:
                continue
            if " = " not in part:
                continue
            field, value = part.split(" = ", 1)
            field = field.strip()
            value = value.strip().strip('"')
            if str(row.get(field, "")) != value:
                return False
        return True

    def _sort(self, rows, sort_expr):
        fields = [field.strip() for field in str(sort_expr or "").split(",") if field.strip()]
        for field in reversed(fields):
            descending = field.startswith("-")
            name = field[1:] if descending else field
            rows.sort(key=lambda row: row.get(name) or "", reverse=descending)
        return rows


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

    def test_runtime_state_overrides_stale_session_authenticated_field(self):
        state = derive_runtime_state(
            {
                "runtime_phase": "running",
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            observed_at="2026-06-05T14:51:00+00:00",
        )

        self.assertEqual(state["status"], "running")
        self.assertTrue(state["ready"])
        self.assertTrue(state["session_authenticated"])
        self.assertTrue(state["gateway_reachable"])
        self.assertTrue(state["websocket_ready"])

    def test_canonical_topology_overrides_stale_runtime_session_field(self):
        topology, service_monitor = canonicalize_topology(
            "paper",
            {
                "services": {
                    "ibkr-runtime": {
                        "service_name": "ibkr-runtime",
                        "status": "peer",
                        "session_authenticated": False,
                    }
                }
            },
            {
                "runtime_phase": "running",
                "gateway": {"running": True, "reachable": True},
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
            },
            observed_at="2026-06-05T14:51:00+00:00",
        )

        self.assertTrue(service_monitor["services"]["ibkr-runtime"]["session_authenticated"])
        self.assertTrue(topology["services"]["ibkr-runtime"]["session_authenticated"])
        self.assertEqual(service_monitor["services"]["ibkr-runtime"]["status"], "running")

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

    def test_monitor_payload_suppresses_legacy_bar_warnings_when_tv_primary_disabled(self):
        payload = build_system_monitor_payload(
            "paper",
            normalize_environment=lambda value, default="paper": str(value or default).strip().lower() or default,
            fetch_compute_monitor=lambda environment: {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": False,
                    "status": "warning",
                    "environment": environment,
                    "flags": [
                        {
                            "code": "no_active_targets",
                            "severity": "warning",
                            "title": "No active trade targets",
                            "detail": "legacy runtime target count is zero",
                        },
                        {
                            "code": "stale_active_symbols",
                            "severity": "warning",
                            "title": "Stale active symbols",
                            "detail": "legacy bar sample stale",
                        },
                    ],
                    "runtime": {
                        "status": "running",
                        "runtime_phase": "running",
                        "gateway": {"running": True, "reachable": True},
                        "session": {"authenticated": True},
                        "websocket": {"connected": True, "ready": True},
                        "data_backfill": {
                            "bar_pipeline": {
                                "enabled": False,
                                "status": "disabled_tv_primary",
                                "reason": "legacy_bar_pipeline_disabled",
                            },
                            "active_requests": 4,
                            "active_symbols_total": 12,
                        },
                        "canonical_5m": {
                            "enabled": True,
                            "status": "stale",
                            "pending_symbols_total": 3,
                        },
                        "market_universe": {
                            "active_target_count": 10,
                            "execution_eligible_target_count": 10,
                            "no_active_targets": True,
                            "no_execution_eligible_targets": True,
                        },
                    },
                    "compute": {"status": "running", "total_engines": 1, "ready_engines": 1},
                    "service_topology": {"services": {}},
                },
            },
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            config_refresh=lambda: None,
            scheduler_status=lambda environment: {"ok": True, "status": "running", "environment": environment, "jobs": {}},
            build_cron_payload=lambda config, environment, jobs: [],
            config=object(),
            build_scheduler_summary=lambda environment, scheduler_payload: {
                "status": "running",
                "loop_interval_seconds": 30,
                "latest_ingested_bar_time_ms": 1713797100000,
                "dispatch_lag_min": 30.0,
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

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["bar_pipeline"]["status"], "disabled_tv_primary")
        self.assertEqual(payload["runtime"]["canonical_5m"]["status"], "disabled_tv_primary")
        self.assertEqual(payload["flags"], [])
        scheduler = payload["service_monitor"]["services"]["ibkr-scheduler"]
        self.assertEqual(scheduler["status"], "running")
        self.assertIn("bar pipeline disabled_tv_primary", scheduler["detail"])

    def test_tv_flow_summary_flags_event_driven_stuck_and_pending_actions(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [
                    {
                        "id": "evt_received_old",
                        "event_id": "tv:AAPL:entry:old",
                        "event_type": "entry",
                        "symbol": "AAPL",
                        "status": "received",
                        "environment": "live",
                        "created": _utc_minutes_ago(8),
                    },
                    {
                        "id": "evt_failed",
                        "event_id": "tv:MSFT:entry:failed",
                        "event_type": "entry",
                        "symbol": "MSFT",
                        "status": "failed",
                        "environment": "live",
                        "error_msg": "route exploded",
                        "created": _utc_minutes_ago(4),
                        "updated": _utc_minutes_ago(3),
                    },
                    {
                        "id": "evt_heartbeat_old",
                        "event_id": "tv:heartbeat:old",
                        "event_type": "heartbeat",
                        "symbol": "",
                        "status": "received",
                        "environment": "live",
                        "created": _utc_minutes_ago(60),
                    },
                ],
                "ibkr_signals": [
                    {
                        "id": "sig_tv_old",
                        "signal_id": "sig-tv-old",
                        "symbol": "NVDA",
                        "status": "pending",
                        "environment": "live",
                        "extra": {"source": "tradingview", "tv_event_id": "tv:NVDA:entry:old"},
                        "created": _utc_minutes_ago(25),
                    },
                    {
                        "id": "sig_compute_pending",
                        "signal_id": "sig-compute",
                        "symbol": "AMD",
                        "status": "pending",
                        "environment": "live",
                        "extra": {"source": "ibkr_compute"},
                        "created": _utc_minutes_ago(2),
                    },
                ],
                "ibkr_reverse_signals": [
                    {
                        "id": "rev_tv_old",
                        "symbol": "TSLA",
                        "status": "pending",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "created": _utc_minutes_ago(30),
                    },
                    {
                        "id": "rev_tv_failed",
                        "symbol": "META",
                        "status": "cancelled",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "reason": "execution action blocked: close_order_failed",
                        "created": _utc_minutes_ago(12),
                        "updated": _utc_minutes_ago(11),
                        "extra": {
                            "result_status": "reentry_blocked",
                            "blocked": True,
                            "flow_error_code": "tv_action_close_order_failed",
                            "reentry_blocked": {"reason": "close_order_failed"},
                        },
                    },
                    {
                        "id": "rev_tv_confirmed_no_reentry",
                        "symbol": "APH",
                        "status": "confirmed",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "reason": "tv_exit_confirmed_no_reentry",
                        "created": _utc_minutes_ago(6),
                        "updated": _utc_minutes_ago(5),
                        "extra": {
                            "result_status": "tv_exit_confirmed",
                            "auto_reentry_disabled": True,
                            "reentry_blocked": {"reason": "tv_primary_exit_requires_next_tv_entry"},
                        },
                    }
                ],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="paper",
            config_map={
                "tv_flow_received_stuck_warn_min": "2",
                "tv_flow_action_pending_stuck_warn_min": "15",
                "tv_flow_failed_lookback_min": "120",
            },
        )

        codes = {item["code"] for item in summary["flags"]}
        self.assertFalse(summary["heartbeat_required"])
        self.assertEqual(summary["mode"], "event_driven")
        self.assertIn("tv_flow_received_stuck", codes)
        self.assertIn("tv_flow_route_failed", codes)
        self.assertIn("tv_flow_tv_action_pending_stuck", codes)
        self.assertIn("tv_flow_non_tv_pending_actions", codes)
        self.assertIn("tv_flow_execution_action_failed", codes)
        self.assertEqual(summary["events"]["heartbeat_ignored_count"], 1)
        self.assertEqual(summary["events"]["received_stuck_count"], 1)
        self.assertEqual(summary["events"]["route_failed_count"], 1)
        self.assertEqual(summary["actions"]["tv_pending_stuck_count"], 2)
        self.assertEqual(summary["actions"]["tv_failed_count"], 1)
        self.assertEqual(summary["actions"]["non_tv_pending_count"], 1)
        self.assertEqual(summary["status"], "error")

    def test_tv_flow_summary_ignores_other_broker_pending_signals(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [],
                "ibkr_signals": [
                    {
                        "id": "sig_paper_rejected",
                        "signal_id": "sig-paper-rejected",
                        "symbol": "MRVL",
                        "status": "pending",
                        "environment": "live",
                        "extra": {
                            "source": "tradingview",
                            "broker_mode": "paper",
                            "last_runtime_broker_mode": "paper",
                            "execution_by_mode": {
                                "paper": {"status": "rejected", "note": "entry_guard_no_fresh_quote"}
                            },
                        },
                        "created": _utc_minutes_ago(30),
                    },
                    {
                        "id": "sig_paper_expired",
                        "signal_id": "sig-paper-expired",
                        "symbol": "NVO",
                        "status": "pending",
                        "environment": "live",
                        "extra": {
                            "source": "tradingview",
                            "broker_mode": "paper",
                            "last_runtime_broker_mode": "paper",
                            "execution_by_mode": {"paper": {"status": "expired", "note": "signal_expired"}},
                        },
                        "created": _utc_minutes_ago(28),
                    },
                    {
                        "id": "sig_live_pending",
                        "signal_id": "sig-live-pending",
                        "symbol": "AAPL",
                        "status": "pending",
                        "environment": "live",
                        "extra": {
                            "source": "tradingview",
                            "broker_mode": "live",
                            "execution_by_mode": {"live": {"status": "pending"}},
                        },
                        "created": _utc_minutes_ago(27),
                    },
                    {
                        "id": "sig_legacy_pending",
                        "signal_id": "sig-legacy-pending",
                        "symbol": "TSLA",
                        "status": "pending",
                        "environment": "live",
                        "extra": {"source": "tradingview", "tv_event_id": "tv:TSLA:entry:legacy"},
                        "created": _utc_minutes_ago(26),
                    },
                ],
                "ibkr_reverse_signals": [],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="live",
            config_map={"tv_flow_action_pending_stuck_warn_min": "15"},
        )

        codes = {item["code"] for item in summary["flags"]}
        signal_counts = summary["actions"]["signals"]
        self.assertIn("tv_flow_tv_action_pending_stuck", codes)
        self.assertEqual(summary["actions"]["tv_pending_stuck_count"], 2)
        self.assertEqual(signal_counts["raw_pending_count"], 4)
        self.assertEqual(signal_counts["pending_count"], 2)
        self.assertEqual(signal_counts["broker_scoped_ignored_count"], 2)
        self.assertEqual(signal_counts["broker_scoped_handled_count"], 0)

    def test_tv_flow_summary_ignores_current_broker_terminal_execution_status(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [],
                "ibkr_signals": [
                    {
                        "id": "sig_paper_rejected",
                        "signal_id": "sig-paper-rejected",
                        "symbol": "MRVL",
                        "status": "pending",
                        "environment": "live",
                        "extra": {
                            "source": "tradingview",
                            "broker_mode": "paper",
                            "last_runtime_broker_mode": "paper",
                            "execution_by_mode": {
                                "paper": {"status": "rejected", "note": "entry_guard_no_fresh_quote"}
                            },
                        },
                        "created": _utc_minutes_ago(30),
                    },
                    {
                        "id": "sig_paper_expired",
                        "signal_id": "sig-paper-expired",
                        "symbol": "NVO",
                        "status": "pending",
                        "environment": "live",
                        "extra": {
                            "source": "tradingview",
                            "broker_mode": "paper",
                            "last_runtime_broker_mode": "paper",
                            "execution_by_mode": {"paper": {"status": "expired", "note": "signal_expired"}},
                        },
                        "created": _utc_minutes_ago(28),
                    },
                ],
                "ibkr_reverse_signals": [],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="paper",
            config_map={"tv_flow_action_pending_stuck_warn_min": "15"},
        )

        signal_counts = summary["actions"]["signals"]
        self.assertEqual(summary["status"], "ok")
        self.assertNotIn("tv_flow_tv_action_pending_stuck", {item["code"] for item in summary["flags"]})
        self.assertEqual(summary["actions"]["tv_pending_count"], 0)
        self.assertEqual(summary["actions"]["tv_pending_stuck_count"], 0)
        self.assertEqual(signal_counts["raw_pending_count"], 2)
        self.assertEqual(signal_counts["pending_count"], 0)
        self.assertEqual(signal_counts["broker_scoped_ignored_count"], 0)
        self.assertEqual(signal_counts["broker_scoped_handled_count"], 2)

    def test_tv_flow_execution_failure_still_alerts_intraday_current_market_date(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [],
                "ibkr_signals": [],
                "ibkr_reverse_signals": [
                    {
                        "id": "rev_close_failed",
                        "symbol": "SMCI",
                        "status": "cancelled",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "reason": "reverse blocked: conid_unresolved",
                        "created": "2026-06-02 13:55:00Z",
                        "updated": "2026-06-02 13:56:00Z",
                        "extra": {
                            "result_status": "reentry_blocked",
                            "reentry_blocked": {"reason": "conid_unresolved"},
                        },
                    }
                ],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="paper",
            config_map={"tv_flow_failed_lookback_min": "120", "eod_close_time": "15:55"},
            now_ms=_utc_ms(2026, 6, 2, 14, 0),
        )

        self.assertEqual(summary["status"], "error")
        self.assertTrue(summary["action_monitor"]["active"])
        self.assertIn("tv_flow_execution_action_failed", {item["code"] for item in summary["flags"]})
        self.assertEqual(summary["actions"]["tv_failed_count"], 1)
        self.assertEqual(summary["actions"]["tv_failed_raw_count"], 1)
        self.assertEqual(summary["actions"]["tv_failed_suppressed_after_eod_count"], 0)

    def test_tv_flow_safe_preflight_invalidation_is_not_execution_failure(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [],
                "ibkr_signals": [],
                "ibkr_reverse_signals": [
                    {
                        "id": "rev_close_no_order",
                        "symbol": "AAPL",
                        "status": "expired",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "reason": "execution invalidated: real_filled_order_required_for_close",
                        "created": "2026-06-02 13:55:00Z",
                        "updated": "2026-06-02 13:56:00Z",
                        "extra": {
                            "result_status": "invalidated",
                            "invalidated_by": "real_order_preflight",
                            "gateway_request_blocked": True,
                        },
                    }
                ],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="paper",
            config_map={"tv_flow_failed_lookback_min": "120", "eod_close_time": "15:55"},
            now_ms=_utc_ms(2026, 6, 2, 14, 0),
        )

        self.assertEqual(summary["status"], "ok")
        self.assertNotIn("tv_flow_execution_action_failed", {item["code"] for item in summary["flags"]})
        self.assertEqual(summary["actions"]["tv_failed_count"], 0)
        self.assertEqual(summary["actions"]["tv_failed_raw_count"], 0)

    def test_tv_flow_async_reconcile_states_are_not_execution_failures(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [],
                "ibkr_signals": [],
                "ibkr_reverse_signals": [
                    {
                        "id": "rev_retryable_missing_child",
                        "symbol": "AAPL",
                        "status": "cancelled",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "adjust_bracket",
                        "reason": "reverse pending retry: missing_child_order",
                        "created": "2026-06-02 13:50:00Z",
                        "updated": "2026-06-02 13:51:00Z",
                        "extra": {
                            "result_status": "pending_retry",
                            "blocked": True,
                            "reentry_blocked": {"reason": "missing_child_order", "retryable": True},
                        },
                    },
                    {
                        "id": "rev_wait_confirm",
                        "symbol": "MSFT",
                        "status": "cancelled",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "reason": "execution action blocked: waiting for order confirmation",
                        "created": "2026-06-02 13:49:00Z",
                        "updated": "2026-06-02 13:50:00Z",
                        "extra": {
                            "result_status": "blocked",
                            "execution_state": "submitted_wait_confirm",
                            "flow_error_code": "order_confirmation_pending",
                        },
                    },
                    {
                        "id": "rev_deferred",
                        "symbol": "NVDA",
                        "status": "expired",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "reason": "execution action blocked: deferred until broker state settles",
                        "created": "2026-06-02 13:48:00Z",
                        "updated": "2026-06-02 13:49:00Z",
                        "extra": {
                            "execution_state": "deferred",
                            "reverse_runtime_detail": {"blocked_reason": "broker_state_deferred"},
                        },
                    },
                    {
                        "id": "rev_pending_executable",
                        "symbol": "TSLA",
                        "status": "cancelled",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "reason": "execution action blocked: pending executable command",
                        "created": "2026-06-02 13:47:00Z",
                        "updated": "2026-06-02 13:48:00Z",
                        "extra": {
                            "blocked": True,
                            "execution_state": "pending_executable",
                            "reentry_blocked": {"reason": "pending_executable"},
                        },
                    },
                    {
                        "id": "rev_real_blocked",
                        "symbol": "META",
                        "status": "cancelled",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "reason": "execution action blocked: conid_unresolved",
                        "created": "2026-06-02 13:46:00Z",
                        "updated": "2026-06-02 13:47:00Z",
                        "extra": {
                            "result_status": "blocked",
                            "reverse_runtime_detail": {"blocked_reason": "conid_unresolved"},
                        },
                    },
                ],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="paper",
            config_map={"tv_flow_failed_lookback_min": "120", "eod_close_time": "15:55"},
            now_ms=_utc_ms(2026, 6, 2, 14, 0),
        )

        self.assertEqual(summary["status"], "error")
        self.assertIn("tv_flow_execution_action_failed", {item["code"] for item in summary["flags"]})
        self.assertEqual(summary["actions"]["reverse"]["processed_recent_count"], 5)
        self.assertEqual(summary["actions"]["tv_failed_count"], 1)
        self.assertEqual(summary["actions"]["tv_failed_raw_count"], 1)
        self.assertEqual(summary["actions"]["tv_failed"][0]["id"], "rev_real_blocked")

    def test_tv_flow_execution_failure_is_suppressed_after_eod(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [],
                "ibkr_signals": [],
                "ibkr_reverse_signals": [
                    {
                        "id": "rev_close_failed",
                        "symbol": "SMCI",
                        "status": "cancelled",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "reason": "reverse blocked: conid_unresolved",
                        "created": "2026-06-02 19:55:00Z",
                        "updated": "2026-06-02 19:56:00Z",
                        "extra": {
                            "result_status": "reentry_blocked",
                            "reentry_blocked": {"reason": "conid_unresolved"},
                        },
                    }
                ],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="paper",
            config_map={"tv_flow_failed_lookback_min": "120", "eod_close_time": "15:55"},
            now_ms=_utc_ms(2026, 6, 2, 20, 5),
        )

        self.assertEqual(summary["status"], "ok")
        self.assertFalse(summary["action_monitor"]["active"])
        self.assertNotIn("tv_flow_execution_action_failed", {item["code"] for item in summary["flags"]})
        self.assertEqual(summary["actions"]["tv_failed_count"], 0)
        self.assertEqual(summary["actions"]["tv_failed_raw_count"], 1)
        self.assertEqual(summary["actions"]["tv_failed_suppressed_after_eod_count"], 1)

    def test_tv_flow_previous_market_date_failures_are_suppressed_before_eod(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [],
                "ibkr_signals": [],
                "ibkr_reverse_signals": [
                    {
                        "id": "rev_close_failed_old",
                        "symbol": "SMCI",
                        "status": "cancelled",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "reason": "reverse blocked: conid_unresolved",
                        "created": "2026-06-01 19:55:00Z",
                        "updated": "2026-06-01 19:56:00Z",
                        "extra": {
                            "result_status": "reentry_blocked",
                            "reentry_blocked": {"reason": "conid_unresolved"},
                        },
                    }
                ],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="paper",
            config_map={"tv_flow_failed_lookback_min": "1440", "eod_close_time": "15:55"},
            now_ms=_utc_ms(2026, 6, 2, 13, 0),
        )

        self.assertEqual(summary["status"], "ok")
        self.assertTrue(summary["action_monitor"]["active"])
        self.assertEqual(summary["actions"]["tv_failed_count"], 0)
        self.assertEqual(summary["actions"]["tv_failed_raw_count"], 1)
        self.assertEqual(summary["actions"]["tv_failed_suppressed_cross_day_count"], 1)

    def test_tv_flow_previous_market_date_failure_cleanup_update_is_suppressed(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [],
                "ibkr_signals": [],
                "ibkr_reverse_signals": [
                    {
                        "id": "rev_cleanup_updated_today",
                        "symbol": "ET",
                        "status": "cancelled",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "adjust_bracket",
                        "reason": "reverse blocked: risk_update_child_order_id_unresolved",
                        "created": "2026-06-02 13:48:00Z",
                        "updated": "2026-06-03 04:06:00Z",
                        "extra": {
                            "result_status": "blocked",
                            "reverse_runtime_detail": {"blocked_reason": "risk_update_child_order_id_unresolved"},
                        },
                    }
                ],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="paper",
            config_map={"tv_flow_failed_lookback_min": "1440", "eod_close_time": "15:55"},
            now_ms=_utc_ms(2026, 6, 3, 13, 1),
        )

        self.assertEqual(summary["status"], "ok")
        self.assertTrue(summary["action_monitor"]["active"])
        self.assertEqual(summary["actions"]["tv_failed_count"], 0)
        self.assertEqual(summary["actions"]["tv_failed_raw_count"], 1)
        self.assertEqual(summary["actions"]["tv_failed_suppressed_cross_day_count"], 1)

    def test_tv_flow_pending_actions_are_suppressed_after_eod(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [],
                "ibkr_signals": [],
                "ibkr_reverse_signals": [
                    {
                        "id": "rev_pending",
                        "symbol": "SMCI",
                        "status": "pending",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "created": "2026-06-02 19:40:00Z",
                        "updated": "2026-06-02 19:40:00Z",
                    }
                ],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="paper",
            config_map={"tv_flow_action_pending_stuck_warn_min": "15", "eod_close_time": "15:55"},
            now_ms=_utc_ms(2026, 6, 2, 20, 10),
        )

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["actions"]["tv_pending_count"], 0)
        self.assertEqual(summary["actions"]["tv_pending_raw_count"], 1)
        self.assertEqual(summary["actions"]["tv_pending_suppressed_after_eod_count"], 1)
        self.assertNotIn("tv_flow_tv_action_pending_stuck", {item["code"] for item in summary["flags"]})

    def test_tv_flow_pending_warn_uses_tv_command_reconcile_threshold(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [],
                "ibkr_signals": [],
                "ibkr_reverse_signals": [
                    {
                        "id": "rev_pending",
                        "symbol": "SMCI",
                        "status": "pending",
                        "source": "tradingview",
                        "environment": "paper",
                        "action_type": "close",
                        "created": "2026-06-02 13:40:00Z",
                        "updated": "2026-06-02 13:40:00Z",
                    }
                ],
            }
        )

        summary = build_tv_flow_monitor_summary(
            pb,
            data_environment="live",
            runtime_environment="paper",
            config_map={"tv_command_reconcile_pending_warn_min": "30", "eod_close_time": "15:55"},
            now_ms=_utc_ms(2026, 6, 2, 14, 0),
        )

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["thresholds"]["action_pending_stuck_warn_min"], 30)
        self.assertEqual(summary["actions"]["tv_pending_count"], 1)
        self.assertEqual(summary["actions"]["tv_pending_stuck_count"], 0)
        self.assertNotIn("tv_flow_tv_action_pending_stuck", {item["code"] for item in summary["flags"]})

    def test_monitor_payload_merges_tv_flow_flags_from_pb_client_alias(self):
        pb = _FakePocketBase(
            {
                "tv_webhook_events": [
                    {
                        "id": "evt_received_old",
                        "event_id": "tv:AAPL:entry:old",
                        "event_type": "entry",
                        "symbol": "AAPL",
                        "status": "received",
                        "environment": "live",
                        "created": _utc_minutes_ago(10),
                    }
                ],
                "ibkr_signals": [],
                "ibkr_reverse_signals": [],
            }
        )
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
                    "runtime": {
                        "status": "running",
                        "runtime_phase": "running",
                        "gateway": {"running": True, "reachable": True},
                        "session": {"authenticated": True},
                        "websocket": {"connected": True, "ready": True},
                    },
                    "compute": {"status": "running", "total_engines": 1, "ready_engines": 1},
                    "service_topology": {"services": {}},
                },
            },
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            config_refresh=lambda: None,
            scheduler_status=lambda environment: {"ok": True, "status": "running", "environment": environment, "jobs": {}},
            build_cron_payload=lambda config, environment, jobs: [],
            config=object(),
            build_scheduler_summary=lambda environment, scheduler_payload: {"status": "running", "loop_interval_seconds": 30, "job_count": 0},
            augment_scheduler_summary=lambda summary, items: summary,
            request_json=lambda *args, **kwargs: {"ok": True, "status_code": 200, "payload": {}},
            pb_base_url="http://127.0.0.1:8090",
            console_base_url="https://quant.lzw-glory.top",
            probe_console_status=lambda *_args, **_kwargs: {"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html", "error": ""},
            load_effective_config_map=lambda *args, **kwargs: {"tv_flow_received_stuck_warn_min": "2"},
            monitor_config_keys=("ibkr_target_refresh_sec",),
            load_recent_system_events=lambda *args, **kwargs: [],
            enrich_monitor_payload_with_pocketbase_disk=lambda payload: payload,
            derive_monitor_service_map=derive_monitor_service_map,
            merge_service_topology=lambda *payloads: {"services": {}},
            build_service_topology=lambda: {"services": {}},
            pb_client=pb,
            service_profile="api",
        )

        self.assertEqual(payload["tv_flow"]["events"]["received_stuck_count"], 1)
        self.assertIn("tv_flow_received_stuck", {item["code"] for item in payload["flags"]})
        self.assertEqual(payload["status"], "warning")

    def test_monitor_payload_flags_slow_account_snapshot(self):
        payload = build_system_monitor_payload(
            "paper",
            normalize_environment=lambda value, default="paper": str(value or default).strip().lower() or default,
            fetch_compute_monitor=lambda environment: {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "status": "ok",
                    "environment": environment,
                    "flags": [],
                    "runtime": {
                        "status": "running",
                        "runtime_phase": "running",
                        "gateway": {"running": True, "reachable": True},
                        "session": {"authenticated": True},
                        "websocket": {"connected": True, "ready": True},
                    },
                    "compute": {"status": "running", "total_engines": 1, "ready_engines": 1},
                    "service_topology": {"services": {}},
                },
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
            account_snapshot_probe=lambda environment: {
                "ok": True,
                "status_code": 200,
                "elapsed_ms": 13000.0,
                "payload": {
                    "ok": True,
                    "service_running": True,
                    "gateway_running": True,
                    "session_authenticated": True,
                    "errors": {},
                },
            },
            service_profile="api",
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "warning")
        flag_codes = {item["code"] for item in payload["flags"]}
        self.assertIn("account_snapshot_timeout", flag_codes)

    def test_monitor_payload_flags_account_runtime_unavailable(self):
        payload = build_system_monitor_payload(
            "paper",
            normalize_environment=lambda value, default="paper": str(value or default).strip().lower() or default,
            fetch_compute_monitor=lambda environment: {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "status": "ok",
                    "environment": environment,
                    "flags": [],
                    "runtime": {},
                    "compute": {"status": "running", "total_engines": 1, "ready_engines": 1},
                    "service_topology": {"services": {}},
                },
            },
            as_dict=lambda value: dict(value) if isinstance(value, dict) else {},
            config_refresh=lambda: None,
            scheduler_status=lambda environment: {"ok": True, "status": "running", "environment": environment, "jobs": {}},
            build_cron_payload=lambda config, environment, jobs: [],
            config=object(),
            build_scheduler_summary=lambda environment, scheduler_payload: {"status": "running", "loop_interval_seconds": 30, "job_count": 0},
            augment_scheduler_summary=lambda summary, items: summary,
            request_json=lambda *args, **kwargs: {"ok": True, "status_code": 200, "payload": {}},
            pb_base_url="http://127.0.0.1:8090",
            console_base_url="https://quant.lzw-glory.top",
            probe_console_status=lambda *_args, **_kwargs: {"ok": True, "status_code": 200, "target_url": "https://quant.lzw-glory.top/index.html", "error": ""},
            load_effective_config_map=lambda *args, **kwargs: {},
            monitor_config_keys=("ibkr_target_refresh_sec",),
            load_recent_system_events=lambda *args, **kwargs: [],
            enrich_monitor_payload_with_pocketbase_disk=lambda payload: payload,
            derive_monitor_service_map=derive_monitor_service_map,
            merge_service_topology=lambda *payloads: {"services": {}},
            build_service_topology=lambda: {"services": {}},
            account_snapshot_probe=lambda environment: {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "service_running": False,
                    "gateway_running": False,
                    "session_authenticated": False,
                    "errors": {},
                },
            },
            service_profile="api",
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "error")
        flag_codes = {item["code"] for item in payload["flags"]}
        self.assertIn("account_runtime_unavailable", flag_codes)

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
