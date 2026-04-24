from __future__ import annotations

from typing import Any

from ibkr_api.system.core_routes import register_system_core_routes
from ibkr_api.system.job_routes import register_system_job_routes
from ibkr_api.system.read_routes import register_system_read_routes



def register_system_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    exports: dict[str, Any] = {}
    register_system_core_routes(app, deps=deps, exports=exports)
    register_system_read_routes(app, deps=deps, exports=exports)
    register_system_job_routes(app, deps=deps, exports=exports)
    return exports


__all__ = ["register_system_routes"]
