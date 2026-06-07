from __future__ import annotations

import time
import threading

from ibkr_compute.api.account.live import _api_app
from ibkr_compute.api.shared.service_status import get_service_status_snapshot


def _resolve_snapshot_account_id(api_app, service) -> str:
    use_paper = api_app._ibkr_service_uses_paper_account(service)
    account_id = ""
    if hasattr(service, "order_placer"):
        try:
            account_id = str(service.order_placer.get_active_account_id(use_paper=use_paper) or "").strip()
        except Exception:
            account_id = ""
    if not account_id and hasattr(service, "order_lifecycle"):
        account_id = str(getattr(service.order_lifecycle, "account_id", "") or "").strip()
    return account_id


def _component_status(service, attr_name: str) -> dict:
    component = getattr(service, attr_name, None)
    status_fn = getattr(component, "status", None)
    if not callable(status_fn):
        return {}
    try:
        payload = status_fn()
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _build_fast_snapshot_status(service) -> dict:
    return {
        "gateway": _component_status(service, "gateway_manager"),
        "session": _component_status(service, "session_keeper"),
        "websocket": _component_status(service, "ws_client"),
    }


def build_snapshot_context(service, *, include_pnl: bool = True, fast_status: bool = False) -> dict:
    api_app = _api_app()
    runtime_environment = api_app._ibkr_service_environment(service)
    service_status = _build_fast_snapshot_status(service) if fast_status else get_service_status_snapshot(service)
    account_id = _resolve_snapshot_account_id(api_app, service)
    include_pnl_flag = bool(include_pnl)
    return {
        "api_app": api_app,
        "runtime_environment": runtime_environment,
        "service_status": service_status,
        "account_id": account_id,
        "include_pnl": include_pnl_flag,
        "cache_key": (runtime_environment, account_id, include_pnl_flag),
    }


def _snapshot_cache_ttl_seconds(api_app) -> float:
    try:
        return max(1.0, float(getattr(api_app, "IBKR_ACCOUNT_SNAPSHOT_TTL_SECONDS", 5.0) or 5.0))
    except Exception:
        return 5.0


def _snapshot_cache_stale_seconds(api_app) -> float:
    try:
        fallback = max(60.0, _snapshot_cache_ttl_seconds(api_app) * 12.0)
        return max(_snapshot_cache_ttl_seconds(api_app), float(getattr(api_app, "IBKR_ACCOUNT_SNAPSHOT_STALE_SECONDS", fallback) or fallback))
    except Exception:
        return 60.0


def _ensure_snapshot_cache_state(api_app) -> None:
    if not hasattr(api_app, "ibkr_account_snapshot_cache"):
        api_app.ibkr_account_snapshot_cache = {}
    if not hasattr(api_app, "ibkr_account_snapshot_cache_lock"):
        api_app.ibkr_account_snapshot_cache_lock = threading.Lock()
    if not hasattr(api_app, "ibkr_account_snapshot_refresh_locks"):
        api_app.ibkr_account_snapshot_refresh_locks = {}


def _snapshot_cache_payload(entry: dict, *, now: float, stale: bool, state: str) -> dict:
    payload = dict(entry.get("payload") or {})
    payload["cache_state"] = state
    payload["stale"] = bool(stale)
    payload["cache_age_s"] = round(max(0.0, now - float(entry.get("stored_at", now) or now)), 3)
    refresh_error = str(entry.get("refresh_error") or "").strip()
    if refresh_error:
        payload["refresh_error"] = refresh_error
    return payload


def load_cached_snapshot(api_app, cache_key: tuple[str, str, bool], *, allow_stale: bool = False):
    _ensure_snapshot_cache_state(api_app)
    now = time.time()
    with api_app.ibkr_account_snapshot_cache_lock:
        cached_entry = api_app.ibkr_account_snapshot_cache.get(cache_key)
        if not cached_entry:
            return None
        if float(cached_entry.get("fresh_until", cached_entry.get("expires_at", 0)) or 0) > now:
            return _snapshot_cache_payload(cached_entry, now=now, stale=False, state="fresh")
        if allow_stale and float(cached_entry.get("stale_until", cached_entry.get("expires_at", 0)) or 0) > now:
            return _snapshot_cache_payload(cached_entry, now=now, stale=True, state="stale")
        if float(cached_entry.get("stale_until", cached_entry.get("expires_at", 0)) or 0) <= now:
            api_app.ibkr_account_snapshot_cache.pop(cache_key, None)
    return None


def store_cached_snapshot(api_app, cache_key: tuple[str, str, bool], payload: dict):
    _ensure_snapshot_cache_state(api_app)
    now = time.time()
    ttl_seconds = _snapshot_cache_ttl_seconds(api_app)
    stale_seconds = _snapshot_cache_stale_seconds(api_app)
    cached_payload = dict(payload or {})
    cached_payload["cache_state"] = "fresh"
    cached_payload["stale"] = False
    cached_payload.pop("refresh_error", None)
    with api_app.ibkr_account_snapshot_cache_lock:
        api_app.ibkr_account_snapshot_cache[cache_key] = {
            "stored_at": now,
            "fresh_until": now + ttl_seconds,
            "stale_until": now + stale_seconds,
            "expires_at": now + ttl_seconds,
            "payload": cached_payload,
        }


def mark_cached_snapshot_refresh_error(api_app, cache_key: tuple[str, str, bool], error: str) -> None:
    _ensure_snapshot_cache_state(api_app)
    message = str(error or "").strip()
    if not message:
        return
    with api_app.ibkr_account_snapshot_cache_lock:
        cached_entry = api_app.ibkr_account_snapshot_cache.get(cache_key)
        if cached_entry:
            cached_entry["refresh_error"] = message


def get_snapshot_refresh_lock(api_app, cache_key: tuple[str, str, bool]) -> threading.Lock:
    _ensure_snapshot_cache_state(api_app)
    with api_app.ibkr_account_snapshot_cache_lock:
        lock = api_app.ibkr_account_snapshot_refresh_locks.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            api_app.ibkr_account_snapshot_refresh_locks[cache_key] = lock
        return lock


__all__ = [
    "build_snapshot_context",
    "get_snapshot_refresh_lock",
    "load_cached_snapshot",
    "mark_cached_snapshot_refresh_error",
    "store_cached_snapshot",
]
