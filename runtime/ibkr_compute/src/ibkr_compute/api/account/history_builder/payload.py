from __future__ import annotations

from datetime import datetime

from ibkr_compute.api.account.history_builder.normalize import _normalize_broker_history_order
from ibkr_compute.api.account.history_builder.reconcile import _build_broker_order_reconciliation
from ibkr_compute.api.account.live import _api_app, _canonical_order_status


def _build_ibkr_order_history(service, requested_days: int = 1) -> dict:
    api_app = _api_app()
    runtime_environment = api_app._ibkr_service_environment(service)
    service_status = service.status() if hasattr(service, "status") else {}
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
        broker_payload = service.order_tracker.get_broker_order_history(days=requested_days, force=True)
    except Exception as exc:
        broker_error = str(exc)
        broker_payload = {}

    if not broker_error:
        broker_error = str(broker_payload.get("error") or "").strip()

    raw_orders = broker_payload.get("orders") or []
    broker_orders = [
        _normalize_broker_history_order(item)
        for item in raw_orders
        if isinstance(item, dict)
    ]
    reconciliation = _build_broker_order_reconciliation(service, runtime_environment, broker_orders)

    canonical_statuses = [_canonical_order_status(item.get("status")) for item in broker_orders]
    fetched_at = datetime.utcnow().isoformat()
    return {
        "ok": not broker_error,
        "error": broker_error,
        "environment": runtime_environment,
        "account_id": account_id,
        "source": "ibkr_direct_order_history",
        "service_running": bool(getattr(service, "is_running", False)),
        "session_authenticated": bool((service_status.get("session") or {}).get("authenticated")),
        "gateway_running": bool((service_status.get("gateway") or {}).get("running")),
        "requested_days": max(1, int(requested_days or 1)),
        "effective_days": int(broker_payload.get("effective_days") or 1),
        "current_day_only": bool(broker_payload.get("current_day_only", True)),
        "items": broker_orders,
        "counts": {
            "total": len(broker_orders),
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
