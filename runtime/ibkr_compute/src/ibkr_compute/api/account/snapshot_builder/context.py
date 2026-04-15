from __future__ import annotations

import time

from ibkr_compute.api.account.live import _api_app


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


def build_snapshot_context(service) -> dict:
    api_app = _api_app()
    runtime_environment = api_app._ibkr_service_environment(service)
    service_status = service.status() if hasattr(service, "status") else {}
    account_id = _resolve_snapshot_account_id(api_app, service)
    return {
        "api_app": api_app,
        "runtime_environment": runtime_environment,
        "service_status": service_status,
        "account_id": account_id,
        "cache_key": (runtime_environment, account_id),
    }


def load_cached_snapshot(api_app, cache_key: tuple[str, str]):
    now = time.time()
    with api_app.ibkr_account_snapshot_cache_lock:
        cached_entry = api_app.ibkr_account_snapshot_cache.get(cache_key)
        if cached_entry and float(cached_entry.get("expires_at", 0) or 0) > now:
            return dict(cached_entry.get("payload") or {})
        if cached_entry:
            api_app.ibkr_account_snapshot_cache.pop(cache_key, None)
    return None


def store_cached_snapshot(api_app, cache_key: tuple[str, str], payload: dict):
    cache_expires_at = time.time() + api_app.IBKR_ACCOUNT_SNAPSHOT_TTL_SECONDS
    with api_app.ibkr_account_snapshot_cache_lock:
        api_app.ibkr_account_snapshot_cache[cache_key] = {
            "expires_at": cache_expires_at,
            "payload": payload,
        }


__all__ = [
    "build_snapshot_context",
    "load_cached_snapshot",
    "store_cached_snapshot",
]
