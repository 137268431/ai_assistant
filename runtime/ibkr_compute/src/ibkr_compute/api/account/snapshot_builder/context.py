from __future__ import annotations

import time
import threading

from ibkr_compute.api.account.live import _api_app
from ibkr_compute.api.shared.service_status import get_service_status_snapshot
from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode


def _resolve_snapshot_account_id(api_app, service, runtime_environment: str) -> str:
    use_paper = str(runtime_environment or "").strip().lower() == "paper"
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


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _fast_account_data_circuit_status(client) -> dict:
    current = time.time()
    try:
        until = float(getattr(client, "_account_data_circuit_until", 0.0) or 0.0)
    except Exception:
        until = 0.0
    active = bool(until and current < until)
    return {
        "active": active,
        "reason": str(getattr(client, "_account_data_circuit_reason", "") or ""),
        "remaining_s": round(max(0.0, until - current), 1) if active else 0.0,
        "source": "fast_runtime_state",
    }


def _fast_gateway_status(service) -> dict:
    broker = getattr(service, "broker", None)
    client = getattr(broker, "client", None)
    if client is not None:
        is_connected = getattr(client, "isConnected", None)
        try:
            connected = bool(is_connected()) if callable(is_connected) else False
        except Exception:
            connected = False
        ready_event = getattr(client, "_ready_event", None)
        ready_event_set = getattr(ready_event, "is_set", None)
        try:
            event_ready = bool(ready_event_set()) if callable(ready_event_set) else False
        except Exception:
            event_ready = False
        ready = bool(getattr(client, "_ready", False) or event_ready)
        status_code = _to_int(getattr(client, "_status_code", 0), 0)
        circuit = _fast_account_data_circuit_status(client)
        return {
            "running": bool(connected or ready or getattr(service, "is_running", False)),
            "reachable": bool(connected or ready or status_code not in {0, 502, 503}),
            "connected": connected,
            "ready": ready,
            "status_code": status_code,
            "broker": {
                "connected": connected,
                "ready": ready,
                "status_code": status_code,
                "account_data_circuit": circuit,
                "source": "fast_runtime_state",
            },
            "account_data_circuit": circuit,
            "source": "fast_runtime_state",
        }

    broker_status: dict = {}
    status_fn = getattr(broker, "status", None)
    if callable(status_fn):
        try:
            payload = status_fn()
            broker_status = dict(payload) if isinstance(payload, dict) else {}
        except Exception:
            broker_status = {}
    connected = bool(broker_status.get("connected"))
    ready = bool(broker_status.get("ready"))
    status_code = _to_int(broker_status.get("status_code"), 0)
    return {
        "running": bool(connected or ready or getattr(service, "is_running", False)),
        "reachable": bool(connected or ready or status_code not in {0, 502, 503}),
        "connected": connected,
        "ready": ready,
        "status_code": status_code,
        "broker": broker_status,
        "account_data_circuit": broker_status.get("account_data_circuit") if isinstance(broker_status.get("account_data_circuit"), dict) else {},
        "source": "fast_runtime_state",
    }


def _fast_session_status(service) -> dict:
    session = getattr(service, "session_keeper", None)
    return {
        "authenticated": bool(getattr(session, "is_authenticated", False)),
        "running": bool(getattr(session, "_running", False)),
        "status_code": _to_int(getattr(session, "_last_status_code", 0), 0),
        "last_check": getattr(session, "_last_check", ""),
        "source": "fast_runtime_state",
    }


def _fast_websocket_status(service) -> dict:
    ws_client = getattr(service, "ws_client", None)
    return {
        "connected": bool(getattr(ws_client, "is_connected", False) or getattr(ws_client, "_connected", False)),
        "ready": bool(getattr(ws_client, "is_ready", False) or getattr(ws_client, "_ready", False)),
        "running": bool(getattr(ws_client, "_running", False)),
        "source": "fast_runtime_state",
    }


def build_fast_snapshot_status(service) -> dict:
    return {
        "gateway": _fast_gateway_status(service),
        "session": _fast_session_status(service),
        "websocket": _fast_websocket_status(service),
    }


def _fast_runtime_environment(service) -> str:
    for component in (
        service,
        getattr(service, "order_placer", None),
        getattr(service, "order_lifecycle", None),
        getattr(service, "order_tracker", None),
        getattr(service, "order_modifier", None),
        getattr(service, "signal_processor", None),
    ):
        if component is None:
            continue
        for attr_name in ("broker_mode", "environment", "runtime_environment"):
            value = str(getattr(component, attr_name, "") or "").strip()
            if value:
                return normalize_broker_mode(value, configured_broker_mode())
    return ""


def resolve_snapshot_runtime_environment(api_app, service, *, fast_status: bool = False) -> str:
    if fast_status:
        resolved = _fast_runtime_environment(service)
        if resolved:
            return resolved
    try:
        return api_app._ibkr_service_environment(service)
    except Exception:
        return configured_broker_mode()


def build_snapshot_context(service, *, include_pnl: bool = True, fast_status: bool = False) -> dict:
    api_app = _api_app()
    runtime_environment = resolve_snapshot_runtime_environment(api_app, service, fast_status=bool(fast_status))
    service_status = build_fast_snapshot_status(service) if fast_status else get_service_status_snapshot(service)
    account_id = _resolve_snapshot_account_id(api_app, service, runtime_environment)
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
        fallback = max(900.0, _snapshot_cache_ttl_seconds(api_app) * 60.0)
        configured = float(getattr(api_app, "IBKR_ACCOUNT_SNAPSHOT_STALE_SECONDS", fallback) or fallback)
        return max(fallback, _snapshot_cache_ttl_seconds(api_app), configured)
    except Exception:
        return 900.0


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
    "build_fast_snapshot_status",
    "build_snapshot_context",
    "get_snapshot_refresh_lock",
    "load_cached_snapshot",
    "mark_cached_snapshot_refresh_error",
    "resolve_snapshot_runtime_environment",
    "store_cached_snapshot",
]
