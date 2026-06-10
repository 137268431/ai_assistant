from __future__ import annotations

import threading
import time

from flask import jsonify, request

from ibkr_compute.api.compute.prime import build_multi_timeframe_readiness
from ibkr_compute.api.ops.common import _build_engine_status_map, _build_runtime_summary, _snapshot_engine_items
from ibkr_compute.api.route_runtime import get_app_module, get_requested_environment
from ibkr_compute.api.service_topology import build_service_topology, get_runtime_mode, get_service_profile
from ibkr_compute.api.shared.route_request import coerce_request_bool
from ibkr_compute.api.startup_preload import get_compute_startup_preload_state
from ibkr_compute.observability.trade_result_metrics import (
    refresh_trade_result_today_metrics,
    trade_result_metrics_enabled,
    trade_result_metrics_interval_sec,
)


_TRADE_RESULT_METRICS_LOCK = threading.Lock()
_TRADE_RESULT_METRICS_LAST_REFRESH: dict[str, float] = {}
_TRADE_RESULT_METRICS_LAST_SUMMARY: dict[str, dict] = {}


def _full_health_requested() -> bool:
    try:
        args = request.args or {}
    except Exception:
        return False
    return coerce_request_bool(args.get("full"), False) or coerce_request_bool(args.get("deep"), False)


def _health_requested_symbols(app_mod, *, full: bool) -> list[str]:
    symbols = _requested_status_symbols(app_mod)
    if full or symbols:
        return symbols
    return []


def _lite_omitted_status(reason: str) -> dict:
    return {
        "status": "omitted",
        "reason": reason,
    }


def _safe_service_status(service, *, full: bool, reason: str) -> dict:
    if not full:
        return _lite_omitted_status(reason)
    try:
        status_fn = getattr(service, "status", None)
        if callable(status_fn):
            return status_fn()
    except Exception as exc:
        return {"status": "unknown", "error": str(exc)}
    return {"status": "unavailable"}


def _safe_history_rebuild_status(app_mod, requested_environment: str, *, full: bool) -> dict:
    manager = getattr(app_mod, "history_rebuild_manager", None)
    if not full:
        return _lite_omitted_status("health_lite")
    try:
        status_fn = getattr(manager, "status", None)
        if callable(status_fn):
            return status_fn(requested_environment)
    except Exception as exc:
        return {"status": "unknown", "environment": requested_environment, "error": str(exc)}
    return {"status": "unavailable", "environment": requested_environment}


def _lite_runtime_status_payload(app_mod, requested_environment: str) -> dict:
    payload: dict[str, object] = {
        "ok": True,
        "environment": requested_environment,
    }
    if get_service_profile() != "runtime":
        return payload
    service = getattr(app_mod, "_ibkr_service", None)
    if service is None:
        return payload
    gateway_manager = getattr(service, "gateway_manager", None)
    gateway_status = getattr(gateway_manager, "status", None)
    if callable(gateway_status):
        try:
            payload["gateway"] = gateway_status()
        except Exception as exc:
            payload["gateway"] = {"running": False, "reachable": False, "error": str(exc)}
    session_keeper = getattr(service, "session_keeper", None)
    session_status = getattr(session_keeper, "status", None)
    if callable(session_status):
        try:
            payload["session"] = session_status()
        except Exception as exc:
            payload["session"] = {"authenticated": False, "error": str(exc)}
    return payload


def _build_topology_payload(app_mod, requested_environment: str) -> dict:
    if _skip_runtime_status_lookup():
        payload = build_service_topology(
            service_status={"environment": requested_environment},
            fetch_runtime_status=False,
        )
        payload["runtime_status_lookup_skipped"] = True
        return payload

    resolver = getattr(app_mod, "_get_runtime_status_snapshot", None)
    if callable(resolver):
        try:
            payload = resolver(requested_environment) or {}
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload:
            return build_service_topology(service_status=payload)
    return build_service_topology()


def _skip_runtime_status_lookup() -> bool:
    try:
        args = request.args or {}
    except Exception:
        return False
    if coerce_request_bool(args.get("skip_runtime_status"), False):
        return True
    if coerce_request_bool(args.get("skip_runtime"), False):
        return True
    return str(args.get("topology") or "").strip().lower() in {"local", "static", "shallow"}


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


def _safe_backtest_preload_queue(app_mod) -> dict:
    coordinator = getattr(app_mod, "backtest_preload_coordinator", None)
    if coordinator is None or not hasattr(coordinator, "status"):
        return {"ok": True, "available": False, "pending": 0, "inflight": 0, "failed": 0}
    try:
        payload = coordinator.status()
        payload["available"] = True
        return payload
    except Exception as exc:
        return {"ok": False, "available": True, "error": str(exc), "pending": 0, "inflight": 0, "failed": 0}


def _safe_refresh_trade_result_metrics(app_mod, requested_environment: str) -> dict:
    broker_environment = str(
        getattr(app_mod, "BROKER_MODE", "")
        or requested_environment
        or "paper"
    ).strip().lower() or "paper"
    config = getattr(app_mod, "cfg", None)
    if not trade_result_metrics_enabled(config, broker_environment, True):
        return {"enabled": False, "environment": broker_environment}

    interval = trade_result_metrics_interval_sec(config, broker_environment, 60.0)
    now = time.time()
    with _TRADE_RESULT_METRICS_LOCK:
        last_refresh = float(_TRADE_RESULT_METRICS_LAST_REFRESH.get(broker_environment) or 0.0)
        if now - last_refresh < interval:
            return dict(_TRADE_RESULT_METRICS_LAST_SUMMARY.get(broker_environment) or {})
        _TRADE_RESULT_METRICS_LAST_REFRESH[broker_environment] = now

    try:
        summary = refresh_trade_result_today_metrics(
            getattr(app_mod, "pb", None),
            environment=broker_environment,
        )
    except Exception as exc:
        summary = {"ok": False, "environment": broker_environment, "error": str(exc)[:240]}

    with _TRADE_RESULT_METRICS_LOCK:
        _TRADE_RESULT_METRICS_LAST_SUMMARY[broker_environment] = dict(summary or {})
    return dict(summary or {})


def build_health_response():
    app_mod = get_app_module()
    requested_environment = get_requested_environment("live")
    trade_result_metrics = _safe_refresh_trade_result_metrics(app_mod, requested_environment)
    full_health = _full_health_requested()
    service_profile = get_service_profile()
    requested_symbols = _health_requested_symbols(app_mod, full=full_health)
    multi_timeframe_readiness = (
        _safe_multi_timeframe_readiness(app_mod, requested_environment, requested_symbols)
        if full_health or requested_symbols
        else _lite_omitted_status("health_lite")
    )
    if full_health:
        lite_runtime_status = {}
        service_topology = _build_topology_payload(app_mod, requested_environment)
    else:
        lite_runtime_status = _lite_runtime_status_payload(app_mod, requested_environment)
        service_topology = build_service_topology(
            service_status=lite_runtime_status,
            fetch_runtime_status=False,
        )
    return jsonify(
        {
            "ok": True,
            "status": "running",
            "health_mode": "full" if full_health else "lite",
            "service_profile": service_profile,
            "runtime_mode": get_runtime_mode(),
            "engines": len(app_mod.engines),
            "compute_startup_preload": get_compute_startup_preload_state(app_mod),
            "multi_timeframe_readiness": multi_timeframe_readiness,
            "bar_repair_queue": _safe_bar_repair_queue(app_mod),
            "backtest_preload_queue": _safe_backtest_preload_queue(app_mod),
            "trade_result_metrics": trade_result_metrics,
            **_build_runtime_summary(app_mod),
            "backtest": _safe_service_status(
                getattr(app_mod, "backtest_service", None),
                full=full_health,
                reason="health_lite",
            ),
            "history_rebuild": _safe_history_rebuild_status(
                app_mod,
                requested_environment,
                full=full_health,
            ),
            **(
                {
                    "gateway": lite_runtime_status.get("gateway"),
                    "session": lite_runtime_status.get("session"),
                }
                if not full_health and service_profile == "runtime"
                else {}
            ),
            "service_topology": service_topology,
        }
    )


def build_status_response():
    app_mod = get_app_module()
    requested_environment = get_requested_environment("live")
    trade_result_metrics = _safe_refresh_trade_result_metrics(app_mod, requested_environment)
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
            "backtest_preload_queue": _safe_backtest_preload_queue(app_mod),
            "trade_result_metrics": trade_result_metrics,
            **_build_runtime_summary(app_mod),
            "backtest": app_mod.backtest_service.status(),
            "history_rebuild": app_mod.history_rebuild_manager.status(requested_environment),
            "service_topology": _build_topology_payload(app_mod, requested_environment),
        }
    )
