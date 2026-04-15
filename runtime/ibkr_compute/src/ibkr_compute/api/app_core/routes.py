from __future__ import annotations

from ibkr_compute.api.routes.account import register_account_routes
from ibkr_compute.api.routes.chart import register_chart_routes
from ibkr_compute.api.routes.compute import register_compute_routes
from ibkr_compute.api.routes.legacy_proxy import register_legacy_proxy_routes
from ibkr_compute.api.routes.market import register_market_routes
from ibkr_compute.api.routes.ops import register_ops_routes
from ibkr_compute.api.routes.runtime import register_runtime_routes


def register_api_routes(app):
    register_legacy_proxy_routes(app)
    register_compute_routes(app)
    register_chart_routes(app)
    register_market_routes(app)
    register_ops_routes(app)
    register_runtime_routes(app)
    register_account_routes(app)
