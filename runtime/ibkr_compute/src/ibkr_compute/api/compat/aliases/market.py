from __future__ import annotations


MARKET_ALIASES = {
    "ibkr_compute.api.market_data_views": "ibkr_compute.api.market.data_views",
    "ibkr_compute.api.market_routes": "ibkr_compute.api.routes.market",
    "ibkr_compute.api.market_screener_views": "ibkr_compute.api.market.screener_views",
    "ibkr_compute.api.screener_support": "ibkr_compute.api.market.support",
}


__all__ = ["MARKET_ALIASES"]
