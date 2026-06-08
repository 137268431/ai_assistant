from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.app_core.cache_snapshots import build_snapshot_cache_key, cached_snapshot_response, clear_cached_snapshots, request_force_refresh
from ibkr_api.app_core.route_cache import RouteSWRCache, cache_seconds, canonical_cache_key
from ibkr_api.modes import request_broker_mode, request_market_data_mode

_REVERSE_ROUTE_CACHE = RouteSWRCache("reverse")
_REVERSE_PB: Any | None = None
_REVERSE_SNAPSHOT_SCOPES = ("reverse-list", "reverse-pending")


def _clear_reverse_route_cache() -> None:
    _REVERSE_ROUTE_CACHE.clear()
    clear_cached_snapshots(_REVERSE_PB, scopes=_REVERSE_SNAPSHOT_SCOPES)


def _snapshot_market_date(query_payload: dict[str, Any]) -> str:
    return str(query_payload.get("date") or query_payload.get("market_date") or "global").strip() or "global"


def register_reverse_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    global _REVERSE_PB
    pb = deps["pb"]
    _REVERSE_PB = pb
    normalize_environment = deps["normalize_environment"]
    escape_filter_string = deps["escape_filter_string"]
    build_reverse_list_response = deps["build_reverse_list_response"]
    build_reverse_calculate_response = deps["build_reverse_calculate_response"]
    build_reverse_pending_response = deps["build_reverse_pending_response"]
    build_reverse_dispatch_response = deps["build_reverse_dispatch_response"]
    build_reverse_ack_response = deps["build_reverse_ack_response"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/reverse/list", methods=["GET"])
    def custom_ibkr_reverse_list() -> Response:
        query_payload = request.args.to_dict(flat=True)
        mode_payload = {
            "broker_mode": query_payload.get("broker_mode") or query_payload.get("environment"),
            "market_data_mode": query_payload.get("market_data_mode"),
            "data_environment": query_payload.get("data_environment"),
        }
        ttl_seconds = cache_seconds("IBKR_ROUTE_CACHE_REVERSE_TTL_SEC", 30.0)
        stale_seconds = cache_seconds("IBKR_ROUTE_CACHE_REVERSE_STALE_SEC", 120.0)
        builder = lambda: build_reverse_list_response(
            pb,
            environment=request_broker_mode(mode_payload),
            data_environment=request_market_data_mode(mode_payload),
            date_str=query_payload.get("date") or "",
            symbol=query_payload.get("symbol") or "",
            statuses=query_payload.get("status") or "",
            limit=query_payload.get("limit"),
            normalize_environment=normalize_environment,
            escape_filter=escape_filter_string,
        )
        payload, status_code = _REVERSE_ROUTE_CACHE.get(
            canonical_cache_key("reverse_list", query_payload),
            builder=lambda: cached_snapshot_response(
                pb,
                scope="reverse-list",
                cache_key=build_snapshot_cache_key("reverse-list", query_payload),
                builder=builder,
                ttl_seconds=ttl_seconds,
                stale_seconds=stale_seconds,
                environment=request_broker_mode(mode_payload),
                market_date=_snapshot_market_date(query_payload),
                force=request_force_refresh(query_payload),
                background_refresh=True,
            ),
            ttl_seconds=ttl_seconds,
            stale_seconds=stale_seconds,
            force=request_force_refresh(query_payload),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reverse_list"] = custom_ibkr_reverse_list

    @app.route("/api/custom/ibkr/reverse/calculate", methods=["POST"])
    def custom_ibkr_reverse_calculate() -> Response:
        payload, status_code = build_reverse_calculate_response(
            pb,
            payload=request.get_json(silent=True) or {},
            escape_filter=escape_filter_string,
        )
        if status_code < 400:
            _clear_reverse_route_cache()
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reverse_calculate"] = custom_ibkr_reverse_calculate

    @app.route("/api/custom/ibkr/reverse/pending", methods=["GET"])
    def custom_ibkr_reverse_pending() -> Response:
        query_payload = request.args.to_dict(flat=True)
        mode_payload = {
            "broker_mode": query_payload.get("broker_mode") or query_payload.get("environment"),
            "market_data_mode": query_payload.get("market_data_mode"),
            "data_environment": query_payload.get("data_environment"),
        }
        ttl_seconds = cache_seconds("IBKR_ROUTE_CACHE_REVERSE_TTL_SEC", 30.0)
        stale_seconds = cache_seconds("IBKR_ROUTE_CACHE_REVERSE_STALE_SEC", 120.0)
        builder = lambda: build_reverse_pending_response(
            pb,
            environment=request_broker_mode(mode_payload),
            data_environment=request_market_data_mode(mode_payload),
            limit=query_payload.get("limit"),
            normalize_environment=normalize_environment,
            escape_filter=escape_filter_string,
        )
        payload, status_code = _REVERSE_ROUTE_CACHE.get(
            canonical_cache_key("reverse_pending", query_payload),
            builder=lambda: cached_snapshot_response(
                pb,
                scope="reverse-pending",
                cache_key=build_snapshot_cache_key("reverse-pending", query_payload),
                builder=builder,
                ttl_seconds=ttl_seconds,
                stale_seconds=stale_seconds,
                environment=request_broker_mode(mode_payload),
                market_date=_snapshot_market_date(query_payload),
                force=request_force_refresh(query_payload),
                background_refresh=True,
            ),
            ttl_seconds=ttl_seconds,
            stale_seconds=stale_seconds,
            force=request_force_refresh(query_payload),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reverse_pending"] = custom_ibkr_reverse_pending

    @app.route("/api/custom/ibkr/reverse/dispatch", methods=["POST"])
    def custom_ibkr_reverse_dispatch() -> Response:
        payload, status_code = build_reverse_dispatch_response(
            pb,
            payload=request.get_json(silent=True) or {},
            escape_filter=escape_filter_string,
        )
        if status_code < 400:
            _clear_reverse_route_cache()
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reverse_dispatch"] = custom_ibkr_reverse_dispatch

    @app.route("/api/custom/ibkr/reverse/ack", methods=["POST"])
    def custom_ibkr_reverse_ack() -> Response:
        payload, status_code = build_reverse_ack_response(
            pb,
            payload=request.get_json(silent=True) or {},
            escape_filter=escape_filter_string,
        )
        if status_code < 400:
            _clear_reverse_route_cache()
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)
    exports["custom_ibkr_reverse_ack"] = custom_ibkr_reverse_ack

    return exports


__all__ = ["register_reverse_routes", "_clear_reverse_route_cache"]
