from __future__ import annotations

from typing import Any, Callable

RequestJson = Callable[..., dict[str, Any]]
RequestJsonRequest = Callable[..., dict[str, Any]]
SchedulerStatusFn = Callable[[str], dict[str, Any]]

MANUAL_SCHEDULER_JOB_ALLOWLIST = {"ibkr_scan_runtime"}
NON_COMPUTE_DISPATCH_SOURCES = {
    "backfill",
    "history_backfill",
    "history_rebuild",
    "history_repair",
    "ibkr_history_backfill",
    "ibkr_history_rebuild",
}


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
            "compute_ingest_source_policy": "legacy_non_compute_source_filter",
        }
    return {
        "latest_compute_ingested_bar_time_ms": raw_latest,
        "latest_compute_ingest_sources": raw_sources,
        "dispatch_lag_compute_relevant": True,
        "dispatch_lag_reason": "",
        "compute_ingest_source_policy": "legacy_ingest_cursor",
    }



def build_scheduler_summary(environment: str, scheduler_payload: dict[str, Any]) -> dict[str, Any]:
    payload = scheduler_payload if isinstance(scheduler_payload, dict) else {}
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), dict) else {}
    ingest_cursor = payload.get("ingest_cursor") if isinstance(payload.get("ingest_cursor"), dict) else {}
    dispatch_cursor = payload.get("compute_dispatch_cursor") if isinstance(payload.get("compute_dispatch_cursor"), dict) else {}
    ingest_5m = extract_cursor_interval(ingest_cursor, "5m")
    dispatch_5m = extract_cursor_interval(dispatch_cursor, "5m")
    latest_ingested_bar_time_ms = int(ingest_5m.get("latest_bar_time_ms") or 0)
    latest_dispatched_bar_time_ms = int(dispatch_5m.get("latest_bar_time_ms") or 0)
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

    return {
        "ok": bool(payload.get("ok", False)) if payload else False,
        "status": str(payload.get("status") or ("running" if payload else "offline")).strip().lower() or "offline",
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
        "dispatch_lag_reason": str(compute_ingest.get("dispatch_lag_reason") or ""),
        "compute_ingest_source_policy": str(compute_ingest.get("compute_ingest_source_policy") or ""),
    }



def scheduler_status(
    environment: str = "live",
    *,
    request_json: RequestJson,
    scheduler_base_url: str,
) -> dict[str, Any]:
    result = request_json(
        scheduler_base_url,
        "/status",
        params=[("environment", environment)],
        timeout=5,
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if payload:
        return {
            **payload,
            "ok": bool(payload.get("ok", result.get("ok", False))),
            "_meta": {
                "target_url": result.get("target_url"),
                "status_code": result.get("status_code"),
                "error": result.get("error") or "",
            },
        }
    return {
        "ok": False,
        "status": "offline",
        "environment": environment,
        "jobs": {},
        "ingest_cursor": {},
        "compute_dispatch_cursor": {},
        "_meta": {
            "target_url": result.get("target_url"),
            "status_code": result.get("status_code"),
            "error": result.get("error") or "scheduler_unavailable",
        },
    }


def run_scheduler_job(
    *,
    job_id: str,
    environment: str,
    trigger_source: str,
    request_json_request: RequestJsonRequest,
    scheduler_base_url: str,
) -> tuple[dict[str, Any], int]:
    normalized_job_id = str(job_id or "").strip()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_trigger_source = str(trigger_source or "api_manual").strip() or "api_manual"

    if normalized_job_id not in MANUAL_SCHEDULER_JOB_ALLOWLIST:
        return {
            "ok": False,
            "environment": runtime_environment,
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
            "environment": runtime_environment,
            "trigger_source": normalized_trigger_source,
        },
        timeout=120,
    )
    scheduler_result = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    scheduler_error = str(result.get("error") or "").strip()
    status_code = int(result.get("status_code") or 0)
    scan_result = scheduler_result.get("payload") if isinstance(scheduler_result.get("payload"), dict) else {}
    ok = bool(result.get("ok")) and bool(scheduler_result.get("ok", False))

    response_payload = {
        "ok": ok,
        "environment": runtime_environment,
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
