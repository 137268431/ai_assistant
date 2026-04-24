from __future__ import annotations

from typing import Any


def forward_request(
    *,
    requests_module,
    request_obj,
    jsonify_fn,
    request_timeout_seconds: float,
    forwarded_request_headers: set[str],
    build_response_from_upstream_fn,
    build_service_topology_fn,
    base_url: str,
    path: str,
    params: list[tuple[str, str]] | None = None,
    json_body: Any = None,
):
    headers = {
        key: value
        for key, value in request_obj.headers.items()
        if key in forwarded_request_headers and value
    }
    target_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        upstream_response = requests_module.request(
            method=request_obj.method,
            url=target_url,
            params=params if params is not None else list(request_obj.args.items(multi=True)),
            data=None if json_body is not None else request_obj.get_data(cache=True),
            json=json_body,
            headers=headers,
            timeout=request_timeout_seconds,
            allow_redirects=False,
        )
    except requests_module.RequestException as exc:
        return jsonify_fn(
            {
                "ok": False,
                "status": "offline",
                "error": str(exc),
                "upstream": target_url,
                "service_topology": build_service_topology_fn(),
            }
        ), 502
    return build_response_from_upstream_fn(upstream_response)


def proxy_custom_to_pb(subpath: str, *, forward_request_fn, pb_base_url: str):
    return forward_request_fn(pb_base_url, f"/api/custom/{subpath}")


def proxy_webhook_to_pb(subpath: str, *, forward_request_fn, pb_base_url: str):
    return forward_request_fn(pb_base_url, f"/webhook/{subpath}")
