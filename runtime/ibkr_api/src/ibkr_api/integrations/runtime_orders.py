from __future__ import annotations

from typing import Any, Callable


def cancel_broker_order_via_runtime(
    environment: str,
    order_id: str,
    payload: dict[str, Any] | None = None,
    *,
    request_json_request: Callable[..., dict[str, Any]],
    runtime_base_url: str,
    normalize_environment: Callable[[Any, str], str],
    as_dict: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    result = request_json_request(
        "POST",
        runtime_base_url,
        "/ibkr/orders/cancel",
        json_body={
            "order_id": str(order_id or "").strip(),
            "environment": normalize_environment(environment, "live"),
            **(as_dict(payload) if isinstance(payload, dict) else {}),
        },
        timeout=20,
    )
    payload_dict = as_dict(result.get("payload"))
    return {
        "ok": bool(result.get("ok")),
        "status_code": int(result.get("status_code") or 0),
        "payload": payload_dict,
        "error": str(result.get("error") or ""),
        "message": str(payload_dict.get("message") or payload_dict.get("error") or ""),
        "target_url": str(result.get("target_url") or ""),
    }
