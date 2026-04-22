from __future__ import annotations

from typing import Any

import requests


def run_upstream_http_job(
    *,
    method: str,
    base_url: str,
    path: str,
    environment: str,
    timeout_seconds: int = 60,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.request(
        method=method,
        url=f"{str(base_url or '').rstrip('/')}{path}",
        json={
            "environment": environment,
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
