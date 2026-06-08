from __future__ import annotations

import time
from typing import Any

from ibkr_compute.api.account.action_builders.common import _build_snapshot_action_response
from ibkr_compute.api.account.live import _api_app, _app_coerce_float


def _text(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _lower(value)
    if text in {"0", "false", "no", "n", "off"}:
        return False
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    return default


def _list_text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    result: list[str] = []
    for item in raw_items:
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


def _order_value(order: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = order.get(key)
        if value not in (None, ""):
            return _text(value)
    raw = order.get("raw")
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if value not in (None, ""):
                return _text(value)
    return ""


def _order_id(order: dict[str, Any]) -> str:
    return _order_value(order, "order_id", "orderId", "id")


def _client_order_id(order: dict[str, Any]) -> str:
    return _order_value(order, "client_order_id", "cOID", "coid", "order_ref", "orderRef")


def _parent_order_id(order: dict[str, Any]) -> str:
    return _order_value(order, "parent_id", "parentId", "parent_order_id")


def _order_symbol(order: dict[str, Any]) -> str:
    return _upper(_order_value(order, "symbol", "ticker", "contractDesc", "contract_description_1"))


def _order_side(order: dict[str, Any]) -> str:
    return _upper(_order_value(order, "side", "action"))


def _order_status(order: dict[str, Any]) -> str:
    return _upper(_order_value(order, "status_key", "status", "order_status", "orderStatus"))


def _order_role(order: dict[str, Any]) -> str:
    role = _lower(_order_value(order, "role", "leg_role"))
    if role:
        return role
    order_type = _upper(_order_value(order, "order_type", "orderType", "orderDesc"))
    if _parent_order_id(order) and ("STP" in order_type or "STOP" in order_type):
        return "stop_loss"
    if _parent_order_id(order) and ("LMT" in order_type or "LIMIT" in order_type):
        return "take_profit"
    return "entry" if not _parent_order_id(order) else "child"


def _pb_context(order: dict[str, Any]) -> dict[str, Any]:
    context = order.get("pb_context")
    return dict(context) if isinstance(context, dict) else {}


def _is_open_order(order: dict[str, Any]) -> bool:
    if order.get("is_open") is False:
        return False
    status = _order_status(order)
    return status not in {"FILLED", "EXECUTED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"}


def _is_protection_order(order: dict[str, Any]) -> bool:
    role = _order_role(order)
    client_order_id = _lower(_client_order_id(order))
    return bool(
        _parent_order_id(order)
        or role in {"take_profit", "stop_loss", "repair_tp", "repair_sl", "tp", "sl"}
        or client_order_id.startswith("tp_")
        or client_order_id.startswith("sl_")
    )


def _close_side_for_direction(direction: str) -> str:
    return "SELL" if direction == "long" else "BUY" if direction == "short" else ""


def _order_matches_close_side(order: dict[str, Any], direction: str) -> bool:
    expected = _close_side_for_direction(direction)
    side = _order_side(order)
    return not expected or not side or side == expected


def _group_aliases(payload: dict[str, Any]) -> list[str]:
    values = _list_text_values(payload.get("trade_group_id"))
    values += _list_text_values(payload.get("entry_order_unique_id"))
    values += _list_text_values(payload.get("group_key"))
    aliases: list[str] = []
    for value in values:
        if value and value not in aliases:
            aliases.append(value)
        if value.startswith("entry_"):
            stripped = value.removeprefix("entry_")
            if stripped and stripped not in aliases:
                aliases.append(stripped)
        else:
            prefixed = f"entry_{value}" if value else ""
            if prefixed and prefixed not in aliases:
                aliases.append(prefixed)
    return aliases


def _order_identifier_values(order: dict[str, Any]) -> list[str]:
    context = _pb_context(order)
    values = [
        _client_order_id(order),
        _order_value(order, "oca_group", "ocaGroup"),
        _order_value(order, "trade_group_id"),
        _order_value(order, "entry_order_unique_id"),
        _order_value(order, "parent_order_unique_id"),
        _order_value(order, "sibling_order_unique_id"),
        _text(context.get("trade_group_id")),
        _text(context.get("entry_order_unique_id")),
        _text(context.get("parent_order_unique_id")),
        _text(context.get("sibling_order_unique_id")),
    ]
    return [value for value in values if value]


def _order_matches_alias(order: dict[str, Any], aliases: list[str]) -> bool:
    if not aliases:
        return False
    values = _order_identifier_values(order)
    for value in values:
        for alias in aliases:
            if value == alias or value.endswith(alias) or alias in value:
                return True
    return False


def _explicit_cancel_order_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("cancel_order_ids", "protection_order_ids", "bracket_order_ids"):
        for value in _list_text_values(payload.get(key)):
            if value not in ids:
                ids.append(value)
    return ids


def _load_live_orders(service: Any) -> list[dict[str, Any]]:
    order_tracker = getattr(service, "order_tracker", None)
    if order_tracker and hasattr(order_tracker, "get_live_orders"):
        try:
            return [dict(order) for order in (order_tracker.get_live_orders() or []) if isinstance(order, dict)]
        except Exception:
            return []
    broker = getattr(getattr(service, "order_modifier", None), "broker", None) or getattr(getattr(service, "order_placer", None), "broker", None)
    if broker and hasattr(broker, "list_open_orders"):
        try:
            return [dict(order) for order in (broker.list_open_orders() or []) if isinstance(order, dict)]
        except Exception:
            return []
    return []


def _collect_protection_cancel_order_ids(
    service: Any,
    *,
    payload: dict[str, Any],
    symbol: str,
    direction: str,
) -> list[str]:
    ids = _explicit_cancel_order_ids(payload)
    aliases = _group_aliases(payload)
    for order in _load_live_orders(service):
        order_id = _order_id(order)
        if not order_id or order_id in ids:
            continue
        if not _is_open_order(order) or not _is_protection_order(order):
            continue
        if symbol and _order_symbol(order) and _order_symbol(order) != symbol:
            continue
        if not _order_matches_close_side(order, direction):
            continue
        if aliases and _order_matches_alias(order, aliases):
            ids.append(order_id)
            continue
        if not aliases and symbol:
            ids.append(order_id)
            continue
        if symbol and _parent_order_id(order):
            # Broker-only bracket children often lack PB aliases but still need
            # cancellation when the whole symbol position is flattened.
            ids.append(order_id)
    return ids


def _cancel_protection_orders(service: Any, order_ids: list[str]) -> dict[str, Any]:
    order_modifier = getattr(service, "order_modifier", None)
    result = {
        "ok": True,
        "requested_order_ids": list(order_ids),
        "cancelled_order_ids": [],
        "errors": [],
        "skipped": False,
        "reason": "",
    }
    if not order_ids:
        result["skipped"] = True
        result["reason"] = "no_matching_protection_orders"
        return result
    if not order_modifier or not hasattr(order_modifier, "cancel_order"):
        result["ok"] = False
        result["skipped"] = True
        result["reason"] = "order_modifier_unavailable"
        result["errors"] = [{"order_id": order_id, "error": "order_modifier_unavailable"} for order_id in order_ids]
        return result
    for order_id in order_ids:
        try:
            cancel_result = order_modifier.cancel_order(order_id)
        except Exception as exc:
            cancel_result = {"ok": False, "error": str(exc)}
        if cancel_result.get("ok"):
            result["cancelled_order_ids"].append(order_id)
        else:
            result["errors"].append({"order_id": order_id, "error": _text(cancel_result.get("error")) or "cancel_failed"})
    result["ok"] = not result["errors"]
    if not result["ok"]:
        result["reason"] = "protection_cancel_incomplete"
    return result


def _resolve_conid(service: Any, symbol: str, conid: int) -> int:
    if conid > 0:
        return conid
    resolver = getattr(service, "conid_resolver", None)
    if resolver and hasattr(resolver, "resolve"):
        try:
            resolved = int(_app_coerce_float(resolver.resolve(symbol), 0) or 0)
            if resolved > 0:
                return resolved
        except Exception:
            pass
    broker = getattr(getattr(service, "order_placer", None), "broker", None)
    if broker and hasattr(broker, "resolve_contract"):
        try:
            contract = broker.resolve_contract(symbol=symbol, conid=0)
            resolved = int(_app_coerce_float((contract or {}).get("conid"), 0) or 0)
            if resolved > 0:
                return resolved
        except Exception:
            pass
    return conid


def _position_snapshot_from_payload(payload: dict[str, Any], *, symbol: str, conid: int, quantity: float, position: float | None, direction: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "symbol": symbol,
        "conid": conid,
        "quantity": quantity,
        "position": position if position is not None else (-quantity if direction == "short" else quantity),
        "direction": direction,
    }
    numeric_aliases = {
        "avg_cost": ("avg_cost", "avgCost"),
        "avg_price": ("avg_price", "avgPrice"),
        "market_price": ("market_price", "mktPrice", "marketPrice"),
        "market_value": ("market_value", "mktValue", "marketValue"),
        "unrealized_pnl": ("unrealized_pnl", "unrealizedPnl"),
        "realized_pnl": ("realized_pnl", "realizedPnl"),
    }
    for target_key, source_keys in numeric_aliases.items():
        for source_key in source_keys:
            value = _app_coerce_float(payload.get(source_key))
            if value is not None:
                snapshot[target_key] = float(value)
                break
    for target_key in ("account", "currency", "asset_class"):
        value = _text(payload.get(target_key))
        if value:
            snapshot[target_key] = value
    return snapshot


def _build_ibkr_close_position_response(service, payload: dict) -> tuple[dict, int]:
    action_started_at = time.perf_counter()
    payload = payload or {}
    api_app = _api_app()
    conid = int(_app_coerce_float(payload.get("conid"), 0) or 0)
    symbol = str(payload.get("symbol") or "").strip().upper()
    position_value = _app_coerce_float(payload.get("position"))
    quantity = _app_coerce_float(payload.get("quantity"))

    if quantity is None and position_value is not None:
        quantity = abs(float(position_value))
    elif quantity is not None:
        quantity = abs(float(quantity))

    direction = str((payload or {}).get("direction") or "").strip().lower()
    if direction not in {"long", "short"}:
        if position_value is not None:
            direction = "long" if float(position_value) > 0 else "short"
        else:
            direction = "long"
    conid = _resolve_conid(service, symbol, conid)

    if conid <= 0 or not symbol or not quantity or quantity <= 0:
        return {"ok": False, "error": "Missing conid/symbol/quantity"}, 400

    cancel_after_close = _truthy(payload.get("cancel_bracket_after_close"), True)
    protection_order_ids = (
        _collect_protection_cancel_order_ids(service, payload=payload, symbol=symbol, direction=direction)
        if cancel_after_close
        else []
    )
    position_snapshot = _position_snapshot_from_payload(
        payload,
        symbol=symbol,
        conid=conid,
        quantity=float(quantity or 0),
        position=position_value,
        direction=direction,
    )
    order_type = _text(payload.get("order_type") or payload.get("close_order_type") or "MKT") or "MKT"
    limit_price = float(_app_coerce_float(payload.get("limit_price") or payload.get("close_limit_price"), 0) or 0)
    wait_for_fill = _truthy(payload.get("wait_for_fill"), False)
    fill_timeout = float(_app_coerce_float(payload.get("fill_timeout") or payload.get("fill_timeout_sec"), 5.0) or 5.0)
    outside_rth = _truthy(payload.get("outside_rth", payload.get("outsideRth")), False)
    tif = _text(payload.get("tif")) or "DAY"
    operation_started_at = time.perf_counter()
    result = service.order_placer.place_market_close(
        conid=conid,
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        use_paper=api_app._ibkr_service_uses_paper_account(service),
        order_type=order_type,
        limit_price=limit_price,
        wait_for_fill=wait_for_fill,
        fill_timeout=fill_timeout,
        outside_rth=outside_rth,
        tif=tif,
        trade_group_id=_text(payload.get("trade_group_id")),
        entry_order_unique_id=_text(payload.get("entry_order_unique_id")),
        signal_id=_text(payload.get("signal_id")),
        source=_text(payload.get("source")) or "positions_close",
        position_snapshot=position_snapshot,
        close_reason=_text(payload.get("close_reason")) or "positions_close",
        close_reason_human=_text(payload.get("close_reason_human")) or "手动平仓",
    )
    if result.get("ok") and cancel_after_close:
        protection_cancel = _cancel_protection_orders(service, protection_order_ids)
        result = {
            **result,
            "protection_cancel": protection_cancel,
        }
        if not protection_cancel.get("ok"):
            result["warning"] = "close_submitted_protection_cancel_incomplete"
    elif not cancel_after_close:
        result = {
            **result,
            "protection_cancel": {
                "ok": True,
                "requested_order_ids": [],
                "cancelled_order_ids": [],
                "errors": [],
                "skipped": True,
                "reason": "cancel_bracket_after_close_disabled",
            },
        }
    operation_elapsed_s = time.perf_counter() - operation_started_at
    return _build_snapshot_action_response(
        service,
        "close_position",
        result,
        delay_seconds=0.0,
        extra={
            "symbol": symbol,
            "conid": conid,
            "quantity": quantity,
            "direction": direction,
            "position_snapshot": position_snapshot,
        },
        action_started_at=action_started_at,
        operation_elapsed_s=operation_elapsed_s,
    )


__all__ = ["_build_ibkr_close_position_response"]
