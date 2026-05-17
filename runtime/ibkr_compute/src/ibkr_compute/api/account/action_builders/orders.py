from __future__ import annotations

import time

from ibkr_compute.api.account.action_builders.common import _build_snapshot_action_response
from ibkr_compute.api.account.buying_power_guard import (
    _config_bool,
    build_buying_power_guard,
    estimate_entry_exposure,
)
from ibkr_compute.api.account.live import _api_app, _app_coerce_float
from ibkr_compute.api.account.snapshot import _build_ibkr_account_snapshot
from ibkr_compute.api.shared.service_status import get_service_status_snapshot


def _notify_manual_buying_power_event(
    service,
    *,
    title: str,
    level: str,
    environment: str,
    symbol: str,
    direction: str,
    quantity: int,
    guard: dict,
) -> None:
    if not _config_bool(getattr(service, "config", None), "ibkr_buying_power_notify_enabled", environment, True):
        return
    pb = getattr(service, "pb", None)
    notifier = getattr(pb, "notify_system_event", None)
    if not callable(notifier):
        return
    try:
        notifier(
            title,
            {
                "标的": symbol,
                "方向": direction,
                "数量": quantity,
                "当前剩余购买力": round(float((guard or {}).get("remaining") or 0.0), 2),
                "本次预估占用": round(float((guard or {}).get("requested_exposure") or 0.0), 2),
                "下单后剩余购买力": round(float((guard or {}).get("remaining_after") or 0.0), 2),
                "预警阈值": round(float((guard or {}).get("warn_floor") or 0.0), 2),
                "禁止阈值": round(float((guard or {}).get("block_floor") or 0.0), 2),
                "状态": str((guard or {}).get("state") or "ok"),
                "原因": str((guard or {}).get("reason") or ""),
            },
            event_type="account_order",
            level=level,
            source="ibkr_compute",
            environment=environment,
        )
    except Exception:
        pass


def _build_ibkr_cancel_order_response(service, payload: dict) -> tuple[dict, int]:
    order_id = str((payload or {}).get("order_id") or (payload or {}).get("id") or "").strip()
    acct_id = str((payload or {}).get("account_id") or "").strip() or None
    if not order_id:
        return {"ok": False, "error": "Missing order_id"}, 400

    result = service.order_modifier.cancel_order(order_id, acct_id=acct_id)
    return _build_snapshot_action_response(
        service,
        "cancel_order",
        result,
        delay_seconds=0.5,
        extra={"order_id": order_id},
    )


def _build_ibkr_cancel_all_orders_response(service, payload: dict) -> tuple[dict, int]:
    acct_id = str((payload or {}).get("account_id") or "").strip() or None
    result = service.order_modifier.cancel_all_orders(acct_id=acct_id)
    return _build_snapshot_action_response(
        service,
        "cancel_all_orders",
        result,
        delay_seconds=0.5,
    )


def _build_ibkr_modify_order_response(service, payload: dict) -> tuple[dict, int]:
    order_id = str((payload or {}).get("order_id") or (payload or {}).get("id") or "").strip()
    acct_id = str((payload or {}).get("account_id") or "").strip() or None
    updates = {}

    if not order_id:
        return {"ok": False, "error": "Missing order_id"}, 400

    price = _app_coerce_float((payload or {}).get("price"))
    quantity = _app_coerce_float((payload or {}).get("quantity"))
    tif = str((payload or {}).get("tif") or "").strip().upper()

    if price is not None:
        updates["price"] = price
    if quantity is not None:
        updates["quantity"] = quantity
    if tif:
        updates["tif"] = tif
    if not updates:
        return {"ok": False, "error": "No valid modify fields supplied"}, 400

    result = service.order_modifier.modify_order(order_id, updates, acct_id=acct_id)
    return _build_snapshot_action_response(
        service,
        "modify_order",
        result,
        delay_seconds=0.5,
        extra={
            "order_id": order_id,
            "updates": updates,
        },
    )


def _build_ibkr_place_order_response(service, payload: dict) -> tuple[dict, int]:
    api_app = _api_app()
    runtime_environment = api_app._ibkr_service_environment(service)
    if runtime_environment == "backtest":
        return {"ok": False, "error": "Backtest environment does not support live order placement"}, 400
    trading_enabled = _config_bool(getattr(service, "config", None), "ibkr_trading_enabled", runtime_environment, True)
    live_trading_enabled = _config_bool(
        getattr(service, "config", None),
        "ibkr_live_trading_enabled",
        runtime_environment,
        True,
    )
    if not trading_enabled or (runtime_environment == "live" and not live_trading_enabled):
        return {
            "ok": False,
            "error": "trading_disabled",
            "environment": runtime_environment,
            "broker_mode": runtime_environment,
            "ibkr_trading_enabled": bool(trading_enabled),
            "ibkr_live_trading_enabled": bool(live_trading_enabled),
        }, 403

    service_status = get_service_status_snapshot(service)
    session_authenticated = bool((service_status.get("session") or {}).get("authenticated"))
    service_running = bool(getattr(service, "is_running", False) or getattr(service, "is_starting", False))
    if not service_running:
        return {"ok": False, "error": "IBKR service is not running"}, 409
    if not session_authenticated:
        return {"ok": False, "error": "IBKR session is not authenticated"}, 409
    if not hasattr(service, "order_placer") or not hasattr(service, "conid_resolver"):
        return {"ok": False, "error": "IBKR order components are unavailable"}, 503

    symbol = str((payload or {}).get("symbol") or "").strip().upper()
    direction = str((payload or {}).get("direction") or "").strip().lower()
    order_type = str(
        (payload or {}).get("order_type") or (payload or {}).get("entry_order_type") or "LMT"
    ).strip().upper()
    quantity_value = _app_coerce_float((payload or {}).get("quantity"))
    conid = int(_app_coerce_float((payload or {}).get("conid"), 0) or 0)
    entry_price = _app_coerce_float((payload or {}).get("entry_price"))
    take_profit_price = _app_coerce_float((payload or {}).get("take_profit_price"))
    stop_loss_price = _app_coerce_float((payload or {}).get("stop_loss_price"))

    if not symbol:
        return {"ok": False, "error": "Missing symbol"}, 400
    if direction not in {"long", "short"}:
        return {"ok": False, "error": "direction must be long or short"}, 400
    if order_type not in {"LMT", "MKT"}:
        return {"ok": False, "error": "order_type must be LMT or MKT"}, 400
    if quantity_value is None or quantity_value <= 0 or abs(quantity_value - round(quantity_value)) > 1e-9:
        return {"ok": False, "error": "quantity must be a positive integer"}, 400
    if take_profit_price is None or take_profit_price <= 0 or stop_loss_price is None or stop_loss_price <= 0:
        return {"ok": False, "error": "take_profit_price and stop_loss_price are required"}, 400
    if order_type == "LMT" and (entry_price is None or entry_price <= 0):
        return {"ok": False, "error": "entry_price is required for limit orders"}, 400

    quantity = int(round(quantity_value))
    if direction == "long" and take_profit_price <= stop_loss_price:
        return {"ok": False, "error": "For long orders, take profit must be above stop loss"}, 400
    if direction == "short" and take_profit_price >= stop_loss_price:
        return {"ok": False, "error": "For short orders, take profit must be below stop loss"}, 400
    if order_type == "LMT" and entry_price is not None:
        if direction == "long" and not (stop_loss_price < entry_price < take_profit_price):
            return {"ok": False, "error": "For long limit orders, stop < entry < take profit is required"}, 400
        if direction == "short" and not (take_profit_price < entry_price < stop_loss_price):
            return {"ok": False, "error": "For short limit orders, take profit < entry < stop is required"}, 400

    pre_submit_snapshot = _build_ibkr_account_snapshot(service)
    requested_exposure = estimate_entry_exposure(
        quantity,
        entry_price,
        take_profit_price,
        stop_loss_price,
        direction,
        order_type,
    )
    if requested_exposure <= 0:
        return {
            "ok": False,
            "error": "buying_power_price_unavailable",
            "action": "place_order",
            "environment": runtime_environment,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "order_type": order_type,
            "snapshot": pre_submit_snapshot,
        }, 400
    buying_power_guard = build_buying_power_guard(
        (pre_submit_snapshot or {}).get("summary") or {},
        config=getattr(service, "config", None),
        environment=runtime_environment,
        requested_exposure=requested_exposure,
    )
    if buying_power_guard.get("state") == "blocked":
        _notify_manual_buying_power_event(
            service,
            title="手动开仓已被购买力阈值拦截",
            level="error",
            environment=runtime_environment,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            guard=buying_power_guard,
        )
        return {
            "ok": False,
            "error": "buying_power_blocked",
            "action": "place_order",
            "environment": runtime_environment,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "order_type": order_type,
            "entry_price": float(entry_price or 0.0),
            "take_profit_price": float(take_profit_price),
            "stop_loss_price": float(stop_loss_price),
            "buying_power_guard": buying_power_guard,
            "snapshot": pre_submit_snapshot,
        }, 409
    if buying_power_guard.get("state") == "warning":
        _notify_manual_buying_power_event(
            service,
            title="手动开仓购买力预警",
            level="warning",
            environment=runtime_environment,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            guard=buying_power_guard,
        )

    if conid <= 0:
        try:
            conid = int(service.conid_resolver.resolve(symbol) or 0)
        except Exception as exc:
            return {"ok": False, "error": f"Failed to resolve contract for {symbol}: {exc}"}, 500
    if conid <= 0:
        return {"ok": False, "error": f"Cannot resolve conid for {symbol}"}, 404

    duplicate_order = None
    if hasattr(service, "order_tracker"):
        try:
            duplicate_order = service.order_tracker.find_duplicate_open_entry(
                symbol=symbol,
                direction=direction,
                quantity=quantity,
                entry_price=float(entry_price or 0.0),
                entry_order_type=order_type,
            )
        except Exception as exc:
            return {"ok": False, "error": f"Failed to inspect live orders before placement: {exc}"}, 500

    if duplicate_order:
        try:
            service.order_tracker.sync_live_orders_snapshot([duplicate_order])
        except Exception:
            pass
        snapshot = _build_ibkr_account_snapshot(service)
        broker_order_id = str((duplicate_order or {}).get("orderId") or (duplicate_order or {}).get("id") or "").strip()
        return {
            "ok": False,
            "error": "Duplicate open broker order already exists",
            "action": "place_order",
            "environment": runtime_environment,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "order_type": order_type,
            "entry_price": float(entry_price or 0.0),
            "take_profit_price": float(take_profit_price),
            "stop_loss_price": float(stop_loss_price),
            "duplicate_order": {
                "order_id": broker_order_id,
                "status": str((duplicate_order or {}).get("status") or "").strip(),
                "symbol": str(
                    (duplicate_order or {}).get("ticker") or (duplicate_order or {}).get("symbol") or ""
                ).strip().upper(),
                "side": str((duplicate_order or {}).get("side") or "").strip().upper(),
                "price": _app_coerce_float((duplicate_order or {}).get("price"), 0.0) or 0.0,
                "quantity": _app_coerce_float(
                    (duplicate_order or {}).get("totalSize")
                    if (duplicate_order or {}).get("totalSize") is not None
                    else (duplicate_order or {}).get("quantity"),
                    0.0,
                ) or 0.0,
            },
            "snapshot": snapshot,
        }, 409

    signal_id = f"MANUAL_{runtime_environment.upper()}_{symbol}_{int(time.time())}"
    result = service.order_placer.place_bracket_order(
        conid=conid,
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        entry_price=float(entry_price or 0.0),
        take_profit_price=float(take_profit_price),
        stop_loss_price=float(stop_loss_price),
        use_paper=api_app._ibkr_service_uses_paper_account(service),
        signal_id=signal_id,
        entry_order_type=order_type,
    )
    if result.get("ok"):
        _notify_manual_buying_power_event(
            service,
            title="手动开仓已提交",
            level="info",
            environment=runtime_environment,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            guard=buying_power_guard,
        )
    return _build_snapshot_action_response(
        service,
        "place_order",
        result,
        delay_seconds=0.75,
        extra={
            "environment": runtime_environment,
            "symbol": symbol,
            "conid": conid,
            "direction": direction,
            "quantity": quantity,
            "order_type": order_type,
            "entry_price": float(entry_price or 0.0),
            "take_profit_price": float(take_profit_price),
            "stop_loss_price": float(stop_loss_price),
            "signal_id": signal_id,
            "buying_power_guard": buying_power_guard,
            "pre_submit_buying_power_guard": buying_power_guard,
        },
    )


__all__ = [
    "_build_ibkr_cancel_all_orders_response",
    "_build_ibkr_cancel_order_response",
    "_build_ibkr_modify_order_response",
    "_build_ibkr_place_order_response",
]
