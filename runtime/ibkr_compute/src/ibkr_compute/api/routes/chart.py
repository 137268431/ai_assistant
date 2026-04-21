from __future__ import annotations

from ibkr_compute.api.chart.route_views import build_chart_compare_response, build_chart_timeline_response
from ibkr_compute.api.runtime_proxy import register_runtime_proxy_route, should_proxy_runtime_requests


def register_chart_routes(app):
    @app.route("/chart/timeline", methods=["POST"])
    def chart_timeline():
        return build_chart_timeline_response()

    if should_proxy_runtime_requests():
        register_runtime_proxy_route(app, "chart_compare", "/chart/compare", ["POST"])
    else:
        @app.route("/chart/compare", methods=["POST"])
        def chart_compare():
            return build_chart_compare_response()
