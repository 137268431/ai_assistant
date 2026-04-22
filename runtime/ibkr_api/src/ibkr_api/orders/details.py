from __future__ import annotations

from typing import Any

from ibkr_api.orders.timestamps import format_timestamp_ms
from ibkr_api.orders.values import escape_filter_string, strip_internal_extra, to_text


def build_order_detail_payload(
    pb: Any,
    order_row: dict[str, Any],
    *,
    source: str,
    reason: str,
) -> dict[str, Any]:
    unique_id = to_text(order_row.get("unique_id") or order_row.get("id"))
    environment = to_text(order_row.get("environment") or "live") or "live"
    detail_rows = pb.get_records(
        "ibkr_order_details",
        filter=(
            f'order_id = "{escape_filter_string(unique_id)}" && '
            f'environment = "{escape_filter_string(environment)}"'
        ),
        sort="-created,-bar_time_ms",
        per_page=200,
        page=1,
    )
    sequence = len(detail_rows or []) + 1
    extra = strip_internal_extra(order_row.get("extra"))
    default_times = format_timestamp_ms(None)
    return {
        "order_id": unique_id,
        "symbol": order_row.get("symbol") or "",
        "environment": environment,
        "direction": order_row.get("direction") or "",
        "order_type": order_row.get("order_type") or "",
        "status": order_row.get("status") or "",
        "reason": reason,
        "signal_id": order_row.get("signal_id") or "",
        "us_time": order_row.get("us_time") or default_times["us_time"],
        "cn_time": order_row.get("cn_time") or default_times["cn_time"],
        "bar_time_ms": order_row.get("bar_time_ms") or default_times["bar_time_ms"],
        "broker_order_id": order_row.get("broker_order_id") or "",
        "trade_group_id": order_row.get("trade_group_id") or "",
        "entry_order_unique_id": order_row.get("entry_order_unique_id") or "",
        "parent_order_unique_id": order_row.get("parent_order_unique_id") or "",
        "sibling_order_unique_id": order_row.get("sibling_order_unique_id") or "",
        "role": order_row.get("role") or "",
        "relation_status": order_row.get("relation_status") or "",
        "position_side": order_row.get("position_side") or "",
        "extra": {
            "sequence": sequence,
            "environment": environment,
            "source": source,
            "status": order_row.get("status") or "",
            "original_order_id": order_row.get("order_id") or "",
            "order_id": order_row.get("order_id") or "",
            "broker_order_id": order_row.get("broker_order_id") or "",
            "trade_group_id": order_row.get("trade_group_id") or "",
            "entry_order_unique_id": order_row.get("entry_order_unique_id") or "",
            "parent_order_unique_id": order_row.get("parent_order_unique_id") or "",
            "sibling_order_unique_id": order_row.get("sibling_order_unique_id") or "",
            "role": order_row.get("role") or "",
            "relation_status": order_row.get("relation_status") or "",
            "position_side": order_row.get("position_side") or "",
            "quantity": order_row.get("quantity"),
            "limit_price": order_row.get("limit_price"),
            "fill_price": order_row.get("fill_price"),
            "filled_qty": order_row.get("filled_qty"),
            "tp_price": order_row.get("tp_price"),
            "sl_price": order_row.get("sl_price"),
            "pnl": order_row.get("pnl"),
            "commission": order_row.get("commission"),
            "rr_ratio": order_row.get("rr_ratio"),
            "order_time": order_row.get("order_time") or "",
            **extra,
        },
    }
