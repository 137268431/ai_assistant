from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests


DEFAULT_RUNTIME_INTERNAL_URL = "http://127.0.0.1:5101"
RUNTIME_STATUS_TIMEOUT_SECONDS = max(
    0.5,
    float(os.environ.get("IBKR_RUNTIME_STATUS_TIMEOUT_SEC", "2.0")),
)
RUNTIME_STATUS_CACHE_TTL_SECONDS = max(
    0.0,
    float(os.environ.get("IBKR_RUNTIME_STATUS_CACHE_TTL_SEC", "1.0")),
)

_runtime_status_cache_lock = threading.RLock()
_runtime_status_cache: dict[str, Any] = {
    "expires_at": 0.0,
    "payload": None,
    "last_success_at": 0.0,
    "last_success_payload": None,
}


def get_runtime_internal_url(default: str | None = None) -> str:
    text = str(os.environ.get("IBKR_RUNTIME_INTERNAL_URL") or "").strip() or (default or DEFAULT_RUNTIME_INTERNAL_URL)
    return text.rstrip("/")


def is_runtime_status_payload(payload: dict | None) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if str(payload.get("service_profile") or "").strip().lower() == "runtime":
        return True
    return any(key in payload for key in ("session", "gateway", "websocket", "auth_recovery", "runtime_phase"))


def _runtime_status_stale_payload(error: str = "") -> dict:
    with _runtime_status_cache_lock:
        payload = _runtime_status_cache.get("last_success_payload")
        last_success_at = float(_runtime_status_cache.get("last_success_at") or 0.0)
    if not isinstance(payload, dict) or not payload:
        return {}
    stale_payload = dict(payload)
    stale_payload["runtime_status_stale"] = True
    stale_payload["runtime_status_cache_age_s"] = round(max(0.0, time.time() - last_success_at), 3) if last_success_at else 0.0
    if last_success_at:
        stale_payload["runtime_status_last_success_at"] = last_success_at
    if error:
        stale_payload["runtime_status_error"] = str(error)[:500]
    return stale_payload


def get_remote_runtime_status(*, force_refresh: bool = False) -> dict:
    now = time.time()
    if not force_refresh:
        with _runtime_status_cache_lock:
            cached_payload = _runtime_status_cache.get("payload")
            expires_at = float(_runtime_status_cache.get("expires_at") or 0.0)
            if now < expires_at and isinstance(cached_payload, dict):
                return dict(cached_payload)

    payload: dict[str, Any] = {}
    error = ""
    try:
        response = requests.get(
            f"{get_runtime_internal_url()}/ibkr/status",
            params={"skip_compute_status": "1"},
            timeout=RUNTIME_STATUS_TIMEOUT_SECONDS,
        )
        if response.ok:
            candidate = response.json()
            if isinstance(candidate, dict):
                payload = dict(candidate)
        else:
            error = f"http_{int(response.status_code)}"
    except Exception as exc:
        error = str(exc)

    with _runtime_status_cache_lock:
        if payload:
            _runtime_status_cache["payload"] = dict(payload)
            _runtime_status_cache["last_success_payload"] = dict(payload)
            _runtime_status_cache["last_success_at"] = time.time()
        else:
            payload = _runtime_status_stale_payload(error)
            _runtime_status_cache["payload"] = dict(payload)
        _runtime_status_cache["expires_at"] = time.time() + RUNTIME_STATUS_CACHE_TTL_SECONDS

    return dict(payload)
