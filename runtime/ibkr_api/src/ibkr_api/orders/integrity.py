from __future__ import annotations

from typing import Any, Callable

from ibkr_api.orders.reconcile import build_orders_reconcile_response


NormalizeEnvironment = Callable[[Any, str], str]
EscapeFilterString = Callable[[Any], str]


def build_order_detail_integrity_response(
    pb: Any,
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    escape_filter_string: EscapeFilterString,
) -> tuple[dict[str, Any], int]:
    request_payload = {
        **(payload or {}),
        "only_missing": True if (payload or {}).get("only_missing") is None else (payload or {}).get("only_missing"),
        "dry_run": False if (payload or {}).get("dry_run") is None else (payload or {}).get("dry_run"),
        "suppress_notification": True if (payload or {}).get("suppress_notification") is None else (payload or {}).get("suppress_notification"),
        "source": (payload or {}).get("source") or "order_detail_integrity_guard",
        "repair_source": (payload or {}).get("repair_source") or "order_detail_integrity_guard",
        "scan_all": True if (payload or {}).get("scan_all") is None else (payload or {}).get("scan_all"),
        "limit": (payload or {}).get("limit") or 100,
        "page_size": (payload or {}).get("page_size") or 100,
        "max_scan": (payload or {}).get("max_scan") or 500,
    }
    response, status_code = build_orders_reconcile_response(
        pb,
        payload=request_payload,
        normalize_environment=normalize_environment,
        escape_filter_string=escape_filter_string,
    )
    payload_out = dict(response if isinstance(response, dict) else {})
    payload_out.setdefault("source", "ibkr-api")
    payload_out.setdefault("job_id", "order_detail_integrity_guard")
    return payload_out, status_code
