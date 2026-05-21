from __future__ import annotations

import logging
import os
import time
from functools import partial

import requests
from flask import Response, jsonify, request

from ibkr_compute.api.service_topology import (
    build_service_topology,
    get_runtime_internal_url,
    is_runtime_remote_mode,
)


logger = logging.getLogger(__name__)
RUNTIME_PROXY_TIMEOUT_SECONDS = 60
RUNTIME_PROXY_LONG_TIMEOUT_SECONDS = 300
RUNTIME_PROXY_LONG_TIMEOUT_PATHS = {
    "/ibkr/data-quality/repair",
    "/ibkr/data-quality/daily-repair",
}


def _coerce_timeout_seconds(env_name: str, default: float) -> float:
    try:
        value = float(os.environ.get(env_name, "") or default)
    except Exception:
        value = float(default)
    return max(1.0, value)


def _runtime_proxy_timeout_seconds(path: str) -> float:
    normalized_path = str(path or "").strip()
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if normalized_path in RUNTIME_PROXY_LONG_TIMEOUT_PATHS:
        return _coerce_timeout_seconds(
            "IBKR_COMPUTE_RUNTIME_PROXY_REPAIR_TIMEOUT_SEC",
            RUNTIME_PROXY_LONG_TIMEOUT_SECONDS,
        )
    return _coerce_timeout_seconds(
        "IBKR_COMPUTE_RUNTIME_PROXY_TIMEOUT_SEC",
        RUNTIME_PROXY_TIMEOUT_SECONDS,
    )


def _runtime_proxy_slow_log_threshold_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("IBKR_COMPUTE_RUNTIME_PROXY_SLOW_LOG_SEC", "5.0") or 0.0))
    except Exception:
        return 5.0


def should_proxy_runtime_requests() -> bool:
    return is_runtime_remote_mode()


def _build_runtime_upstream(path: str) -> str:
    normalized_path = str(path or "").strip()
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    return f"{get_runtime_internal_url()}{normalized_path}"


def proxy_runtime_request(path: str):
    upstream = _build_runtime_upstream(path)
    timeout_seconds = _runtime_proxy_timeout_seconds(path)
    params = list(request.args.items(multi=True))
    headers = {}
    for header_name in ("Accept", "Content-Type"):
        header_value = request.headers.get(header_name)
        if header_value:
            headers[header_name] = header_value

    started = time.monotonic()
    try:
        upstream_response = requests.request(
            method=request.method,
            url=upstream,
            params=params,
            data=request.get_data(cache=True),
            headers=headers,
            timeout=timeout_seconds,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        logger.warning(
            "Runtime proxy request failed: path=%s upstream=%s elapsed_ms=%.1f timeout_s=%.1f error=%s",
            path,
            upstream,
            elapsed_ms,
            float(timeout_seconds),
            exc,
        )
        return jsonify(
            {
                "ok": False,
                "status": "offline",
                "error": str(exc),
                "proxy_upstream": upstream,
                "service_topology": build_service_topology(),
            }
        ), 502

    elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    slow_threshold_s = _runtime_proxy_slow_log_threshold_seconds()
    if (not upstream_response.ok) or (slow_threshold_s > 0 and elapsed_ms >= slow_threshold_s * 1000.0):
        log_fn = logger.warning if not upstream_response.ok else logger.info
        log_fn(
            "Runtime proxy request completed: path=%s upstream=%s status=%s elapsed_ms=%.1f timeout_s=%.1f",
            path,
            upstream,
            int(upstream_response.status_code),
            elapsed_ms,
            float(timeout_seconds),
        )

    response = Response(
        upstream_response.content,
        status=upstream_response.status_code,
    )
    content_type = upstream_response.headers.get("Content-Type")
    if content_type:
        response.headers["Content-Type"] = content_type
    location = upstream_response.headers.get("Location")
    if location:
        response.headers["Location"] = location
    return response


def register_runtime_proxy_route(app, endpoint: str, rule: str, methods: list[str] | tuple[str, ...]):
    app.add_url_rule(
        rule,
        endpoint=endpoint,
        view_func=partial(proxy_runtime_request, rule),
        methods=list(methods),
    )
