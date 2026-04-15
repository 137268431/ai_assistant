from __future__ import annotations

from ibkr_compute.api.account.live.runtime import _app_coerce_float
from ibkr_compute.api.account.live.time import _coerce_time_ms


def _extract_live_order_text(order: dict, *keys: str) -> str:
    for key in keys:
        value = order.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _coerce_live_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _canonical_order_status(value) -> str:
    text = str(value or "").strip().upper()
    if text in {"PENDING", "PRESUBMITTED", "SUBMITTED", "PENDINGSUBMIT", "INPROGRESS", "INIT"}:
        return "SUBMITTED"
    if text in {"FILLED", "EXECUTED"}:
        return "FILLED"
    if text in {"CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}:
        return "CANCELED"
    return text or "UNKNOWN"


def _display_order_status(value) -> str:
    canonical = _canonical_order_status(value)
    return {
        "SUBMITTED": "Submitted",
        "FILLED": "Filled",
        "CANCELED": "Canceled",
        "UNKNOWN": "Unknown",
    }.get(canonical, str(value or canonical or "Unknown").strip() or "Unknown")


def _direction_from_side(value) -> str:
    side = str(value or "").strip().upper()
    if side == "BUY":
        return "long"
    if side == "SELL":
        return "short"
    return ""


def _normalize_live_order(order: dict) -> dict:
    # Support both bulk /iserver/account/orders format and individual /iserver/account/order/status/{id} format
    status = str(order.get("status") or order.get("order_status") or order.get("orderStatus") or "").strip()
    parent_id = str(order.get("parentId") or order.get("parent_order_id") or "").strip()
    client_order_id = _extract_live_order_text(order, "cOID", "coid", "order_ref", "orderRef")
    order_type = str(order.get("orderType") or order.get("order_type") or order.get("orderDesc") or "").strip().upper()
    total_quantity = float(
        _app_coerce_float(
            order.get("totalSize")
            if order.get("totalSize") is not None
            else order.get("total_size")
            if order.get("total_size") is not None
            else order.get("quantity"),
            0.0,
        )
        or 0.0
    )
    filled_quantity = float(_app_coerce_float(order.get("filledQuantity") or order.get("cum_fill"), 0.0) or 0.0)
    remaining_quantity = _app_coerce_float(order.get("remainingQuantity") or order.get("remainingSize"))
    if remaining_quantity is None:
        remaining_quantity = max(total_quantity - filled_quantity, 0.0)

    closed_statuses = {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}
    normalized_status = status.upper()
    canonical_status = _canonical_order_status(status)
    if not parent_id:
        role = "entry"
    elif "STP" in order_type or "STOP" in order_type:
        role = "stop_loss"
    elif "LMT" in order_type or "LIMIT" in order_type:
        role = "take_profit"
    else:
        role = "child"

    price = float(_app_coerce_float(order.get("price") or order.get("limit_price"), 0.0) or 0.0)
    trigger_price = float(_app_coerce_float(order.get("auxPrice") or order.get("stop_price"), 0.0) or 0.0)
    submitted_time = _extract_live_order_text(order, "submittedTime", "submitTime", "order_time", "createdTime", "createTime")
    last_execution_time = _extract_live_order_text(order, "lastExecutionTime", "lastFillTime", "lastExecutionTime_r")
    good_till_date = _extract_live_order_text(order, "goodTillDate")
    is_open = bool(normalized_status and normalized_status not in closed_statuses)
    seed_sources = order.get("_seed_sources") or order.get("seed_sources") or []
    if isinstance(seed_sources, (tuple, set)):
        seed_sources = list(seed_sources)
    if not isinstance(seed_sources, list):
        seed_sources = [str(seed_sources)]

    return {
        "order_id": str(order.get("orderId") or order.get("order_id") or order.get("id") or "").strip(),
        "parent_id": parent_id,
        "client_order_id": client_order_id,
        "symbol": str(order.get("ticker") or order.get("symbol") or order.get("contractDesc") or order.get("contract_description_1") or "").strip().upper(),
        "conid": int(_app_coerce_float(order.get("conid") or order.get("conidex"), 0) or 0),
        "side": str(order.get("side") or "").strip().upper(),
        "status": status,
        "status_key": canonical_status,
        "role": role,
        "order_type": order_type,
        "order_description": _extract_live_order_text(order, "orderDesc", "order_description", "order_description_with_contract", "description"),
        "price": price,
        "trigger_price": trigger_price,
        "avg_price": float(_app_coerce_float(order.get("avgPrice") or order.get("average_price"), 0.0) or 0.0),
        "total_quantity": total_quantity,
        "filled_quantity": filled_quantity,
        "remaining_quantity": float(remaining_quantity or 0.0),
        "time_in_force": str(order.get("tif") or order.get("timeInForce") or "").strip().upper(),
        "account": str(order.get("acct") or order.get("acctId") or order.get("account") or "").strip(),
        "currency": str(order.get("currency") or "USD").strip().upper(),
        "asset_class": _extract_live_order_text(order, "secType", "sec_type", "assetClass").upper(),
        "listing_exchange": _extract_live_order_text(order, "listingExchange", "listing_exchange", "exchange"),
        "submitted_time": submitted_time,
        "submitted_time_ms": _coerce_time_ms(submitted_time),
        "last_execution_time": last_execution_time,
        "last_execution_time_ms": _coerce_time_ms(last_execution_time),
        "good_till_date": good_till_date,
        "good_till_date_ms": _coerce_time_ms(good_till_date),
        "outside_rth": _coerce_live_bool(order.get("outsideRth") or order.get("outside_rth"), False),
        "is_open": is_open,
        "is_child": bool(parent_id),
        "can_cancel": bool(is_open and not _coerce_live_bool(order.get("cannot_cancel_order"), False)),
        "can_modify": bool(is_open and not _coerce_live_bool(order.get("order_not_editable"), False)),
        "recovery_source": _extract_live_order_text(order, "_recovery_source", "recovery_source") or "bulk",
        "seed_sources": [str(item).strip() for item in seed_sources if str(item).strip()],
        "raw": order,
    }


__all__ = [
    "_canonical_order_status",
    "_coerce_live_bool",
    "_direction_from_side",
    "_display_order_status",
    "_extract_live_order_text",
    "_normalize_live_order",
]
