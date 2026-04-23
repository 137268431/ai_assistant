from __future__ import annotations

from typing import Any, Callable

RequestJson = Callable[..., dict[str, Any]]
SchedulerStatusFn = Callable[[str], dict[str, Any]]


def extract_cursor_interval(cursor_payload: dict[str, Any], interval: str = "5m") -> dict[str, Any]:
    intervals = cursor_payload.get("intervals") if isinstance(cursor_payload.get("intervals"), dict) else {}
    bucket = intervals.get(interval) if isinstance(intervals, dict) else {}
    return dict(bucket) if isinstance(bucket, dict) else {}



def build_scheduler_summary(environment: str, scheduler_payload: dict[str, Any]) -> dict[str, Any]:
    payload = scheduler_payload if isinstance(scheduler_payload, dict) else {}
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), dict) else {}
    ingest_cursor = payload.get("ingest_cursor") if isinstance(payload.get("ingest_cursor"), dict) else {}
    dispatch_cursor = payload.get("compute_dispatch_cursor") if isinstance(payload.get("compute_dispatch_cursor"), dict) else {}
    ingest_5m = extract_cursor_interval(ingest_cursor, "5m")
    dispatch_5m = extract_cursor_interval(dispatch_cursor, "5m")
    latest_ingested_bar_time_ms = int(ingest_5m.get("latest_bar_time_ms") or 0)
    latest_dispatched_bar_time_ms = int(dispatch_5m.get("latest_bar_time_ms") or 0)
    lag_ms = max(0, latest_ingested_bar_time_ms - latest_dispatched_bar_time_ms) if latest_ingested_bar_time_ms else 0

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
        "latest_dispatched_bar_time_ms": latest_dispatched_bar_time_ms,
        "last_dispatch_at_ms": int(dispatch_5m.get("last_dispatched_at_ms") or 0),
        "dispatch_lag_ms": lag_ms,
        "dispatch_lag_min": round(lag_ms / 60000.0, 2) if lag_ms else 0.0,
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
    "augment_scheduler_summary",
    "build_scheduler_summary",
    "extract_cursor_interval",
    "scheduler_job_states",
    "scheduler_status",
]
