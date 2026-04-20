"""Common helpers for ops API endpoints."""

from __future__ import annotations

import time
from datetime import datetime

from ibkr_compute.api.route_request import coerce_request_int
from ibkr_compute.api.route_runtime import get_app_module


def _build_runtime_summary(app_mod) -> dict:
    return {
        "compute_count": app_mod.compute_count,
        "error_count": app_mod.error_count,
        "last_compute": datetime.fromtimestamp(app_mod.last_compute_time).isoformat() if app_mod.last_compute_time else None,
        "last_scan": datetime.fromtimestamp(app_mod.last_scan_time).isoformat() if app_mod.last_scan_time else None,
        "uptime_s": round(time.time() - app_mod._start_time, 1),
    }


def _build_engine_status_map(app_mod) -> dict:
    engine_status = {}
    for (environment, symbol, interval), engine in app_mod.engines.items():
        key = f"{environment}/{symbol}/{interval}"
        engine_status[key] = {
            "environment": environment,
            "bar_count": engine.bar_count,
            "is_ready": engine.is_ready(),
            "last_bar_time_ms": engine.last_bar_time_ms,
            "last_close": engine.get_snapshot().get("close"),
        }
    return engine_status


def _resolve_data_quality_symbols(service, payload: dict) -> list[str]:
    app_mod = get_app_module()
    requested_symbols = app_mod.normalize_symbols(payload.get("symbols"))
    if requested_symbols:
        return requested_symbols

    scan_scope = str(payload.get("scan_scope") or "manual").strip().lower()
    batch_size = coerce_request_int(payload.get("batch_size"), 8, minimum=1)
    if scan_scope == "active_target":
        try:
            status_payload = service.status()
            return app_mod.normalize_symbols(((status_payload.get("market_universe") or {}).get("active_target_symbols") or []))
        except Exception:
            return []
    if scan_scope == "watchlist_full":
        try:
            service.config.refresh()
            service._refresh_watchlist_pool(force=True)
            return app_mod.normalize_symbols(service._watchlist_symbols or [])
        except Exception:
            return []
    if scan_scope == "watchlist":
        try:
            service.config.refresh()
            service._refresh_watchlist_pool(force=True)
            return app_mod.normalize_symbols((service._watchlist_symbols or [])[:batch_size])
        except Exception:
            return []
    return []
