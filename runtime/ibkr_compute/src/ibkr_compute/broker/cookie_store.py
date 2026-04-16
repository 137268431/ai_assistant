"""
Persist lightweight IB Gateway cookie snapshots for auxiliary tooling.

The socket API itself does not use these cookies, but the runtime still keeps a
shared cookie file so a panic reset or related ops tooling can clear any stale
auth remnants consistently.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterable

import requests

logger = logging.getLogger(__name__)

COOKIE_FILE = os.environ.get(
    "IBKR_GATEWAY_COOKIE_FILE",
    "/tmp/ibkr_gateway_cookies.json",
)


def _cookie_path() -> Path:
    return Path(COOKIE_FILE)


def _normalize_cookie(cookie: dict) -> dict | None:
    name = str(cookie.get("name") or "").strip()
    value = str(cookie.get("value") or "")
    if not name:
        return None
    return {
        "name": name,
        "value": value,
        "domain": str(cookie.get("domain") or ""),
        "path": str(cookie.get("path") or "/") or "/",
        "secure": bool(cookie.get("secure", False)),
        "expires": cookie.get("expires"),
    }


def _session_cookie_payload(session: requests.Session) -> list[dict]:
    payload = []
    for item in session.cookies:
        normalized = _normalize_cookie(
            {
                "name": item.name,
                "value": item.value,
                "domain": item.domain or "",
                "path": item.path or "/",
                "secure": item.secure,
                "expires": item.expires,
            }
        )
        if normalized:
            payload.append(normalized)
    return payload


def load_cookies(session: requests.Session) -> int:
    path = _cookie_path()
    if not path.exists():
        return 0

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Failed to read gateway cookie store %s: %s", path, exc)
        return 0

    count = 0
    for item in raw if isinstance(raw, list) else []:
        normalized = _normalize_cookie(item if isinstance(item, dict) else {})
        if not normalized:
            continue
        try:
            session.cookies.set(
                normalized["name"],
                normalized["value"],
                domain=normalized["domain"] or None,
                path=normalized["path"] or "/",
            )
            count += 1
        except Exception as exc:
            logger.debug("Failed to hydrate cookie %s: %s", normalized["name"], exc)
    return count


def save_cookies(session: requests.Session) -> int:
    payload = _session_cookie_payload(session)
    if not payload:
        return 0

    path = _cookie_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="ibkr-cookie-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return len(payload)
    except Exception as exc:
        logger.debug("Failed to persist gateway cookies to %s: %s", path, exc)
        return 0


def save_browser_cookies(cookies: Iterable[dict]) -> int:
    session = requests.Session()
    for item in cookies or []:
        normalized = _normalize_cookie(item if isinstance(item, dict) else {})
        if not normalized:
            continue
        session.cookies.set(
            normalized["name"],
            normalized["value"],
            domain=normalized["domain"] or None,
            path=normalized["path"] or "/",
        )
    return save_cookies(session)


def clear_cookies() -> dict:
    path = _cookie_path()
    existed = path.exists()
    removed = False
    error = ""
    if existed:
        try:
            path.unlink()
            removed = True
        except Exception as exc:
            error = str(exc)
            logger.warning("Failed to clear gateway cookie store %s: %s", path, exc)
    return {
        "path": str(path),
        "existed": existed,
        "removed": removed,
        "error": error,
    }
