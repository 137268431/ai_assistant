from __future__ import annotations

from typing import Any

from ibkr_compute.api.account.snapshot_builder.context import resolve_snapshot_runtime_environment


OPEN_ORDER_STATUSES = {
    "INIT",
    "SUBMITTED",
    "PRESUBMITTED",
    "PENDING",
    "PENDINGSUBMIT",
    "APIPENDING",
    "API_PENDING",
}
TERMINAL_ORDER_STATUSES = {
    "FILLED",
    "EXECUTED",
    "CANCELLED",
    "CANCELED",
    "INACTIVE",
    "REJECTED",
    "EXPIRED",
    "API_CANCELLED",
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _open_like_order_rows(rows: list[dict] | None) -> list[dict]:
    open_rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or row.get("order_status") or row.get("orderStatus") or "").strip().upper()
        if status and status not in TERMINAL_ORDER_STATUSES:
            open_rows.append(dict(row))
    return open_rows


def _reservation_snapshot(service) -> dict:
    store = getattr(service, "buying_power_reservations", None)
    snapshotter = getattr(store, "snapshot", None)
    if not callable(snapshotter):
        return {}
    try:
        snapshot = snapshotter()
    except Exception:
        return {}
    return dict(snapshot) if isinstance(snapshot, dict) else {}


def _active_symbol_order_command_count(service) -> int:
    for component in (
        getattr(service, "symbol_order_scheduler", None),
        getattr(getattr(service, "order_placer", None), "symbol_scheduler", None),
        getattr(getattr(service, "order_modifier", None), "symbol_scheduler", None),
    ):
        status_fn = getattr(component, "status", None)
        if not callable(status_fn):
            continue
        try:
            status = status_fn()
        except Exception:
            continue
        if not isinstance(status, dict):
            continue
        active_symbols = status.get("active_symbols") if isinstance(status.get("active_symbols"), list) else []
        try:
            queued_total = int(float(status.get("queued_total") or 0))
        except Exception:
            queued_total = 0
        return max(0, len(active_symbols) + queued_total)
    return 0


def pb_fallback_trust_context(service, broker_rows: list[dict] | None = None) -> dict:
    reservation_snapshot = _reservation_snapshot(service)
    active_reservations = [
        dict(item)
        for item in (reservation_snapshot.get("active") or [])
        if isinstance(item, dict) and str(item.get("status") or "active").strip().lower() == "active"
    ]
    active_order_commands = _active_symbol_order_command_count(service)
    broker_open_rows = _open_like_order_rows(broker_rows)
    return {
        "trusted": bool(active_reservations or active_order_commands or broker_open_rows),
        "active_reservation_count": int(reservation_snapshot.get("count") or len(active_reservations)),
        "active_order_command_count": active_order_commands,
        "broker_open_count": len(broker_open_rows),
        "active_reservations": active_reservations,
    }


def _row_matches_active_reservation(row: dict, active_reservations: list[dict]) -> bool:
    if not active_reservations:
        return False
    order_ids = {
        _normalize_text(item.get("entry_order_id"))
        for item in active_reservations
        if _normalize_text(item.get("entry_order_id"))
    }
    trade_groups = {
        _normalize_text(item.get("trade_group_id"))
        for item in active_reservations
        if _normalize_text(item.get("trade_group_id"))
    }
    signal_ids = {
        _normalize_text(item.get("signal_id"))
        for item in active_reservations
        if _normalize_text(item.get("signal_id"))
    }
    symbols = {
        _normalize_text(item.get("symbol")).upper()
        for item in active_reservations
        if _normalize_text(item.get("symbol"))
    }
    row_order_id = _normalize_text(row.get("broker_order_id") or row.get("order_id"))
    row_trade_group = _normalize_text(row.get("trade_group_id") or row.get("entry_order_unique_id"))
    row_signal_id = _normalize_text(row.get("signal_id"))
    row_symbol = _normalize_text(row.get("symbol")).upper()
    return bool(
        (row_order_id and row_order_id in order_ids)
        or (row_trade_group and row_trade_group in trade_groups)
        or (row_signal_id and row_signal_id in signal_ids)
        or (row_symbol and row_symbol in symbols)
    )


def filter_trusted_pb_fallback_order_rows(
    service,
    rows: list[dict] | None,
    *,
    broker_rows: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    open_like_rows = [dict(row) for row in (rows or []) if isinstance(row, dict) and _pb_order_row_is_open_like(row)]
    context = pb_fallback_trust_context(service, broker_rows=broker_rows)
    context["raw_count"] = len(open_like_rows)
    if not context.get("trusted"):
        context["filtered_count"] = 0
        context["filter_reason"] = "no_live_or_pressure_evidence"
        return [], context
    active_reservations = context.get("active_reservations") if isinstance(context.get("active_reservations"), list) else []
    broker_open_count = int(context.get("broker_open_count") or 0)
    if active_reservations and broker_open_count <= 0:
        matched_rows = [row for row in open_like_rows if _row_matches_active_reservation(row, active_reservations)]
        context["filtered_count"] = len(matched_rows)
        context["filter_reason"] = "active_reservation_match"
        return matched_rows, context
    context["filtered_count"] = len(open_like_rows)
    context["filter_reason"] = "trusted_live_or_pressure_evidence"
    return open_like_rows, context


def _build_default_live_open_payload(pb_seed_count: int) -> dict:
    return {
        "orders": [],
        "coverage": {
            "coverage_state": "complete",
            "bulk_open_count": 0,
            "recovered_from_status_count": 0,
            "tracker_seed_count": 0,
            "pb_seed_count": int(pb_seed_count or 0),
            "unresolved_seed_count": 0,
            "unresolved_order_ids": [],
        },
        "diagnostics": {
            "seed_sources": {},
            "recovered_order_ids": [],
            "resolved_closed_order_ids": [],
            "bulk_order_ids": [],
            "pb_fallback_trust": {},
        },
    }


def load_pb_fallback_order_ids(api_app, service) -> list[str]:
    return [
        str(row.get("broker_order_id") or row.get("order_id") or "").strip()
        for row in load_pb_fallback_order_rows(api_app, service)
        if row.get("broker_order_id") or row.get("order_id")
    ]


def load_pb_fallback_order_rows(api_app, service) -> list[dict]:
    try:
        runtime_environment = resolve_snapshot_runtime_environment(api_app, service, fast_status=True)
        pb_active = api_app.pb.get_records(
            "orders",
            filter=(
                f'environment="{runtime_environment}" && broker_order_id!="" '
                '&& (relation_status="active" || relation_status="planned" || relation_status="" || relation_status=null) '
                '&& (status="Submitted" || status="Init" || status="PreSubmitted" || '
                'status="PendingSubmit" || status="Pending" || status="ApiPending" || status="API_PENDING")'
            ),
            sort="-updated",
            per_page=200,
        )
        return [
            dict(row)
            for row in (pb_active or [])
            if isinstance(row, dict) and _pb_order_row_is_open_like(row)
        ]
    except Exception as exc:
        api_app.logger.debug("Live orders PB fallback seed load failed: %s", exc)
        return []


def _pb_order_row_is_open_like(row: dict) -> bool:
    relation_status = str(row.get("relation_status") or "").strip().lower()
    status = str(row.get("status") or "").strip().upper()
    if relation_status == "closed" or status in TERMINAL_ORDER_STATUSES:
        return False
    return relation_status in {"active", "planned", ""} or status in OPEN_ORDER_STATUSES


def _pb_order_row_to_live_order(row: dict) -> dict:
    order_id = str(row.get("broker_order_id") or row.get("order_id") or "").strip()
    if not order_id:
        return {}
    if not _pb_order_row_is_open_like(row):
        return {}
    role = str(row.get("role") or row.get("order_type") or "").strip().lower()
    direction = str(row.get("position_side") or row.get("direction") or "").strip().lower()
    is_child = role in {"take_profit", "takeprofit", "stop_loss", "stoploss"}
    side = ""
    if direction == "long":
        side = "SELL" if is_child else "BUY"
    elif direction == "short":
        side = "BUY" if is_child else "SELL"
    order_type = "STP" if role in {"stop_loss", "stoploss"} else "LMT"
    status = str(row.get("status") or "").strip() or ("Submitted" if str(row.get("relation_status") or "").lower() == "active" else "Init")
    return {
        "orderId": order_id,
        "id": order_id,
        "ticker": str(row.get("symbol") or "").strip().upper(),
        "conid": int(float(row.get("conid") or 0) or 0),
        "side": side,
        "status": status,
        "orderType": order_type,
        "quantity": float(row.get("quantity") or 0.0),
        "totalSize": float(row.get("quantity") or 0.0),
        "price": float(row.get("limit_price") or 0.0),
        "parentId": str(row.get("parent_order_unique_id") or "").strip(),
        "cOID": str(row.get("unique_id") or "").strip(),
        "orderRef": str(row.get("unique_id") or "").strip(),
        "filledQuantity": float(row.get("filled_qty") or 0.0),
        "remainingQuantity": max(float(row.get("quantity") or 0.0) - float(row.get("filled_qty") or 0.0), 0.0),
        "_recovery_source": "pb_active_seed",
        "_seed_sources": ["pb"],
    }


def pb_order_rows_to_live_orders(rows: list[dict] | None) -> list[dict]:
    orders = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = _pb_order_row_to_live_order(row)
        if item:
            orders.append(item)
    return orders


def recover_live_open_orders(
    api_app,
    service,
    orders_raw: list[dict],
    fallback_ids: list[str],
    fallback_rows: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    live_open_payload = _build_default_live_open_payload(len(fallback_ids))
    merged_orders = list(orders_raw or [])

    if not hasattr(service, "order_tracker") or not service.order_tracker:
        return merged_orders, live_open_payload

    try:
        live_open_payload = service.order_tracker.get_complete_live_open_orders(
            pb_seed_ids=fallback_ids,
            bulk_orders=orders_raw,
            force=False,
        )
        existing_ids = {
            str(item.get("orderId") or item.get("order_id") or item.get("id") or "").strip()
            for item in (orders_raw or [])
            if isinstance(item, dict)
        }
        recovered_count = 0
        for item in live_open_payload.get("orders") or []:
            if not isinstance(item, dict):
                continue
            order_id = str(item.get("orderId") or item.get("order_id") or item.get("id") or "").strip()
            if not order_id or order_id in existing_ids:
                continue
            merged_orders.append(item)
            existing_ids.add(order_id)
            recovered_count += 1
        unresolved_ids = set((live_open_payload.get("coverage") or {}).get("unresolved_order_ids") or [])
        for row in fallback_rows or []:
            item = _pb_order_row_to_live_order(row if isinstance(row, dict) else {})
            order_id = str(item.get("orderId") or "").strip()
            if not order_id or order_id in existing_ids or (unresolved_ids and order_id not in unresolved_ids):
                continue
            merged_orders.append(item)
            existing_ids.add(order_id)
            recovered_count += 1
            live_open_payload.setdefault("orders", []).append(item)
        if fallback_rows:
            coverage = live_open_payload.get("coverage") if isinstance(live_open_payload.get("coverage"), dict) else {}
            unresolved_after = [
                order_id
                for order_id in (coverage.get("unresolved_order_ids") or [])
                if str(order_id or "").strip() not in existing_ids
            ]
            coverage["unresolved_order_ids"] = unresolved_after
            coverage["unresolved_seed_count"] = len(unresolved_after)
            if not unresolved_after and str(coverage.get("coverage_state") or "") == "degraded":
                coverage["coverage_state"] = "recovered"
            live_open_payload["coverage"] = coverage
        if recovered_count:
            api_app.logger.info(
                "Live orders supplemental fallback: bulk=%d recovered=%d total=%d",
                max(len(existing_ids) - recovered_count, 0),
                recovered_count,
                len(merged_orders),
            )
    except Exception as exc:
        api_app.logger.debug("Live open order recovery failed: %s", exc)

    return merged_orders, live_open_payload


__all__ = [
    "load_pb_fallback_order_ids",
    "load_pb_fallback_order_rows",
    "filter_trusted_pb_fallback_order_rows",
    "pb_order_rows_to_live_orders",
    "pb_fallback_trust_context",
    "recover_live_open_orders",
]
