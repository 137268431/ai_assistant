from __future__ import annotations

import time

from ibkr_compute.api.account.snapshot import _build_ibkr_account_snapshot
from ibkr_compute.observability.prometheus import record_order_event


def _build_snapshot_action_response(
    service,
    action: str,
    result: dict,
    *,
    delay_seconds: float = 0.0,
    extra: dict | None = None,
    snapshot_force_refresh: bool | None = None,
    snapshot_allow_stale: bool | None = None,
    action_started_at: float | None = None,
    operation_elapsed_s: float | None = None,
) -> tuple[dict, int]:
    response_started = time.perf_counter()
    started = float(action_started_at) if action_started_at is not None else response_started
    status = 500
    ok = False
    try:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        fast_snapshot_actions = {
            "place_order",
            "modify_order",
            "cancel_order",
            "cancel_all_orders",
            "close_position",
        }
        force_refresh = bool(snapshot_force_refresh) if snapshot_force_refresh is not None else action not in fast_snapshot_actions
        allow_stale = bool(snapshot_allow_stale) if snapshot_allow_stale is not None else action in fast_snapshot_actions
        snapshot = _build_ibkr_account_snapshot(
            service,
            force_refresh=force_refresh,
            allow_stale=allow_stale,
        )
        ok = bool((result or {}).get("ok"))
        payload = {
            "ok": ok,
            "action": action,
            "result": result,
            "snapshot": snapshot,
            "order_action_elapsed_s": round(time.perf_counter() - started, 6),
            "order_operation_elapsed_s": round(float(operation_elapsed_s), 6)
            if operation_elapsed_s is not None
            else None,
            "order_snapshot_elapsed_s": round(time.perf_counter() - response_started, 6),
        }
        if extra:
            payload.update(extra)
        if ok:
            status = 200
        else:
            error = str((result or {}).get("error") or "")
            status = 409 if error == "buying_power_blocked" else 503 if error == "buying_power_unavailable" else 500
        return payload, status
    finally:
        record_order_event(
            environment=getattr(service, "environment", ""),
            operation=f"account_action_{action}",
            result="ok" if ok else "error",
            duration_s=time.perf_counter() - started,
        )


__all__ = ["_build_snapshot_action_response"]
