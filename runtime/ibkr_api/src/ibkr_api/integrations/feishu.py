from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

import requests


DEFAULT_FEISHU_APP_ID = "cli_a936b8d2cc79dccb"
DEFAULT_FEISHU_APP_SECRET = "ZZySOkZPaKBVkNhhk4upvfROPnXcSsry"
_FEISHU_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}


NormalizeEnvironment = Callable[[Any, str], str]


def _resolved_app_id(app_id: str | None = None) -> str:
    return str(app_id or os.environ.get("FEISHU_APP_ID") or DEFAULT_FEISHU_APP_ID).strip()



def _resolved_app_secret(app_secret: str | None = None) -> str:
    return str(app_secret or os.environ.get("FEISHU_APP_SECRET") or DEFAULT_FEISHU_APP_SECRET).strip()



def _payload_code(payload: Any, *, default: int = -1) -> int:
    if not isinstance(payload, dict):
        return int(default)
    code = payload.get("code")
    if code in (None, ""):
        return int(default)
    try:
        return int(code)
    except Exception:
        return int(default)



def feishu_suppressed(environment: str, *, normalize_environment: NormalizeEnvironment) -> bool:
    return normalize_environment(environment, "live") == "paper"



def feishu_token(
    *,
    app_id: str | None = None,
    app_secret: str | None = None,
    requests_module: Any = requests,
    now_fn: Callable[[], float] = time.time,
    cache: dict[str, Any] | None = None,
) -> str:
    token_cache = cache if isinstance(cache, dict) else _FEISHU_TOKEN_CACHE
    now = float(now_fn())
    cached_token = str(token_cache.get("token") or "")
    expires_at = float(token_cache.get("expires_at") or 0.0)
    if cached_token and now < max(0.0, expires_at - 300.0):
        return cached_token

    resolved_app_id = _resolved_app_id(app_id)
    resolved_app_secret = _resolved_app_secret(app_secret)
    if not resolved_app_id or not resolved_app_secret:
        return ""

    try:
        response = requests_module.post(
            "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
            json={"app_id": resolved_app_id, "app_secret": resolved_app_secret},
            timeout=10,
        )
        payload = response.json() if response.content else {}
    except Exception:
        return ""

    if not response.ok or _payload_code(payload) != 0:
        return ""

    token = str(payload.get("app_access_token") or "")
    expire_seconds = int(payload.get("expire") or 0)
    if token:
        token_cache["token"] = token
        token_cache["expires_at"] = now + max(0, expire_seconds)
    return token



def feishu_send_interactive(
    card: dict[str, Any],
    chat_id: str,
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    token_loader: Callable[[], str] = feishu_token,
    requests_module: Any = requests,
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    if feishu_suppressed(runtime_environment, normalize_environment=normalize_environment):
        return {
            "success": True,
            "skipped": True,
            "suppressed": True,
            "message_id": "",
            "reason": "paper_environment_disabled",
        }

    token = str(token_loader() or "")
    if not token:
        return {"success": False, "message_id": "", "error": "missing_token"}

    try:
        response = requests_module.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "receive_id": str(chat_id or ""),
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            timeout=15,
        )
        payload = response.json() if response.content else {}
    except Exception as exc:
        return {"success": False, "message_id": "", "error": str(exc)}

    if not response.ok or _payload_code(payload) != 0:
        return {
            "success": False,
            "message_id": "",
            "error": f"http_{response.status_code}" if not response.ok else f"api_{payload.get('code')}",
        }

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {"success": True, "message_id": str(data.get("message_id") or ""), "data": data}



def feishu_update_interactive(
    message_id: str,
    card: dict[str, Any],
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    token_loader: Callable[[], str] = feishu_token,
    requests_module: Any = requests,
) -> dict[str, Any]:
    runtime_environment = normalize_environment(environment, "live")
    if feishu_suppressed(runtime_environment, normalize_environment=normalize_environment):
        return {
            "success": True,
            "skipped": True,
            "suppressed": True,
            "message_id": str(message_id or ""),
            "reason": "paper_environment_disabled",
        }
    if not message_id:
        return {"success": False, "message_id": "", "error": "missing_message_id"}

    token = str(token_loader() or "")
    if not token:
        return {"success": False, "message_id": str(message_id or ""), "error": "missing_token"}

    try:
        response = requests_module.patch(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
            timeout=15,
        )
        payload = response.json() if response.content else {}
    except Exception as exc:
        return {"success": False, "message_id": str(message_id or ""), "error": str(exc)}

    if not response.ok or _payload_code(payload) != 0:
        return {
            "success": False,
            "message_id": str(message_id or ""),
            "error": f"http_{response.status_code}" if not response.ok else f"api_{payload.get('code')}",
        }

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {"success": True, "message_id": str(message_id or ""), "data": data}
