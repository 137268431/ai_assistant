from __future__ import annotations

from ibkr_compute.api.route_runtime import get_app_module


def register_compute_routes(app):
    app.add_url_rule("/compute", endpoint="compute", view_func=lambda: get_app_module().compute(), methods=["POST"])
    app.add_url_rule(
        "/compute/prime",
        endpoint="compute_prime",
        view_func=lambda: get_app_module().compute_prime(),
        methods=["POST"],
    )
    app.add_url_rule("/scan", endpoint="scan", view_func=lambda: get_app_module().scan(), methods=["POST"])
    app.add_url_rule("/recompute", endpoint="recompute", view_func=lambda: get_app_module().recompute(), methods=["POST"])
