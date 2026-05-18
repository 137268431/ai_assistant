from __future__ import annotations

from typing import Any

import requests


def run_upstream_http_job(
    *,
    method: str,
    base_url: str,
    path: str,
    environment: str,
    broker_mode: str = "",
    market_data_mode: str = "",
    mode_scope: str = "",
    timeout_seconds: int = 60,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_mode = str(environment or "").strip().lower()
    normalized_broker_mode = str(broker_mode or "").strip().lower() or selected_mode
    normalized_market_data_mode = str(market_data_mode or "").strip().lower() or selected_mode
    normalized_scope = str(mode_scope or "").strip().lower()
    response = requests.request(
        method=method,
        url=f"{str(base_url or '').rstrip('/')}{path}",
        json={
            "broker_mode": normalized_broker_mode,
            "market_data_mode": normalized_market_data_mode,
            "data_environment": normalized_market_data_mode,
            "mode_scope": normalized_scope,
            "selected_mode": selected_mode,
            "source": "ibkr_scheduler",
            **(payload or {}),
        },
        timeout=timeout_seconds,
    )
    try:
        response_payload = response.json() if response.content else {}
    except Exception:
        response_payload = {}
    return {
        "ok": bool(response.ok and response_payload.get("ok", response_payload.get("success", True))),
        "status_code": response.status_code,
        "payload": response_payload,
    }
