from __future__ import annotations

import os
import threading
import time
from typing import Any

from ibkr_api.system.service_state import canonicalize_topology

from .status_types import AsDict, BuildServiceTopology, RequestJson


UPSTREAM_CACHE_TTL_SECONDS = max(
    0.0,
    float(os.environ.get("IBKR_API_UPSTREAM_STATUS_CACHE_TTL_SEC", "60.0")),
)

_upstream_cache_lock = threading.Lock()
_upstream_success_cache: dict[tuple[Any, ...], dict[str, Any]] = {}


def _cache_key(
    base_url: str,
    path: str,
    environment: str,
    extra_params: list[tuple[str, str]] | None,
) -> tuple[Any, ...]:
    return (
        str(base_url or "").rstrip("/"),
        str(path or "").strip(),
        str(environment or "").strip().lower(),
        tuple(extra_params or ()),
    )


def _with_stale_cache(key: tuple[Any, ...], result: dict[str, Any]) -> dict[str, Any]:
    if UPSTREAM_CACHE_TTL_SECONDS <= 0:
        return result
    now = time.time()
    payload = result.get("payload") if isinstance(result, dict) else {}
    if bool(result.get("ok")) and isinstance(payload, dict) and payload:
        with _upstream_cache_lock:
            _upstream_success_cache[key] = {
                "expires_at": now + UPSTREAM_CACHE_TTL_SECONDS,
                "payload": dict(payload),
                "target_url": result.get("target_url") or "",
                "status_code": int(result.get("status_code") or 0),
            }
        return result

    # A JSON payload with HTTP 200 but ok=false is an authoritative degraded
    # snapshot, not a transport miss. Do not mask it with stale data.
    if isinstance(payload, dict) and payload:
        return result

    with _upstream_cache_lock:
        cached = dict(_upstream_success_cache.get(key) or {})
    cached_payload = cached.get("payload") if isinstance(cached.get("payload"), dict) else {}
    if cached_payload and now < float(cached.get("expires_at") or 0.0):
        stale_payload = {
            **dict(cached_payload),
            "stale": True,
            "stale_error": str(result.get("error") or ""),
            "stale_status_code": int(result.get("status_code") or 0),
        }
        return {
            **result,
            "ok": True,
            "payload": stale_payload,
            "error": "",
            "stale": True,
            "stale_error": str(result.get("error") or ""),
            "target_url": result.get("target_url") or cached.get("target_url") or "",
            "cached_status_code": int(cached.get("status_code") or 0),
        }
    return result


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
    result = request_json(
        base_url,
        path,
        params=params,
        timeout=timeout,
    )
    return _with_stale_cache(_cache_key(base_url, path, environment, extra_params), result)


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


def fetch_backtest_health(environment: str, *, request_json: RequestJson, backtest_base_url: str, as_dict: AsDict) -> dict[str, Any]:
    result = _request_environment_payload(
        backtest_base_url,
        "/health",
        environment,
        request_json=request_json,
    )
    return {
        "payload": as_dict(result.get("payload")),
        "ok": bool(result.get("ok")),
        "error": str(result.get("error") or ""),
        "upstream": f"{backtest_base_url}/health",
        "status_code": int(result.get("status_code") or 0),
        "target_url": str(result.get("target_url") or ""),
        "stale": bool(result.get("stale", False)),
    }


def fetch_backtest_status(environment: str, *, request_json: RequestJson, backtest_base_url: str, as_dict: AsDict) -> dict[str, Any]:
    result = _request_environment_payload(
        backtest_base_url,
        "/backtest/status",
        environment,
        request_json=request_json,
    )
    return {
        "payload": as_dict(result.get("payload")),
        "ok": bool(result.get("ok")),
        "error": str(result.get("error") or ""),
        "upstream": f"{backtest_base_url}/backtest/status",
        "status_code": int(result.get("status_code") or 0),
        "target_url": str(result.get("target_url") or ""),
        "stale": bool(result.get("stale", False)),
    }


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
    base_service_profile = str(merged.get("service_profile") or "").strip()
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
            if key == "service_profile" and base_service_profile:
                continue
            merged[key] = value
        if isinstance(topology.get("services"), dict):
            for service_name, service_item in (topology.get("services") or {}).items():
                current_item = merged_services.get(service_name) if isinstance(merged_services.get(service_name), dict) else {}
                incoming_item = service_item if isinstance(service_item, dict) else {}
                merged_services[service_name] = {
                    **current_item,
                    **incoming_item,
                }
    merged["services"] = merged_services
    if base_service_profile:
        merged["service_profile"] = base_service_profile
    environment = "live"
    for payload in payloads:
        if isinstance(payload, dict) and str(payload.get("environment") or "").strip():
            environment = str(payload.get("environment") or "").strip().lower()
            break
    canonical_topology, _ = canonicalize_topology(environment, merged, *payloads)
    return canonical_topology


__all__ = [
    "fetch_backtest_health",
    "fetch_backtest_status",
    "fetch_compute_health",
    "fetch_compute_monitor",
    "fetch_compute_status",
    "fetch_runtime_health",
    "fetch_runtime_status",
    "merge_service_topology",
]
