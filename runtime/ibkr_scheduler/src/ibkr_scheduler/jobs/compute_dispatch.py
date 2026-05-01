from __future__ import annotations

import time
from typing import Any, Callable

import requests


NON_COMPUTE_DISPATCH_SOURCES = {
    "backfill",
    "history_backfill",
    "history_rebuild",
    "history_repair",
    "ibkr_history_backfill",
    "ibkr_history_rebuild",
}


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


def _cursor_sources(bucket: dict[str, Any], key: str = "latest_sources") -> list[str]:
    raw_sources = bucket.get(key)
    if not isinstance(raw_sources, list):
        raw_sources = []
    sources = []
    seen = set()
    for item in raw_sources:
        source = str(item or "").strip().lower()
        if not source or source in seen:
            continue
        seen.add(source)
        sources.append(source)
    return sources


def _sources_are_non_compute_only(sources: list[str]) -> bool:
    return bool(sources) and all(source in NON_COMPUTE_DISPATCH_SOURCES for source in sources)


def _resolve_compute_ingest_bar_time_ms(ingest_5m: dict[str, Any], latest_dispatched_bar_time_ms: int) -> tuple[int, str]:
    explicit_latest = int(ingest_5m.get("latest_compute_ingest_bar_time_ms") or 0)
    if explicit_latest > 0:
        return explicit_latest, ""
    raw_latest = int(ingest_5m.get("latest_bar_time_ms") or 0)
    if raw_latest > 0 and _sources_are_non_compute_only(_cursor_sources(ingest_5m)):
        return int(latest_dispatched_bar_time_ms or 0), "non_compute_ingest_source"
    return raw_latest, ""


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
        ingest_5m = ingest_intervals.get("5m") if isinstance(ingest_intervals.get("5m"), dict) else {}
        latest_raw_ingested = int(ingest_5m.get("latest_bar_time_ms") or 0)
        latest_dispatched = int(((dispatch_intervals.get("5m") or {}).get("latest_bar_time_ms") or 0))
        latest_ingested, skip_reason = _resolve_compute_ingest_bar_time_ms(ingest_5m, latest_dispatched)
        return latest_ingested > latest_dispatched, {
            "latest_ingested_bar_time_ms": latest_ingested,
            "latest_raw_ingested_bar_time_ms": latest_raw_ingested,
            "latest_dispatched_bar_time_ms": latest_dispatched,
            "compute_dispatch_skip_reason": skip_reason,
            "ingest_cursor": ingest,
            "dispatch_cursor": dispatch,
        }

    def _run_compute_dispatch(environment: str) -> dict[str, Any]:
        due, detail = _native_compute_due(environment)
        if not due:
            reason = str(detail.get("compute_dispatch_skip_reason") or "no_new_persisted_bars")
            dispatch_cursor = detail.get("dispatch_cursor") if isinstance(detail.get("dispatch_cursor"), dict) else {}
            saved_dispatch_cursor = dispatch_cursor
            if reason == "non_compute_ingest_source":
                ingest_cursor = detail.get("ingest_cursor") if isinstance(detail.get("ingest_cursor"), dict) else {}
                ingest_intervals = ingest_cursor.get("intervals") if isinstance(ingest_cursor.get("intervals"), dict) else {}
                latest_5m = dict(ingest_intervals.get("5m") or {})
                dispatch_5m = dict((dispatch_cursor.get("intervals") or {}).get("5m") or {})
                dispatch_intervals = {
                    **(dispatch_cursor.get("intervals") or {}),
                    "5m": {
                        **dispatch_5m,
                        "latest_non_compute_ingest_bar_time_ms": int(detail.get("latest_raw_ingested_bar_time_ms") or 0),
                        "latest_non_compute_ingest_sources": _cursor_sources(latest_5m),
                        "last_dispatched_at_ms": int(time.time() * 1000),
                        "dispatch_source": "ibkr_scheduler_skip_non_compute_ingest",
                        "dispatch_skip_reason": reason,
                    },
                }
                saved_dispatch_cursor = save_dispatch_cursor(
                    environment,
                    {
                        "intervals": dispatch_intervals,
                    },
                )
            return {
                "ok": True,
                "skipped": True,
                "reason": reason,
                **detail,
                "dispatch_cursor": saved_dispatch_cursor,
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
        latest_dispatch_ms = int(detail.get("latest_ingested_bar_time_ms") or latest_5m.get("latest_bar_time_ms") or 0)
        latest_5m["latest_bar_time_ms"] = latest_dispatch_ms
        if latest_dispatch_ms == int(latest_5m.get("latest_compute_ingest_bar_time_ms") or 0):
            latest_5m["latest_bar_us"] = str(latest_5m.get("latest_compute_ingest_bar_us") or latest_5m.get("latest_bar_us") or "")
            latest_5m["latest_bar_cn"] = str(latest_5m.get("latest_compute_ingest_bar_cn") or latest_5m.get("latest_bar_cn") or "")
            latest_5m["latest_batch_symbols"] = list(latest_5m.get("latest_compute_ingest_symbols") or latest_5m.get("latest_batch_symbols") or [])
            latest_5m["latest_sources"] = list(latest_5m.get("latest_compute_ingest_sources") or latest_5m.get("latest_sources") or [])
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
