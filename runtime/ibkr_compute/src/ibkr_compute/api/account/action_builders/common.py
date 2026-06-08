from __future__ import annotations

import time

from ibkr_compute.api.account.snapshot import _build_ibkr_account_snapshot
from ibkr_compute.observability.prometheus import record_order_event


_CLOSED_ORDER_STATUSES = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}


def _cached_order_rows(service) -> list[dict]:
    tracker = getattr(service, "order_tracker", None)
    getter = getattr(tracker, "get_cached_live_orders", None)
    if not callable(getter):
        return []
    try:
        rows = getter(include_all=True)
    except TypeError:
        try:
            rows = getter()
        except Exception:
            return []
    except Exception:
        return []
    return [dict(item) for item in list(rows or []) if isinstance(item, dict)]


def _order_status(row: dict) -> str:
    return str(row.get("status") or row.get("order_status") or row.get("orderStatus") or "").strip().upper()


def _build_fast_action_snapshot(service) -> dict:
    rows = _cached_order_rows(service)
    open_rows = [item for item in rows if _order_status(item) not in _CLOSED_ORDER_STATUSES]
    return {
        "ok": True,
        "environment": str(getattr(service, "environment", "") or ""),
        "source": "account_action_orders_fast",
        "snapshot_profile": "orders_fast_action",
        "orders_fast": True,
        "summary_available": None,
        "orders": rows,
        "live_open_orders": open_rows,
        "counts": {
            "orders": len(rows),
            "open_orders": len(open_rows),
            "cancelable_orders": len(open_rows),
            "editable_orders": len(open_rows),
            "positions": 0,
            "open_positions": 0,
        },
        "orders_fast_diagnostics": {
            "order_source": "callback_cache",
            "cached_order_count": len(rows),
            "pb_fallback_order_count": 0,
            "pb_fallback_skipped": True,
            "skipped_account_data_fetch": True,
            "skipped_full_snapshot": True,
        },
    }


def _build_minimal_action_snapshot(service) -> dict:
    return {
        "ok": True,
        "environment": str(getattr(service, "environment", "") or ""),
        "source": "account_action_snapshot_skipped",
        "snapshot_profile": "minimal_action",
        "snapshot_skipped": True,
        "summary_available": None,
        "orders": [],
        "live_open_orders": [],
        "positions": [],
        "counts": {
            "orders": 0,
            "open_orders": 0,
            "positions": 0,
            "open_positions": 0,
        },
    }


def _build_snapshot_action_response(
    service,
    action: str,
    result: dict,
    *,
    delay_seconds: float = 0.0,
    extra: dict | None = None,
    snapshot_force_refresh: bool | None = None,
    snapshot_allow_stale: bool | None = None,
    include_snapshot: bool = True,
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
            "cancel_order_ids",
            "cancel_all_orders",
            "close_position",
        }
        force_refresh = bool(snapshot_force_refresh) if snapshot_force_refresh is not None else action not in fast_snapshot_actions
        allow_stale = bool(snapshot_allow_stale) if snapshot_allow_stale is not None else action in fast_snapshot_actions
        use_orders_fast_snapshot = action in fast_snapshot_actions
        if not include_snapshot:
            snapshot = _build_minimal_action_snapshot(service)
        elif use_orders_fast_snapshot:
            snapshot = _build_fast_action_snapshot(service)
        else:
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
