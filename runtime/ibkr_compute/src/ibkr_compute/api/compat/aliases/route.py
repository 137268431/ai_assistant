from __future__ import annotations


ROUTE_ALIASES = {
    "ibkr_compute.api.route_common": "ibkr_compute.api.shared.route_common",
    "ibkr_compute.api.route_request": "ibkr_compute.api.shared.route_request",
    "ibkr_compute.api.route_response": "ibkr_compute.api.shared.route_response",
    "ibkr_compute.api.route_runtime": "ibkr_compute.api.shared.route_runtime",
}


__all__ = ["ROUTE_ALIASES"]
