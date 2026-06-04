from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.analytics.daily_signals import build_daily_signal_analytics_response
from ibkr_api.analytics.daily_trade_review import build_daily_trade_review_response
from ibkr_api.analytics.realized_pnl import build_realized_pnl_summary_response
from ibkr_api.analytics.signal_latency import build_signal_latency_analytics_response


def register_analytics_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    escape_filter_string = deps["escape_filter_string"]
    time_strings = deps["time_strings"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/analytics/daily-signals", methods=["GET"])
    def custom_ibkr_daily_signal_analytics() -> Response:
        payload, status_code = build_daily_signal_analytics_response(
            pb,
            params=dict(request.args or {}),
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            time_strings=time_strings,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_daily_signal_analytics"] = custom_ibkr_daily_signal_analytics

    @app.route("/api/custom/ibkr/analytics/signal-latency", methods=["GET"])
    def custom_ibkr_signal_latency_analytics() -> Response:
        payload, status_code = build_signal_latency_analytics_response(
            pb,
            params=dict(request.args or {}),
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            time_strings=time_strings,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_signal_latency_analytics"] = custom_ibkr_signal_latency_analytics

    @app.route("/api/custom/ibkr/analytics/daily-trade-review", methods=["GET"])
    def custom_ibkr_daily_trade_review() -> Response:
        payload, status_code = build_daily_trade_review_response(
            pb,
            params=dict(request.args or {}),
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            time_strings=time_strings,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_daily_trade_review"] = custom_ibkr_daily_trade_review

    @app.route("/api/custom/ibkr/analytics/realized-pnl-summary", methods=["GET"])
    def custom_ibkr_realized_pnl_summary() -> Response:
        payload, status_code = build_realized_pnl_summary_response(
            pb,
            params=dict(request.args or {}),
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            time_strings=time_strings,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_realized_pnl_summary"] = custom_ibkr_realized_pnl_summary
    return exports


__all__ = ["register_analytics_routes"]
