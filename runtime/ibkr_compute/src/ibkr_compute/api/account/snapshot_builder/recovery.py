from __future__ import annotations


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
        pb_active = api_app.pb.get_records(
            "orders",
            filter=(
                f'environment="{api_app._ibkr_service_environment(service)}" && broker_order_id!="" '
                '&& (relation_status="active" || relation_status="planned" || status="Submitted" || '
                'status="Init" || status="PreSubmitted" || status="PendingSubmit" || status="Pending")'
            ),
            sort="-updated",
            per_page=200,
        )
        return [dict(row) for row in (pb_active or []) if isinstance(row, dict)]
    except Exception as exc:
        api_app.logger.debug("Live orders PB fallback seed load failed: %s", exc)
        return []


def _pb_order_row_to_live_order(row: dict) -> dict:
    order_id = str(row.get("broker_order_id") or row.get("order_id") or "").strip()
    if not order_id:
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
    "pb_order_rows_to_live_orders",
    "recover_live_open_orders",
]
