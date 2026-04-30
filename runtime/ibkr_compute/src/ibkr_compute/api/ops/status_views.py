from __future__ import annotations

from flask import jsonify, request

from ibkr_compute.api.compute.prime import build_multi_timeframe_readiness
from ibkr_compute.api.ops.common import _build_engine_status_map, _build_runtime_summary, _snapshot_engine_items
from ibkr_compute.api.route_runtime import get_app_module, get_requested_environment
from ibkr_compute.api.service_topology import build_service_topology, get_runtime_mode, get_service_profile
from ibkr_compute.api.shared.route_request import coerce_request_bool
from ibkr_compute.api.startup_preload import get_compute_startup_preload_state


def _build_topology_payload(app_mod, requested_environment: str) -> dict:
    resolver = getattr(app_mod, "_get_runtime_status_snapshot", None)
    if callable(resolver):
        try:
            payload = resolver(requested_environment) or {}
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload:
            return build_service_topology(service_status=payload)
    return build_service_topology()


def _include_engines_in_status() -> bool:
    return coerce_request_bool(request.args.get("full"), False) or not coerce_request_bool(request.args.get("lite"), True)


def _requested_status_symbols(app_mod) -> list[str]:
    raw_value = request.args.get("symbols") or request.args.get("symbol") or ""
    try:
        normalizer = getattr(app_mod, "normalize_symbol_csv", None)
        if callable(normalizer):
            return normalizer(raw_value)
        return app_mod.normalize_symbols(str(raw_value or "").replace("\n", ",").split(","))
    except Exception:
        normalized = []
        seen = set()
        for item in str(raw_value or "").replace("\n", ",").split(","):
            symbol = str(item or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append(symbol)
        return normalized


def _filter_engine_items(engine_items, symbols: list[str] | None):
    symbol_set = {str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()}
    if not symbol_set:
        return list(engine_items or [])
    return [
        (key, engine)
        for key, engine in (engine_items or [])
        if isinstance(key, tuple)
        and len(key) >= 2
        and str(key[1] or "").strip().upper() in symbol_set
    ]


def _safe_multi_timeframe_readiness(app_mod, requested_environment: str, symbols: list[str] | None = None) -> dict:
    try:
        # Storage readiness is precise but does many indexed lookups; keep broad
        # status probes fast and let warmup ask for the small active subscription set.
        include_storage = bool(symbols) and len(symbols) <= 30
        return build_multi_timeframe_readiness(
            app_mod,
            environment=requested_environment,
            symbols=symbols,
            intervals=["5m"],
            include_storage=include_storage,
        )
    except Exception as exc:
        return {
            "environment": requested_environment,
            "status": "unknown",
            "error": str(exc),
        }


def _safe_bar_repair_queue(app_mod) -> dict:
    coordinator = getattr(app_mod, "bar_repair_coordinator", None)
    if coordinator is None or not hasattr(coordinator, "status"):
        return {"ok": True, "available": False, "pending": 0, "inflight": 0, "failed": 0}
    try:
        payload = coordinator.status()
        payload["available"] = True
        return payload
    except Exception as exc:
        return {"ok": False, "available": True, "error": str(exc), "pending": 0, "inflight": 0, "failed": 0}


def build_health_response():
    app_mod = get_app_module()
    requested_environment = get_requested_environment("live")
    requested_symbols = _requested_status_symbols(app_mod)
    multi_timeframe_readiness = _safe_multi_timeframe_readiness(app_mod, requested_environment, requested_symbols)
    return jsonify(
        {
            "ok": True,
            "status": "running",
            "service_profile": get_service_profile(),
            "runtime_mode": get_runtime_mode(),
            "engines": len(app_mod.engines),
            "compute_startup_preload": get_compute_startup_preload_state(app_mod),
            "multi_timeframe_readiness": multi_timeframe_readiness,
            "bar_repair_queue": _safe_bar_repair_queue(app_mod),
            **_build_runtime_summary(app_mod),
            "backtest": app_mod.backtest_service.status(),
            "history_rebuild": app_mod.history_rebuild_manager.status(requested_environment),
            "service_topology": _build_topology_payload(app_mod, requested_environment),
        }
    )


def build_status_response():
    app_mod = get_app_module()
    requested_environment = get_requested_environment("live")
    include_engines = _include_engines_in_status()
    requested_symbols = _requested_status_symbols(app_mod)
    engine_items = _filter_engine_items(_snapshot_engine_items(app_mod, blocking=False), requested_symbols)
    engine_status = _build_engine_status_map(engine_items) if include_engines else {}
    multi_timeframe_readiness = _safe_multi_timeframe_readiness(app_mod, requested_environment, requested_symbols)
    return jsonify(
        {
            "ok": True,
            "service_profile": get_service_profile(),
            "runtime_mode": get_runtime_mode(),
            "compute_enabled": app_mod.cfg.compute_enabled,
            "compute_enabled_by_environment": {
                environment: app_mod.is_environment_compute_enabled(environment)
                for environment in app_mod.SUPPORTED_COMPUTE_ENVIRONMENTS
            },
            "supported_environments": app_mod.SUPPORTED_COMPUTE_ENVIRONMENTS,
            "default_environments": app_mod.DEFAULT_COMPUTE_ENVIRONMENTS,
            "total_engines": len(engine_items),
            "ready_engines": sum(1 for _, engine in engine_items if engine.is_ready()),
            "engines_included": bool(include_engines),
            "status_mode": "full" if include_engines else "lite",
            "engines": engine_status,
            "persisted_cursor_envs_loaded": sorted(app_mod.persistent_cursor_envs_loaded),
            "tracked_cursors": len(app_mod.last_processed_ms),
            "compute_startup_preload": get_compute_startup_preload_state(app_mod),
            "multi_timeframe_readiness": multi_timeframe_readiness,
            "bar_repair_queue": _safe_bar_repair_queue(app_mod),
            **_build_runtime_summary(app_mod),
            "backtest": app_mod.backtest_service.status(),
            "history_rebuild": app_mod.history_rebuild_manager.status(requested_environment),
            "service_topology": _build_topology_payload(app_mod, requested_environment),
        }
    )
