from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests

from ibkr_compute.api.service_topology import get_compute_internal_url


COMPUTE_STATUS_TIMEOUT_SECONDS = max(
    0.5,
    float(os.environ.get("IBKR_COMPUTE_STATUS_TIMEOUT_SEC", "8.0")),
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
    "cache_key": None,
}


def is_compute_status_payload(payload: dict | None) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if str(payload.get("service_profile") or "").strip().lower() == "compute":
        return True
    return any(
        key in payload
        for key in (
            "engines",
            "ready_engines",
            "total_engines",
            "compute_enabled",
            "multi_timeframe_readiness",
        )
    )


def _normalize_symbol_param(symbols) -> list[str]:
    if symbols is None:
        return []
    source = symbols if isinstance(symbols, (list, tuple, set)) else [symbols]
    normalized = []
    seen = set()
    for value in source:
        for item in str(value or "").replace("\n", ",").split(","):
            symbol = str(item or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append(symbol)
    return normalized


def get_remote_compute_status(
    *,
    force_refresh: bool = False,
    symbols=None,
    include_engines: bool = False,
) -> dict:
    requested_symbols = _normalize_symbol_param(symbols)
    cache_key = (tuple(requested_symbols), bool(include_engines))
    now = time.time()
    if not force_refresh:
        with _compute_status_cache_lock:
            cached_payload = _compute_status_cache.get("payload")
            cached_key = _compute_status_cache.get("cache_key")
            expires_at = float(_compute_status_cache.get("expires_at") or 0.0)
            if cached_key == cache_key and now < expires_at and isinstance(cached_payload, dict):
                return dict(cached_payload)

    payload: dict[str, Any] = {}
    base_params = {"full": "1"} if include_engines else {"lite": "1"}
    if requested_symbols:
        base_params["symbols"] = ",".join(requested_symbols)
    fallback_params = {"full": "1"}
    if requested_symbols:
        fallback_params["symbols"] = ",".join(requested_symbols)
    query_plan = [base_params] if include_engines else [base_params, fallback_params]
    for params in query_plan:
        try:
            response = requests.get(
                f"{get_compute_internal_url()}/status",
                params=params,
                timeout=COMPUTE_STATUS_TIMEOUT_SECONDS,
            )
            if response.ok:
                candidate = response.json()
                if isinstance(candidate, dict) and candidate:
                    payload = dict(candidate)
                    break
        except Exception:
            payload = {}

    with _compute_status_cache_lock:
        _compute_status_cache["payload"] = dict(payload)
        _compute_status_cache["cache_key"] = cache_key
        _compute_status_cache["expires_at"] = time.time() + COMPUTE_STATUS_CACHE_TTL_SECONDS

    return dict(payload)


def _post_remote_compute_path(path: str, payload: dict | None = None) -> dict:
    safe_path = "/" + str(path or "").strip().lstrip("/")
    try:
        response = requests.post(
            f"{get_compute_internal_url()}{safe_path}",
            json=payload or {},
            timeout=COMPUTE_TRIGGER_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "compute_scan_submit_timeout" if safe_path == "/scan" else "compute_request_timeout",
            "retryable": True,
            "path": safe_path,
            "timeout_s": COMPUTE_TRIGGER_TIMEOUT_SECONDS,
        }
    except requests.exceptions.ConnectionError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "compute_unreachable",
            "retryable": True,
            "path": safe_path,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_code": "compute_request_failed", "retryable": True, "path": safe_path}

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
        "error_code": "compute_http_error",
        "retryable": response.status_code >= 500,
        "path": safe_path,
    }


def _get_remote_compute_path(path: str, params: dict | None = None) -> dict:
    safe_path = "/" + str(path or "").strip().lstrip("/")
    try:
        response = requests.get(
            f"{get_compute_internal_url()}{safe_path}",
            params=params or {},
            timeout=COMPUTE_STATUS_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "compute_status_timeout",
            "retryable": True,
            "path": safe_path,
            "timeout_s": COMPUTE_STATUS_TIMEOUT_SECONDS,
        }
    except requests.exceptions.ConnectionError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "compute_unreachable",
            "retryable": True,
            "path": safe_path,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_code": "compute_status_failed", "retryable": True, "path": safe_path}

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
        "error_code": "compute_http_error",
        "retryable": response.status_code >= 500,
        "path": safe_path,
    }


def trigger_remote_compute(payload: dict | None = None) -> dict:
    return _post_remote_compute_path("/compute", payload)


def trigger_remote_prime(payload: dict | None = None) -> dict:
    return _post_remote_compute_path("/compute/prime", payload)


def trigger_remote_scan(payload: dict | None = None) -> dict:
    return _post_remote_compute_path("/scan", payload)


def get_remote_scan_status(payload: dict | None = None) -> dict:
    return _get_remote_compute_path("/scan/status", payload)
