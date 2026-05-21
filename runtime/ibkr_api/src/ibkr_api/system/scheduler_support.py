from __future__ import annotations

import copy
import os
import threading
import time
from typing import Any, Callable

from ibkr_compute.core.broker_mode import (
    configured_broker_mode,
    normalize_broker_mode,
    resolve_market_data_mode,
)

RequestJson = Callable[..., dict[str, Any]]
RequestJsonRequest = Callable[..., dict[str, Any]]
SchedulerStatusFn = Callable[[str], dict[str, Any]]

MANUAL_SCHEDULER_JOB_ALLOWLIST = {
    "ibkr_compute_runtime",
    "ibkr_data_quality_repair_sweep",
    "ibkr_scan_runtime",
}
MANUAL_SCHEDULER_JOB_TIMEOUT_SECONDS = {
    "ibkr_data_quality_repair_sweep": 300,
}
NON_COMPUTE_DISPATCH_SOURCES = {
    "backfill",
    "history_backfill",
    "history_rebuild",
    "history_repair",
    "ibkr_history_backfill",
    "ibkr_history_rebuild",
}
SCHEDULER_STATUS_TIMEOUT_SECONDS = max(
    1.0,
    float(os.environ.get("IBKR_SCHEDULER_STATUS_TIMEOUT_SEC", "12") or "12"),
)
SCHEDULER_STATUS_CACHE_TTL_SECONDS = max(
    0.0,
    float(os.environ.get("IBKR_SCHEDULER_STATUS_CACHE_TTL_SEC", "180") or "180"),
)
_SCHEDULER_STATUS_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_SCHEDULER_STATUS_CACHE_LOCK = threading.Lock()


def _scheduler_cache_key(broker_mode: str, market_data_mode: str) -> tuple[str, str]:
    return (str(broker_mode or "").strip().lower(), str(market_data_mode or "").strip().lower())


def _scheduler_result_meta(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_url": result.get("target_url"),
        "status_code": result.get("status_code"),
        "error": result.get("error") or "",
    }


def _is_complete_scheduler_payload(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().lower()
    jobs = payload.get("jobs")
    return bool(payload.get("ok", False)) and status not in {"", "unknown", "offline", "error"} and isinstance(jobs, dict) and bool(jobs)


def _store_scheduler_status_cache(key: tuple[str, str], payload: dict[str, Any]) -> None:
    if SCHEDULER_STATUS_CACHE_TTL_SECONDS <= 0:
        return
    with _SCHEDULER_STATUS_CACHE_LOCK:
        _SCHEDULER_STATUS_CACHE[key] = {
            "stored_at": time.time(),
            "payload": copy.deepcopy(payload),
        }


def _cached_scheduler_status_payload(key: tuple[str, str], result: dict[str, Any]) -> dict[str, Any]:
    if SCHEDULER_STATUS_CACHE_TTL_SECONDS <= 0:
        return {}
    now = time.time()
    with _SCHEDULER_STATUS_CACHE_LOCK:
        cached = _SCHEDULER_STATUS_CACHE.get(key)
        if not cached:
            return {}
        age_s = max(0.0, now - float(cached.get("stored_at") or 0.0))
        if age_s > SCHEDULER_STATUS_CACHE_TTL_SECONDS:
            _SCHEDULER_STATUS_CACHE.pop(key, None)
            return {}
        payload = copy.deepcopy(cached.get("payload") if isinstance(cached.get("payload"), dict) else {})
    if not payload:
        return {}
    meta = dict(payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {})
    meta.update(
        {
            "from_cache": True,
            "stale_age_s": round(age_s, 3),
            "cache_stored_at_ms": int(float(cached.get("stored_at") or now) * 1000),
            "target_url": result.get("target_url") or meta.get("target_url"),
            "status_code": result.get("status_code") or meta.get("status_code"),
            "error": result.get("error") or meta.get("error") or "scheduler_status_unavailable",
        }
    )
    payload["_meta"] = meta
    payload["stale"] = True
    payload["stale_age_s"] = round(age_s, 3)
    return payload


def extract_cursor_interval(cursor_payload: dict[str, Any], interval: str = "5m") -> dict[str, Any]:
    intervals = cursor_payload.get("intervals") if isinstance(cursor_payload.get("intervals"), dict) else {}
    bucket = intervals.get(interval) if isinstance(intervals, dict) else {}
    return dict(bucket) if isinstance(bucket, dict) else {}


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


def _normalize_symbols(raw_symbols: Any) -> list[str]:
    if not isinstance(raw_symbols, (list, tuple, set)):
        return []
    symbols = set()
    for item in raw_symbols:
        symbol = str(item or "").strip().upper()
        if symbol:
            symbols.add(symbol)
    return sorted(symbols)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return int(default or 0)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return float(default or 0.0)


def _compute_runtime_last_result(jobs: dict[str, Any]) -> dict[str, Any]:
    compute_job = jobs.get("ibkr_compute_runtime") if isinstance(jobs.get("ibkr_compute_runtime"), dict) else {}
    last_result = compute_job.get("last_result") if isinstance(compute_job.get("last_result"), dict) else {}
    nested = last_result.get("payload") if isinstance(last_result.get("payload"), dict) else {}
    return dict(nested or last_result) if isinstance((nested or last_result), dict) else {}


def _resolve_compute_ingest_cursor(ingest_5m: dict[str, Any], latest_dispatched_bar_time_ms: int) -> dict[str, Any]:
    raw_latest = int(ingest_5m.get("latest_bar_time_ms") or 0)
    explicit_latest = int(ingest_5m.get("latest_compute_ingest_bar_time_ms") or 0)
    raw_sources = _cursor_sources(ingest_5m, "latest_sources")
    explicit_sources = _cursor_sources(ingest_5m, "latest_compute_ingest_sources")
    if explicit_latest > 0:
        return {
            "latest_compute_ingested_bar_time_ms": explicit_latest,
            "latest_compute_ingest_sources": explicit_sources,
            "dispatch_lag_compute_relevant": True,
            "dispatch_lag_reason": "",
            "compute_ingest_source_policy": str(
                ingest_5m.get("compute_ingest_source_policy") or "compute_ingest_cursor"
            ),
        }
    if raw_latest > 0 and _sources_are_non_compute_only(raw_sources):
        return {
            "latest_compute_ingested_bar_time_ms": int(latest_dispatched_bar_time_ms or 0),
            "latest_compute_ingest_sources": [],
            "dispatch_lag_compute_relevant": False,
            "dispatch_lag_reason": "non_compute_ingest_source",
            "compute_ingest_source_policy": "non_compute_source_filter",
        }
    return {
        "latest_compute_ingested_bar_time_ms": raw_latest,
        "latest_compute_ingest_sources": raw_sources,
        "dispatch_lag_compute_relevant": True,
        "dispatch_lag_reason": "",
        "compute_ingest_source_policy": "ingest_cursor",
    }



def build_scheduler_summary(environment: str, scheduler_payload: dict[str, Any]) -> dict[str, Any]:
    payload = scheduler_payload if isinstance(scheduler_payload, dict) else {}
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), dict) else {}
    raw_status = str(payload.get("status") or "").strip().lower()
    ingest_cursor = payload.get("ingest_cursor") if isinstance(payload.get("ingest_cursor"), dict) else {}
    dispatch_cursor = payload.get("compute_dispatch_cursor") if isinstance(payload.get("compute_dispatch_cursor"), dict) else {}
    ingest_5m = extract_cursor_interval(ingest_cursor, "5m")
    dispatch_5m = extract_cursor_interval(dispatch_cursor, "5m")
    latest_ingested_bar_time_ms = int(ingest_5m.get("latest_bar_time_ms") or 0)
    latest_dispatched_bar_time_ms = int(dispatch_5m.get("latest_bar_time_ms") or 0)
    last_compute_result = _compute_runtime_last_result(jobs)
    compute_ingest = _resolve_compute_ingest_cursor(ingest_5m, latest_dispatched_bar_time_ms)
    latest_compute_ingested_bar_time_ms = int(compute_ingest.get("latest_compute_ingested_bar_time_ms") or 0)
    lag_ms = (
        max(0, latest_compute_ingested_bar_time_ms - latest_dispatched_bar_time_ms)
        if latest_compute_ingested_bar_time_ms else 0
    )
    raw_lag_ms = max(0, latest_ingested_bar_time_ms - latest_dispatched_bar_time_ms) if latest_ingested_bar_time_ms else 0

    status_counts: dict[str, int] = {}
    for state in jobs.values():
        normalized = str((state or {}).get("status") or "idle").strip().lower() or "idle"
        status_counts[normalized] = status_counts.get(normalized, 0) + 1

    indicator_coverage_status = str(
        dispatch_5m.get("indicator_coverage_status")
        or last_compute_result.get("indicator_coverage_status")
        or ""
    ).strip().lower()
    missing_indicator_symbols = _normalize_symbols(
        dispatch_5m.get("missing_indicator_symbols")
        or last_compute_result.get("missing_indicator_symbols")
        or []
    )
    deferred_busy_symbols = _normalize_symbols(
        dispatch_5m.get("deferred_busy_symbols")
        or last_compute_result.get("deferred_busy_symbols")
        or []
    )
    realtime_compute = last_compute_result.get("realtime_compute") if isinstance(last_compute_result.get("realtime_compute"), dict) else {}
    canonical_5m = last_compute_result.get("canonical_5m") if isinstance(last_compute_result.get("canonical_5m"), dict) else {}
    last_reason = str(last_compute_result.get("reason") or "").strip().lower()
    compute_in_progress = bool(
        last_compute_result.get("compute_in_progress")
        or dispatch_5m.get("compute_in_progress")
        or last_compute_result.get("official_5m_close_in_progress")
        or last_reason in {"close_compute_inflight", "official_5m_close_inflight"}
    )
    inflight_stalled = bool(
        last_compute_result.get("inflight_stalled")
        or last_compute_result.get("compute_in_progress_stalled")
        or last_compute_result.get("official_5m_close_stalled")
        or (realtime_compute or {}).get("stalled")
    )
    dispatch_lag_reason = str(compute_ingest.get("dispatch_lag_reason") or "")
    if compute_in_progress and dispatch_lag_reason != "non_compute_ingest_source":
        dispatch_lag_reason = last_reason if last_reason in {"close_compute_inflight", "official_5m_close_inflight"} else "close_compute_inflight"

    return {
        "ok": bool(payload.get("ok", False)) if payload else False,
        "status": raw_status or ("running" if payload else "unknown"),
        "environment": str(payload.get("environment") or environment).strip().lower() or environment,
        "loop_interval_seconds": float(payload.get("loop_interval_seconds") or 0),
        "job_count": len(jobs),
        "job_status_counts": status_counts,
        "jobs": jobs,
        "ingest_cursor": ingest_cursor,
        "compute_dispatch_cursor": dispatch_cursor,
        "latest_ingested_bar_time_ms": latest_ingested_bar_time_ms,
        "latest_ingest_sources": _cursor_sources(ingest_5m, "latest_sources"),
        "latest_compute_ingested_bar_time_ms": latest_compute_ingested_bar_time_ms,
        "latest_compute_ingest_sources": list(compute_ingest.get("latest_compute_ingest_sources") or []),
        "latest_dispatched_bar_time_ms": latest_dispatched_bar_time_ms,
        "last_dispatch_at_ms": int(dispatch_5m.get("last_dispatched_at_ms") or 0),
        "raw_dispatch_lag_ms": raw_lag_ms,
        "raw_dispatch_lag_min": round(raw_lag_ms / 60000.0, 2) if raw_lag_ms else 0.0,
        "dispatch_lag_ms": lag_ms,
        "dispatch_lag_min": round(lag_ms / 60000.0, 2) if lag_ms else 0.0,
        "dispatch_lag_compute_relevant": bool(compute_ingest.get("dispatch_lag_compute_relevant", True)),
        "dispatch_lag_reason": dispatch_lag_reason,
        "compute_ingest_source_policy": str(compute_ingest.get("compute_ingest_source_policy") or ""),
        "indicator_coverage_status": indicator_coverage_status,
        "indicator_coverage_bar_time_ms": _coerce_int(
            dispatch_5m.get("indicator_coverage_bar_time_ms")
            or last_compute_result.get("indicator_coverage_bar_time_ms")
        ),
        "missing_indicator_symbols": missing_indicator_symbols,
        "missing_indicator_symbol_count": _coerce_int(
            dispatch_5m.get("missing_indicator_symbol_count")
            or last_compute_result.get("missing_indicator_symbol_count")
            or len(missing_indicator_symbols)
        ),
        "missing_indicator_symbols_sample": missing_indicator_symbols[:20],
        "covered_indicator_symbol_count": _coerce_int(
            dispatch_5m.get("covered_indicator_symbol_count")
            or last_compute_result.get("covered_indicator_symbol_count")
        ),
        "compute_in_progress": compute_in_progress,
        "compute_in_progress_stalled": inflight_stalled,
        "inflight_age_s": _coerce_float(
            last_compute_result.get("inflight_age_s")
            or last_compute_result.get("official_5m_close_age_s")
            or dispatch_5m.get("inflight_age_s")
        ),
        "inflight_timeout_threshold_s": _coerce_float(
            last_compute_result.get("inflight_timeout_threshold_s")
            or last_compute_result.get("official_5m_close_timeout_threshold_s")
            or dispatch_5m.get("inflight_timeout_threshold_s")
        ),
        "inflight_stall_reason": str(
            last_compute_result.get("inflight_stall_reason")
            or (realtime_compute or {}).get("stall_reason")
            or ""
        ),
        "official_5m_close_in_progress": bool(last_compute_result.get("official_5m_close_in_progress")),
        "official_5m_close_stalled": bool(last_compute_result.get("official_5m_close_stalled")),
        "official_5m_close_age_s": _coerce_float(last_compute_result.get("official_5m_close_age_s")),
        "canonical_5m": canonical_5m,
        "deferred_compute_busy": bool(dispatch_5m.get("deferred_compute_busy") or last_compute_result.get("deferred_compute_busy")),
        "deferred_busy_symbols": deferred_busy_symbols,
        "deferred_busy_symbol_count": _coerce_int(
            dispatch_5m.get("deferred_busy_symbol_count")
            or last_compute_result.get("deferred_busy_symbol_count")
            or len(deferred_busy_symbols)
        ),
        "deferred_busy_symbols_sample": deferred_busy_symbols[:20],
        "latest_partial_dispatched_bar_time_ms": _coerce_int(dispatch_5m.get("latest_partial_dispatched_bar_time_ms")),
    }



def scheduler_status(
    environment: str = "live",
    *,
    request_json: RequestJson,
    scheduler_base_url: str,
    broker_mode: str = "",
    market_data_mode: str = "",
    lite: bool = False,
) -> dict[str, Any]:
    normalized_broker_mode = normalize_broker_mode(broker_mode, configured_broker_mode())
    normalized_market_data_mode = resolve_market_data_mode(market_data_mode or environment)
    cache_key = _scheduler_cache_key(normalized_broker_mode, normalized_market_data_mode)
    params = [
        ("broker_mode", normalized_broker_mode),
        ("market_data_mode", normalized_market_data_mode),
    ]
    if lite:
        params.append(("lite", "1"))
    result = request_json(
        scheduler_base_url,
        "/status",
        params=params,
        timeout=SCHEDULER_STATUS_TIMEOUT_SECONDS,
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if payload:
        payload_with_meta = {
            **payload,
            "ok": bool(payload.get("ok", result.get("ok", False))),
            "stale": bool(payload.get("stale", False)),
            "_meta": _scheduler_result_meta(result),
        }
        if _is_complete_scheduler_payload(payload_with_meta):
            _store_scheduler_status_cache(cache_key, payload_with_meta)
            return payload_with_meta
        cached_payload = _cached_scheduler_status_payload(cache_key, result)
        if cached_payload:
            return cached_payload
        return payload_with_meta
    cached_payload = _cached_scheduler_status_payload(cache_key, result)
    if cached_payload:
        return cached_payload
    return {
        "ok": False,
        "status": "unknown",
        "environment": normalized_market_data_mode,
        "broker_mode": normalized_broker_mode,
        "market_data_mode": normalized_market_data_mode,
        "jobs": {},
        "ingest_cursor": {},
        "compute_dispatch_cursor": {},
        "stale": False,
        "_meta": {
            **_scheduler_result_meta(result),
            "error": result.get("error") or "scheduler_unavailable",
        },
    }


def run_scheduler_job(
    *,
    job_id: str,
    environment: str = "",
    broker_mode: str = "",
    market_data_mode: str = "",
    trigger_source: str,
    request_json_request: RequestJsonRequest,
    scheduler_base_url: str,
) -> tuple[dict[str, Any], int]:
    normalized_job_id = str(job_id or "").strip()
    normalized_broker_mode = normalize_broker_mode(broker_mode, configured_broker_mode())
    normalized_market_data_mode = resolve_market_data_mode(market_data_mode or environment)
    normalized_trigger_source = str(trigger_source or "api_manual").strip() or "api_manual"

    if normalized_job_id not in MANUAL_SCHEDULER_JOB_ALLOWLIST:
        return {
            "ok": False,
            "environment": normalized_market_data_mode,
            "broker_mode": normalized_broker_mode,
            "market_data_mode": normalized_market_data_mode,
            "job_id": normalized_job_id,
            "error": "unsupported_scheduler_job",
            "allowed_jobs": sorted(MANUAL_SCHEDULER_JOB_ALLOWLIST),
            "source": "ibkr-api",
        }, 400

    result = request_json_request(
        "POST",
        scheduler_base_url,
        f"/jobs/run/{normalized_job_id}",
        json_body={
            "broker_mode": normalized_broker_mode,
            "market_data_mode": normalized_market_data_mode,
            "trigger_source": normalized_trigger_source,
        },
        timeout=MANUAL_SCHEDULER_JOB_TIMEOUT_SECONDS.get(normalized_job_id, 120),
    )
    scheduler_result = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    scheduler_error = str(result.get("error") or "").strip()
    status_code = int(result.get("status_code") or 0)
    scan_result = scheduler_result.get("payload") if isinstance(scheduler_result.get("payload"), dict) else {}
    ok = bool(result.get("ok")) and bool(scheduler_result.get("ok", False))

    response_payload = {
        "ok": ok,
        "environment": normalized_market_data_mode,
        "broker_mode": normalized_broker_mode,
        "market_data_mode": normalized_market_data_mode,
        "job_id": normalized_job_id,
        "trigger_source": normalized_trigger_source,
        "scheduler_result": scheduler_result,
        "scan_result": scan_result,
        "source": "ibkr-api",
        "_meta": {
            "target_url": result.get("target_url"),
            "status_code": status_code,
            "error": scheduler_error,
        },
    }
    if scheduler_error:
        response_payload["error"] = scheduler_error
    elif not scheduler_result:
        response_payload["error"] = "scheduler_unavailable"
    elif not ok:
        response_payload["error"] = str(scheduler_result.get("error") or scheduler_result.get("reason") or "scheduler_job_failed")

    if ok:
        return response_payload, 200
    if status_code in {400, 401, 403, 404}:
        return response_payload, status_code
    return response_payload, 502



def scheduler_job_states(environment: str = "live", *, scheduler_status_fn: SchedulerStatusFn) -> dict[str, Any]:
    payload = scheduler_status_fn(environment)
    jobs = payload.get("jobs") if isinstance(payload, dict) else {}
    return jobs if isinstance(jobs, dict) else {}



def augment_scheduler_summary(summary: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    definitions = items if isinstance(items, list) else []
    return {
        **(summary if isinstance(summary, dict) else {}),
        "enabled_job_count": sum(1 for item in definitions if bool(item.get("effective_enabled"))),
        "native_job_count": sum(1 for item in definitions if str(item.get("runner_kind") or "").startswith("native_")),
        "compatibility_job_count": sum(
            1 for item in definitions if str(item.get("runner_kind") or "").strip().lower() == "compatibility_pending"
        ),
    }


__all__ = [
    "MANUAL_SCHEDULER_JOB_ALLOWLIST",
    "augment_scheduler_summary",
    "build_scheduler_summary",
    "extract_cursor_interval",
    "run_scheduler_job",
    "scheduler_job_states",
    "scheduler_status",
]
