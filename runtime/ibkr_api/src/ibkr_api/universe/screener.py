from __future__ import annotations

from datetime import date
from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, to_text

RequestJsonRequest = Callable[..., dict[str, Any]]
NormalizeEnvironment = Callable[[Any, str], str]


def _normalize_symbols(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = str(value or "").split(",")
    items: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        symbol = to_text(raw_item).upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        items.append(symbol)
    return items


def _parse_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except Exception:
        return 0
    return max(0, min(parsed, 500))


def build_screener_proxy_response(
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    request_json_request: RequestJsonRequest,
    compute_base_url: str,
) -> tuple[dict[str, Any], int]:
    environment = normalize_environment(payload.get("environment"), "live")
    market_date = to_text(payload.get("market_date") or payload.get("date"))
    symbols = _normalize_symbols(payload.get("symbols"))
    limit = _parse_limit(payload.get("limit"))

    if market_date:
        try:
            date.fromisoformat(market_date)
        except ValueError:
            return {
                "ok": False,
                "error": "invalid_market_date",
                "market_date": market_date,
                "source": "ibkr-api",
            }, 400

    params: list[tuple[str, str]] = [("environment", environment)]
    if market_date:
        params.append(("market_date", market_date))
    if symbols:
        params.append(("symbols", ",".join(symbols)))
    if limit > 0:
        params.append(("limit", str(limit)))

    result = request_json_request(
        "GET",
        compute_base_url,
        "/screener",
        params=params,
        timeout=30.0,
    )
    status_code = int(result.get("status_code") or 200)
    response_payload = ensure_object(result.get("payload"))
    if not response_payload:
        response_payload = {
            "ok": False,
            "error": result.get("error") or "screener_upstream_unavailable",
        }
        if status_code < 400:
            status_code = 502
    response_payload.setdefault("environment", environment)
    response_payload.setdefault("source", "ibkr-api")
    response_payload.setdefault("proxy_upstream", to_text(result.get("target_url")) or f"{compute_base_url.rstrip('/')}/screener")
    if market_date and not response_payload.get("market_date"):
        response_payload["market_date"] = market_date
    return response_payload, status_code


__all__ = ["build_screener_proxy_response"]
