from __future__ import annotations

from typing import Any


def json_response(*, jsonify_fn, payload: dict[str, Any], status_code: int = 200, headers: dict[str, str] | None = None):
    response = jsonify_fn(payload)
    if hasattr(response, "headers") and isinstance(headers, dict):
        for key, value in headers.items():
            response.headers[str(key)] = str(value)
    if hasattr(response, "headers"):
        return response, int(status_code or 200)
    if int(status_code or 200) == 200:
        return payload
    return payload, int(status_code or 200)


def feishu_callback_response(*, jsonify_fn, payload: dict[str, Any], update_token: str = "", status_code: int = 200):
    headers = {"update_card_token": update_token} if update_token else {}
    return json_response(jsonify_fn=jsonify_fn, payload=payload, status_code=status_code, headers=headers)


def build_response_from_upstream(*, response, response_class, excluded_headers: set[str]):
    headers = [
        (key, value)
        for key, value in response.headers.items()
        if key.lower() not in excluded_headers
    ]
    return response_class(response.content, status=response.status_code, headers=headers)


def request_json(*, requests_module, base_url: str, path: str, params: list[tuple[str, str]] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    target_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        response = requests_module.get(
            target_url,
            params=params,
            timeout=max(1.0, float(timeout or 0)),
        )
    except requests_module.RequestException as exc:
        return {
            "ok": False,
            "status_code": 0,
            "payload": {},
            "error": str(exc),
            "target_url": target_url,
        }

    payload: Any = {}
    try:
        payload = response.json() if response.content else {}
    except Exception:
        payload = {}
    return {
        "ok": bool(response.ok),
        "status_code": int(response.status_code),
        "payload": payload if isinstance(payload, dict) else {},
        "target_url": target_url,
        "error": "",
    }


def request_json_request(
    *,
    requests_module,
    method: str,
    base_url: str,
    path: str,
    params: list[tuple[str, str]] | None = None,
    json_body: Any = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    target_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        response = requests_module.request(
            method=method.upper(),
            url=target_url,
            params=params,
            json=json_body,
            timeout=max(1.0, float(timeout or 0)),
        )
    except requests_module.RequestException as exc:
        return {
            "ok": False,
            "status_code": 0,
            "payload": {},
            "error": str(exc),
            "target_url": target_url,
        }

    payload: Any = {}
    try:
        payload = response.json() if response.content else {}
    except Exception:
        payload = {}
    return {
        "ok": bool(response.ok),
        "status_code": int(response.status_code),
        "payload": payload if isinstance(payload, dict) else {},
        "target_url": target_url,
        "error": "",
    }
