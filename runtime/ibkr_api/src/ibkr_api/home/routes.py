from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.app_core.route_cache import RouteSWRCache, cache_seconds, canonical_cache_key, request_cache_bypass
from ibkr_api.home.dashboard import build_home_dashboard_response
from ibkr_api.home.market import build_home_market_response


_HOME_ROUTE_CACHE = RouteSWRCache("home")


def _clear_home_route_cache() -> None:
    _HOME_ROUTE_CACHE.clear()


def _cached_home_response(
    namespace: str,
    query_payload: dict[str, Any],
    builder: Any,
    *,
    ttl_seconds: float,
    stale_seconds: float,
) -> tuple[dict[str, Any], int]:
    return _HOME_ROUTE_CACHE.get(
        canonical_cache_key(namespace, query_payload),
        builder=builder,
        ttl_seconds=ttl_seconds,
        stale_seconds=stale_seconds,
        force=request_cache_bypass(query_payload),
    )


def register_home_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    time_strings = deps["time_strings"]
    config_value = deps["config_value"]
    request_json_request = deps["request_json_request"]
    runtime_base_url = deps["runtime_base_url"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/home-dashboard", methods=["GET"])
    def custom_ibkr_home_dashboard() -> Response:
        query_payload = request.args.to_dict(flat=True)
        response_payload, status_code = _cached_home_response(
            "home-dashboard",
            query_payload,
            lambda: build_home_dashboard_response(
                pb,
                payload=query_payload,
                time_strings=time_strings,
                request_json_request=request_json_request,
                runtime_base_url=runtime_base_url,
            ),
            ttl_seconds=cache_seconds("IBKR_ROUTE_CACHE_HOME_DASHBOARD_TTL_SEC", 15.0),
            stale_seconds=cache_seconds("IBKR_ROUTE_CACHE_HOME_DASHBOARD_STALE_SEC", 45.0),
        )
        response = jsonify(response_payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_home_dashboard"] = custom_ibkr_home_dashboard

    @app.route("/api/custom/ibkr/home-market", methods=["GET"])
    def custom_ibkr_home_market() -> Response:
        query_payload = request.args.to_dict(flat=True)
        response_payload, status_code = _cached_home_response(
            "home-market",
            query_payload,
            lambda: build_home_market_response(
                pb,
                payload=query_payload,
                config_value=config_value,
                request_json_request=request_json_request,
                runtime_base_url=runtime_base_url,
                time_strings=time_strings,
            ),
            ttl_seconds=cache_seconds("IBKR_ROUTE_CACHE_HOME_MARKET_TTL_SEC", 15.0),
            stale_seconds=cache_seconds("IBKR_ROUTE_CACHE_HOME_MARKET_STALE_SEC", 45.0),
        )
        response = jsonify(response_payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_home_market"] = custom_ibkr_home_market
    exports["_clear_home_route_cache"] = _clear_home_route_cache
    return exports


__all__ = ["register_home_routes", "_clear_home_route_cache"]
