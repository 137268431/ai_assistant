from __future__ import annotations

import threading
import traceback

from ibkr_compute.api.shared.service_status import get_service_status_snapshot
from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode


def _api_app():
    from .. import app as api_app

    return api_app


def _ibkr_runtime_control_default(environment: str) -> dict:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    return {
        "environment": runtime_environment,
        "desired_running": False,
        "last_source": "",
        "last_reason": "",
        "last_start_request_at": "",
        "last_stop_request_at": "",
        "last_restore_attempt_at": "",
        "last_restore_trigger_login": False,
        "updated_at": "",
    }


def get_ibkr_runtime_control(environment: str) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    fallback = _ibkr_runtime_control_default(runtime_environment)
    try:
        state = api_app.pb.get_state(
            api_app.IBKR_RUNTIME_CONTROL_STATE_KEY,
            runtime_environment,
            date=api_app.IBKR_RUNTIME_CONTROL_STATE_DATE,
        )
    except Exception:
        traceback.print_exc()
        return fallback

    payload = state.get("data") if isinstance(state, dict) else {}
    if not isinstance(payload, dict):
        return fallback
    return {
        **fallback,
        **payload,
        "environment": runtime_environment,
        "desired_running": bool(payload.get("desired_running", False)),
        "last_restore_trigger_login": bool(payload.get("last_restore_trigger_login", False)),
    }


def set_ibkr_runtime_control(
    environment: str,
    desired_running: bool,
    source: str,
    reason: str,
    extra: dict | None = None,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    timestamps = api_app.build_runtime_timestamps()
    current = get_ibkr_runtime_control(runtime_environment)
    patch = {
        **current,
        "environment": runtime_environment,
        "desired_running": bool(desired_running),
        "last_source": str(source or "").strip(),
        "last_reason": str(reason or "").strip(),
        "updated_at": timestamps.get("us", ""),
    }
    if desired_running:
        patch["last_start_request_at"] = timestamps.get("us", "")
    else:
        patch["last_stop_request_at"] = timestamps.get("us", "")
    if isinstance(extra, dict):
        patch.update(extra)
    try:
        return api_app.pb.upsert_state(
            api_app.IBKR_RUNTIME_CONTROL_STATE_KEY,
            runtime_environment,
            patch,
            date=api_app.IBKR_RUNTIME_CONTROL_STATE_DATE,
        )
    except Exception:
        traceback.print_exc()
        return {"data": patch}


def _background_start_ibkr_service(service, trigger_login: bool, reason: str, source: str):
    thread = threading.Thread(
        target=service.start,
        kwargs={
            "trigger_login": bool(trigger_login),
            "reason": reason,
            "source": source,
        },
        daemon=True,
        name=f"ibkr-service-{source}",
    )
    thread.start()
    return thread


def _background_panic_reset_auth(
    service,
    *,
    restart_gateway: bool,
    restart_runtime: bool,
    trigger_login: bool,
    reason: str,
    source: str,
):
    thread = threading.Thread(
        target=service.panic_reset_auth,
        kwargs={
            "restart_gateway": bool(restart_gateway),
            "restart_runtime": bool(restart_runtime),
            "trigger_login": bool(trigger_login),
            "reason": reason,
            "source": source,
        },
        daemon=True,
        name=f"ibkr-panic-reset-{source}",
    )
    thread.start()
    return thread


def get_ibkr_service():
    from ibkr_compute.api.service_topology import is_runtime_remote_mode

    if is_runtime_remote_mode():
        return None

    api_app = _api_app()
    service = getattr(api_app, "_ibkr_service", None)
    if service is not None:
        return service

    service_lock = getattr(api_app, "_ibkr_service_lock", None)
    if service_lock is None:
        service_lock = threading.Lock()
        api_app._ibkr_service_lock = service_lock

    with service_lock:
        service = getattr(api_app, "_ibkr_service", None)
        if service is not None:
            return service
        try:
            from ibkr_compute.ibkr_service import IBKRTradingService

            api_app._ibkr_service = IBKRTradingService()
        except Exception as exc:
            print(f"[IBKR] Service init failed: {exc}")
        return getattr(api_app, "_ibkr_service", None)


def _normalize_runtime_environment_name(value, default: str = "live") -> str:
    return normalize_broker_mode(value, normalize_broker_mode(default, configured_broker_mode()))


def _ibkr_service_environment(service) -> str:
    try:
        status_payload = get_service_status_snapshot(service, {"environment": "live"})
        return _normalize_runtime_environment_name(
            status_payload.get("broker_mode") or status_payload.get("environment"),
            configured_broker_mode(),
        )
    except Exception:
        return configured_broker_mode()


def _ibkr_service_uses_paper_account(service) -> bool:
    return _ibkr_service_environment(service) == "paper"


def _coerce_float(value, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        return default if number != number or abs(number) >= 1e100 else number
    text = str(value).strip().replace(",", "")
    if not text:
        return default
    try:
        number = float(text)
    except Exception:
        return default
    return default if number != number or abs(number) >= 1e100 else number
