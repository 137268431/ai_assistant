from __future__ import annotations


APP_CORE_ALIASES = {
    "ibkr_compute.api.app_bootstrap": "ibkr_compute.api.app_core.bootstrap",
    "ibkr_compute.api.app_entrypoints": "ibkr_compute.api.app_core.entrypoints",
    "ibkr_compute.api.app_exports": "ibkr_compute.api.app_core.exports",
    "ibkr_compute.api.app_exports_compute": "ibkr_compute.api.app_core.exports_compute",
    "ibkr_compute.api.app_exports_runtime": "ibkr_compute.api.app_core.exports_runtime",
    "ibkr_compute.api.app_exports_support": "ibkr_compute.api.app_core.exports_support",
    "ibkr_compute.api.app_logging": "ibkr_compute.api.app_core.logging",
    "ibkr_compute.api.app_market_time": "ibkr_compute.api.support.market_time",
    "ibkr_compute.api.app_routes": "ibkr_compute.api.app_core.routes",
    "ibkr_compute.api.app_support": "ibkr_compute.api.support.app_support",
    "ibkr_compute.api.app_symbols": "ibkr_compute.api.support.symbols",
}


__all__ = ["APP_CORE_ALIASES"]
