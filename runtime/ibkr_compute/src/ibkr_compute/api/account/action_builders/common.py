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
    snapshot = _build_ibkr_account_snapshot(service, force_refresh=True, allow_stale=False)
    payload = {
        "ok": bool((result or {}).get("ok")),
        "action": action,
        "result": result,
        "snapshot": snapshot,
    }
    if extra:
        payload.update(extra)
    if (result or {}).get("ok"):
        status = 200
    else:
        error = str((result or {}).get("error") or "")
        status = 409 if error == "buying_power_blocked" else 503 if error == "buying_power_unavailable" else 500
    return payload, status


__all__ = ["_build_snapshot_action_response"]
