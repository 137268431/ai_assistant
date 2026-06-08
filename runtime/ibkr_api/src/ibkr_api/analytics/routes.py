from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.analytics.daily_signals import build_daily_signal_analytics_response
from ibkr_api.analytics.daily_trade_review import build_daily_trade_review_response
from ibkr_api.analytics.realized_pnl import build_realized_pnl_summary_response
from ibkr_api.analytics.signal_latency import build_signal_latency_analytics_response
from ibkr_api.app_core.cache_snapshots import build_snapshot_cache_key, cached_snapshot_response, clear_cached_snapshots, request_force_refresh
from ibkr_api.app_core.route_cache import RouteSWRCache, cache_seconds, canonical_cache_key
from ibkr_api.modes import request_broker_mode


_ANALYTICS_ROUTE_CACHE = RouteSWRCache("analytics")
_ANALYTICS_PB: Any | None = None
_ANALYTICS_SNAPSHOT_SCOPES = (
    "analytics-daily-signals",
    "analytics-signal-latency",
    "analytics-daily-trade-review",
    "analytics-realized-pnl-summary",
)


def _clear_analytics_route_cache() -> None:
    _ANALYTICS_ROUTE_CACHE.clear()
    clear_cached_snapshots(_ANALYTICS_PB, scopes=_ANALYTICS_SNAPSHOT_SCOPES)


def _snapshot_market_date(params: dict[str, Any], time_strings: Any) -> str:
    explicit = str(params.get("market_date") or params.get("date") or params.get("end_date") or params.get("date_to") or "").strip()
    if explicit:
        return explicit
    try:
        value = time_strings()
    except Exception:
        value = {}
    return str((value if isinstance(value, dict) else {}).get("date") or "global")


def _cached_analytics_response(
    pb: Any,
    namespace: str,
    params: dict[str, Any],
    builder: Any,
    *,
    ttl_seconds: float,
    stale_seconds: float,
    environment: str,
    market_date: str,
) -> tuple[dict[str, Any], int]:
    snapshot_key = build_snapshot_cache_key(namespace, params)

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
            force=request_force_refresh(params),
            background_refresh=True,
        )

    return _ANALYTICS_ROUTE_CACHE.get(
        canonical_cache_key(namespace, params),
        builder=build_with_snapshot,
        ttl_seconds=ttl_seconds,
        stale_seconds=stale_seconds,
        force=request_force_refresh(params),
    )


def register_analytics_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    global _ANALYTICS_PB
    pb = deps["pb"]
    _ANALYTICS_PB = pb
    normalize_environment = deps["normalize_environment"]
    escape_filter_string = deps["escape_filter_string"]
    time_strings = deps["time_strings"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/analytics/daily-signals", methods=["GET"])
    def custom_ibkr_daily_signal_analytics() -> Response:
        params = dict(request.args or {})
        payload, status_code = _cached_analytics_response(
            pb,
            "analytics-daily-signals",
            params,
            lambda: build_daily_signal_analytics_response(
                pb,
                params=params,
                normalize_environment=normalize_environment,
                escape_filter_string=escape_filter_string,
                time_strings=time_strings,
            ),
            ttl_seconds=cache_seconds("IBKR_ROUTE_CACHE_ANALYTICS_DAILY_SIGNALS_TTL_SEC", 120.0),
            stale_seconds=cache_seconds("IBKR_ROUTE_CACHE_ANALYTICS_STALE_SEC", 600.0),
            environment=request_broker_mode(params),
            market_date=_snapshot_market_date(params, time_strings),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_daily_signal_analytics"] = custom_ibkr_daily_signal_analytics

    @app.route("/api/custom/ibkr/analytics/signal-latency", methods=["GET"])
    def custom_ibkr_signal_latency_analytics() -> Response:
        params = dict(request.args or {})
        payload, status_code = _cached_analytics_response(
            pb,
            "analytics-signal-latency",
            params,
            lambda: build_signal_latency_analytics_response(
                pb,
                params=params,
                normalize_environment=normalize_environment,
                escape_filter_string=escape_filter_string,
                time_strings=time_strings,
            ),
            ttl_seconds=cache_seconds("IBKR_ROUTE_CACHE_ANALYTICS_SIGNAL_LATENCY_TTL_SEC", 120.0),
            stale_seconds=cache_seconds("IBKR_ROUTE_CACHE_ANALYTICS_STALE_SEC", 600.0),
            environment=request_broker_mode(params),
            market_date=_snapshot_market_date(params, time_strings),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_signal_latency_analytics"] = custom_ibkr_signal_latency_analytics

    @app.route("/api/custom/ibkr/analytics/daily-trade-review", methods=["GET"])
    def custom_ibkr_daily_trade_review() -> Response:
        params = dict(request.args or {})
        payload, status_code = _cached_analytics_response(
            pb,
            "analytics-daily-trade-review",
            params,
            lambda: build_daily_trade_review_response(
                pb,
                params=params,
                normalize_environment=normalize_environment,
                escape_filter_string=escape_filter_string,
                time_strings=time_strings,
            ),
            ttl_seconds=cache_seconds("IBKR_ROUTE_CACHE_ANALYTICS_DAILY_REVIEW_TTL_SEC", 300.0),
            stale_seconds=cache_seconds("IBKR_ROUTE_CACHE_ANALYTICS_STALE_SEC", 900.0),
            environment=request_broker_mode(params),
            market_date=_snapshot_market_date(params, time_strings),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_daily_trade_review"] = custom_ibkr_daily_trade_review

    @app.route("/api/custom/ibkr/analytics/realized-pnl-summary", methods=["GET"])
    def custom_ibkr_realized_pnl_summary() -> Response:
        params = dict(request.args or {})
        payload, status_code = _cached_analytics_response(
            pb,
            "analytics-realized-pnl-summary",
            params,
            lambda: build_realized_pnl_summary_response(
                pb,
                params=params,
                normalize_environment=normalize_environment,
                escape_filter_string=escape_filter_string,
                time_strings=time_strings,
            ),
            ttl_seconds=cache_seconds("IBKR_ROUTE_CACHE_ANALYTICS_REALIZED_PNL_TTL_SEC", 300.0),
            stale_seconds=cache_seconds("IBKR_ROUTE_CACHE_ANALYTICS_STALE_SEC", 900.0),
            environment=request_broker_mode(params),
            market_date=_snapshot_market_date(params, time_strings),
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_realized_pnl_summary"] = custom_ibkr_realized_pnl_summary
    exports["_clear_analytics_route_cache"] = _clear_analytics_route_cache
    return exports


__all__ = ["register_analytics_routes", "_clear_analytics_route_cache"]
