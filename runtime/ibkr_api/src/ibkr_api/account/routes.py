from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.account.snapshot import build_account_snapshot_response
from ibkr_api.app_core.route_cache import RouteSWRCache, cache_seconds, canonical_cache_key, request_cache_bypass


_ACCOUNT_ROUTE_CACHE = RouteSWRCache("account")


def _clear_account_route_cache() -> None:
    _ACCOUNT_ROUTE_CACHE.clear()


def register_account_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    request_json_request = deps["request_json_request"]
    runtime_base_url = deps["runtime_base_url"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/account_snapshot", methods=["GET"])
    def custom_ibkr_account_snapshot() -> Response:
        query_payload = request.args.to_dict(flat=True)
        payload, status_code = _ACCOUNT_ROUTE_CACHE.get(
            canonical_cache_key("account_snapshot", query_payload),
            builder=lambda: build_account_snapshot_response(
                pb,
                payload=query_payload,
                normalize_environment=normalize_environment,
                request_json_request=request_json_request,
                runtime_base_url=runtime_base_url,
            ),
            ttl_seconds=cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_SNAPSHOT_TTL_SEC", 5.0),
            stale_seconds=cache_seconds("IBKR_ROUTE_CACHE_ACCOUNT_SNAPSHOT_STALE_SEC", 20.0),
            force=request_cache_bypass(query_payload),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_account_snapshot"] = custom_ibkr_account_snapshot
    return exports


__all__ = ["register_account_routes", "_clear_account_route_cache"]
