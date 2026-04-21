from __future__ import annotations

from ibkr_compute.api.market.data_views import (
    build_contracts_search_response,
    build_ibkr_forming_bar_response,
    build_ibkr_ingest_close_response,
    build_ibkr_quotes_response,
)
from ibkr_compute.api.market.rules_views import build_rules_response
from ibkr_compute.api.market.screener_views import build_screener_response
from ibkr_compute.api.runtime_proxy import register_runtime_proxy_route, should_proxy_runtime_requests


def register_market_routes(app):
    if should_proxy_runtime_requests():
        register_runtime_proxy_route(app, "ibkr_quotes", "/ibkr/quotes", ["GET"])
        register_runtime_proxy_route(app, "ibkr_forming_bar", "/ibkr/quotes/forming_bar", ["GET"])
        register_runtime_proxy_route(app, "ibkr_ingest_close", "/ibkr/ingest/close", ["POST"])
        register_runtime_proxy_route(app, "contracts_search", "/contracts/search", ["GET"])
    else:
        @app.route("/ibkr/quotes", methods=["GET"])
        def ibkr_quotes():
            return build_ibkr_quotes_response()

        @app.route("/ibkr/quotes/forming_bar", methods=["GET"])
        def ibkr_forming_bar():
            return build_ibkr_forming_bar_response()

        @app.route("/ibkr/ingest/close", methods=["POST"])
        def ibkr_ingest_close():
            return build_ibkr_ingest_close_response()

        @app.route("/contracts/search", methods=["GET"])
        def contracts_search():
            return build_contracts_search_response()

    @app.route("/screener", methods=["GET"])
    def screener():
        return build_screener_response()

    @app.route("/ibkr/rules", methods=["GET"])
    def ibkr_rules():
        return build_rules_response()
