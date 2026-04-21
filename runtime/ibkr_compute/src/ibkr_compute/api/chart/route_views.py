from __future__ import annotations

import traceback

from flask import jsonify

from ibkr_compute.api.chart.request import load_chart_request_payload, validate_chart_request
from ibkr_compute.api.chart.views import build_chart_compare_payload, build_chart_timeline_payload
from ibkr_compute.api.route_runtime import get_app_module, get_service_status
from ibkr_compute.market.timeframe_utils import interval_to_chart_tf


def _build_chart_compare_error_response(exc: Exception, params: dict, service_status: dict, status_code: int):
    return jsonify(
        {
            "ok": False,
            "error": str(exc),
            "environment": params["environment"],
            "symbol": params["symbol"],
            "interval": interval_to_chart_tf(params["interval"]),
            "gateway_running": bool((service_status.get("gateway") or {}).get("running")),
            "session_authenticated": bool((service_status.get("session") or {}).get("authenticated")),
        }
    ), status_code


def build_chart_timeline_response():
    app_mod = get_app_module()
    params = load_chart_request_payload()
    validation_error = validate_chart_request(app_mod, params)
    if validation_error:
        return validation_error

    result = build_chart_timeline_payload(
        params["environment"],
        params["symbol"],
        params["interval"],
        start_ms=params["start_ms"],
        end_ms=params["end_ms"],
        include_signals=params["include_signals"],
        include_trace=params["include_trace"],
        preview_bar=params["preview_bar"],
    )
    return jsonify(result), 200


def build_chart_compare_response():
    app_mod = get_app_module()
    params = load_chart_request_payload()
    validation_error = validate_chart_request(app_mod, params, require_bounded_start=True)
    if validation_error:
        return validation_error

    service = app_mod.get_ibkr_service()
    service_status = get_service_status(service)

    try:
        result = build_chart_compare_payload(
            params["environment"],
            params["symbol"],
            params["interval"],
            start_ms=params["start_ms"],
            end_ms=params["end_ms"],
            include_signals=params["include_signals"],
        )
        return jsonify(result), 200
    except RuntimeError as exc:
        return _build_chart_compare_error_response(exc, params, service_status, 409)
    except Exception as exc:
        traceback.print_exc()
        return _build_chart_compare_error_response(exc, params, service_status, 500)
