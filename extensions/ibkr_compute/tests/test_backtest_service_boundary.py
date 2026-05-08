import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
for src_root in (
    REPO_ROOT / "runtime" / "ibkr_api" / "src",
    REPO_ROOT / "runtime" / "ibkr_compute" / "src",
):
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

from ibkr_api.compat.bootstrap import build_compat_proxy_deps
from ibkr_compute.api import service_topology


COMPUTE_URL = "http://compute.internal:5100"
BACKTEST_URL = "http://backtest.internal:5105"
RUNTIME_URL = "http://runtime.internal:5101"
SCHEDULER_URL = "http://scheduler.internal:5103"
PB_URL = "http://pocketbase.internal:8090"


def _build_proxy_deps():
    return build_compat_proxy_deps(
        pb_base_url=PB_URL,
        compute_base_url=COMPUTE_URL,
        backtest_base_url=BACKTEST_URL,
        runtime_base_url=RUNTIME_URL,
        scheduler_base_url=SCHEDULER_URL,
        build_service_topology=lambda: {"services": {}},
        config=object(),
        normalize_environment=lambda value, default="live": value or default,
        scheduler_status=lambda environment: {"jobs": {}},
        scheduler_job_states=lambda environment="live": {},
        build_cron_payload=lambda config, environment, jobs: [],
        build_scheduler_summary=lambda environment, payload: {},
        augment_scheduler_summary=lambda summary, items: summary,
        forward_request=lambda base_url, path, **kwargs: (base_url, path, kwargs),
        proxy_custom_to_pb=lambda subpath: ("pb-custom", subpath),
        proxy_webhook_to_pb=lambda subpath: ("pb-webhook", subpath),
    )


class IbkrApiBacktestProxyBoundaryTest(unittest.TestCase):
    def test_backtest_custom_routes_use_backtest_service_url(self):
        deps = _build_proxy_deps()
        direct_proxy_map = deps["direct_proxy_map"]
        expected_routes = {
            ("GET", "ibkr/backtest/status"): "/backtest/status",
            ("GET", "ibkr/backtest/runs"): "/backtest/runs",
            ("GET", "ibkr/backtest/run"): "/backtest/run",
            ("GET", "ibkr/backtest/batches"): "/backtest/batches",
            ("GET", "ibkr/backtest/batch"): "/backtest/batch",
            ("POST", "ibkr/backtest/run"): "/backtest/run",
            ("POST", "ibkr/backtest/cancel"): "/backtest/cancel",
            ("GET", "ibkr/backtest/replay"): "/backtest/replay",
            ("POST", "ibkr/backtest/cleanup"): "/backtest/cleanup",
        }

        for route_key, upstream_path in expected_routes.items():
            with self.subTest(route=route_key):
                self.assertEqual((BACKTEST_URL, upstream_path), direct_proxy_map[route_key])

        for (method, subpath), (base_url, upstream_path) in direct_proxy_map.items():
            if not subpath.startswith("ibkr/backtest/"):
                continue
            with self.subTest(method=method, subpath=subpath):
                self.assertEqual(BACKTEST_URL, base_url)
                self.assertNotEqual(COMPUTE_URL, base_url)
                self.assertTrue(upstream_path.startswith("/backtest/"))

    def test_non_backtest_compute_routes_remain_on_compute_service_url(self):
        deps = _build_proxy_deps()
        direct_proxy_map = deps["direct_proxy_map"]
        action_proxy_map = deps["action_proxy_map"]

        compute_direct_routes = {
            ("GET", "ibkr/rules"): "/ibkr/rules",
            ("GET", "ibkr/screener"): "/screener",
            ("GET", "ibkr/history/rebuild/status"): "/ibkr/history/rebuild/status",
            ("POST", "ibkr/history/rebuild/start"): "/ibkr/history/rebuild/start",
            ("POST", "ibkr/data_quality/rescan"): "/ibkr/data-quality/scan",
            ("POST", "ibkr/data_quality/repair"): "/ibkr/data-quality/repair",
            ("GET", "ibkr/bar-repair/status"): "/ibkr/bar-repair/status",
        }
        for route_key, upstream_path in compute_direct_routes.items():
            with self.subTest(route=route_key):
                self.assertEqual((COMPUTE_URL, upstream_path), direct_proxy_map[route_key])

        compute_actions = {
            "compute": "/compute",
            "scan": "/scan",
            "recompute": "/recompute",
            "chart/timeline": "/chart/timeline",
            "chart/compare": "/chart/compare",
            "bar_repair/status": "/ibkr/bar-repair/status",
            "bar-repair/status": "/ibkr/bar-repair/status",
        }
        for action, upstream_path in compute_actions.items():
            with self.subTest(action=action):
                self.assertEqual((COMPUTE_URL, upstream_path), action_proxy_map[action])

        self.assertEqual(COMPUTE_URL, deps["compute_base_url"])
        self.assertEqual(BACKTEST_URL, deps["backtest_base_url"])


class IbkrBacktestTopologyBoundaryTest(unittest.TestCase):
    def test_compute_profile_topology_includes_backtest_peer_boundary(self):
        env = {
            "IBKR_SERVICE_PROFILE": "compute",
            "IBKR_RUNTIME_MODE": "remote",
            "IBKR_COMPUTE_INTERNAL_URL": f"{COMPUTE_URL}/",
            "IBKR_BACKTEST_INTERNAL_URL": f"{BACKTEST_URL}/",
            "IBKR_RUNTIME_INTERNAL_URL": f"{RUNTIME_URL}/",
            "IBKR_API_INTERNAL_URL": "http://api.internal:5102/",
            "IBKR_SCHEDULER_INTERNAL_URL": f"{SCHEDULER_URL}/",
            "PB_BASE_URL": f"{PB_URL}/",
        }
        runtime_status = {
            "service_profile": "runtime",
            "session": {"authenticated": True},
            "gateway": {"running": True, "managed_by": "ibkr-runtime", "pid": 42},
        }

        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(service_topology, "get_remote_runtime_status", side_effect=AssertionError("no live network")):
                payload = service_topology.build_service_topology(service_status=runtime_status)

        services = payload["services"]
        expected_core_services = {
            "pocketbase",
            "ibkr-console",
            "ibkr-api",
            "ibkr-scheduler",
            "ibkr-compute",
            "ibkr-backtest",
            "ibkr-runtime",
            "ibkr-gateway",
        }
        self.assertEqual(expected_core_services, set(services))
        self.assertEqual(8, len(services))

        backtest = services["ibkr-backtest"]
        self.assertEqual("compute", payload["service_profile"])
        self.assertEqual("remote", payload["runtime_mode"])
        self.assertTrue(payload["restart_independent"])
        self.assertEqual("peer", backtest["status"])
        self.assertEqual("backtest_plane", backtest["fault_domain"])
        self.assertTrue(backtest["restart_independent"])
        self.assertEqual(BACKTEST_URL, backtest["internal_url"])
        self.assertEqual("running", services["ibkr-compute"]["status"])

    def test_topology_exposes_backtest_as_independent_service(self):
        env = {
            "IBKR_SERVICE_PROFILE": "backtest",
            "IBKR_RUNTIME_MODE": "remote",
            "IBKR_COMPUTE_INTERNAL_URL": f"{COMPUTE_URL}/",
            "IBKR_BACKTEST_INTERNAL_URL": f"{BACKTEST_URL}/",
            "IBKR_RUNTIME_INTERNAL_URL": f"{RUNTIME_URL}/",
            "IBKR_API_INTERNAL_URL": "http://api.internal:5102/",
            "IBKR_SCHEDULER_INTERNAL_URL": f"{SCHEDULER_URL}/",
            "PB_BASE_URL": f"{PB_URL}/",
        }
        runtime_status = {
            "service_profile": "runtime",
            "session": {"authenticated": True},
            "gateway": {"running": True, "managed_by": "ibkr-runtime", "pid": 42},
        }

        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(service_topology, "get_remote_runtime_status", side_effect=AssertionError("no live network")):
                payload = service_topology.build_service_topology(service_status=runtime_status)

        services = payload["services"]
        self.assertIn("ibkr-backtest", services)
        backtest = services["ibkr-backtest"]
        compute = services["ibkr-compute"]

        self.assertEqual("backtest", payload["service_profile"])
        self.assertEqual("remote", payload["runtime_mode"])
        self.assertTrue(payload["restart_independent"])
        self.assertEqual("ibkr-backtest", backtest["service_name"])
        self.assertEqual("backtest_plane", backtest["kind"])
        self.assertEqual("backtest_plane", backtest["fault_domain"])
        self.assertEqual("ibkr-backtest", backtest["owner"])
        self.assertEqual("running", backtest["status"])
        self.assertEqual(BACKTEST_URL, backtest["internal_url"])
        self.assertEqual(PB_URL, backtest["upstream"])
        self.assertTrue(backtest["restart_independent"])

        self.assertEqual(COMPUTE_URL, compute["internal_url"])
        self.assertEqual("peer", compute["status"])
        self.assertNotEqual(compute["fault_domain"], backtest["fault_domain"])
        self.assertNotIn("backtest", str(compute.get("responsibility", "")).lower())


if __name__ == "__main__":
    unittest.main()
