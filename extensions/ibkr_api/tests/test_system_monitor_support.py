import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
if str(SERVICE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_SRC_ROOT))

from ibkr_api.system.monitor_support import build_system_monitor_payload
from ibkr_api.system.monitor_support import derive_monitor_service_map


def _raise(message):
    raise RuntimeError(message)


class SystemMonitorSupportTest(unittest.TestCase):
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
