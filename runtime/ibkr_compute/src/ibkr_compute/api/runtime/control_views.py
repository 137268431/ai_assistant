from __future__ import annotations

from flask import request

from ibkr_compute.api.monitor.views import _build_ibkr_monitor_snapshot
from ibkr_compute.api.shared.route_request import coerce_request_bool
from ibkr_compute.api.shared.service_status import _build_minimal_runtime_status, get_service_status_snapshot
from ibkr_compute.api.support.market_data_session import record_market_data_session_conflict_state
from ibkr_compute.api.runtime.common import (
    _api_app,
    _background_start_ibkr_service,
    _ibkr_service_environment,
    get_ibkr_runtime_control,
    get_ibkr_service,
    set_ibkr_runtime_control,
)
from ibkr_compute.api.runtime.restore import _maybe_restore_ibkr_service
from ibkr_compute.api.service_topology import build_service_topology, get_runtime_mode, get_service_profile
from ibkr_compute.core.broker_mode import normalize_broker_mode


def _coerce_symbol_list(value) -> list[str]:
    raw_items = value if isinstance(value, list) else [value]
    normalized = []
    seen = set()
    for raw in raw_items:
        parts = raw if isinstance(raw, list) else str(raw or "").replace("\n", ",").split(",")
        for part in parts:
            symbol = str(part or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                normalized.append(symbol)
    return normalized


def _build_ibkr_start_response(payload: dict | None = None) -> tuple[dict, int]:
    api_app = _api_app()
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503
    try:
        payload = payload if isinstance(payload, dict) else {}
        trigger_login = payload.get("trigger_login", False)
        reason = str(payload.get("reason") or "manual_start")
        source = str(payload.get("source") or "api_start")
        runtime_environment = _ibkr_service_environment(service)

        api_app._ibkr_restore_attempted = False
        set_ibkr_runtime_control(
            runtime_environment,
            True,
            source=source,
            reason=reason,
            extra={
                "last_restore_trigger_login": False,
            },
        )

        if getattr(service, "is_busy", False):
            return {
                "ok": True,
                "message": (
                    "IBKR service already starting"
                    if getattr(service, "is_starting", False)
                    else "IBKR service already running"
                ),
                "trigger_login": bool(trigger_login),
                "reason": reason,
                "source": source,
                "starting": bool(getattr(service, "is_starting", False)),
                "running": bool(getattr(service, "is_running", False)),
            }, 200

        _background_start_ibkr_service(
            service,
            trigger_login=bool(trigger_login),
            reason=reason,
            source=source,
        )
        return {
            "ok": True,
            "message": "IBKR service starting",
            "trigger_login": bool(trigger_login),
            "reason": reason,
            "source": source,
        }, 200
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 500


def _build_ibkr_stop_response() -> tuple[dict, int]:
    api_app = _api_app()
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503
    service.stop()
    api_app._ibkr_restore_attempted = False
    runtime_environment = _ibkr_service_environment(service)
    set_ibkr_runtime_control(
        runtime_environment,
        False,
        source="api_stop",
        reason="manual_stop",
        extra={
            "last_restore_trigger_login": False,
        },
    )
    return {"ok": True, "message": "IBKR service stopped"}, 200


def _build_ibkr_status_response() -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        return {
            "ok": False,
            "error": "IBKR service not initialized",
            "service_profile": get_service_profile(),
            "runtime_mode": get_runtime_mode(),
            "service_topology": build_service_topology(),
        }, 200
    try:
        skip_compute_status = coerce_request_bool(request.args.get("skip_compute_status"), False)
    except Exception:
        skip_compute_status = False
    try:
        lite_status = (
            coerce_request_bool(request.args.get("lite"), False)
            or coerce_request_bool(request.args.get("fast"), False)
            or coerce_request_bool(request.args.get("fast_status"), False)
            or coerce_request_bool(request.args.get("lightweight"), False)
        )
    except Exception:
        lite_status = False
    if not lite_status:
        _maybe_restore_ibkr_service(service, refresh_auth=False, block=False)
    if lite_status:
        status_payload = _build_minimal_runtime_status(service)
        status_payload["status_mode"] = "lite"
    else:
        status_payload = get_service_status_snapshot(
            service,
            refresh_calendar=True,
            include_compute_status=not skip_compute_status,
        )
        status_payload.setdefault("status_mode", "full")
    if skip_compute_status:
        status_payload["compute_status_lookup_skipped"] = True
    if "market_data_session_conflict" not in status_payload:
        data_environment = (
            status_payload.get("data_environment")
            or status_payload.get("market_data_environment")
            or status_payload.get("environment")
            or "live"
        )
        status_payload["market_data_session_conflict"] = record_market_data_session_conflict_state(
            service,
            status_payload,
            environment=str(data_environment or "live"),
        )
    runtime_environment = normalize_broker_mode(
        status_payload.get("broker_mode") or status_payload.get("environment"),
        "live",
    )
    status_payload["runtime_control"] = get_ibkr_runtime_control(runtime_environment)
    status_payload["service_profile"] = get_service_profile()
    status_payload["runtime_mode"] = get_runtime_mode()
    status_payload["service_topology"] = build_service_topology(service=service, service_status=status_payload)
    try:
        from ibkr_compute.observability.prometheus import set_runtime_status_metrics

        set_runtime_status_metrics(status_payload, environment=runtime_environment)
    except Exception:
        pass
    return {"ok": True, **status_payload}, 200


def _build_ibkr_monitor_response(requested_environment: str) -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        payload = _build_ibkr_monitor_snapshot(
            None,
            requested_environment=requested_environment,
            service_error="IBKR service not initialized",
        )
        payload["service_profile"] = get_service_profile()
        payload["runtime_mode"] = get_runtime_mode()
        payload["service_topology"] = build_service_topology()
        return payload, 200
    _maybe_restore_ibkr_service(service, refresh_auth=False, block=False)
    payload = _build_ibkr_monitor_snapshot(service, requested_environment=requested_environment)
    payload["service_profile"] = get_service_profile()
    payload["runtime_mode"] = get_runtime_mode()
    payload["service_topology"] = build_service_topology(service=service, service_status=payload.get("runtime") or {})
    return payload, 200


def _build_ibkr_universe_reconcile_response(payload: dict | None = None) -> tuple[dict, int]:
    service = get_ibkr_service()
    if not service:
        return {"ok": False, "error": "IBKR service not initialized"}, 503

    _maybe_restore_ibkr_service(service)
    payload = payload if isinstance(payload, dict) else {}
    runtime_environment = _ibkr_service_environment(service)
    requested_environment = normalize_broker_mode(
        payload.get("broker_mode") or payload.get("environment"),
        runtime_environment,
    )
    if requested_environment != runtime_environment:
        return {
            "ok": False,
            "error": (
                f"runtime environment mismatch: requested={requested_environment} "
                f"running={runtime_environment}"
            ),
            "environment": runtime_environment,
            "requested_environment": requested_environment,
            "runtime_environment_mismatch": True,
        }, 409

    try:
        result = service.reconcile_market_universe(
            prime_symbols=_coerce_symbol_list(payload.get("prime_symbols")),
            cleanup_symbols=_coerce_symbol_list(payload.get("cleanup_symbols")),
            emit_signals=coerce_request_bool(payload.get("emit_signals"), False),
            source=str(payload.get("source") or "runtime_api").strip().lower() or "runtime_api",
            reason=str(payload.get("reason") or "manual_reconcile").strip() or "manual_reconcile",
        )
        return {
            "ok": bool((result or {}).get("ok", True)),
            **(result or {}),
            "requested_environment": requested_environment,
            "runtime_environment_mismatch": False,
        }, 200
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "environment": runtime_environment,
            "requested_environment": requested_environment,
            "runtime_environment_mismatch": False,
        }, 500
