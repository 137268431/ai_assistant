from __future__ import annotations

from ibkr_compute.api.account.live.runtime import _app_coerce_float


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


__all__ = ["_normalize_live_position"]
