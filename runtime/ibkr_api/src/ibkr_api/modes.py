from __future__ import annotations

from typing import Any

from ibkr_compute.core.broker_mode import (
    configured_broker_mode,
    normalize_broker_mode,
    resolve_market_data_mode,
)


def request_broker_mode(payload: dict[str, Any] | None) -> str:
    request_payload = payload if isinstance(payload, dict) else {}
    requested = request_payload.get("broker_mode") or request_payload.get("environment")
    if requested is not None and str(requested).strip() != "":
        return normalize_broker_mode(requested, configured_broker_mode())
    return configured_broker_mode()


def request_market_data_mode(payload: dict[str, Any] | None) -> str:
    request_payload = payload if isinstance(payload, dict) else {}
    requested = (
        request_payload.get("market_data_mode")
        or request_payload.get("data_environment")
        or request_payload.get("environment")
    )
    if requested is not None and str(requested).strip() != "":
        return resolve_market_data_mode(requested)
    return resolve_market_data_mode(None)


__all__ = ["request_broker_mode", "request_market_data_mode"]
