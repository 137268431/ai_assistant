from __future__ import annotations

import traceback

from ibkr_compute.api.runtime.common import (
    _api_app,
    _background_start_ibkr_service,
    _ibkr_service_environment,
    get_ibkr_runtime_control,
    set_ibkr_runtime_control,
)


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
        if api_app._ibkr_restore_attempted:
            return

        runtime_environment = _ibkr_service_environment(service)
        control = get_ibkr_runtime_control(runtime_environment)
        if not control.get("desired_running"):
            return
        if getattr(service, "is_busy", False):
            return
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
        if (
            hasattr(service, "clear_stale_startup_cycle")
            and not getattr(service, "is_starting", False)
            and not getattr(service, "is_running", False)
            and bool(auth_status.get("authenticated"))
        ):
            try:
                service.clear_stale_startup_cycle(
                    reason="authenticated_before_restore",
                    source="server_boot",
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

        api_app._ibkr_restore_attempted = True
        timestamps = api_app.build_runtime_timestamps()
        restore_trigger_login = False
        set_ibkr_runtime_control(
            runtime_environment,
            True,
            source="server_boot",
            reason="auto_restore",
            extra={
                "last_restore_attempt_at": timestamps.get("us", ""),
                "last_restore_trigger_login": restore_trigger_login,
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
