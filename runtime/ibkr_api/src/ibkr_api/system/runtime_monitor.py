from __future__ import annotations

import os
from typing import Any, Callable


def build_extract_cursor_interval(*, support: Callable[..., dict[str, Any]]):
    def _extract_cursor_interval(cursor_payload: dict[str, Any], interval: str = "5m") -> dict[str, Any]:
        return support(cursor_payload, interval)

    return _extract_cursor_interval


def build_scheduler_summary(*, support: Callable[..., dict[str, Any]]):
    def _build_scheduler_summary(environment: str, scheduler_payload: dict[str, Any]) -> dict[str, Any]:
        return support(environment, scheduler_payload)

    return _build_scheduler_summary


def build_scheduler_status(
    *,
    globals_dict: dict[str, Any],
    scheduler_base_url: str,
    support: Callable[..., dict[str, Any]],
):
    def _scheduler_status(environment: str = "live") -> dict[str, Any]:
        return support(
            environment,
            request_json=globals_dict["_request_json"],
            scheduler_base_url=scheduler_base_url,
        )

    return _scheduler_status


def build_scheduler_job_states(*, globals_dict: dict[str, Any], support: Callable[..., dict[str, Any]]):
    def _scheduler_job_states(environment: str = "live") -> dict[str, Any]:
        return support(environment, scheduler_status_fn=globals_dict["_scheduler_status"])

    return _scheduler_job_states


def build_augment_scheduler_summary(*, support: Callable[..., dict[str, Any]]):
    def _augment_scheduler_summary(summary: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
        return support(summary, items)

    return _augment_scheduler_summary


def build_system_summary_payload(*, globals_dict: dict[str, Any], support: Callable[..., dict[str, Any]]):
    def _build_system_summary_payload(environment: str, *, lite_mode: bool) -> dict[str, Any]:
        return support(
            environment,
            lite_mode=lite_mode,
            normalize_environment=globals_dict["_normalize_environment"],
            load_effective_config_map=globals_dict["_load_effective_config_map"],
            is_enabled_text=globals_dict["_is_enabled_text"],
            fetch_compute_health=globals_dict["_fetch_compute_health"],
            fetch_compute_status=globals_dict["_fetch_compute_status"],
            fetch_runtime_status=globals_dict["_fetch_runtime_status"],
            as_dict=globals_dict["_as_dict"],
            merge_service_topology=globals_dict["_merge_service_topology"],
            load_recent_system_events=globals_dict["_load_recent_system_events"],
            time_strings=globals_dict["_time_strings"],
            load_today_counts=globals_dict["_load_today_counts"],
            collect_storage_health=globals_dict.get("_collect_storage_health"),
        )

    return _build_system_summary_payload


def build_probe_console_status(*, globals_dict: dict[str, Any], support: Callable[..., dict[str, Any]]):
    def _probe_console_status() -> dict[str, Any]:
        return support(globals_dict["_console_base_url"]())

    return _probe_console_status


def build_derive_monitor_service_map(*, globals_dict: dict[str, Any], support: Callable[..., dict[str, Any]]):
    def _derive_monitor_service_map(
        environment: str,
        base_payload: dict[str, Any],
        scheduler_summary: dict[str, Any],
        *,
        console_probe: dict[str, Any],
        pb_health: dict[str, Any],
        backtest_health: dict[str, Any] | None = None,
        build_service_topology_fn=None,
        build_service_topology: Any = None,
    ) -> dict[str, Any]:
        topology_builder = build_service_topology_fn or build_service_topology or globals_dict.get("build_service_topology")
        return support(
            environment,
            base_payload,
            scheduler_summary,
            console_probe=console_probe,
            pb_health=pb_health,
            backtest_health=backtest_health,
            build_service_topology=topology_builder,
        )

    return _derive_monitor_service_map


def build_system_monitor_payload(
    *,
    globals_dict: dict[str, Any],
    config: Any,
    build_cron_payload: Callable[..., list[dict[str, Any]]],
    build_service_topology: Callable[[], dict[str, Any]],
    pb_base_url: str,
    monitor_config_keys: tuple[str, ...],
    support: Callable[..., dict[str, Any]],
):
    def _build_system_monitor_payload(environment: str) -> dict[str, Any]:
        return support(
            environment,
            normalize_environment=globals_dict["_normalize_environment"],
            fetch_compute_monitor=globals_dict["_fetch_compute_monitor"],
            as_dict=globals_dict["_as_dict"],
            config_refresh=config.refresh,
            scheduler_status=globals_dict["_scheduler_status"],
            build_cron_payload=build_cron_payload,
            config=config,
            build_scheduler_summary=globals_dict["_build_scheduler_summary"],
            augment_scheduler_summary=globals_dict["_augment_scheduler_summary"],
            request_json=globals_dict["_request_json"],
            pb_base_url=pb_base_url,
            console_base_url=globals_dict["_console_base_url"](),
            backtest_base_url=str(globals_dict.get("BACKTEST_BASE_URL") or "http://127.0.0.1:5105").rstrip("/"),
            probe_console_status=globals_dict["_probe_console_status"],
            load_effective_config_map=globals_dict["_load_effective_config_map"],
            monitor_config_keys=monitor_config_keys,
            load_recent_system_events=globals_dict["_load_recent_system_events"],
            enrich_monitor_payload_with_pocketbase_disk=globals_dict["_enrich_monitor_payload_with_pocketbase_disk"],
            derive_monitor_service_map=globals_dict["_derive_monitor_service_map"],
            merge_service_topology=globals_dict["_merge_service_topology"],
            build_service_topology=build_service_topology,
            service_profile=str(os.environ.get("IBKR_SERVICE_PROFILE") or "api"),
        )

    return _build_system_monitor_payload


__all__ = [
    "build_augment_scheduler_summary",
    "build_derive_monitor_service_map",
    "build_extract_cursor_interval",
    "build_probe_console_status",
    "build_scheduler_job_states",
    "build_scheduler_status",
    "build_scheduler_summary",
    "build_system_monitor_payload",
    "build_system_summary_payload",
]
