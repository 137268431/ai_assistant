from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import partial
from typing import Any

import requests
from flask import Response, jsonify, request

from ibkr_compute.api.service_topology import (
    build_service_topology,
    get_runtime_internal_url,
    is_runtime_remote_mode,
)


logger = logging.getLogger(__name__)
RUNTIME_PROXY_TIMEOUT_SECONDS = 60
RUNTIME_PROXY_LONG_TIMEOUT_SECONDS = 300
RUNTIME_PROXY_LONG_TIMEOUT_PATHS = {
    "/ibkr/data-quality/repair",
    "/ibkr/data-quality/truth-audit",
    "/ibkr/data-quality/daily-repair",
}
ASYNC_RUNTIME_OPERATION_STATE_PREFIX = "ibkr_runtime_async_operation:"
ASYNC_RUNTIME_OPERATION_PATHS = {
    "/ibkr/data-quality/repair",
    "/ibkr/data-quality/truth-audit",
}
_ASYNC_OPERATION_LOCK = threading.RLock()
_ASYNC_OPERATION_STATES: dict[str, dict[str, Any]] = {}


def _coerce_timeout_seconds(env_name: str, default: float) -> float:
    try:
        value = float(os.environ.get(env_name, "") or default)
    except Exception:
        value = float(default)
    return max(1.0, value)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_path(path: str) -> str:
    normalized_path = str(path or "").strip()
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    return normalized_path


def _parse_json_body(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads((raw_body or b"{}").decode("utf-8") or "{}")
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _payload_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "force"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _runtime_proxy_timeout_seconds(path: str) -> float:
    normalized_path = _normalize_path(path)
    if normalized_path in RUNTIME_PROXY_LONG_TIMEOUT_PATHS:
        return _coerce_timeout_seconds(
            "IBKR_COMPUTE_RUNTIME_PROXY_REPAIR_TIMEOUT_SEC",
            RUNTIME_PROXY_LONG_TIMEOUT_SECONDS,
        )
    return _coerce_timeout_seconds(
        "IBKR_COMPUTE_RUNTIME_PROXY_TIMEOUT_SEC",
        RUNTIME_PROXY_TIMEOUT_SECONDS,
    )


def _runtime_proxy_slow_log_threshold_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("IBKR_COMPUTE_RUNTIME_PROXY_SLOW_LOG_SEC", "5.0") or 0.0))
    except Exception:
        return 5.0


def _runtime_async_operation_state_key(operation_id: str) -> str:
    return f"{ASYNC_RUNTIME_OPERATION_STATE_PREFIX}{str(operation_id or '').strip()}"


def _async_operation_environment(payload: dict[str, Any]) -> str:
    return str(
        payload.get("market_data_mode")
        or payload.get("data_environment")
        or payload.get("environment")
        or "live"
    ).strip().lower() or "live"


def _operation_id(path: str, payload: dict[str, Any], environment: str) -> str:
    requested = str(payload.get("operation_id") or "").strip()
    if requested:
        return requested
    slug = _normalize_path(path).strip("/").replace("/", "_").replace("-", "_") or "runtime_operation"
    return f"compute:{slug}:{environment}:{_now_ms()}:{uuid.uuid4().hex[:10]}"


def _get_app_pb():
    try:
        from ibkr_compute.api.shared.route_runtime import get_app_module

        return getattr(get_app_module(), "pb", None)
    except Exception:
        return None


def _persist_async_operation_state(state: dict[str, Any]) -> None:
    operation_id = str((state or {}).get("operation_id") or "").strip()
    if not operation_id:
        return
    environment = _async_operation_environment(state)
    with _ASYNC_OPERATION_LOCK:
        _ASYNC_OPERATION_STATES[operation_id] = dict(state)
    pb = _get_app_pb()
    if pb is None or not hasattr(pb, "upsert_state"):
        return
    try:
        pb.upsert_state(_runtime_async_operation_state_key(operation_id), environment, state, date="global")
    except Exception:
        logger.exception("Failed to persist async runtime operation state: operation_id=%s", operation_id)


def _load_async_operation_state(operation_id: str, environment: str = "") -> dict[str, Any] | None:
    normalized_operation_id = str(operation_id or "").strip()
    if not normalized_operation_id:
        return None
    with _ASYNC_OPERATION_LOCK:
        state = _ASYNC_OPERATION_STATES.get(normalized_operation_id)
    if isinstance(state, dict):
        return dict(state)
    pb = _get_app_pb()
    if pb is None or not hasattr(pb, "get_state"):
        return None
    candidates = [str(environment or "").strip().lower(), "live", "paper", "backtest"]
    seen: set[str] = set()
    for candidate in candidates:
        runtime_environment = candidate or "live"
        if runtime_environment in seen:
            continue
        seen.add(runtime_environment)
        try:
            row = pb.get_state(
                _runtime_async_operation_state_key(normalized_operation_id),
                runtime_environment,
                date="global",
            )
        except Exception:
            row = None
        data = row.get("data") if isinstance(row, dict) else {}
        if isinstance(data, dict) and data:
            with _ASYNC_OPERATION_LOCK:
                _ASYNC_OPERATION_STATES[normalized_operation_id] = dict(data)
            return dict(data)
    return None


def _async_terminal_status(status: str) -> bool:
    return str(status or "").strip().lower() in {"completed", "failed", "cancelled"}


def should_proxy_runtime_requests() -> bool:
    return is_runtime_remote_mode()


def _build_runtime_upstream(path: str) -> str:
    normalized_path = _normalize_path(path)
    return f"{get_runtime_internal_url()}{normalized_path}"


def _async_runtime_worker(
    *,
    operation_id: str,
    method: str,
    upstream: str,
    params: list[tuple[str, str]],
    body: bytes,
    headers: dict[str, str],
    timeout_seconds: float,
    base_state: dict[str, Any],
) -> None:
    started_state = {
        **base_state,
        "status": "running",
        "started_at": _now_iso(),
        "started_at_ms": _now_ms(),
        "last_error": "",
    }
    _persist_async_operation_state(started_state)
    started = time.monotonic()
    try:
        upstream_response = requests.request(
            method=method,
            url=upstream,
            params=params,
            data=body,
            headers=headers,
            timeout=timeout_seconds,
            allow_redirects=False,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        try:
            result_payload = upstream_response.json() if upstream_response.content else {}
        except Exception:
            result_payload = {}
        if not isinstance(result_payload, dict):
            result_payload = {}
        result_ok = bool(upstream_response.ok and result_payload.get("ok", True) is not False)
        final_state = {
            **started_state,
            "ok": result_ok,
            "status": "completed" if result_ok else "failed",
            "finished_at": _now_iso(),
            "finished_at_ms": _now_ms(),
            "elapsed_ms": elapsed_ms,
            "status_code": int(upstream_response.status_code),
            "result": result_payload,
            "last_error": "" if result_ok else str(result_payload.get("error") or f"http_{int(upstream_response.status_code)}"),
        }
        _persist_async_operation_state(final_state)
    except requests.RequestException as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        logger.warning(
            "Async runtime operation failed: operation_id=%s upstream=%s elapsed_ms=%.1f timeout_s=%.1f error=%s",
            operation_id,
            upstream,
            elapsed_ms,
            float(timeout_seconds),
            exc,
        )
        _persist_async_operation_state(
            {
                **started_state,
                "ok": False,
                "status": "failed",
                "finished_at": _now_iso(),
                "finished_at_ms": _now_ms(),
                "elapsed_ms": elapsed_ms,
                "status_code": 0,
                "result": {},
                "last_error": str(exc),
                "error": str(exc),
            }
        )


def _build_async_runtime_operation_response(path: str, raw_body: bytes):
    normalized_path = _normalize_path(path)
    upstream = _build_runtime_upstream(normalized_path)
    timeout_seconds = _runtime_proxy_timeout_seconds(normalized_path)
    params = list(request.args.items(multi=True))
    headers = {}
    for header_name in ("Accept", "Content-Type"):
        header_value = request.headers.get(header_name)
        if header_value:
            headers[header_name] = header_value
    payload = _parse_json_body(raw_body)
    environment = _async_operation_environment(payload)
    operation_id = _operation_id(normalized_path, payload, environment)
    forward_payload = dict(payload)
    forward_payload.pop("async", None)
    forward_payload["operation_id"] = operation_id
    forward_body = json.dumps(forward_payload).encode("utf-8")
    headers["Content-Type"] = "application/json"
    base_state = {
        "ok": True,
        "accepted": True,
        "async": True,
        "operation_id": operation_id,
        "status": "accepted",
        "path": normalized_path,
        "proxy_upstream": upstream,
        "environment": environment,
        "market_data_mode": str(payload.get("market_data_mode") or payload.get("data_environment") or environment).strip().lower() or environment,
        "broker_mode": str(payload.get("broker_mode") or "").strip().lower(),
        "created_at": _now_iso(),
        "created_at_ms": _now_ms(),
        "started_at": "",
        "finished_at": "",
        "last_error": "",
        "result": {},
    }
    _persist_async_operation_state(base_state)
    thread = threading.Thread(
        target=_async_runtime_worker,
        kwargs={
            "operation_id": operation_id,
            "method": request.method,
            "upstream": upstream,
            "params": params,
            "body": forward_body,
            "headers": headers,
            "timeout_seconds": timeout_seconds,
            "base_state": base_state,
        },
        name=f"runtime-async-operation-{operation_id[:32]}",
        daemon=True,
    )
    thread.start()
    return jsonify(
        {
            "ok": True,
            "accepted": True,
            "async": True,
            "operation_id": operation_id,
            "status": "accepted",
            "path": normalized_path,
            "proxy_upstream": upstream,
        }
    ), 202


def build_runtime_async_operation_status_response():
    operation_id = str(request.args.get("operation_id") or "").strip()
    environment = str(
        request.args.get("market_data_mode")
        or request.args.get("data_environment")
        or request.args.get("environment")
        or ""
    ).strip().lower()
    state = _load_async_operation_state(operation_id, environment)
    if not state:
        return jsonify(
            {
                "ok": False,
                "status": "not_found",
                "error": "async_operation_not_found",
                "operation_id": operation_id,
            }
        ), 404
    status = str(state.get("status") or "").strip().lower()
    return jsonify(
        {
            "ok": bool(state.get("ok", True)) if _async_terminal_status(status) else True,
            **state,
        }
    )


def proxy_runtime_request(path: str):
    normalized_path = _normalize_path(path)
    raw_body = request.get_data(cache=True)
    payload = _parse_json_body(raw_body)
    if (
        request.method == "POST"
        and normalized_path in ASYNC_RUNTIME_OPERATION_PATHS
        and _payload_bool(payload.get("async"), False)
    ):
        return _build_async_runtime_operation_response(normalized_path, raw_body)

    upstream = _build_runtime_upstream(normalized_path)
    timeout_seconds = _runtime_proxy_timeout_seconds(normalized_path)
    params = list(request.args.items(multi=True))
    headers = {}
    for header_name in ("Accept", "Content-Type"):
        header_value = request.headers.get(header_name)
        if header_value:
            headers[header_name] = header_value

    started = time.monotonic()
    try:
        upstream_response = requests.request(
            method=request.method,
            url=upstream,
            params=params,
            data=raw_body,
            headers=headers,
            timeout=timeout_seconds,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        logger.warning(
            "Runtime proxy request failed: path=%s upstream=%s elapsed_ms=%.1f timeout_s=%.1f error=%s",
            normalized_path,
            upstream,
            elapsed_ms,
            float(timeout_seconds),
            exc,
        )
        return jsonify(
            {
                "ok": False,
                "status": "offline",
                "error": str(exc),
                "proxy_upstream": upstream,
                "service_topology": build_service_topology(),
            }
        ), 502

    elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    slow_threshold_s = _runtime_proxy_slow_log_threshold_seconds()
    if (not upstream_response.ok) or (slow_threshold_s > 0 and elapsed_ms >= slow_threshold_s * 1000.0):
        log_fn = logger.warning if not upstream_response.ok else logger.info
        log_fn(
            "Runtime proxy request completed: path=%s upstream=%s status=%s elapsed_ms=%.1f timeout_s=%.1f",
            normalized_path,
            upstream,
            int(upstream_response.status_code),
            elapsed_ms,
            float(timeout_seconds),
        )

    response = Response(
        upstream_response.content,
        status=upstream_response.status_code,
    )
    content_type = upstream_response.headers.get("Content-Type")
    if content_type:
        response.headers["Content-Type"] = content_type
    location = upstream_response.headers.get("Location")
    if location:
        response.headers["Location"] = location
    return response


def register_runtime_proxy_route(app, endpoint: str, rule: str, methods: list[str] | tuple[str, ...]):
    app.add_url_rule(
        rule,
        endpoint=endpoint,
        view_func=partial(proxy_runtime_request, rule),
        methods=list(methods),
    )
