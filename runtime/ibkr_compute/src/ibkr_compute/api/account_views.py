from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone


def _api_app():
    from . import app as api_app

    return api_app


def _app_coerce_float(value, default: float | None = None) -> float | None:
    return _api_app()._coerce_float(value, default)


def _summary_lookup(summary: dict) -> dict:
    if not isinstance(summary, dict):
        return {}
    return {str(key).strip().lower(): value for key, value in summary.items()}


def _extract_summary_number(summary_map: dict, *keys: str) -> float:
    for key in keys:
        raw_value = summary_map.get(str(key).strip().lower())
        if isinstance(raw_value, dict):
            lowered = {str(k).strip().lower(): v for k, v in raw_value.items()}
            for field in ("amount", "value"):
                number = _app_coerce_float(lowered.get(field))
                if number is not None:
                    return float(number)
        else:
            number = _app_coerce_float(raw_value)
            if number is not None:
                return float(number)
    return 0.0


def _extract_summary_text(summary_map: dict, *keys: str) -> str:
    for key in keys:
        raw_value = summary_map.get(str(key).strip().lower())
        if isinstance(raw_value, dict):
            lowered = {str(k).strip().lower(): v for k, v in raw_value.items()}
            for field in ("value", "displayvalue", "text"):
                value = lowered.get(field)
                if value not in (None, ""):
                    return str(value)
            amount = lowered.get("amount")
            if amount not in (None, ""):
                return str(amount)
        elif raw_value not in (None, ""):
            return str(raw_value)
    return ""


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


def _coerce_time_ms(value) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except Exception:
            return 0
        if number <= 0:
            return 0
        return int(number if number > 1e12 else number * 1000)

    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        number = int(text)
        return int(number if number > 1_000_000_000_000 else number * 1000)
    if len(text) == 17 and text[8] == "-" and text[:8].isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}T{text[9:]}"
    elif len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    else:
        text = text.replace(" ", "T")

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _normalize_live_position(position: dict) -> dict:
    quantity = float(_app_coerce_float(position.get("position"), 0.0) or 0.0)
    market_price = float(_app_coerce_float(position.get("mktPrice"), 0.0) or 0.0)
    market_value = _app_coerce_float(position.get("mktValue"))
    if market_value is None:
        market_value = quantity * market_price

    return {
        "symbol": str(position.get("ticker") or position.get("contractDesc") or "").strip().upper(),
        "conid": int(_app_coerce_float(position.get("conid"), 0) or 0),
        "quantity": quantity,
        "direction": "long" if quantity > 0 else "short" if quantity < 0 else "flat",
        "avg_cost": float(_app_coerce_float(position.get("avgCost"), 0.0) or 0.0),
        "avg_price": float(_app_coerce_float(position.get("avgPrice"), 0.0) or 0.0),
        "market_price": market_price,
        "market_value": float(market_value or 0.0),
        "unrealized_pnl": float(_app_coerce_float(position.get("unrealizedPnl"), 0.0) or 0.0),
        "realized_pnl": float(_app_coerce_float(position.get("realizedPnl"), 0.0) or 0.0),
        "account": str(position.get("acctId") or position.get("account") or "").strip(),
        "currency": str(position.get("currency") or "USD").strip().upper(),
        "asset_class": str(position.get("assetClass") or "").strip().upper(),
        "raw": position,
    }


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


def _extract_market_date_text(value) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    normalized = text.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except Exception:
        return ""
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d")
    return parsed.astimezone(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")


def _order_history_time_value(record: dict) -> str:
    for key in ("order_time", "us_time", "updated", "created", "fill_time"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _normalize_broker_history_order(order: dict) -> dict:
    live_order = _normalize_live_order(order)
    order_id = str(live_order.get("order_id") or "").strip()
    parent_id = str(live_order.get("parent_id") or "").strip()
    coid = _extract_live_order_text(order, "cOID", "coid", "order_ref", "orderRef")
    unique_id = coid or order_id
    entry_unique_id = parent_id or coid or order_id
    if not unique_id:
        unique_id = order_id
    if not entry_unique_id:
        entry_unique_id = unique_id
    status = _display_order_status(live_order.get("status"))
    canonical_status = _canonical_order_status(status)
    closed_statuses = {"FILLED", "CANCELED"}
    submitted_time = str(live_order.get("submitted_time") or "").strip()
    fill_time = str(live_order.get("last_execution_time") or "").strip()
    updated_time = fill_time or submitted_time or datetime.utcnow().isoformat()
    direction = _direction_from_side(live_order.get("side"))

    return {
        "source": "ibkr_direct",
        "source_kind": "broker_order",
        "source_label": "IBKR Direct",
        "unique_id": unique_id,
        "order_id": order_id,
        "broker_order_id": order_id,
        "order_type": live_order.get("order_type") or "",
        "symbol": live_order.get("symbol") or "",
        "direction": direction,
        "position_side": direction,
        "trade_group_id": entry_unique_id or unique_id,
        "entry_order_unique_id": entry_unique_id,
        "parent_order_unique_id": parent_id,
        "role": live_order.get("role") or "",
        "relation_status": "closed" if canonical_status in closed_statuses else "active",
        "quantity": live_order.get("total_quantity") or 0,
        "limit_price": live_order.get("price") or 0,
        "status": status,
        "filled_qty": live_order.get("filled_quantity") or 0,
        "fill_price": live_order.get("avg_price") or 0,
        "order_time": submitted_time,
        "fill_time": fill_time,
        "us_time": submitted_time,
        "updated": updated_time,
        "diagnostic_state": "",
        "diagnostic_note": "",
        "raw": live_order.get("raw") or {},
    }


def _normalize_pb_history_order(record: dict) -> dict:
    return {
        "record_id": str(record.get("id") or "").strip(),
        "order_id": str(record.get("order_id") or "").strip(),
        "broker_order_id": str(record.get("broker_order_id") or record.get("order_id") or "").strip(),
        "symbol": str(record.get("symbol") or "").strip().upper(),
        "status": _display_order_status(record.get("status")),
        "quantity": float(_app_coerce_float(record.get("quantity"), 0.0) or 0.0),
        "filled_qty": float(_app_coerce_float(record.get("filled_qty"), 0.0) or 0.0),
        "time_value": _order_history_time_value(record),
        "raw": record,
    }


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


def _build_ibkr_account_snapshot(service) -> dict:
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
    if not account_id and hasattr(service, "order_lifecycle"):
        account_id = str(getattr(service.order_lifecycle, "account_id", "") or "").strip()

    cache_key = (runtime_environment, account_id)
    now = time.time()
    with api_app.ibkr_account_snapshot_cache_lock:
        cached_entry = api_app.ibkr_account_snapshot_cache.get(cache_key)
        if cached_entry and float(cached_entry.get("expires_at", 0) or 0) > now:
            return dict(cached_entry.get("payload") or {})
        if cached_entry:
            api_app.ibkr_account_snapshot_cache.pop(cache_key, None)

    summary_raw = {}
    positions_raw = []
    orders_raw = []
    summary_error = ""
    positions_error = ""
    orders_error = ""

    fetchers = {}
    if hasattr(service, "order_lifecycle") and service.order_lifecycle:
        fetchers["summary"] = lambda: service.order_lifecycle.get_account_summary(account_id)
        fetchers["positions"] = lambda: service.order_lifecycle.get_positions(account_id)
    if hasattr(service, "order_tracker") and service.order_tracker:
        fetchers["orders"] = service.order_tracker.get_live_orders

    if fetchers:
        with ThreadPoolExecutor(max_workers=len(fetchers), thread_name_prefix="ibkr-account") as executor:
            future_map = {
                executor.submit(fetcher): name
                for name, fetcher in fetchers.items()
            }
            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    value = future.result()
                except Exception as exc:
                    if name == "summary":
                        summary_error = str(exc)
                    elif name == "positions":
                        positions_error = str(exc)
                    else:
                        orders_error = str(exc)
                    continue

                if name == "summary":
                    summary_raw = value if isinstance(value, dict) else {}
                elif name == "positions":
                    positions_raw = value if isinstance(value, list) else []
                else:
                    orders_raw = value if isinstance(value, list) else []

    fallback_ids = []
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
        fallback_ids = [str(r.get("broker_order_id") or "").strip() for r in (pb_active or []) if r.get("broker_order_id")]
    except Exception as exc:
        api_app.logger.debug("Live orders PB fallback seed load failed: %s", exc)

    live_open_payload = {
        "orders": [],
        "coverage": {
            "coverage_state": "complete",
            "bulk_open_count": 0,
            "recovered_from_status_count": 0,
            "tracker_seed_count": 0,
            "pb_seed_count": len(fallback_ids),
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
    if hasattr(service, "order_tracker") and service.order_tracker:
        try:
            live_open_payload = service.order_tracker.get_complete_live_open_orders(
                pb_seed_ids=fallback_ids,
                bulk_orders=orders_raw,
                force=True,
            )
            existing_ids = {
                str(item.get("orderId") or item.get("order_id") or item.get("id") or "").strip()
                for item in (orders_raw or [])
                if isinstance(item, dict)
            }
            merged_orders = list(orders_raw or [])
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
                orders_raw = merged_orders
                api_app.logger.info(
                    "Live orders supplemental fallback: bulk=%d recovered=%d total=%d",
                    max(len(existing_ids) - recovered_count, 0),
                    recovered_count,
                    len(orders_raw),
                )
        except Exception as exc:
            api_app.logger.debug("Live open order recovery failed: %s", exc)

    positions = [_normalize_live_position(item) for item in (positions_raw or []) if isinstance(item, dict)]
    orders = [_normalize_live_order(item) for item in (orders_raw or []) if isinstance(item, dict)]
    live_open_orders = [_normalize_live_order(item) for item in (live_open_payload.get("orders") or []) if isinstance(item, dict)]
    if not live_open_orders:
        existing_coverage = live_open_payload.get("coverage") or {}
        live_open_orders = [item for item in orders if item.get("is_open")]
        live_open_payload["coverage"] = {
            "coverage_state": str(existing_coverage.get("coverage_state") or "complete"),
            "bulk_open_count": int(existing_coverage.get("bulk_open_count") or len(live_open_orders)),
            "recovered_from_status_count": int(existing_coverage.get("recovered_from_status_count") or 0),
            "tracker_seed_count": int(existing_coverage.get("tracker_seed_count") or 0),
            "pb_seed_count": int(existing_coverage.get("pb_seed_count") or len(fallback_ids)),
            "unresolved_seed_count": int(existing_coverage.get("unresolved_seed_count") or 0),
            "unresolved_order_ids": list(existing_coverage.get("unresolved_order_ids") or []),
        }
    summary_map = _summary_lookup(summary_raw)

    total_unrealized = sum(float(item.get("unrealized_pnl", 0) or 0) for item in positions)
    total_market_value = sum(abs(float(item.get("market_value", 0) or 0)) for item in positions)
    open_orders_count = len(live_open_orders)
    cancelable_orders_count = len([item for item in live_open_orders if item.get("can_cancel")])
    editable_orders_count = len([item for item in live_open_orders if item.get("can_modify")])
    outside_rth_orders_count = len([item for item in live_open_orders if item.get("outside_rth")])
    recovered_open_orders_count = len([
        item for item in live_open_orders
        if str(item.get("recovery_source") or "").strip() == "status_recovered"
    ])

    summary = {
        "account_code": _extract_summary_text(summary_map, "accountcode") or account_id,
        "account_type": _extract_summary_text(summary_map, "accounttype"),
        "net_liquidation": _extract_summary_number(summary_map, "netliquidation", "netliq"),
        "available_funds": _extract_summary_number(summary_map, "availablefunds"),
        "buying_power": _extract_summary_number(summary_map, "buyingpower"),
        "excess_liquidity": _extract_summary_number(summary_map, "excessliquidity"),
        "equity_with_loan": _extract_summary_number(summary_map, "equitywithloanvalue"),
        "gross_position_value": _extract_summary_number(summary_map, "grosspositionvalue", "stockmarketvalue") or total_market_value,
        "total_cash_value": _extract_summary_number(summary_map, "totalcashvalue", "cashbalance", "settledcash"),
        "initial_margin": _extract_summary_number(summary_map, "initmarginreq"),
        "maintenance_margin": _extract_summary_number(summary_map, "maintmarginreq"),
        "unrealized_pnl": _extract_summary_number(summary_map, "unrealizedpnl") or total_unrealized,
        "realized_pnl": _extract_summary_number(summary_map, "realizedpnl"),
        "currency": _extract_summary_text(summary_map, "currency", "basecurrency") or "USD",
    }

    payload = {
        "ok": True,
        "environment": runtime_environment,
        "account_id": account_id,
        "service_running": bool(getattr(service, "is_running", False)),
        "service_starting": bool(getattr(service, "is_starting", False)),
        "session_authenticated": bool((service_status.get("session") or {}).get("authenticated")),
        "gateway_running": bool((service_status.get("gateway") or {}).get("running")),
        "websocket_ready": bool((service_status.get("websocket") or {}).get("ready")),
        "summary": summary,
        "summary_raw": summary_raw if isinstance(summary_raw, dict) else {},
        "positions": positions,
        "orders": orders,
        "live_open_orders": live_open_orders,
        "live_order_coverage": live_open_payload.get("coverage") or {},
        "recovery_diagnostics": live_open_payload.get("diagnostics") or {},
        "counts": {
            "positions": len(positions),
            "open_positions": len([item for item in positions if float(item.get("quantity", 0) or 0) != 0]),
            "orders": len(orders),
            "open_orders": open_orders_count,
            "cancelable_orders": cancelable_orders_count,
            "editable_orders": editable_orders_count,
            "outside_rth_orders": outside_rth_orders_count,
            "recovered_open_orders": recovered_open_orders_count,
        },
        "errors": {
            "summary": summary_error,
            "positions": positions_error,
            "orders": orders_error,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_expires_at = time.time() + api_app.IBKR_ACCOUNT_SNAPSHOT_TTL_SECONDS
    with api_app.ibkr_account_snapshot_cache_lock:
        api_app.ibkr_account_snapshot_cache[cache_key] = {
            "expires_at": cache_expires_at,
            "payload": payload,
        }
    return payload
