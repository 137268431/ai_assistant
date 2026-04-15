from __future__ import annotations

from ibkr_compute.api.route_runtime import get_app_module


def register_legacy_proxy_routes(app):
    app.add_url_rule(
        "/api/collections/<path:subpath>",
        endpoint="proxy_legacy_pb_collections",
        view_func=lambda subpath: get_app_module().proxy_legacy_pb_collections(subpath),
        methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    )
