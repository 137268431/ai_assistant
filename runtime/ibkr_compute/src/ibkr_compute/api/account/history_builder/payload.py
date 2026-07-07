from __future__ import annotations

from datetime import datetime

from ibkr_compute.api.account.history_builder.normalize import _normalize_broker_history_order
from ibkr_compute.api.account.history_builder.reconcile import _build_broker_order_reconciliation
from ibkr_compute.api.account.live import _api_app, _canonical_order_status
from ibkr_compute.api.shared.service_status import get_service_status_snapshot


def _build_ibkr_order_history(service, requested_days: int = 1, *, broker_force: bool = False) -> dict:
    api_app = _api_app()
    runtime_environment = api_app._ibkr_service_environment(service)
    service_status = get_service_status_snapshot(service)
    use_paper = api_app._ibkr_service_uses_paper_account(service)
    account_id = ""
    if hasattr(service, "order_placer"):
        try:
            account_id = str(service.order_placer.get_active_account_id(use_paper=use_paper) or "").strip()
        except Exception:
            account_id = ""

    broker_payload = {}
    broker_error = ""
    try:
        broker_payload = service.order_tracker.get_broker_order_history(
            days=requested_days,
            force=bool(broker_force),
            include_executions=bool(broker_force),
        )
    except Exception as exc:
        broker_error = str(exc)
        broker_payload = {}

    if not broker_error:
        broker_error = str(broker_payload.get("error") or "").strip()

    execution_diagnostics = {}
    if isinstance(broker_payload.get("execution_diagnostics"), dict):
        execution_diagnostics = broker_payload.get("execution_diagnostics")
    raw_orders = broker_payload.get("orders") or []
    broker_orders = [
        _normalize_broker_history_order(item)
        for item in raw_orders
        if isinstance(item, dict)
    ]
    reconciliation = _build_broker_order_reconciliation(service, runtime_environment, broker_orders)
    pb_today_rows = reconciliation.pop("pb_today_rows", [])
    history_items = list(broker_orders)
    if not broker_force:
        seen_order_ids = {
            str(item.get("broker_order_id") or item.get("order_id") or "").strip()
            for item in history_items
            if isinstance(item, dict)
        }
        for row in pb_today_rows if isinstance(pb_today_rows, list) else []:
            if not isinstance(row, dict):
                continue
            order_id = str(row.get("broker_order_id") or row.get("order_id") or "").strip()
            if order_id and order_id in seen_order_ids:
                continue
            if order_id:
                seen_order_ids.add(order_id)
            row = dict(row)
            row["diagnostic_state"] = row.get("diagnostic_state") or "pb_cache_only"
            row["diagnostic_note"] = row.get("diagnostic_note") or "默认历史使用 PB 今日订单缓存，未强制拉取 IBKR broker/executions。"
            history_items.append(row)

    canonical_statuses = [_canonical_order_status(item.get("status")) for item in history_items]
    fetched_at = datetime.utcnow().isoformat()
    return {
        "ok": not broker_error,
        "error": broker_error,
        "environment": runtime_environment,
        "account_id": account_id,
        "source": "ibkr_direct_order_history" if broker_force else "ibkr_cached_order_history",
        "broker_force": bool(broker_force),
        "executions_requested": bool(broker_payload.get("executions_requested")),
        "execution_diagnostics": {
            "executions_requested": bool(broker_payload.get("executions_requested")),
            **dict(execution_diagnostics),
            "execution_count": int(
                execution_diagnostics.get("execution_count")
                or len((broker_payload.get("executions") or []))
            ),
            "order_count": int(execution_diagnostics.get("order_count") or len(raw_orders)),
        },
        "service_running": bool(getattr(service, "is_running", False)),
        "session_authenticated": bool((service_status.get("session") or {}).get("authenticated")),
        "gateway_running": bool((service_status.get("gateway") or {}).get("running")),
        "requested_days": max(1, int(requested_days or 1)),
        "effective_days": int(broker_payload.get("effective_days") or 1),
        "current_day_only": bool(broker_payload.get("current_day_only", True)),
        "items": history_items,
        "counts": {
            "total": len(history_items),
            "open": len([item for item in canonical_statuses if item == "SUBMITTED"]),
            "filled": len([item for item in canonical_statuses if item == "FILLED"]),
            "canceled": len([item for item in canonical_statuses if item == "CANCELED"]),
        },
        "reconciliation": reconciliation,
        "limitations": broker_payload.get("limitations") or [
            "IBKR Client Portal /iserver/account/orders 仅返回当前美东交易日订单。",
            "如果需要跨日历史订单，请补充 Flex / Statement 链路。",
        ],
        "errors": {
            "broker": broker_error,
            "pb": reconciliation.get("pb_error") or "",
        },
        "fetched_at": fetched_at,
        "raw": broker_payload.get("raw") if isinstance(broker_payload.get("raw"), dict) else {},
    }


__all__ = ["_build_ibkr_order_history"]
