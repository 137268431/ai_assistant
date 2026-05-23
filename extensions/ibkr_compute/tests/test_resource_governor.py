import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.core.host_resources import BYTES_PER_GB, build_cpu_usage_snapshot, build_resource_governor_snapshot


def _host_snapshot(
    *,
    cpu_5m_pct=30.0,
    iowait_5m_pct=2.0,
    load5=1.0,
    mem_available_gb=4.0,
    mem_total_gb=8.0,
    disk_free_gb=100.0,
    disk_total_gb=200.0,
    sample_age_s=1.0,
):
    mem_available_bytes = int(mem_available_gb * BYTES_PER_GB)
    mem_total_bytes = int(mem_total_gb * BYTES_PER_GB)
    disk_free_bytes = int(disk_free_gb * BYTES_PER_GB)
    disk_total_bytes = int(disk_total_gb * BYTES_PER_GB)
    return {
        "ok": True,
        "sample_age_s": sample_age_s,
        "cpu_count": 4,
        "cpu": {
            "used_pct_5m": cpu_5m_pct,
            "used_pct_1m": cpu_5m_pct,
            "used_pct": cpu_5m_pct,
            "iowait_pct_5m": iowait_5m_pct,
            "iowait_pct_1m": iowait_5m_pct,
            "iowait_pct": iowait_5m_pct,
        },
        "loadavg": {"1": load5, "5": load5, "15": load5, "per_cpu_5": load5 / 4.0},
        "memory": {
            "total_bytes": mem_total_bytes,
            "available_bytes": mem_available_bytes,
            "available_pct": round((mem_available_bytes / mem_total_bytes) * 100.0, 2),
        },
        "disk": {
            "total_bytes": disk_total_bytes,
            "free_bytes": disk_free_bytes,
            "free_pct": round((disk_free_bytes / disk_total_bytes) * 100.0, 2),
        },
    }


def _reason_codes(snapshot):
    return {reason["code"] for reason in snapshot["reasons"]}


def _watchlist_blocker_codes(snapshot):
    return {reason["code"] for reason in snapshot["admission"]["watchlist_idle_topup"]["blockers"]}


class ResourceGovernorTests(unittest.TestCase):
    def test_cpu_usage_snapshot_calculates_used_and_iowait_percentages(self):
        previous = {"total": 1000, "idle": 600, "iowait": 20, "sampled_at": 100.0}
        current = {"total": 1100, "idle": 665, "iowait": 25, "sampled_at": 102.5}

        snapshot = build_cpu_usage_snapshot(previous, current, source="test")

        self.assertEqual(snapshot["used_pct"], 35.0)
        self.assertEqual(snapshot["idle_pct"], 65.0)
        self.assertEqual(snapshot["iowait_pct"], 5.0)
        self.assertEqual(snapshot["sample_span_s"], 2.5)
        self.assertEqual(snapshot["source"], "test")

    def test_green_snapshot_admits_watchlist_idle_topup(self):
        snapshot = build_resource_governor_snapshot(_host_snapshot())

        self.assertEqual(snapshot["status"], "green")
        self.assertEqual(snapshot["shedding_mode"], "green")
        self.assertEqual(snapshot["health"], "ok")
        self.assertEqual(snapshot["reasons"], [])
        self.assertTrue(snapshot["lane_admission"]["critical"]["admit"])
        self.assertTrue(snapshot["lane_admission"]["realtime"]["admit"])
        self.assertTrue(snapshot["lane_admission"]["opportunistic"]["admit"])
        self.assertTrue(snapshot["lane_admission"]["batch"]["admit"])
        self.assertEqual(snapshot["recommended_limits"]["watchlist_idle_topup"]["mode"], "green")
        self.assertTrue(snapshot["admission"]["watchlist_idle_topup"]["admit"])
        self.assertEqual(snapshot["admission"]["watchlist_idle_topup"]["blockers"], [])
        self.assertTrue(snapshot["admission"]["non_priority"]["admit"])

    def test_warning_classification_keeps_non_priority_admitted(self):
        snapshot = build_resource_governor_snapshot(_host_snapshot(cpu_5m_pct=72.0))

        self.assertEqual(snapshot["status"], "warning")
        self.assertEqual(snapshot["shedding_mode"], "warning")
        self.assertEqual(snapshot["health"], "degraded")
        self.assertIn("cpu_warning", _reason_codes(snapshot))
        self.assertTrue(snapshot["admission"]["non_priority"]["admit"])
        self.assertFalse(snapshot["admission"]["watchlist_idle_topup"]["admit"])
        self.assertTrue(snapshot["lane_admission"]["realtime"]["admit"])
        self.assertFalse(snapshot["lane_admission"]["opportunistic"]["admit"])
        self.assertEqual(snapshot["recommended_limits"]["watchlist_idle_topup"]["max_symbols_per_cycle"], 40)
        self.assertEqual(snapshot["recommended_limits"]["watchlist_idle_topup"]["history_concurrency"], 6)

    def test_cpu_shedding_mode_preserves_realtime_and_defers_lower_lanes(self):
        snapshot = build_resource_governor_snapshot(_host_snapshot(cpu_5m_pct=80.0))

        self.assertEqual(snapshot["status"], "warning")
        self.assertEqual(snapshot["shedding_mode"], "shedding")
        self.assertIn("cpu_shedding", _reason_codes(snapshot))
        self.assertTrue(snapshot["lane_admission"]["critical"]["admit"])
        self.assertTrue(snapshot["lane_admission"]["realtime"]["admit"])
        self.assertFalse(snapshot["lane_admission"]["opportunistic"]["admit"])
        self.assertFalse(snapshot["lane_admission"]["batch"]["admit"])
        self.assertEqual(snapshot["deferred_lanes"], ["opportunistic", "batch"])
        limits = snapshot["recommended_limits"]["watchlist_idle_topup"]
        self.assertFalse(limits["enabled"])
        self.assertEqual(limits["max_symbols_per_cycle"], 8)
        self.assertEqual(limits["history_concurrency"], 2)
        self.assertEqual(limits["request_spacing_s"], 0.3)

    def test_critical_classification_blocks_non_priority(self):
        snapshot = build_resource_governor_snapshot(_host_snapshot(cpu_5m_pct=86.0))

        self.assertEqual(snapshot["status"], "critical")
        self.assertEqual(snapshot["shedding_mode"], "critical")
        self.assertEqual(snapshot["health"], "unhealthy")
        self.assertIn("cpu_critical", _reason_codes(snapshot))
        self.assertTrue(snapshot["lane_admission"]["critical"]["admit"])
        self.assertFalse(snapshot["lane_admission"]["realtime"]["admit"])
        self.assertFalse(snapshot["lane_admission"]["opportunistic"]["admit"])
        self.assertFalse(snapshot["lane_admission"]["batch"]["admit"])
        self.assertFalse(snapshot["admission"]["non_priority"]["admit"])
        self.assertIn("cpu_critical", {reason["code"] for reason in snapshot["admission"]["non_priority"]["blockers"]})

    def test_watchlist_blockers_use_4_core_8_gb_thresholds(self):
        snapshot = build_resource_governor_snapshot(
            _host_snapshot(
                cpu_5m_pct=60.0,
                iowait_5m_pct=8.0,
                load5=2.8,
                mem_available_gb=2.0,
                mem_total_gb=8.0,
                disk_free_gb=15.0,
                disk_total_gb=75.0,
            )
        )

        self.assertEqual(snapshot["status"], "green")
        self.assertFalse(snapshot["admission"]["watchlist_idle_topup"]["admit"])
        self.assertEqual(
            _watchlist_blocker_codes(snapshot),
            {
                "watchlist_cpu_over_limit",
                "watchlist_iowait_over_limit",
                "watchlist_load5_over_limit",
                "watchlist_memory_mb_below_limit",
                "watchlist_memory_pct_below_limit",
                "watchlist_disk_gb_below_limit",
                "watchlist_disk_pct_below_limit",
            },
        )
        self.assertEqual(snapshot["metrics"]["mem_available_mb"], 2048.0)
        self.assertEqual(snapshot["metrics"]["mem_available_pct"], 25.0)
        self.assertEqual(snapshot["metrics"]["disk_free_gb"], 15.0)
        self.assertEqual(snapshot["metrics"]["disk_free_pct"], 20.0)

    def test_watchlist_disk_gb_limit_does_not_block_when_free_percent_is_healthy(self):
        snapshot = build_resource_governor_snapshot(
            _host_snapshot(
                disk_free_gb=12.0,
                disk_total_gb=49.0,
            )
        )

        self.assertEqual(snapshot["status"], "green")
        self.assertTrue(snapshot["admission"]["watchlist_idle_topup"]["admit"])
        self.assertNotIn("watchlist_disk_gb_below_limit", _watchlist_blocker_codes(snapshot))
        self.assertEqual(snapshot["metrics"]["disk_free_gb"], 12.0)
        self.assertEqual(snapshot["metrics"]["disk_free_pct"], 24.49)


if __name__ == "__main__":
    unittest.main()
