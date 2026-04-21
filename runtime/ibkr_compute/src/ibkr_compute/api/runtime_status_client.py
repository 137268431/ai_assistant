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

_runtime_status_cache_lock = threading.Lock()
_runtime_status_cache: dict[str, Any] = {
    "expires_at": 0.0,
    "payload": None,
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


def get_remote_runtime_status(*, force_refresh: bool = False) -> dict:
    now = time.time()
    if not force_refresh:
        with _runtime_status_cache_lock:
            cached_payload = _runtime_status_cache.get("payload")
            expires_at = float(_runtime_status_cache.get("expires_at") or 0.0)
            if now < expires_at and isinstance(cached_payload, dict):
                return dict(cached_payload)

    payload: dict[str, Any] = {}
    try:
        response = requests.get(
            f"{get_runtime_internal_url()}/ibkr/status",
            timeout=RUNTIME_STATUS_TIMEOUT_SECONDS,
        )
        if response.ok:
            candidate = response.json()
            if isinstance(candidate, dict):
                payload = dict(candidate)
    except Exception:
        payload = {}

    with _runtime_status_cache_lock:
        _runtime_status_cache["payload"] = dict(payload)
        _runtime_status_cache["expires_at"] = time.time() + RUNTIME_STATUS_CACHE_TTL_SECONDS

    return dict(payload)
