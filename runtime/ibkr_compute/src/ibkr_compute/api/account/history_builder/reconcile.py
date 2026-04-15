from __future__ import annotations

from ibkr_compute.api.account.history_builder.normalize import _normalize_pb_history_order
from ibkr_compute.api.account.live import (
    _api_app,
    _app_coerce_float,
    _canonical_order_status,
    _extract_market_date_text,
)


def _build_broker_order_reconciliation(service, environment: str, broker_orders: list[dict]) -> dict:
    api_app = _api_app()
    pb_error = ""
    market_date = api_app.current_market_date()
    pb_today_rows = []
    matched_count = 0
    broker_only_ids = []
    status_mismatches = []
    filled_qty_mismatches = []
    quantity_mismatches = []

    try:
        pb_rows = []
        if getattr(service, "pb", None):
            safe_environment = str(environment or "live").replace("\\", "\\\\").replace('"', '\\"')
            pb_rows = service.pb.get_records(
                "orders",
                filter=f'environment = "{safe_environment}"',
                sort="-updated",
                per_page=200,
                page=1,
            )
        for row in pb_rows or []:
            if not isinstance(row, dict):
                continue
            normalized = _normalize_pb_history_order(row)
            if _extract_market_date_text(normalized.get("time_value")) != market_date:
                continue
            pb_today_rows.append(normalized)
    except Exception as exc:
        pb_error = str(exc)
        pb_today_rows = []

    pb_by_order_id = {}
    for row in pb_today_rows:
        key = str(row.get("broker_order_id") or row.get("order_id") or "").strip()
        if key and key not in pb_by_order_id:
            pb_by_order_id[key] = row

    for order in broker_orders:
        order_id = str(order.get("broker_order_id") or order.get("order_id") or "").strip()
        if not order_id:
            order["diagnostic_state"] = "missing_broker_order_id"
            order["diagnostic_note"] = "IBKR 未返回 broker order id，无法和 PB 订单表对账"
            continue

        pb_match = pb_by_order_id.pop(order_id, None)
        if not pb_match:
            broker_only_ids.append(order_id)
            order["diagnostic_state"] = "missing_in_pb"
            order["diagnostic_note"] = "IBKR 有该订单，但 PB 今日订单表未找到对应 broker_order_id"
            continue

        matched_count += 1
        mismatch_fields = []
        if _canonical_order_status(order.get("status")) != _canonical_order_status(pb_match.get("status")):
            mismatch_fields.append("status")
            status_mismatches.append(
                {
                    "broker_order_id": order_id,
                    "symbol": order.get("symbol") or pb_match.get("symbol") or "",
                    "ibkr_status": order.get("status") or "",
                    "pb_status": pb_match.get("status") or "",
                }
            )

        broker_quantity = float(_app_coerce_float(order.get("quantity"), 0.0) or 0.0)
        pb_quantity = float(_app_coerce_float(pb_match.get("quantity"), 0.0) or 0.0)
        if abs(broker_quantity - pb_quantity) > 1e-9:
            mismatch_fields.append("quantity")
            quantity_mismatches.append(
                {
                    "broker_order_id": order_id,
                    "symbol": order.get("symbol") or pb_match.get("symbol") or "",
                    "ibkr_quantity": broker_quantity,
                    "pb_quantity": pb_quantity,
                }
            )

        broker_filled = float(_app_coerce_float(order.get("filled_qty"), 0.0) or 0.0)
        pb_filled = float(_app_coerce_float(pb_match.get("filled_qty"), 0.0) or 0.0)
        if abs(broker_filled - pb_filled) > 1e-9:
            mismatch_fields.append("filled_qty")
            filled_qty_mismatches.append(
                {
                    "broker_order_id": order_id,
                    "symbol": order.get("symbol") or pb_match.get("symbol") or "",
                    "ibkr_filled_qty": broker_filled,
                    "pb_filled_qty": pb_filled,
                }
            )

        if mismatch_fields:
            order["diagnostic_state"] = "field_mismatch"
            order["diagnostic_note"] = f'PB 对账字段不一致: {", ".join(mismatch_fields)}'
        else:
            order["diagnostic_state"] = "matched"
            order["diagnostic_note"] = "IBKR 与 PB 今日订单记录一致"

    pb_only_ids = sorted(pb_by_order_id.keys())

    return {
        "market_date": market_date,
        "pb_error": pb_error,
        "pb_today_count": len(pb_today_rows),
        "broker_today_count": len(broker_orders),
        "matched_count": matched_count,
        "broker_only_count": len(broker_only_ids),
        "pb_only_count": len(pb_only_ids),
        "status_mismatch_count": len(status_mismatches),
        "filled_qty_mismatch_count": len(filled_qty_mismatches),
        "quantity_mismatch_count": len(quantity_mismatches),
        "broker_only_ids": broker_only_ids[:20],
        "pb_only_ids": pb_only_ids[:20],
        "status_mismatches": status_mismatches[:20],
        "filled_qty_mismatches": filled_qty_mismatches[:20],
        "quantity_mismatches": quantity_mismatches[:20],
    }


__all__ = ["_build_broker_order_reconciliation"]
