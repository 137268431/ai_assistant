from __future__ import annotations

import time
from typing import Any, Callable

import requests


def _extract_compute_startup_preload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    preload = payload.get("compute_startup_preload")
    if isinstance(preload, dict) and preload:
        return dict(preload)
    compute = payload.get("compute") if isinstance(payload.get("compute"), dict) else {}
    nested_preload = compute.get("compute_startup_preload") if isinstance(compute, dict) else None
    return dict(nested_preload) if isinstance(nested_preload, dict) else {}


def _is_compute_startup_preload_active(payload: dict[str, Any]) -> bool:
    preload = _extract_compute_startup_preload(payload)
    status = str(preload.get("status") or "").strip().lower()
    return bool(preload.get("running")) or status in {"running", "scheduled"}


def build_compute_dispatch_runner(
    *,
    pb: Any,
    compute_base_url: str,
    get_ingest_cursor: Callable[[str], dict[str, Any]],
    get_dispatch_cursor: Callable[[str], dict[str, Any]],
    save_dispatch_cursor: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> Callable[[str], dict[str, Any]]:
    def _native_compute_due(environment: str) -> tuple[bool, dict[str, Any]]:
        ingest = get_ingest_cursor(environment)
        dispatch = get_dispatch_cursor(environment)
        ingest_intervals = ingest.get("intervals") if isinstance(ingest.get("intervals"), dict) else {}
        dispatch_intervals = dispatch.get("intervals") if isinstance(dispatch.get("intervals"), dict) else {}
        latest_ingested = int(((ingest_intervals.get("5m") or {}).get("latest_bar_time_ms") or 0))
        latest_dispatched = int(((dispatch_intervals.get("5m") or {}).get("latest_bar_time_ms") or 0))
        return latest_ingested > latest_dispatched, {
            "latest_ingested_bar_time_ms": latest_ingested,
            "latest_dispatched_bar_time_ms": latest_dispatched,
            "ingest_cursor": ingest,
            "dispatch_cursor": dispatch,
        }

    def _run_compute_dispatch(environment: str) -> dict[str, Any]:
        due, detail = _native_compute_due(environment)
        if not due:
            return {
                "ok": True,
                "skipped": True,
                "reason": "no_new_persisted_bars",
                **detail,
            }

        try:
            status_response = requests.get(
                f"{compute_base_url}/health",
                params={"environment": environment, "lite": "1"},
                timeout=5,
            )
            status_payload = status_response.json() if status_response.content else {}
        except requests.RequestException:
            status_payload = {}
        if _is_compute_startup_preload_active(status_payload):
            return {
                "ok": True,
                "skipped": True,
                "reason": "compute_startup_preload_running",
                "compute_startup_preload": _extract_compute_startup_preload(status_payload),
                **detail,
            }

        response = requests.post(
            f"{compute_base_url}/compute",
            json={
                "source": "ibkr_scheduler",
                "environments": [environment],
                "intervals": ["5m"],
                "rollup_intervals": [],
            },
            timeout=60,
        )
        payload = response.json() if response.content else {}
        if not response.ok or payload.get("ok") is False:
            return {
                "ok": False,
                "status_code": response.status_code,
                "error": payload.get("error") or f"http_{response.status_code}",
                **detail,
            }

        ingest_cursor = detail.get("ingest_cursor") if isinstance(detail.get("ingest_cursor"), dict) else {}
        ingest_intervals = ingest_cursor.get("intervals") if isinstance(ingest_cursor.get("intervals"), dict) else {}
        latest_5m = dict(ingest_intervals.get("5m") or {})
        dispatch_intervals = {
            **(detail.get("dispatch_cursor", {}).get("intervals") or {}),
            "5m": {
                **latest_5m,
                "last_dispatched_at_ms": int(time.time() * 1000),
                "dispatch_source": "ibkr_scheduler",
            },
        }
        saved_dispatch_cursor = save_dispatch_cursor(
            environment,
            {
                "intervals": dispatch_intervals,
                "latest_compute_result": payload,
            },
        )
        return {
            "ok": True,
            "skipped": False,
            "compute": payload,
            **{
                **detail,
                "latest_dispatched_bar_time_ms": int((dispatch_intervals.get("5m") or {}).get("latest_bar_time_ms") or 0),
                "dispatch_cursor": saved_dispatch_cursor,
            },
        }

    return _run_compute_dispatch
