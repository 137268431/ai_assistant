from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests

from ibkr_compute.api.service_topology import get_compute_internal_url


COMPUTE_STATUS_TIMEOUT_SECONDS = max(
    0.5,
    float(os.environ.get("IBKR_COMPUTE_STATUS_TIMEOUT_SEC", "2.0")),
)
COMPUTE_STATUS_CACHE_TTL_SECONDS = max(
    0.0,
    float(os.environ.get("IBKR_COMPUTE_STATUS_CACHE_TTL_SEC", "1.0")),
)
COMPUTE_TRIGGER_TIMEOUT_SECONDS = max(
    1.0,
    float(os.environ.get("IBKR_COMPUTE_TRIGGER_TIMEOUT_SEC", "30.0")),
)

_compute_status_cache_lock = threading.Lock()
_compute_status_cache: dict[str, Any] = {
    "expires_at": 0.0,
    "payload": None,
}


def is_compute_status_payload(payload: dict | None) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if str(payload.get("service_profile") or "").strip().lower() == "compute":
        return True
    return any(key in payload for key in ("engines", "ready_engines", "total_engines", "compute_enabled"))


def get_remote_compute_status(*, force_refresh: bool = False) -> dict:
    now = time.time()
    if not force_refresh:
        with _compute_status_cache_lock:
            cached_payload = _compute_status_cache.get("payload")
            expires_at = float(_compute_status_cache.get("expires_at") or 0.0)
            if now < expires_at and isinstance(cached_payload, dict):
                return dict(cached_payload)

    payload: dict[str, Any] = {}
    try:
        response = requests.get(
            f"{get_compute_internal_url()}/status",
            timeout=COMPUTE_STATUS_TIMEOUT_SECONDS,
        )
        if response.ok:
            candidate = response.json()
            if isinstance(candidate, dict):
                payload = dict(candidate)
    except Exception:
        payload = {}

    with _compute_status_cache_lock:
        _compute_status_cache["payload"] = dict(payload)
        _compute_status_cache["expires_at"] = time.time() + COMPUTE_STATUS_CACHE_TTL_SECONDS

    return dict(payload)


def trigger_remote_compute(payload: dict | None = None) -> dict:
    try:
        response = requests.post(
            f"{get_compute_internal_url()}/compute",
            json=payload or {},
            timeout=COMPUTE_TRIGGER_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    candidate: dict[str, Any] = {}
    try:
        json_payload = response.json()
        if isinstance(json_payload, dict):
            candidate = dict(json_payload)
    except Exception:
        candidate = {}

    if candidate:
        if response.ok or "status_code" in candidate:
            return candidate
        return {
            **candidate,
            "ok": bool(candidate.get("ok", False)),
            "status_code": response.status_code,
        }

    return {
        "ok": False,
        "status_code": response.status_code,
        "error": str(response.text or "").strip() or f"http_{response.status_code}",
    }
