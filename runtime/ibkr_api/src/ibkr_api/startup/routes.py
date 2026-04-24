from __future__ import annotations

from typing import Any

from ibkr_api.startup.progress_routes import register_startup_progress_routes
from ibkr_api.startup.status_routes import register_startup_status_routes


StartupDeps = dict[str, Any]


def register_startup_routes(app, *, deps: StartupDeps) -> dict[str, Any]:
    exports: dict[str, Any] = {}
    register_startup_progress_routes(app, deps=deps, exports=exports)
    register_startup_status_routes(app, deps=deps, exports=exports)
    return exports


__all__ = ["register_startup_routes"]
