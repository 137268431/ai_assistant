from __future__ import annotations

from functools import partial

import requests
from flask import Response, jsonify, request

from ibkr_compute.api.service_topology import (
    build_service_topology,
    get_runtime_internal_url,
    is_runtime_remote_mode,
)


RUNTIME_PROXY_TIMEOUT_SECONDS = 60


def should_proxy_runtime_requests() -> bool:
    return is_runtime_remote_mode()


def _build_runtime_upstream(path: str) -> str:
    normalized_path = str(path or "").strip()
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    return f"{get_runtime_internal_url()}{normalized_path}"


def proxy_runtime_request(path: str):
    upstream = _build_runtime_upstream(path)
    params = list(request.args.items(multi=True))
    headers = {}
    for header_name in ("Accept", "Content-Type"):
        header_value = request.headers.get(header_name)
        if header_value:
            headers[header_name] = header_value

    try:
        upstream_response = requests.request(
            method=request.method,
            url=upstream,
            params=params,
            data=request.get_data(cache=True),
            headers=headers,
            timeout=RUNTIME_PROXY_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return jsonify(
            {
                "ok": False,
                "status": "offline",
                "error": str(exc),
                "proxy_upstream": upstream,
                "service_topology": build_service_topology(),
            }
        ), 502

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

