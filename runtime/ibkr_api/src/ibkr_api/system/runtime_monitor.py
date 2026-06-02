from __future__ import annotations

import os
import time
from typing import Any, Callable

DEFAULT_ACCOUNT_SNAPSHOT_MONITOR_TIMEOUT_SEC = 4.0
DEFAULT_ACCOUNT_SNAPSHOT_MONITOR_ATTEMPTS = 2
DEFAULT_ACCOUNT_SNAPSHOT_MONITOR_RETRY_INTERVAL_SEC = 2.0


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)) or default)
    except Exception:
        value = default
    return max(minimum, value)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(float(os.environ.get(name, str(default)) or default))
    except Exception:
        value = default
    return max(minimum, value)


def _account_snapshot_probe_error(payload: dict[str, Any], status_code: int) -> str:
    return str(
        payload.get("error")
        or payload.get("message")
        or (f"status={status_code}" if status_code >= 400 else "")
    ).strip()


def _build_account_snapshot_probe(globals_dict: dict[str, Any], probe_environment: str) -> dict[str, Any]:
    from ibkr_api.account.snapshot import build_account_snapshot_response

    started = time.monotonic()
    timeout_s = _env_float(
        "IBKR_ACCOUNT_SNAPSHOT_MONITOR_TIMEOUT_SEC",
        DEFAULT_ACCOUNT_SNAPSHOT_MONITOR_TIMEOUT_SEC,
        minimum=1.0,
    )
    attempts_limit = _env_int(
        "IBKR_ACCOUNT_SNAPSHOT_MONITOR_ATTEMPTS",
        DEFAULT_ACCOUNT_SNAPSHOT_MONITOR_ATTEMPTS,
        minimum=1,
    )
    retry_interval_s = _env_float(
        "IBKR_ACCOUNT_SNAPSHOT_MONITOR_RETRY_INTERVAL_SEC",
        DEFAULT_ACCOUNT_SNAPSHOT_MONITOR_RETRY_INTERVAL_SEC,
        minimum=0.0,
    )
    attempts: list[dict[str, Any]] = []
    last_payload: dict[str, Any] = {}
    last_status_code = 0
    last_error = ""

    for attempt in range(1, attempts_limit + 1):
        attempt_started = time.monotonic()
        try:
            payload, status_code = build_account_snapshot_response(
                globals_dict["pb"],
                payload={"broker_mode": probe_environment, "environment": probe_environment, "include_pnl": "0"},
                normalize_environment=globals_dict["_normalize_environment"],
                request_json_request=globals_dict["_request_json_request"],
                runtime_base_url=str(globals_dict.get("RUNTIME_BASE_URL") or "http://127.0.0.1:5101").rstrip("/"),
                upstream_timeout=timeout_s,
            )
            payload = payload if isinstance(payload, dict) else {}
            status_code = int(status_code or 0)
            ok = status_code < 400 and payload.get("ok") is not False
            error = "" if ok else _account_snapshot_probe_error(payload, status_code)
        except Exception as exc:
            payload = {}
            status_code = 0
            ok = False
            error = str(exc)

        attempt_result = {
            "attempt": attempt,
            "ok": ok,
            "status_code": status_code,
            "elapsed_ms": round((time.monotonic() - attempt_started) * 1000.0, 1),
            "timeout_s": timeout_s,
            "error": error,
        }
        attempts.append(attempt_result)
        last_payload = payload
        last_status_code = status_code
        last_error = error
        if ok:
            return {
                "ok": True,
                "status_code": status_code,
                "payload": payload,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                "error": "",
                "attempts": attempts,
            }
        if attempt < attempts_limit and retry_interval_s > 0:
            time.sleep(retry_interval_s)

    return {
        "ok": False,
        "status_code": last_status_code,
        "payload": last_payload,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
        "error": last_error,
        "attempts": attempts,
    }


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
    def _scheduler_status(
        environment: str = "live",
        *,
        broker_mode: str = "",
        market_data_mode: str = "",
        lite: bool = False,
    ) -> dict[str, Any]:
        return support(
            environment,
            request_json=globals_dict["_request_json"],
            scheduler_base_url=scheduler_base_url,
            broker_mode=broker_mode,
            market_data_mode=market_data_mode,
            lite=lite,
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
            fetch_backtest_health=globals_dict.get("_fetch_backtest_health"),
            as_dict=globals_dict["_as_dict"],
            merge_service_topology=globals_dict["_merge_service_topology"],
            load_recent_system_events=globals_dict["_load_recent_system_events"],
            time_strings=globals_dict["_time_strings"],
            load_today_counts=globals_dict["_load_today_counts"],
            collect_storage_health=globals_dict.get("_collect_storage_health"),
            pb_client=globals_dict.get("pb"),
            config=globals_dict.get("config"),
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
        def _account_snapshot_probe(probe_environment: str) -> dict[str, Any]:
            return _build_account_snapshot_probe(globals_dict, probe_environment)

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
            account_snapshot_probe=_account_snapshot_probe,
            pb_client=globals_dict.get("pb"),
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
