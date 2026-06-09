from __future__ import annotations

from ibkr_compute.api.account.live.runtime import _app_coerce_float


def _first_text(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_float(*values, default: float | None = 0.0) -> float | None:
    for value in values:
        number = _app_coerce_float(value)
        if number is not None:
            return float(number)
    return default


def _normalize_live_position(position: dict) -> dict:
    raw = position.get("raw") if isinstance(position.get("raw"), dict) else {}
    quantity = float(_first_float(position.get("position"), position.get("quantity"), raw.get("position"), raw.get("quantity"), default=0.0) or 0.0)
    market_price = float(_first_float(position.get("mktPrice"), position.get("market_price"), raw.get("mktPrice"), raw.get("market_price"), default=0.0) or 0.0)
    market_value = _first_float(position.get("mktValue"), position.get("market_value"), raw.get("mktValue"), raw.get("market_value"), default=None)
    if market_value is None:
        market_value = quantity * market_price

    return {
        "symbol": _first_text(position.get("ticker"), position.get("contractDesc"), position.get("symbol"), raw.get("ticker"), raw.get("contractDesc"), raw.get("symbol")).upper(),
        "conid": int(_first_float(position.get("conid"), raw.get("conid"), default=0) or 0),
        "quantity": quantity,
        "direction": "long" if quantity > 0 else "short" if quantity < 0 else "flat",
        "avg_cost": float(_first_float(position.get("avgCost"), position.get("avg_cost"), raw.get("avgCost"), raw.get("avg_cost"), default=0.0) or 0.0),
        "avg_price": float(_first_float(position.get("avgPrice"), position.get("avg_price"), raw.get("avgPrice"), raw.get("avg_price"), default=0.0) or 0.0),
        "market_price": market_price,
        "market_value": float(market_value or 0.0),
        "unrealized_pnl": float(_first_float(position.get("unrealizedPnl"), position.get("unrealized_pnl"), raw.get("unrealizedPnl"), raw.get("unrealized_pnl"), default=0.0) or 0.0),
        "realized_pnl": float(_first_float(position.get("realizedPnl"), position.get("realized_pnl"), raw.get("realizedPnl"), raw.get("realized_pnl"), default=0.0) or 0.0),
        "account": _first_text(position.get("acctId"), position.get("account"), raw.get("acctId"), raw.get("account")),
        "currency": _first_text(position.get("currency"), raw.get("currency"), "USD").upper(),
        "asset_class": _first_text(position.get("assetClass"), position.get("asset_class"), raw.get("assetClass"), raw.get("asset_class")).upper(),
        "raw": position,
    }


__all__ = ["_normalize_live_position"]
