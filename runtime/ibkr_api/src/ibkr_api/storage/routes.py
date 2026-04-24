from __future__ import annotations

from typing import Any

from ibkr_api.storage.bar_routes import register_storage_bar_routes
from ibkr_api.storage.data_quality_routes import register_storage_data_quality_routes
from ibkr_api.storage.indicator_routes import register_storage_indicator_routes
from ibkr_api.storage.ping_routes import register_storage_ping_routes
from ibkr_api.storage.scan_routes import register_storage_scan_routes


StorageDeps = dict[str, Any]


def register_storage_routes(app, *, deps: StorageDeps) -> dict[str, Any]:
    exports: dict[str, Any] = {}
    register_storage_ping_routes(app, deps=deps, exports=exports)
    register_storage_bar_routes(app, deps=deps, exports=exports)
    register_storage_indicator_routes(app, deps=deps, exports=exports)
    register_storage_scan_routes(app, deps=deps, exports=exports)
    register_storage_data_quality_routes(app, deps=deps, exports=exports)
    return exports


__all__ = ["register_storage_routes"]
