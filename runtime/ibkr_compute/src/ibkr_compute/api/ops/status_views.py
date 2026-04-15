from __future__ import annotations

from flask import jsonify

from ibkr_compute.api.ops.common import _build_engine_status_map, _build_runtime_summary
from ibkr_compute.api.route_runtime import get_app_module, get_requested_environment


def build_health_response():
    app_mod = get_app_module()
    requested_environment = get_requested_environment("live")
    return jsonify(
        {
            "ok": True,
            "status": "running",
            "engines": len(app_mod.engines),
            **_build_runtime_summary(app_mod),
            "backtest": app_mod.backtest_service.status(),
            "history_rebuild": app_mod.history_rebuild_manager.status(requested_environment),
        }
    )


def build_status_response():
    app_mod = get_app_module()
    requested_environment = get_requested_environment("live")
    engine_status = _build_engine_status_map(app_mod)
    return jsonify(
        {
            "ok": True,
            "compute_enabled": app_mod.cfg.compute_enabled,
            "compute_enabled_by_environment": {
                environment: app_mod.is_environment_compute_enabled(environment)
                for environment in app_mod.SUPPORTED_COMPUTE_ENVIRONMENTS
            },
            "supported_environments": app_mod.SUPPORTED_COMPUTE_ENVIRONMENTS,
            "default_environments": app_mod.DEFAULT_COMPUTE_ENVIRONMENTS,
            "total_engines": len(app_mod.engines),
            "ready_engines": sum(1 for engine in app_mod.engines.values() if engine.is_ready()),
            "engines": engine_status,
            "persisted_cursor_envs_loaded": sorted(app_mod.persistent_cursor_envs_loaded),
            "tracked_cursors": len(app_mod.last_processed_ms),
            **_build_runtime_summary(app_mod),
            "backtest": app_mod.backtest_service.status(),
            "history_rebuild": app_mod.history_rebuild_manager.status(requested_environment),
        }
    )
