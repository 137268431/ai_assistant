from __future__ import annotations


COMPUTE_ALIASES = {
    "ibkr_compute.api.compute_common": "ibkr_compute.api.compute.common",
    "ibkr_compute.api.compute_cursors": "ibkr_compute.api.compute.cursors",
    "ibkr_compute.api.compute_flush": "ibkr_compute.api.compute.flush",
    "ibkr_compute.api.compute_materialize": "ibkr_compute.api.compute.materialize",
    "ibkr_compute.api.compute_payloads": "ibkr_compute.api.compute.payloads",
    "ibkr_compute.api.compute_pipeline_views": "ibkr_compute.api.compute.pipeline_views",
    "ibkr_compute.api.compute_request": "ibkr_compute.api.compute.request",
    "ibkr_compute.api.compute_rollup": "ibkr_compute.api.compute.rollup",
    "ibkr_compute.api.compute_routes": "ibkr_compute.api.routes.compute",
    "ibkr_compute.api.compute_runtime_ops": "ibkr_compute.api.compute.runtime_ops",
    "ibkr_compute.api.compute_support": "ibkr_compute.api.compute.support",
}


__all__ = ["COMPUTE_ALIASES"]
