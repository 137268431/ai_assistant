from __future__ import annotations


RUNTIME_ALIASES = {
    "ibkr_compute.api.runtime_auth_views": "ibkr_compute.api.runtime.auth_views",
    "ibkr_compute.api.runtime_common": "ibkr_compute.api.runtime.common",
    "ibkr_compute.api.runtime_control_views": "ibkr_compute.api.runtime.control_views",
    "ibkr_compute.api.runtime_gateway_views": "ibkr_compute.api.runtime.gateway_views",
    "ibkr_compute.api.runtime_restore": "ibkr_compute.api.runtime.restore",
    "ibkr_compute.api.runtime_routes": "ibkr_compute.api.routes.runtime",
    "ibkr_compute.api.runtime_views": "ibkr_compute.api.runtime.views",
}


__all__ = ["RUNTIME_ALIASES"]
