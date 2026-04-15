from __future__ import annotations

from ibkr_compute.api.chart.route_views import build_chart_compare_response, build_chart_timeline_response


def register_chart_routes(app):
    @app.route("/chart/timeline", methods=["POST"])
    def chart_timeline():
        return build_chart_timeline_response()

    @app.route("/chart/compare", methods=["POST"])
    def chart_compare():
        return build_chart_compare_response()
