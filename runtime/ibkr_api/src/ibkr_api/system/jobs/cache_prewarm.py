from __future__ import annotations

import os
import time
from typing import Any, Callable

from ibkr_api.app_core.cache_snapshots import build_snapshot_cache_key, get_cached_snapshot, now_ms, upsert_cached_snapshot
from ibkr_api.home.dashboard import build_home_dashboard_response
from ibkr_api.home.market import build_home_market_response
from ibkr_api.modes import request_broker_mode, request_market_data_mode
from ibkr_api.universe.lifecycle_flow import build_lifecycle_flow_response


NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
ConfigValue = Callable[[str, str, str], str]
RequestJsonRequest = Callable[..., dict[str, Any]]
BuildSystemSummaryPayload = Callable[..., dict[str, Any]]
BuildTodayTargetsResponse = Callable[..., tuple[dict[str, Any], int]]
BuildActiveWindowProgressResponse = Callable[..., tuple[dict[str, Any], int]]
SnapshotBuilder = Callable[[], tuple[dict[str, Any], int]]

DEFAULT_PAGES = (
    "home-dashboard",
    "home-market",
    "system-summary-lite",
    "today-targets-home",
    "today-targets-screener",
    "active-window-progress",
    "active-window-progress-default",
    "lifecycle-flow",
)
PAGE_ALIASES = {
    "all": set(DEFAULT_PAGES),
    "home": {"home-dashboard", "home-market", "today-targets-home"},
    "summary": {"system-summary-lite"},
    "system": {"system-summary-lite"},
    "today-targets": {"today-targets-home", "today-targets-screener"},
    "active-window": {"active-window-progress", "active-window-progress-default"},
    "active-window-progress": {"active-window-progress", "active-window-progress-default"},
    "lifecycle": {"lifecycle-flow"},
    "universe": {
        "today-targets-home",
        "today-targets-screener",
        "active-window-progress",
        "active-window-progress-default",
        "lifecycle-flow",
    },
}


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _truthy(value: Any, *, default: bool = False) -> bool:
    text = _to_text(value).lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _cache_seconds(env_name: str, fallback: float) -> float:
    try:
        return max(0.0, float(os.environ.get(env_name, fallback)))
    except (TypeError, ValueError):
        return float(fallback)


def _times_date(time_strings: TimeStrings) -> str:
    try:
        times = time_strings() or {}
    except Exception:
        times = {}
    return _to_text((times if isinstance(times, dict) else {}).get("date")) or time.strftime("%Y-%m-%d")


def _market_date(payload: dict[str, Any], time_strings: TimeStrings) -> str:
    return _to_text(payload.get("market_date") or payload.get("date")) or _times_date(time_strings)


def _normalize_pages(value: Any) -> set[str]:
    if value is None or value == "":
        return set(DEFAULT_PAGES)
    raw_items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    pages: set[str] = set()
    for raw in raw_items:
        item = _to_text(raw).lower()
        if not item:
            continue
        pages.update(PAGE_ALIASES.get(item, {item}))
    if not pages:
        return set(DEFAULT_PAGES)
    return pages


def _fresh(record: dict[str, Any] | None) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        return now_ms() <= int(float(record.get("fresh_until_ms") or 0))
    except Exception:
        return False


def _item_count(payload: dict[str, Any]) -> int | None:
    for key in ("items", "nodes", "events", "symbols"):
        items = payload.get(key)
        if isinstance(items, list):
            return len(items)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    for key in ("total", "active_count", "target_active_count"):
        value = summary.get(key)
        if value is not None:
            try:
                return int(value)
            except Exception:
                continue
    return None


def _call_payload_builder(builder: Callable[..., tuple[dict[str, Any], int]], payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    try:
        return builder(payload=payload)
    except TypeError:
        return builder(payload)


def _prewarm_snapshot(
    pb: Any,
    *,
    page: str,
    scope: str,
    request_payload: dict[str, Any],
    builder: SnapshotBuilder,
    environment: str,
    market_date: str,
    ttl_seconds: float,
    stale_seconds: float,
    force: bool,
) -> dict[str, Any]:
    cache_key = build_snapshot_cache_key(scope, request_payload)
    existing = get_cached_snapshot(pb, cache_key)
    if not force and _fresh(existing):
        return {
            "page": page,
            "scope": scope,
            "cache_key": cache_key,
            "ok": True,
            "status": "fresh_skip",
            "cache_written": False,
            "cached_record_id": _to_text(existing.get("id")) if isinstance(existing, dict) else "",
        }

    started = time.monotonic()
    try:
        payload, status_code = builder()
    except Exception as exc:
        return {
            "page": page,
            "scope": scope,
            "cache_key": cache_key,
            "ok": False,
            "status": "builder_error",
            "error": str(exc),
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        }

    result: dict[str, Any] = {
        "page": page,
        "scope": scope,
        "cache_key": cache_key,
        "ok": status_code < 400,
        "status": "built" if status_code < 400 else "upstream_error",
        "status_code": status_code,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
    }
    if isinstance(payload, dict):
        count = _item_count(payload)
        if count is not None:
            result["item_count"] = count
    if status_code < 400 and isinstance(payload, dict):
        stored = upsert_cached_snapshot(
            pb,
            cache_key,
            scope,
            environment,
            market_date,
            payload,
            ttl_seconds,
            stale_seconds,
        )
        result["cache_written"] = bool(stored)
        if isinstance(stored, dict):
            result["cached_record_id"] = _to_text(stored.get("id"))
    return result


def build_cache_prewarm_response(
    pb: Any,
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    config_value: ConfigValue,
    request_json_request: RequestJsonRequest,
    runtime_base_url: str,
    build_system_summary_payload: BuildSystemSummaryPayload,
    build_today_targets_response: BuildTodayTargetsResponse,
    build_active_window_progress_response: BuildActiveWindowProgressResponse,
) -> tuple[dict[str, Any], int]:
    request_payload = payload if isinstance(payload, dict) else {}
    broker_mode = request_broker_mode(request_payload)
    data_environment = request_market_data_mode(request_payload)
    market_date = _market_date(request_payload, time_strings)
    force = _truthy(request_payload.get("force") or request_payload.get("cache_bust"), default=False)
    selected_pages = _normalize_pages(request_payload.get("pages") or request_payload.get("include"))

    base_page_payload = {
        "broker_mode": broker_mode,
        "market_data_mode": data_environment,
        "data_environment": data_environment,
        "market_date": market_date,
    }
    tasks: list[dict[str, Any]] = [
        {
            "page": "home-dashboard",
            "scope": "home-dashboard",
            "request_payload": dict(base_page_payload),
            "builder": lambda request_payload=dict(base_page_payload): build_home_dashboard_response(
                pb,
                payload=request_payload,
                time_strings=time_strings,
                request_json_request=request_json_request,
                runtime_base_url=runtime_base_url,
            ),
            "environment": data_environment,
            "market_date": market_date,
            "ttl_seconds": _cache_seconds("IBKR_ROUTE_CACHE_HOME_DASHBOARD_TTL_SEC", 15.0),
            "stale_seconds": _cache_seconds("IBKR_ROUTE_CACHE_HOME_DASHBOARD_STALE_SEC", 45.0),
        },
        {
            "page": "home-market",
            "scope": "home-market",
            "request_payload": dict(base_page_payload),
            "builder": lambda request_payload=dict(base_page_payload): build_home_market_response(
                pb,
                payload=request_payload,
                config_value=config_value,
                request_json_request=request_json_request,
                runtime_base_url=runtime_base_url,
                time_strings=time_strings,
            ),
            "environment": data_environment,
            "market_date": market_date,
            "ttl_seconds": _cache_seconds("IBKR_ROUTE_CACHE_HOME_MARKET_TTL_SEC", 15.0),
            "stale_seconds": _cache_seconds("IBKR_ROUTE_CACHE_HOME_MARKET_STALE_SEC", 45.0),
        },
        {
            "page": "system-summary-lite",
            "scope": "system-summaryz",
            "request_payload": {"broker_mode": broker_mode, "environment": broker_mode, "lite": True},
            "builder": lambda: (build_system_summary_payload(broker_mode, lite_mode=True), 200),
            "environment": broker_mode,
            "market_date": "global",
            "ttl_seconds": _cache_seconds("IBKR_CONTROL_PLANE_SUMMARY_LITE_TTL_SEC", 15.0),
            "stale_seconds": _cache_seconds("IBKR_CONTROL_PLANE_STALE_SEC", 60.0),
        },
        {
            "page": "today-targets-home",
            "scope": "today-targets",
            "request_payload": {**base_page_payload, "paginate": False, "per_page": None},
            "builder": lambda request_payload={**base_page_payload, "paginate": False, "per_page": None}: _call_payload_builder(
                build_today_targets_response,
                request_payload,
            ),
            "environment": data_environment,
            "market_date": market_date,
            "ttl_seconds": _cache_seconds("IBKR_ROUTE_CACHE_TODAY_TARGETS_TTL_SEC", 30.0),
            "stale_seconds": _cache_seconds("IBKR_ROUTE_CACHE_TODAY_TARGETS_STALE_SEC", 120.0),
        },
        {
            "page": "today-targets-screener",
            "scope": "today-targets",
            "request_payload": {
                **base_page_payload,
                "signaled_only": "false",
                "sort_by": "attention_asc",
                "page": "1",
                "per_page": "10",
                "paginate": True,
            },
            "builder": lambda request_payload={
                **base_page_payload,
                "signaled_only": "false",
                "sort_by": "attention_asc",
                "page": "1",
                "per_page": "10",
                "paginate": True,
            }: _call_payload_builder(
                build_today_targets_response,
                request_payload,
            ),
            "environment": data_environment,
            "market_date": market_date,
            "ttl_seconds": _cache_seconds("IBKR_ROUTE_CACHE_TODAY_TARGETS_TTL_SEC", 30.0),
            "stale_seconds": _cache_seconds("IBKR_ROUTE_CACHE_TODAY_TARGETS_STALE_SEC", 120.0),
        },
        {
            "page": "active-window-progress",
            "scope": "active-window-progress",
            "request_payload": {**base_page_payload, "status": "all", "limit": "200"},
            "builder": lambda request_payload={**base_page_payload, "status": "all", "limit": "200"}: _call_payload_builder(
                build_active_window_progress_response,
                request_payload,
            ),
            "environment": data_environment,
            "market_date": market_date,
            "ttl_seconds": _cache_seconds("IBKR_ROUTE_CACHE_ACTIVE_WINDOW_TTL_SEC", 30.0),
            "stale_seconds": _cache_seconds("IBKR_ROUTE_CACHE_ACTIVE_WINDOW_STALE_SEC", 120.0),
        },
        {
            "page": "active-window-progress-default",
            "scope": "active-window-progress",
            "request_payload": dict(base_page_payload),
            "builder": lambda request_payload=dict(base_page_payload): _call_payload_builder(
                build_active_window_progress_response,
                request_payload,
            ),
            "environment": data_environment,
            "market_date": market_date,
            "ttl_seconds": _cache_seconds("IBKR_ROUTE_CACHE_ACTIVE_WINDOW_TTL_SEC", 30.0),
            "stale_seconds": _cache_seconds("IBKR_ROUTE_CACHE_ACTIVE_WINDOW_STALE_SEC", 120.0),
        },
        {
            "page": "lifecycle-flow",
            "scope": "lifecycle-flow",
            "request_payload": {
                "environment": data_environment,
                "broker_mode": broker_mode,
                "market_data_mode": data_environment,
                "data_environment": data_environment,
            },
            "builder": lambda request_payload={
                "environment": data_environment,
                "broker_mode": broker_mode,
                "market_data_mode": data_environment,
                "data_environment": data_environment,
            }: build_lifecycle_flow_response(
                pb,
                payload=request_payload,
                normalize_environment=normalize_environment,
                time_strings=time_strings,
            ),
            "environment": data_environment,
            "market_date": market_date,
            "ttl_seconds": _cache_seconds("IBKR_ROUTE_CACHE_LIFECYCLE_TTL_SEC", 30.0),
            "stale_seconds": _cache_seconds("IBKR_ROUTE_CACHE_LIFECYCLE_STALE_SEC", 300.0),
        },
    ]

    started = time.monotonic()
    items = [
        _prewarm_snapshot(pb, force=force, **task)
        for task in tasks
        if _to_text(task.get("page")).lower() in selected_pages
    ]
    failed_items = [item for item in items if not bool(item.get("ok"))]
    written_count = len([item for item in items if bool(item.get("cache_written"))])
    skipped_count = len([item for item in items if item.get("status") == "fresh_skip"])
    response = {
        "ok": not failed_items,
        "job_id": "ibkr_cache_prewarm",
        "source": "ibkr-api",
        "broker_mode": broker_mode,
        "data_environment": data_environment,
        "market_data_mode": data_environment,
        "market_date": market_date,
        "force": force,
        "requested_pages": sorted(selected_pages),
        "attempted_count": len(items),
        "cache_written_count": written_count,
        "fresh_skip_count": skipped_count,
        "failed_count": len(failed_items),
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        "items": items,
    }
    return response, 200


__all__ = ["build_cache_prewarm_response"]
