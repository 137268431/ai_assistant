from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

from ibkr_api.universe.active_window_progress import build_active_window_progress_response
from ibkr_api.universe.fundamentals import build_fundamentals_list_response, build_fundamentals_refresh_response
from ibkr_api.universe.lifecycle_flow import build_lifecycle_flow_response
from ibkr_api.universe.screener import build_screener_proxy_response
from ibkr_api.universe.today_targets import build_today_targets_response
from ibkr_api.universe.targets import (
    build_screener_targets_upsert_response,
    build_target_remove_response,
    build_target_upsert_response,
)
from ibkr_api.universe.watchlist import (
    build_watchlist_eligibility_response,
    build_watchlist_remove_response,
    build_watchlist_upsert_response,
)


def register_universe_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    pb = deps["pb"]
    normalize_environment = deps["normalize_environment"]
    escape_filter_string = deps["escape_filter_string"]
    time_strings = deps["time_strings"]
    request_json_request = deps["request_json_request"]
    compute_base_url = deps["compute_base_url"]
    exports: dict[str, Any] = {}

    @app.route("/api/custom/ibkr/watchlist/upsert", methods=["POST"])
    def custom_ibkr_watchlist_upsert() -> Response:
        payload, status_code = build_watchlist_upsert_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            time_strings=time_strings,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_watchlist_upsert"] = custom_ibkr_watchlist_upsert

    @app.route("/api/custom/ibkr/watchlist/eligibility", methods=["POST"])
    def custom_ibkr_watchlist_eligibility() -> Response:
        payload, status_code = build_watchlist_eligibility_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_watchlist_eligibility"] = custom_ibkr_watchlist_eligibility

    @app.route("/api/custom/ibkr/watchlist/remove", methods=["POST"])
    def custom_ibkr_watchlist_remove() -> Response:
        payload, status_code = build_watchlist_remove_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            time_strings=time_strings,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_watchlist_remove"] = custom_ibkr_watchlist_remove

    @app.route("/api/custom/ibkr/targets/upsert", methods=["POST"])
    def custom_ibkr_targets_upsert() -> Response:
        payload, status_code = build_target_upsert_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            time_strings=time_strings,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_targets_upsert"] = custom_ibkr_targets_upsert

    @app.route("/api/custom/ibkr/targets/remove", methods=["POST"])
    def custom_ibkr_targets_remove() -> Response:
        payload, status_code = build_target_remove_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
            time_strings=time_strings,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_targets_remove"] = custom_ibkr_targets_remove

    @app.route("/api/custom/ibkr/screener", methods=["GET"])
    def custom_ibkr_screener() -> Response:
        payload, status_code = build_screener_proxy_response(
            payload=request.args.to_dict(flat=True),
            normalize_environment=normalize_environment,
            request_json_request=request_json_request,
            compute_base_url=compute_base_url,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_screener"] = custom_ibkr_screener

    @app.route("/api/custom/ibkr/today-targets", methods=["GET"])
    def custom_ibkr_today_targets() -> Response:
        query_payload = request.args.to_dict(flat=True)
        paginate = "page" in request.args or "per_page" in request.args or "page_size" in request.args
        payload, status_code = build_today_targets_response(
            pb,
            payload={
                **query_payload,
                "paginate": paginate,
                "per_page": query_payload.get("per_page") or query_payload.get("page_size"),
            },
            normalize_environment=normalize_environment,
            time_strings=time_strings,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_today_targets"] = custom_ibkr_today_targets

    @app.route("/api/custom/ibkr/active-window-progress", methods=["GET"])
    def custom_ibkr_active_window_progress() -> Response:
        payload, status_code = build_active_window_progress_response(
            pb,
            payload=request.args.to_dict(flat=True),
            normalize_environment=normalize_environment,
            time_strings=time_strings,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_active_window_progress"] = custom_ibkr_active_window_progress

    @app.route("/api/custom/ibkr/lifecycle-flow", methods=["GET"])
    def custom_ibkr_lifecycle_flow() -> Response:
        payload, status_code = build_lifecycle_flow_response(
            pb,
            payload=request.args.to_dict(flat=True),
            normalize_environment=normalize_environment,
            time_strings=time_strings,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_lifecycle_flow"] = custom_ibkr_lifecycle_flow

    @app.route("/api/custom/ibkr/fundamentals/refresh", methods=["POST"])
    def custom_ibkr_fundamentals_refresh() -> Response:
        payload, status_code = build_fundamentals_refresh_response(
            pb,
            payload=request.get_json(silent=True) or {},
            escape_filter_string=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_fundamentals_refresh"] = custom_ibkr_fundamentals_refresh

    @app.route("/api/custom/ibkr/fundamentals/list", methods=["GET"])
    @app.route("/api/custom/ibkr/fundamentals", methods=["GET"])
    def custom_ibkr_fundamentals_list() -> Response:
        payload, status_code = build_fundamentals_list_response(
            pb,
            payload=request.args.to_dict(flat=True),
            escape_filter_string=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_fundamentals_list"] = custom_ibkr_fundamentals_list

    @app.route("/api/custom/ibkr/screener/targets", methods=["POST"])
    def custom_ibkr_screener_targets() -> Response:
        payload, status_code = build_screener_targets_upsert_response(
            pb,
            payload=request.get_json(silent=True) or {},
            normalize_environment=normalize_environment,
            escape_filter_string=escape_filter_string,
        )
        response = jsonify(payload)
        return response if status_code == 200 else (response, status_code)

    exports["custom_ibkr_screener_targets"] = custom_ibkr_screener_targets

    return exports


__all__ = ["register_universe_routes"]
