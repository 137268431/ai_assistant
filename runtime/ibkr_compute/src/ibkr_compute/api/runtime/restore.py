from __future__ import annotations

import os
import time
import traceback

from ibkr_compute.api.runtime.common import (
    _api_app,
    _background_start_ibkr_service,
    _ibkr_service_environment,
    get_ibkr_runtime_control,
    patch_ibkr_runtime_control,
    set_ibkr_runtime_control,
)


def _to_int(value, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(default)
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _env_seconds(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, default))
    except Exception:
        value = float(default)
    return max(float(minimum), value)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(float(os.environ.get(name, default)))
    except Exception:
        value = int(default)
    return max(int(minimum), value)


def _timestamp_text(timestamps: dict | None) -> str:
    data = timestamps if isinstance(timestamps, dict) else {}
    return str(data.get("computed_at_us") or data.get("us") or "").strip()


def _timestamp_ms(timestamps: dict | None) -> int:
    data = timestamps if isinstance(timestamps, dict) else {}
    value = _to_int(data.get("computed_at_ms") or data.get("ms"), 0)
    return value if value > 0 else int(time.time() * 1000)


def _service_running(service) -> bool:
    return bool(getattr(service, "is_running", False) or getattr(service, "_running", False))


def _service_starting(service) -> bool:
    return bool(getattr(service, "is_starting", False) or getattr(service, "_starting", False))


def _component_status(component) -> dict:
    status_fn = getattr(component, "status", None)
    if not callable(status_fn):
        return {}
    try:
        payload = status_fn()
    except Exception:
        traceback.print_exc()
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _bool_attr(component, *names: str) -> bool:
    for name in names:
        value = getattr(component, name, False)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = False
        if bool(value):
            return True
    return False


def _session_authenticated(service, auth_status: dict | None) -> bool:
    if isinstance(auth_status, dict) and bool(auth_status.get("authenticated")):
        return True
    session_keeper = getattr(service, "session_keeper", None)
    if _bool_attr(session_keeper, "is_authenticated", "_authenticated"):
        return True
    status = _component_status(session_keeper)
    return bool(status.get("authenticated"))


def _websocket_state(service) -> dict:
    ws_client = getattr(service, "ws_client", None)
    status = _component_status(ws_client)
    connected = bool(status.get("connected")) or _bool_attr(ws_client, "is_connected", "_connected")
    ready = bool(status.get("ready")) or _bool_attr(ws_client, "is_ready", "_ready")
    running = bool(status.get("running")) or _bool_attr(ws_client, "_running")
    return {
        **status,
        "connected": connected,
        "ready": ready,
        "running": running,
    }


def _self_heal_limit(control: dict, now_ms: int) -> tuple[bool, str]:
    cooldown_ms = int(_env_seconds("IBKR_RUNTIME_SELF_HEAL_COOLDOWN_SECONDS", 90.0, minimum=1.0) * 1000)
    last_ms = _to_int((control or {}).get("last_self_heal_attempt_ms"), 0)
    if last_ms > 0 and now_ms - last_ms < cooldown_ms:
        return False, "self_heal_cooldown"
    max_attempts = _env_int("IBKR_RUNTIME_SELF_HEAL_MAX_ATTEMPTS", 3, minimum=0)
    attempts = _to_int((control or {}).get("self_heal_attempt_count"), 0)
    if max_attempts > 0 and attempts >= max_attempts:
        return False, "self_heal_attempt_limit"
    return True, ""


def _self_heal_extra(control: dict, *, reason: str, source: str, timestamps: dict) -> dict:
    attempt_ms = _timestamp_ms(timestamps)
    return {
        "last_restore_attempt_at": _timestamp_text(timestamps),
        "last_restore_attempt_ms": attempt_ms,
        "last_restore_trigger_login": False,
        "last_self_heal_attempt_at": _timestamp_text(timestamps),
        "last_self_heal_attempt_ms": attempt_ms,
        "last_self_heal_reason": reason,
        "last_self_heal_source": source,
        "last_self_heal_blocked_reason": "",
        "self_heal_attempt_count": _to_int((control or {}).get("self_heal_attempt_count"), 0) + 1,
    }


def _reset_self_heal_extra() -> dict:
    return {
        "last_self_heal_attempt_at": "",
        "last_self_heal_attempt_ms": 0,
        "last_self_heal_reason": "",
        "last_self_heal_source": "",
        "last_self_heal_blocked_reason": "",
        "self_heal_attempt_count": 0,
        "websocket_unready_since_at": "",
        "websocket_unready_since_ms": 0,
    }


def _record_self_heal_event(service, *, reason: str, source: str, environment: str, detail: dict | None = None) -> None:
    emitter = getattr(service, "_emit_system_event", None)
    if not callable(emitter):
        return
    payload = {
        "environment": environment,
        "reason": reason,
        "source": source,
        **(detail if isinstance(detail, dict) else {}),
    }
    try:
        emitter(
            "status_change",
            "warning",
            f"[Broker {environment.upper()}] IBKR Runtime self-heal requested",
            payload,
        )
    except Exception:
        traceback.print_exc()


def _patch_self_heal_blocked(runtime_environment: str, reason: str) -> None:
    try:
        patch_ibkr_runtime_control(
            runtime_environment,
            {
                "last_self_heal_blocked_reason": reason,
            },
        )
    except Exception:
        traceback.print_exc()


def _recent_restore_attempt_blocks_self_heal(api_app, control: dict, now_ms: int) -> bool:
    last_restore_ms = _to_int(getattr(api_app, "_ibkr_restore_last_attempt_ms", 0), 0)
    if last_restore_ms <= 0:
        last_restore_ms = _to_int((control or {}).get("last_restore_attempt_ms"), 0)
    if last_restore_ms <= 0:
        return False
    cooldown_ms = int(_env_seconds("IBKR_RUNTIME_SELF_HEAL_COOLDOWN_SECONDS", 90.0, minimum=1.0) * 1000)
    return now_ms - last_restore_ms < cooldown_ms


def _maybe_reset_healthy_self_heal_state(runtime_environment: str, control: dict, service) -> None:
    if not _service_running(service) or _service_starting(service):
        return
    websocket = _websocket_state(service)
    if not bool(websocket.get("ready") or websocket.get("connected")):
        return
    if not any(
        _to_int((control or {}).get(key), 0) > 0
        for key in ("self_heal_attempt_count", "last_self_heal_attempt_ms", "websocket_unready_since_ms")
    ) and not str((control or {}).get("last_self_heal_reason") or "").strip():
        return
    try:
        patch_ibkr_runtime_control(runtime_environment, _reset_self_heal_extra())
    except Exception:
        traceback.print_exc()


def _maybe_start_stopped_runtime_self_heal(
    service,
    *,
    api_app,
    runtime_environment: str,
    control: dict,
    timestamps: dict,
    authenticated: bool,
) -> bool:
    if _service_running(service) or _service_starting(service):
        return False
    if not authenticated:
        _patch_self_heal_blocked(runtime_environment, "session_not_authenticated")
        return True
    now_ms = _timestamp_ms(timestamps)
    allowed, blocked_reason = _self_heal_limit(control, now_ms)
    if not allowed:
        _patch_self_heal_blocked(runtime_environment, blocked_reason)
        return True

    api_app._ibkr_restore_last_attempt_ms = now_ms
    reason = "runtime_stopped_self_heal"
    source = "runtime_self_heal"
    set_ibkr_runtime_control(
        runtime_environment,
        True,
        source=source,
        reason=reason,
        extra=_self_heal_extra(control, reason=reason, source=source, timestamps=timestamps),
    )
    _record_self_heal_event(
        service,
        reason=reason,
        source=source,
        environment=runtime_environment,
        detail={"Runtime阶段": "stopped", "Session认证": "yes"},
    )
    print(
        f"[IBKR] Runtime self-heal requested for env={runtime_environment}; "
        "service is stopped while desired_running=true"
    )
    _background_start_ibkr_service(
        service,
        trigger_login=False,
        reason=reason,
        source=source,
    )
    return True


def _maybe_restart_websocket_self_heal(
    service,
    *,
    api_app,
    runtime_environment: str,
    control: dict,
    timestamps: dict,
    authenticated: bool,
) -> bool:
    if not _service_running(service) or _service_starting(service):
        return False
    websocket = _websocket_state(service)
    if bool(websocket.get("ready") or websocket.get("connected")):
        _maybe_reset_healthy_self_heal_state(runtime_environment, control, service)
        return True
    if not authenticated:
        return True

    now_ms = _timestamp_ms(timestamps)
    unready_since_ms = _to_int((control or {}).get("websocket_unready_since_ms"), 0)
    if unready_since_ms <= 0:
        try:
            patch_ibkr_runtime_control(
                runtime_environment,
                {
                    "websocket_unready_since_at": _timestamp_text(timestamps),
                    "websocket_unready_since_ms": now_ms,
                    "last_self_heal_reason": "websocket_not_ready_observed",
                    "last_self_heal_source": "runtime_self_heal",
                    "last_self_heal_blocked_reason": "",
                },
            )
        except Exception:
            traceback.print_exc()
        return True

    grace_ms = int(_env_seconds("IBKR_RUNTIME_WEBSOCKET_SELF_HEAL_GRACE_SECONDS", 300.0, minimum=30.0) * 1000)
    if now_ms - unready_since_ms < grace_ms:
        return True

    allowed, blocked_reason = _self_heal_limit(control, now_ms)
    if not allowed:
        _patch_self_heal_blocked(runtime_environment, blocked_reason)
        return True

    restart_fn = getattr(service, "_schedule_auth_restart", None)
    if not callable(restart_fn):
        _patch_self_heal_blocked(runtime_environment, "runtime_restart_method_unavailable")
        return True

    reason = "websocket_not_ready_self_heal"
    source = "runtime_self_heal"
    try:
        scheduled = bool(restart_fn(reason=reason, source=source, trigger_login=False))
    except Exception:
        traceback.print_exc()
        scheduled = False
    if not scheduled:
        _patch_self_heal_blocked(runtime_environment, "runtime_restart_not_scheduled")
        return True

    api_app._ibkr_restore_last_attempt_ms = now_ms
    extra = _self_heal_extra(control, reason=reason, source=source, timestamps=timestamps)
    extra.update({"websocket_unready_since_at": "", "websocket_unready_since_ms": 0})
    set_ibkr_runtime_control(
        runtime_environment,
        True,
        source=source,
        reason=reason,
        extra=extra,
    )
    _record_self_heal_event(
        service,
        reason=reason,
        source=source,
        environment=runtime_environment,
        detail={"Runtime阶段": "running", "WebSocket": "offline"},
    )
    return True


def _maybe_restore_ibkr_service(service, *, refresh_auth: bool = True, block: bool = True):
    api_app = _api_app()
    if not service:
        return

    restore_lock = getattr(api_app, "_ibkr_restore_lock", None)
    if restore_lock is None:
        from threading import Lock

        restore_lock = Lock()
        api_app._ibkr_restore_lock = restore_lock

    acquired = restore_lock.acquire(blocking=bool(block))
    if not acquired:
        return

    try:
        runtime_environment = _ibkr_service_environment(service)
        control = get_ibkr_runtime_control(runtime_environment)
        if not control.get("desired_running"):
            return
        timestamps = api_app.build_runtime_timestamps()
        auth_status = {}
        session_keeper = getattr(service, "session_keeper", None)
        if session_keeper is not None:
            status_fn = getattr(session_keeper, "status", None)
            check_fn = getattr(session_keeper, "check_auth_status", None)
            try:
                if refresh_auth and callable(check_fn):
                    auth_status = check_fn()
                elif callable(status_fn):
                    auth_status = status_fn()
            except Exception:
                traceback.print_exc()
                auth_status = {}
        authenticated = _session_authenticated(service, auth_status)
        if getattr(api_app, "_ibkr_restore_attempted", False) and _recent_restore_attempt_blocks_self_heal(
            api_app,
            control,
            _timestamp_ms(timestamps),
        ):
            return
        if (
            hasattr(service, "clear_stale_startup_cycle")
            and not getattr(service, "is_starting", False)
            and not getattr(service, "is_running", False)
            and authenticated
        ):
            try:
                service.clear_stale_startup_cycle(
                    reason="authenticated_before_restore",
                    source="runtime_self_heal" if getattr(api_app, "_ibkr_restore_attempted", False) else "server_boot",
                )
            except Exception:
                traceback.print_exc()
        if hasattr(service, "auto_restore_guard"):
            try:
                guard = service.auto_restore_guard() or {}
            except Exception:
                traceback.print_exc()
                guard = {}
            if guard.get("blocked"):
                return

        if getattr(api_app, "_ibkr_restore_attempted", False):
            if _maybe_start_stopped_runtime_self_heal(
                service,
                api_app=api_app,
                runtime_environment=runtime_environment,
                control=control,
                timestamps=timestamps,
                authenticated=authenticated,
            ):
                return
            _maybe_restart_websocket_self_heal(
                service,
                api_app=api_app,
                runtime_environment=runtime_environment,
                control=control,
                timestamps=timestamps,
                authenticated=authenticated,
            )
            return

        if _maybe_restart_websocket_self_heal(
            service,
            api_app=api_app,
            runtime_environment=runtime_environment,
            control=control,
            timestamps=timestamps,
            authenticated=authenticated,
        ):
            return

        if getattr(service, "is_busy", False):
            return

        api_app._ibkr_restore_attempted = True
        api_app._ibkr_restore_last_attempt_ms = _timestamp_ms(timestamps)
        restore_trigger_login = False
        set_ibkr_runtime_control(
            runtime_environment,
            True,
            source="server_boot",
            reason="auto_restore",
            extra={
                "last_restore_attempt_at": _timestamp_text(timestamps),
                "last_restore_attempt_ms": _timestamp_ms(timestamps),
                "last_restore_trigger_login": restore_trigger_login,
                **_reset_self_heal_extra(),
            },
        )
        print(
            f"[IBKR] Auto-restore requested for env={runtime_environment}; "
            f"starting runtime with trigger_login={restore_trigger_login}"
        )
        _background_start_ibkr_service(
            service,
            trigger_login=restore_trigger_login,
            reason="auto_restore",
            source="server_boot",
        )
    finally:
        restore_lock.release()
