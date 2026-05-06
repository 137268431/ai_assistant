from __future__ import annotations

__all__ = [
    "_build_compute_summary",
    "_build_empty_api_utilization_snapshot",
    "_build_empty_monitor_samples",
    "_build_gateway_action_payload",
    "_build_ibkr_monitor_snapshot",
    "_build_uninitialized_runtime_status",
]


def __getattr__(name: str):
    if name == "_build_gateway_action_payload":
        from ibkr_compute.api.monitor.runtime.actions import _build_gateway_action_payload

        return _build_gateway_action_payload
    if name == "_build_compute_summary":
        from ibkr_compute.api.monitor.runtime.compute import _build_compute_summary

        return _build_compute_summary
    if name == "_build_empty_api_utilization_snapshot":
        from ibkr_compute.api.monitor.runtime.empty import _build_empty_api_utilization_snapshot

        return _build_empty_api_utilization_snapshot
    if name == "_build_empty_monitor_samples":
        from ibkr_compute.api.monitor.runtime.empty import _build_empty_monitor_samples

        return _build_empty_monitor_samples
    if name == "_build_ibkr_monitor_snapshot":
        from ibkr_compute.api.monitor.runtime.snapshot import _build_ibkr_monitor_snapshot

        return _build_ibkr_monitor_snapshot
    if name == "_build_uninitialized_runtime_status":
        from ibkr_compute.api.monitor.runtime.uninitialized import _build_uninitialized_runtime_status

        return _build_uninitialized_runtime_status
    raise AttributeError(name)
