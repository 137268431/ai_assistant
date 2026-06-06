from __future__ import annotations

from ibkr_compute.api.runtime.common import _ibkr_service_environment, get_ibkr_service
from ibkr_compute.api.service_topology import build_service_topology, get_runtime_mode, get_service_profile
from ibkr_compute.core.broker_mode import normalize_broker_mode


def _service_flag(service, *names: str) -> bool:
    for name in names:
        try:
            value = getattr(service, name)
        except Exception:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if value is not None:
            if bool(value):
                return True
    return False


def _session_authenticated(service) -> bool:
    session_keeper = getattr(service, "session_keeper", None)
    if session_keeper is None:
        return False
    try:
        return bool(getattr(session_keeper, "is_authenticated", False))
    except Exception:
        return False


def _build_ibkr_signal_wakeup_response(payload: dict | None = None) -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        return {
            "ok": False,
            "error": "IBKR service not initialized",
            "reason": "service_unavailable",
            "service_profile": get_service_profile(),
            "runtime_mode": get_runtime_mode(),
            "service_topology": build_service_topology(),
        }, 503

    payload = payload if isinstance(payload, dict) else {}
    signal_wakeup = getattr(service, "_signal_wakeup", None)
    if not hasattr(signal_wakeup, "set"):
        return {
            "ok": False,
            "error": "IBKR signal wakeup event not initialized",
            "reason": "signal_wakeup_unavailable",
            "environment": _ibkr_service_environment(service),
            "service_profile": get_service_profile(),
            "runtime_mode": get_runtime_mode(),
        }, 503

    runtime_environment = _ibkr_service_environment(service)
    requested_environment = normalize_broker_mode(
        payload.get("broker_mode") or payload.get("environment"),
        runtime_environment,
    )
    running = _service_flag(service, "is_running", "_running")
    starting = _service_flag(service, "is_starting", "_starting")
    authenticated = _session_authenticated(service)

    try:
        signal_wakeup.set()
    except Exception as exc:
        return {
            "ok": False,
            "woke": False,
            "error": str(exc),
            "reason": "signal_wakeup_set_failed",
            "environment": runtime_environment,
            "requested_environment": requested_environment,
            "runtime_environment_mismatch": requested_environment != runtime_environment,
            "running": running,
            "starting": starting,
            "authenticated": authenticated,
            "service_profile": get_service_profile(),
            "runtime_mode": get_runtime_mode(),
        }, 500

    reason = "signal_loop_woken" if running else "runtime_not_running"
    return {
        "ok": True,
        "woke": running,
        "event_set": True,
        "will_process": bool(running and authenticated),
        "reason": reason,
        "environment": runtime_environment,
        "requested_environment": requested_environment,
        "runtime_environment_mismatch": requested_environment != runtime_environment,
        "running": running,
        "starting": starting,
        "authenticated": authenticated,
        "source": str(payload.get("source") or "runtime_api").strip() or "runtime_api",
        "tv_event_id": str(payload.get("tv_event_id") or "").strip(),
        "event_type": str(payload.get("event_type") or "").strip(),
        "symbol": str(payload.get("symbol") or "").strip().upper(),
        "signal_id": str(payload.get("signal_id") or "").strip(),
        "route_target": str(payload.get("route_target") or "").strip(),
        "route_record_id": str(payload.get("route_record_id") or "").strip(),
        "service_profile": get_service_profile(),
        "runtime_mode": get_runtime_mode(),
    }, 200
