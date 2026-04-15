from __future__ import annotations

from ibkr_compute.api.route_request import coerce_request_bool, coerce_request_int
from ibkr_compute.api.runtime.common import _ibkr_service_environment, get_ibkr_service


def _build_ibkr_2fa_takeover_response(payload: dict | None = None) -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503
    payload = payload if isinstance(payload, dict) else {}
    enabled = coerce_request_bool(payload.get("enabled"), True)
    ttl_seconds = coerce_request_int(payload.get("ttl_sec"), 600, minimum=1)
    reason = str(payload.get("reason") or "manual_takeover").strip() or "manual_takeover"
    source = str(payload.get("source") or "api_takeover").strip() or "api_takeover"
    state = service.set_manual_takeover(
        enabled=enabled,
        ttl_seconds=ttl_seconds,
        reason=reason,
        source=source,
    )
    return {
        "ok": True,
        "environment": _ibkr_service_environment(service),
        "enabled": bool(enabled),
        "state": state,
    }, 200


def _build_ibkr_2fa_probe_response(payload: dict | None = None) -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503
    payload = payload if isinstance(payload, dict) else {}
    reason = str(payload.get("reason") or "manual_probe").strip() or "manual_probe"
    source = str(payload.get("source") or "api_probe").strip() or "api_probe"
    state = service.trigger_auth_probe(reason=reason, source=source)
    return {
        "ok": True,
        "environment": _ibkr_service_environment(service),
        "state": state,
    }, 200


def _build_ibkr_panic_reset_response(payload: dict | None = None) -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503
    payload = payload if isinstance(payload, dict) else {}
    restart_gateway = coerce_request_bool(payload.get("restart_gateway"), True)
    restart_runtime = coerce_request_bool(payload.get("restart_runtime"), True)
    trigger_login = coerce_request_bool(payload.get("trigger_login"), True)
    reason = str(payload.get("reason") or "panic_reset_2fa").strip() or "panic_reset_2fa"
    source = str(payload.get("source") or "api_panic_reset").strip() or "api_panic_reset"
    result = service.panic_reset_auth(
        restart_gateway=restart_gateway,
        restart_runtime=restart_runtime,
        trigger_login=trigger_login,
        reason=reason,
        source=source,
    )
    return {
        "ok": True,
        "environment": _ibkr_service_environment(service),
        **result,
    }, 200
