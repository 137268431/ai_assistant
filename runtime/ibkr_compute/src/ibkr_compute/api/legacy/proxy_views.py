"""Legacy PocketBase proxy helpers."""

from __future__ import annotations

import requests
from flask import Response, jsonify, request

from ibkr_compute.api.route_request import get_query_arg_page


LEGACY_COLLECTION_MAP = {
    "signals": "ibkr_signals",
    "indicators": "ibkr_indicators",
    "qc_signals": "ibkr_signals",
    "qc_indicators": "ibkr_indicators",
    "qc_bars": "ibkr_bars",
    "daily_targets": "ibkr_targets",
    "qc_state": "ibkr_state",
}
LEGACY_EMPTY_FALLBACKS = {"ibkr_positions", "ibkr_session"}


def _api_app():
    from .. import app as api_app

    return api_app


def build_legacy_pb_proxy_response(subpath):
    api_app = _api_app()
    raw_path = str(subpath or "").lstrip("/")
    if not raw_path:
        return jsonify({"ok": False, "error": "missing collection path"}), 400

    parts = raw_path.split("/", 1)
    collection = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    mapped_collection = LEGACY_COLLECTION_MAP.get(collection, collection)
    target_path = mapped_collection if not rest else f"{mapped_collection}/{rest}"
    upstream = f"{api_app.PB_BASE_URL.rstrip('/')}/api/collections/{target_path}"

    headers = {}
    auth_header = request.headers.get("Authorization")
    content_type = request.headers.get("Content-Type")
    if auth_header:
        headers["Authorization"] = auth_header
    if content_type:
        headers["Content-Type"] = content_type

    try:
        upstream_resp = requests.request(
            method=request.method,
            url=upstream,
            params=request.args,
            data=request.get_data(),
            headers=headers,
            timeout=30,
        )
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": f"proxy to PocketBase failed: {exc}",
            "upstream": upstream,
        }), 502

    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    if request.method == "GET" and upstream_resp.status_code in (400, 404) and mapped_collection in LEGACY_EMPTY_FALLBACKS:
        per_page = get_query_arg_page("perPage", 30)
        page = get_query_arg_page("page", 1)
        return jsonify({
            "page": page,
            "perPage": per_page,
            "totalItems": 0,
            "totalPages": 0,
            "items": [],
        })

    response_headers = [
        (key, value)
        for key, value in upstream_resp.headers.items()
        if key.lower() not in excluded
    ]
    return Response(upstream_resp.content, upstream_resp.status_code, response_headers)
