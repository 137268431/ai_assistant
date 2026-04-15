from __future__ import annotations

from ibkr_compute.api.account.action_builders.common import _build_snapshot_action_response
from ibkr_compute.api.account.live import _api_app, _app_coerce_float


def _build_ibkr_close_position_response(service, payload: dict) -> tuple[dict, int]:
    api_app = _api_app()
    conid = int(_app_coerce_float((payload or {}).get("conid"), 0) or 0)
    symbol = str((payload or {}).get("symbol") or "").strip().upper()
    position_value = _app_coerce_float((payload or {}).get("position"))
    quantity = _app_coerce_float((payload or {}).get("quantity"))

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

    if conid <= 0 or not symbol or not quantity or quantity <= 0:
        return {"ok": False, "error": "Missing conid/symbol/quantity"}, 400

    result = service.order_placer.place_market_close(
        conid=conid,
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        use_paper=api_app._ibkr_service_uses_paper_account(service),
    )
    return _build_snapshot_action_response(
        service,
        "close_position",
        result,
        delay_seconds=0.75,
        extra={
            "symbol": symbol,
            "conid": conid,
            "quantity": quantity,
            "direction": direction,
        },
    )


__all__ = ["_build_ibkr_close_position_response"]
