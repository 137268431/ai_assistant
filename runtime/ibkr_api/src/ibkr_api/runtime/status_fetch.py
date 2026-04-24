from __future__ import annotations

from typing import Any

from .status_types import AsDict, BuildServiceTopology, RequestJson


def _request_environment_payload(
    base_url: str,
    path: str,
    environment: str,
    *,
    request_json: RequestJson,
    extra_params: list[tuple[str, str]] | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    params = [("environment", environment)]
    if extra_params:
        params.extend(extra_params)
    return request_json(
        base_url,
        path,
        params=params,
        timeout=timeout,
    )


def fetch_compute_monitor(environment: str, *, request_json: RequestJson, compute_base_url: str) -> dict[str, Any]:
    return _request_environment_payload(
        compute_base_url,
        "/ibkr/monitor",
        environment,
        request_json=request_json,
    )


def fetch_compute_status(
    environment: str,
    *,
    request_json: RequestJson,
    compute_base_url: str,
    include_engines: bool = False,
) -> dict[str, Any]:
    return _request_environment_payload(
        compute_base_url,
        "/status",
        environment,
        request_json=request_json,
        extra_params=[("full", "1")] if include_engines else [("lite", "1")],
    )


def fetch_compute_health(environment: str, *, request_json: RequestJson, compute_base_url: str) -> dict[str, Any]:
    return _request_environment_payload(
        compute_base_url,
        "/health",
        environment,
        request_json=request_json,
    )


def fetch_runtime_status(
    environment: str,
    *,
    request_json: RequestJson,
    compute_base_url: str,
    runtime_base_url: str,
    as_dict: AsDict,
) -> dict[str, Any]:
    proxy_result = _request_environment_payload(
        compute_base_url,
        "/ibkr/status",
        environment,
        request_json=request_json,
    )
    proxy_upstream = f"{compute_base_url}/ibkr/status"
    direct_upstream = f"{runtime_base_url}/ibkr/status"
    selected_result = proxy_result
    selected_upstream = proxy_upstream
    error = str(proxy_result.get("error") or "")

    if (not bool(proxy_result.get("ok"))) and runtime_base_url:
        direct_result = _request_environment_payload(
            runtime_base_url,
            "/ibkr/status",
            environment,
            request_json=request_json,
        )
        if bool(direct_result.get("ok")):
            selected_result = direct_result
            selected_upstream = direct_upstream
            error = ""
        elif not error:
            error = str(direct_result.get("error") or "")

    return {
        "payload": as_dict(selected_result.get("payload")),
        "ok": bool(selected_result.get("ok")),
        "error": error,
        "selected_upstream": selected_upstream,
        "proxy_upstream": proxy_upstream,
        "direct_upstream": direct_upstream,
        "status_code": int(selected_result.get("status_code") or 0),
    }


def fetch_runtime_health(
    environment: str,
    *,
    request_json: RequestJson,
    runtime_base_url: str,
    as_dict: AsDict,
) -> dict[str, Any]:
    result = _request_environment_payload(
        runtime_base_url,
        "/health",
        environment,
        request_json=request_json,
    )
    return {
        "payload": as_dict(result.get("payload")),
        "ok": bool(result.get("ok")),
        "error": str(result.get("error") or ""),
        "upstream": f"{runtime_base_url}/health",
        "status_code": int(result.get("status_code") or 0),
    }


def merge_service_topology(*payloads: Any, build_service_topology: BuildServiceTopology) -> dict[str, Any]:
    merged = build_service_topology()
    merged_services = dict(merged.get("services") if isinstance(merged.get("services"), dict) else {})
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        topology = payload if isinstance(payload.get("services"), dict) else payload.get("service_topology")
        if not isinstance(topology, dict):
            continue
        for key, value in topology.items():
            if key == "services":
                continue
            merged[key] = value
        if isinstance(topology.get("services"), dict):
            merged_services.update(topology.get("services") or {})
    merged["services"] = merged_services
    return merged


__all__ = [
    "fetch_compute_health",
    "fetch_compute_monitor",
    "fetch_compute_status",
    "fetch_runtime_health",
    "fetch_runtime_status",
    "merge_service_topology",
]
