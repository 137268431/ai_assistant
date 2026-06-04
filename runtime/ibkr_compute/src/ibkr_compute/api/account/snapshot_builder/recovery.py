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
        return [
            str(row.get("broker_order_id") or "").strip()
            for row in (pb_active or [])
            if row.get("broker_order_id")
        ]
    except Exception as exc:
        api_app.logger.debug("Live orders PB fallback seed load failed: %s", exc)
        return []


def recover_live_open_orders(api_app, service, orders_raw: list[dict], fallback_ids: list[str]) -> tuple[list[dict], dict]:
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
    "recover_live_open_orders",
]
