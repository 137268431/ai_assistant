from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.app_core.cache_snapshots import build_snapshot_cache_key, cached_snapshot_response, clear_cached_snapshots, request_force_refresh
from ibkr_api.app_core.route_cache import RouteSWRCache, cache_seconds, canonical_cache_key
from ibkr_api.home.dashboard import build_home_dashboard_response
from ibkr_api.home.market import build_home_market_response


_HOME_ROUTE_CACHE = RouteSWRCache("home")
_HOME_PB: Any | None = None


def _clear_home_route_cache() -> None:
    _HOME_ROUTE_CACHE.clear()
    clear_cached_snapshots(_HOME_PB, scopes=("home-dashboard", "home-market"))


def _cached_home_response(
    pb: Any,
    namespace: str,
    query_payload: dict[str, Any],
    builder: Any,
    *,
    ttl_seconds: float,
    stale_seconds: float,
    environment: str,
    market_date: str,
) -> tuple[dict[str, Any], int]:
    snapshot_key = build_snapshot_cache_key(namespace, query_payload)

    def build_with_snapshot() -> tuple[dict[str, Any], int]:
        return cached_snapshot_response(
            pb,
            scope=namespace,
            cache_key=snapshot_key,
            builder=builder,
            ttl_seconds=ttl_seconds,
            stale_seconds=stale_seconds,
            environment=environment,
            market_date=market_date,
            force=request_force_refresh(query_payload),
            background_refresh=True,
        )

    return _HOME_ROUTE_CACHE.get(
        canonical_cache_key(namespace, query_payload),
        builder=build_with_snapshot,
        ttl_seconds=ttl_seconds,
        stale_seconds=stale_seconds,
        force=request_force_refresh(query_payload),
    )


def register_home_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    global _HOME_PB
    pb = deps["pb"]
    _HOME_PB = pb
    time_strings = deps["time_strings"]
    config_value = deps["config_value"]
    request_json_request = deps["request_json_request"]
    runtime_base_url = deps["runtime_base_url"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/home-dashboard", methods=["GET"])
    def custom_ibkr_home_dashboard() -> Response:
        query_payload = request.args.to_dict(flat=True)
        ttl_seconds = cache_seconds("IBKR_ROUTE_CACHE_HOME_DASHBOARD_TTL_SEC", 15.0)
        stale_seconds = cache_seconds("IBKR_ROUTE_CACHE_HOME_DASHBOARD_STALE_SEC", 45.0)
        response_payload, status_code = _cached_home_response(
            pb,
            "home-dashboard",
            query_payload,
            lambda: build_home_dashboard_response(
                pb,
                payload=query_payload,
                time_strings=time_strings,
                request_json_request=request_json_request,
                runtime_base_url=runtime_base_url,
            ),
            ttl_seconds=ttl_seconds,
            stale_seconds=stale_seconds,
            environment=str(query_payload.get("data_environment") or query_payload.get("market_data_mode") or query_payload.get("environment") or "live"),
            market_date=str(query_payload.get("market_date") or query_payload.get("date") or "global"),
        )
        response = jsonify(response_payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_home_dashboard"] = custom_ibkr_home_dashboard

    @app.route("/api/custom/ibkr/home-market", methods=["GET"])
    def custom_ibkr_home_market() -> Response:
        query_payload = request.args.to_dict(flat=True)
        ttl_seconds = cache_seconds("IBKR_ROUTE_CACHE_HOME_MARKET_TTL_SEC", 15.0)
        stale_seconds = cache_seconds("IBKR_ROUTE_CACHE_HOME_MARKET_STALE_SEC", 45.0)
        response_payload, status_code = _cached_home_response(
            pb,
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
            ttl_seconds=ttl_seconds,
            stale_seconds=stale_seconds,
            environment=str(query_payload.get("data_environment") or query_payload.get("market_data_mode") or query_payload.get("environment") or "live"),
            market_date=str(query_payload.get("market_date") or query_payload.get("date") or "global"),
        )
        response = jsonify(response_payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_home_market"] = custom_ibkr_home_market
    exports["_clear_home_route_cache"] = _clear_home_route_cache
    return exports


__all__ = ["register_home_routes", "_clear_home_route_cache"]
