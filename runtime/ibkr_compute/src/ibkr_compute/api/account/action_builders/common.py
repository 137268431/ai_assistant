from __future__ import annotations

import time

from ibkr_compute.api.account.snapshot import _build_ibkr_account_snapshot


def _build_snapshot_action_response(
    service,
    action: str,
    result: dict,
    *,
    delay_seconds: float = 0.0,
    extra: dict | None = None,
) -> tuple[dict, int]:
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    snapshot = _build_ibkr_account_snapshot(service)
    payload = {
        "ok": bool((result or {}).get("ok")),
        "action": action,
        "result": result,
        "snapshot": snapshot,
    }
    if extra:
        payload.update(extra)
    return payload, (200 if (result or {}).get("ok") else 500)


__all__ = ["_build_snapshot_action_response"]
