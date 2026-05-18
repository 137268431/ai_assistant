from __future__ import annotations

from typing import Any

from ibkr_compute.core.broker_mode import (
    configured_broker_mode,
    normalize_broker_mode,
    resolve_market_data_mode,
)


def request_broker_mode(payload: dict[str, Any] | None) -> str:
    request_payload = payload if isinstance(payload, dict) else {}
    if "broker_mode" in request_payload:
        return normalize_broker_mode(request_payload.get("broker_mode"), configured_broker_mode())
    return configured_broker_mode()


def request_market_data_mode(payload: dict[str, Any] | None) -> str:
    request_payload = payload if isinstance(payload, dict) else {}
    if "market_data_mode" in request_payload:
        return resolve_market_data_mode(request_payload.get("market_data_mode"))
    if "data_environment" in request_payload:
        return resolve_market_data_mode(request_payload.get("data_environment"))
    return resolve_market_data_mode(None)


__all__ = ["request_broker_mode", "request_market_data_mode"]
