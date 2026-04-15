from __future__ import annotations

import traceback

from ibkr_compute.api.runtime.common import (
    _api_app,
    _background_start_ibkr_service,
    _ibkr_service_environment,
    get_ibkr_runtime_control,
    set_ibkr_runtime_control,
)


def _maybe_restore_ibkr_service(service):
    api_app = _api_app()
    if not service or api_app._ibkr_restore_attempted:
        return

    runtime_environment = _ibkr_service_environment(service)
    control = get_ibkr_runtime_control(runtime_environment)
    if not control.get("desired_running"):
        return
    if getattr(service, "is_busy", False):
        return
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
