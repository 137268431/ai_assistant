from __future__ import annotations


MONITOR_ALIASES = {
    "ibkr_compute.api.monitor_host": "ibkr_compute.api.monitor.host",
    "ibkr_compute.api.monitor_runtime": "ibkr_compute.api.monitor.views",
    "ibkr_compute.api.monitor_runtime_flags": "ibkr_compute.api.monitor.flags",
    "ibkr_compute.api.monitor_runtime_samples": "ibkr_compute.api.monitor.samples",
    "ibkr_compute.api.monitor_runtime_snapshot": "ibkr_compute.api.monitor.snapshot",
    "ibkr_compute.api.monitor_views": "ibkr_compute.api.monitor.views",
}


__all__ = ["MONITOR_ALIASES"]
