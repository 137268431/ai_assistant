from __future__ import annotations

from typing import Any, Callable

from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode


AsDict = Callable[[Any], dict[str, Any]]
NormalizeEnvironment = Callable[[Any, str], str]
FetchRuntimeStatus = Callable[[str], dict[str, Any]]


def inspect_requested_runtime_environment(
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    fetch_runtime_status: FetchRuntimeStatus,
    as_dict: AsDict,
) -> dict[str, Any]:
    requested_environment = normalize_broker_mode(environment, configured_broker_mode())
    runtime_result = fetch_runtime_status(requested_environment)
    runtime_payload = as_dict(runtime_result.get("payload"))
    actual_runtime_environment = normalize_broker_mode(
        runtime_payload.get("broker_mode") or runtime_payload.get("environment") or requested_environment,
        requested_environment,
    )
    return {
        "requested_environment": requested_environment,
        "actual_runtime_environment": actual_runtime_environment,
        "runtime_environment_mismatch": actual_runtime_environment != requested_environment,
        "runtime_payload": runtime_payload,
        "proxy_upstream_runtime": str(
            runtime_result.get("selected_upstream") or runtime_result.get("proxy_upstream") or ""
        ),
        "runtime_status_error": str(runtime_result.get("error") or ""),
    }


def build_runtime_environment_mismatch_payload(environment_info: dict[str, Any], route: str) -> dict[str, Any]:
    requested_environment = str(environment_info.get("requested_environment") or "live").strip().lower() or "live"
    actual_runtime_environment = (
        str(environment_info.get("actual_runtime_environment") or requested_environment).strip().lower()
        or requested_environment
    )
    return {
        "ok": False,
        "error": (
            f"当前 {requested_environment.upper()} 页面没有独立 runtime；实际运行中的是 "
            f"{actual_runtime_environment.upper()}，请切到对应环境页面执行此动作。"
        ),
        "requested_environment": requested_environment,
        "actual_runtime_environment": actual_runtime_environment,
        "runtime_environment_mismatch": True,
        "source": "ibkr-api",
        "route": str(route or ""),
        "proxy_upstream_runtime": str(environment_info.get("proxy_upstream_runtime") or ""),
    }


__all__ = [
    "build_runtime_environment_mismatch_payload",
    "inspect_requested_runtime_environment",
]
