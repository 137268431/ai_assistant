from __future__ import annotations

import inspect
import time
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode, resolve_data_environment
from ibkr_api.runtime.effective_gate import build_effective_trading_gate
from ibkr_api.system.service_state import (
    apply_service_monitor_to_topology,
    derive_backtest_state,
    derive_compute_state,
    derive_gateway_state,
    derive_runtime_state,
    rebuild_service_monitor,
    utc_timestamp,
)

NormalizeEnvironment = Callable[[Any, str], str]
FetchPayload = Callable[[str], dict[str, Any]]
AsDict = Callable[[Any], dict[str, Any]]
ConfigRefresh = Callable[[], None]
SchedulerStatus = Callable[..., dict[str, Any]]
BuildCronPayload = Callable[[Any, str, dict[str, Any]], list[dict[str, Any]]]
BuildSchedulerSummary = Callable[[str, dict[str, Any]], dict[str, Any]]
AugmentSchedulerSummary = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]
RequestJson = Callable[..., dict[str, Any]]
LoadEffectiveConfigMap = Callable[[str, tuple[str, ...] | list[str] | set[str] | None], dict[str, str]]
LoadRecentSystemEvents = Callable[[str, int], list[dict[str, Any]]]
EnrichMonitorPayload = Callable[[dict[str, Any]], dict[str, Any]]
DeriveMonitorServiceMap = Callable[..., dict[str, Any]]
MergeServiceTopology = Callable[..., dict[str, Any]]
BuildServiceTopology = Callable[[], dict[str, Any]]
ProbeConsoleStatus = Callable[[str], dict[str, Any]]
RequestsGet = Callable[..., requests.Response]
AccountSnapshotProbe = Callable[[str], dict[str, Any]]

ACCOUNT_SNAPSHOT_WARN_MS = 12_000.0
ET = ZoneInfo("America/New_York")
TV_FLOW_RECEIVED_STUCK_DEFAULT_MIN = 2
TV_FLOW_ACTION_PENDING_STUCK_DEFAULT_MIN = 15
TV_FLOW_FAILED_LOOKBACK_DEFAULT_MIN = 24 * 60
TV_FLOW_PENDING_LOOKBACK_DEFAULT_MIN = 24 * 60
TV_FLOW_SAMPLE_LIMIT_DEFAULT = 5
TV_FLOW_EOD_CLOSE_TIME_DEFAULT = "15:55"
TV_FLOW_EVENT_FETCH_MAX_PAGES = 4
TV_FLOW_ACTION_FETCH_MAX_PAGES = 10
BAR_PIPELINE_CONFIG_KEYS = (
    "ibkr_legacy_bar_pipeline_enabled",
    "ibkr_signal_source",
    "ibkr_tv_primary_runtime_slim_enabled",
    "ibkr_runtime_technical_pipeline_enabled",
)
TV_FLOW_CONFIG_KEYS = (
    "tv_flow_received_stuck_warn_min",
    "tv_flow_action_pending_stuck_warn_min",
    "tv_flow_failed_lookback_min",
    "tv_flow_pending_lookback_min",
    "tv_flow_record_sample_limit",
    "eod_close_time",
)
FALSE_TEXT = {"0", "false", "no", "off", "disabled", "disable"}
BAR_PIPELINE_DISABLED_STATUSES = {"disabled", "disabled_tv_primary", "legacy_bar_pipeline_disabled"}
TV_PRIMARY_SIGNAL_SOURCES = {"tv", "tradingview", "webhook_tv", "tv_webhook"}
TV_FLOW_SOURCE_VALUES = {"tv", "tradingview", "tv_webhook", "webhook_tv", "tradingview_webhook"}
TV_FLOW_BROKER_MODES = {"live", "paper"}
TV_FLOW_HANDLED_SIGNAL_STATUSES = {
    "submitted",
    "protected_active",
    "protection_incomplete",
    "executed",
    "rejected",
    "expired",
    "closed",
    "blocked",
    "duplicate_existing_broker_order",
    "validation_rejected",
    "submit_failed",
}
TV_PRIMARY_SUPPRESSED_FLAG_CODES = {
    "no_active_targets",
    "no_execution_eligible_targets",
    "data_freshness_delayed",
    "data_freshness_offline",
    "stale_active_symbols",
}


def _monitor_builder_error(stage: str, exc: Any, *, severity: str = "warning") -> dict[str, str]:
    detail = str(exc or "").strip() or "unknown_error"
    return {
        "stage": str(stage or "unknown").strip() or "unknown",
        "severity": str(severity or "warning").strip().lower() or "warning",
        "detail": detail,
    }


def _monitor_builder_flag(error: dict[str, Any]) -> dict[str, str]:
    stage = str(error.get("stage") or "unknown").strip() or "unknown"
    severity = str(error.get("severity") or "warning").strip().lower() or "warning"
    detail = str(error.get("detail") or "unknown_error").strip() or "unknown_error"
    return {
        "severity": severity,
        "code": f"monitor_builder_{stage}",
        "title": f"Monitor aggregation degraded ({stage})",
        "detail": detail,
    }


def _fallback_scheduler_payload(environment: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "unknown",
        "environment": environment,
        "jobs": {},
        "ingest_cursor": {},
        "compute_dispatch_cursor": {},
    }


def _fallback_scheduler_summary(environment: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "unknown",
        "environment": environment,
        "loop_interval_seconds": 0.0,
        "job_count": 0,
        "job_status_counts": {},
        "jobs": {},
        "ingest_cursor": {},
        "compute_dispatch_cursor": {},
        "latest_ingested_bar_time_ms": 0,
        "latest_dispatched_bar_time_ms": 0,
        "last_dispatch_at_ms": 0,
        "dispatch_lag_ms": 0,
        "dispatch_lag_min": 0.0,
        "enabled_job_count": 0,
        "native_job_count": 0,
        "compatibility_job_count": 0,
    }


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _false_text(value: Any) -> bool:
    return _to_text(value).lower() in FALSE_TEXT


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = _to_text(value).lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in FALSE_TEXT:
        return False
    return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _bounded_config_int(
    config_map: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = _to_int(config_map.get(key), default)
    return max(int(minimum), min(int(maximum), value))


def _escape_filter_value(value: Any) -> str:
    return _to_text(value).replace("\\", "\\\\").replace('"', '\\"')


def _as_object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _timestamp_ms(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return 0
        return int(number if number >= 100_000_000_000 else number * 1000)
    text = _to_text(value)
    if not text:
        return 0
    try:
        number = float(text)
    except Exception:
        number = 0.0
    if number > 0:
        return int(number if number >= 100_000_000_000 else number * 1000)
    normalized = text.replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _record_created_ms(row: dict[str, Any]) -> int:
    return _timestamp_ms(row.get("created") or row.get("created_at") or row.get("bar_time_ms"))


def _record_updated_ms(row: dict[str, Any]) -> int:
    return _timestamp_ms(row.get("updated") or row.get("updated_at") or row.get("created") or row.get("bar_time_ms"))


def _age_ms(row: dict[str, Any], now_ms: int, *, updated: bool = False) -> int:
    stamp = _record_updated_ms(row) if updated else _record_created_ms(row)
    return max(0, int(now_ms - stamp)) if stamp > 0 else 0


def _format_age(age_ms: int) -> str:
    if age_ms <= 0:
        return "unknown age"
    seconds = int(age_ms / 1000)
    if seconds < 120:
        return f"{seconds}s"
    minutes = int(seconds / 60)
    if minutes < 120:
        return f"{minutes}m"
    hours = minutes / 60
    return f"{hours:.1f}h"


def _parse_hhmm(value: Any, default: str = TV_FLOW_EOD_CLOSE_TIME_DEFAULT) -> tuple[int, int]:
    text = _to_text(value) or default
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = max(0, min(23, int(hour_text)))
        minute = max(0, min(59, int(minute_text)))
        return hour, minute
    except Exception:
        return _parse_hhmm(default, "15:55") if default != "15:55" else (15, 55)


def _et_datetime_from_ms(value_ms: int) -> datetime:
    return datetime.fromtimestamp(max(0, int(value_ms or 0)) / 1000.0, ET)


def _record_et_date(row: dict[str, Any], *, updated: bool = False) -> str:
    stamp = _record_updated_ms(row) if updated else _record_created_ms(row)
    if stamp <= 0:
        return ""
    return _et_datetime_from_ms(stamp).strftime("%Y-%m-%d")


def _tv_action_monitor_window(config_values: dict[str, Any], observed_ms: int) -> dict[str, Any]:
    hour, minute = _parse_hhmm(config_values.get("eod_close_time"), TV_FLOW_EOD_CLOSE_TIME_DEFAULT)
    now_et = _et_datetime_from_ms(observed_ms)
    cutoff = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return {
        "active": now_et < cutoff,
        "market_date": now_et.strftime("%Y-%m-%d"),
        "cutoff_et": cutoff.strftime("%Y-%m-%d %H:%M:%S"),
        "eod_close_time": f"{hour:02d}:{minute:02d}",
        "now_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _filter_tv_action_window_rows(
    rows: list[dict[str, Any]],
    *,
    window: dict[str, Any],
    updated: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not bool(window.get("active")):
        return [], {"suppressed_after_eod_count": len(rows), "suppressed_cross_day_count": 0}
    market_date = _to_text(window.get("market_date"))
    scoped = [row for row in rows if not market_date or _record_et_date(row, updated=updated) == market_date]
    return scoped, {
        "suppressed_after_eod_count": 0,
        "suppressed_cross_day_count": max(0, len(rows) - len(scoped)),
    }


def _load_records(
    pb: Any,
    collection: str,
    *,
    filter_expr: str,
    sort: str = "-created",
    per_page: int = 200,
    max_pages: int = 1,
) -> list[dict[str, Any]]:
    getter = getattr(pb, "get_all_records", None)
    if callable(getter):
        rows = getter(collection, filter=filter_expr or None, sort=sort or None, max_pages=max(1, int(max_pages or 1))) or []
        return [dict(item) for item in rows if isinstance(item, dict)]

    get_records = getattr(pb, "get_records", None)
    if not callable(get_records):
        raise RuntimeError("pocketbase_get_records_unavailable")

    rows: list[dict[str, Any]] = []
    bounded_per_page = max(1, min(500, int(per_page or 200)))
    for page in range(1, max(1, int(max_pages or 1)) + 1):
        batch = get_records(
            collection,
            filter=filter_expr or None,
            sort=sort or None,
            per_page=bounded_per_page,
            page=page,
        ) or []
        rows.extend(dict(item) for item in batch if isinstance(item, dict))
        if len(batch) < bounded_per_page:
            break
    return rows


def _load_status_records(
    pb: Any,
    collection: str,
    *,
    environment: str,
    statuses: tuple[str, ...],
    sort: str = "-created",
    max_pages: int = 1,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    escaped_environment = _escape_filter_value(environment)
    for status in statuses:
        rows.extend(
            _load_records(
                pb,
                collection,
                filter_expr=f'status = "{_escape_filter_value(status)}" && environment = "{escaped_environment}"',
                sort=sort,
                max_pages=max_pages,
            )
        )
    return rows


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = _to_text(row.get("status")).lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _type_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        event_type = _to_text(row.get("event_type")).lower() or "unknown"
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _row_label(row: dict[str, Any]) -> str:
    symbol = _to_text(row.get("symbol")).upper()
    event_type = _to_text(row.get("event_type") or row.get("action_type") or row.get("signal")).lower()
    event_id = _to_text(row.get("event_id") or row.get("signal_id") or row.get("id"))
    parts = [part for part in (symbol, event_type, event_id) if part]
    return " ".join(parts) or "record"


def _row_sample(row: dict[str, Any], *, now_ms: int, record_type: str) -> dict[str, Any]:
    extra = _as_object(row.get("extra"))
    runtime_detail = _as_object(extra.get("reverse_runtime_detail"))
    blocked_detail = _as_object(extra.get("reentry_blocked"))
    return {
        "type": record_type,
        "id": _to_text(row.get("id")),
        "symbol": _to_text(row.get("symbol")).upper(),
        "status": _to_text(row.get("status")).lower(),
        "event_type": _to_text(row.get("event_type") or extra.get("event_type")).lower(),
        "event_id": _to_text(row.get("event_id") or extra.get("tv_event_id")),
        "signal_id": _to_text(row.get("signal_id") or extra.get("origin_signal_id")),
        "action_type": _to_text(row.get("action_type") or extra.get("action_type")).lower(),
        "source": _to_text(row.get("source") or extra.get("source") or extra.get("signal_source")).lower(),
        "created": _to_text(row.get("created")),
        "updated": _to_text(row.get("updated")),
        "age_ms": _age_ms(row, now_ms),
        "age": _format_age(_age_ms(row, now_ms)),
        "error": _to_text(
            row.get("error_msg")
            or extra.get("flow_error_code")
            or extra.get("error")
            or blocked_detail.get("reason")
            or runtime_detail.get("blocked_reason")
            or extra.get("reason")
        ),
    }


def _sample_rows(rows: list[dict[str, Any]], *, now_ms: int, record_type: str, limit: int) -> list[dict[str, Any]]:
    return [_row_sample(row, now_ms=now_ms, record_type=record_type) for row in rows[: max(0, int(limit or 0))]]


def _is_heartbeat_event(row: dict[str, Any]) -> bool:
    return _to_text(row.get("event_type")).lower() == "heartbeat"


def _is_within_lookback(row: dict[str, Any], now_ms: int, lookback_ms: int, *, updated: bool = False) -> bool:
    age = _age_ms(row, now_ms, updated=updated)
    return age <= 0 or age <= int(lookback_ms)


def _is_tv_action(row: dict[str, Any]) -> bool:
    extra = _as_object(row.get("extra"))
    values = {
        _to_text(row.get("source")).lower(),
        _to_text(row.get("signal_source")).lower(),
        _to_text(extra.get("source")).lower(),
        _to_text(extra.get("signal_source")).lower(),
        _to_text(extra.get("route_source")).lower(),
    }
    if values & TV_FLOW_SOURCE_VALUES:
        return True
    if _to_text(extra.get("tv_event_id")):
        return True
    return _to_text(extra.get("reverse_kind")).lower().startswith("tv_")


def _broker_mode_value(value: Any) -> str:
    text = _to_text(value).lower()
    if text in {"prod", "production"}:
        text = "live"
    elif text in {"sim", "simulated", "simulation"}:
        text = "paper"
    return text if text in TV_FLOW_BROKER_MODES else ""


def _signal_explicit_broker_mode(row: dict[str, Any], extra: dict[str, Any]) -> str:
    for value in (row.get("broker_mode"), extra.get("broker_mode")):
        broker_mode = _broker_mode_value(value)
        if broker_mode:
            return broker_mode
    return ""


def _signal_execution_payload(extra: dict[str, Any], broker_mode: str) -> dict[str, Any]:
    execution_by_mode = _as_object(extra.get("execution_by_mode"))
    return _as_object(execution_by_mode.get(broker_mode))


def _signal_execution_status(extra: dict[str, Any], broker_mode: str) -> str:
    return _to_text(_signal_execution_payload(extra, broker_mode).get("status")).lower()


def _signal_pending_filter_reason(row: dict[str, Any], runtime_environment: str) -> str:
    extra = _as_object(row.get("extra"))
    runtime_broker = _broker_mode_value(runtime_environment)
    if not runtime_broker:
        return ""

    current_status = _signal_execution_status(extra, runtime_broker)
    if current_status in TV_FLOW_HANDLED_SIGNAL_STATUSES:
        return "broker_scoped_handled"
    if current_status:
        return ""

    explicit_broker = _signal_explicit_broker_mode(row, extra)
    if explicit_broker and explicit_broker != runtime_broker:
        return "broker_scoped_other_broker"
    if explicit_broker == runtime_broker:
        return ""

    last_runtime_broker = _broker_mode_value(extra.get("last_runtime_broker_mode"))
    if last_runtime_broker and last_runtime_broker != runtime_broker:
        return "broker_scoped_other_broker"
    return ""


def _filter_pending_signal_rows_for_broker(
    rows: list[dict[str, Any]],
    *,
    runtime_environment: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    filtered: list[dict[str, Any]] = []
    stats = {
        "broker_scoped_ignored_count": 0,
        "broker_scoped_handled_count": 0,
    }
    for row in rows:
        reason = _signal_pending_filter_reason(row, runtime_environment)
        if reason == "broker_scoped_other_broker":
            stats["broker_scoped_ignored_count"] += 1
            continue
        if reason == "broker_scoped_handled":
            stats["broker_scoped_handled_count"] += 1
            continue
        filtered.append(row)
    return filtered, stats


def _is_failed_processed_tv_action(row: dict[str, Any]) -> bool:
    extra = _as_object(row.get("extra"))
    runtime_detail = _as_object(extra.get("reverse_runtime_detail"))
    blocked_detail = _as_object(extra.get("reentry_blocked"))
    status = _to_text(row.get("status")).lower()
    result_status = _to_text(row.get("result_status") or extra.get("result_status")).lower()
    reason = _to_text(row.get("reason") or extra.get("reason")).lower()
    failure_markers = (extra.get("flow_error_code"), extra.get("error"))
    if any(_to_text(marker) for marker in failure_markers):
        return True
    if extra.get("blocked") is True:
        return True
    if result_status in {"failed", "reentry_blocked", "pending_retry", "blocked"}:
        return True
    if status == "confirmed":
        return False
    if _to_text(blocked_detail.get("reason") or runtime_detail.get("blocked_reason")):
        return True
    if status in {"cancelled", "canceled", "expired"} and any(
        token in reason for token in ("blocked", "failed", "missing", "invalid", "unconfirmed", "被阻止")
    ):
        return True
    return False


def _old_rows(rows: list[dict[str, Any]], now_ms: int, threshold_ms: int) -> list[dict[str, Any]]:
    return sorted(
        [row for row in rows if _age_ms(row, now_ms) >= int(threshold_ms) > 0],
        key=lambda item: _age_ms(item, now_ms),
        reverse=True,
    )


def _oldest_age_ms(rows: list[dict[str, Any]], now_ms: int) -> int:
    ages = [_age_ms(row, now_ms) for row in rows]
    return max(ages) if ages else 0


def _latest_rows(rows: list[dict[str, Any]], *, updated: bool = False) -> list[dict[str, Any]]:
    stamp = _record_updated_ms if updated else _record_created_ms
    return sorted(rows, key=stamp, reverse=True)


def _tv_flow_flag(
    *,
    severity: str,
    code: str,
    title: str,
    detail: str,
    samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    flag: dict[str, Any] = {
        "severity": severity,
        "code": code,
        "title": title,
        "detail": detail,
    }
    if samples:
        flag["samples"] = samples
    return flag


def build_tv_flow_monitor_summary(
    pb: Any,
    *,
    data_environment: str,
    runtime_environment: str,
    config_map: dict[str, Any] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    config_values = config_map if isinstance(config_map, dict) else {}
    observed_ms = int(now_ms or time.time() * 1000)
    received_stuck_min = _bounded_config_int(
        config_values,
        "tv_flow_received_stuck_warn_min",
        TV_FLOW_RECEIVED_STUCK_DEFAULT_MIN,
        minimum=1,
        maximum=240,
    )
    action_stuck_min = _bounded_config_int(
        config_values,
        "tv_flow_action_pending_stuck_warn_min",
        TV_FLOW_ACTION_PENDING_STUCK_DEFAULT_MIN,
        minimum=1,
        maximum=24 * 60,
    )
    failed_lookback_min = _bounded_config_int(
        config_values,
        "tv_flow_failed_lookback_min",
        TV_FLOW_FAILED_LOOKBACK_DEFAULT_MIN,
        minimum=1,
        maximum=7 * 24 * 60,
    )
    pending_lookback_min = _bounded_config_int(
        config_values,
        "tv_flow_pending_lookback_min",
        TV_FLOW_PENDING_LOOKBACK_DEFAULT_MIN,
        minimum=1,
        maximum=7 * 24 * 60,
    )
    sample_limit = _bounded_config_int(
        config_values,
        "tv_flow_record_sample_limit",
        TV_FLOW_SAMPLE_LIMIT_DEFAULT,
        minimum=1,
        maximum=20,
    )
    received_stuck_ms = received_stuck_min * 60 * 1000
    action_stuck_ms = action_stuck_min * 60 * 1000
    failed_lookback_ms = failed_lookback_min * 60 * 1000
    pending_lookback_ms = pending_lookback_min * 60 * 1000
    action_window = _tv_action_monitor_window(config_values, observed_ms)

    event_environment = _escape_filter_value(data_environment)
    recent_event_rows = _load_records(
        pb,
        "tv_webhook_events",
        filter_expr=f'environment = "{event_environment}"',
        sort="-created",
        per_page=100,
        max_pages=1,
    )
    received_rows = _load_status_records(
        pb,
        "tv_webhook_events",
        environment=data_environment,
        statuses=("received",),
        max_pages=TV_FLOW_EVENT_FETCH_MAX_PAGES,
    )
    failed_rows = _load_status_records(
        pb,
        "tv_webhook_events",
        environment=data_environment,
        statuses=("failed",),
        max_pages=TV_FLOW_EVENT_FETCH_MAX_PAGES,
    )
    pending_signal_rows = _load_status_records(
        pb,
        "ibkr_signals",
        environment=data_environment,
        statuses=("pending", "awaiting_confirm"),
        max_pages=TV_FLOW_ACTION_FETCH_MAX_PAGES,
    )
    pending_reverse_rows = _load_status_records(
        pb,
        "ibkr_reverse_signals",
        environment=runtime_environment,
        statuses=("pending",),
        sort="-priority,-bar_time_ms",
        max_pages=TV_FLOW_ACTION_FETCH_MAX_PAGES,
    )
    processed_reverse_rows = _load_status_records(
        pb,
        "ibkr_reverse_signals",
        environment=runtime_environment,
        statuses=("cancelled", "expired", "confirmed"),
        sort="-updated",
        max_pages=TV_FLOW_ACTION_FETCH_MAX_PAGES,
    )

    recent_non_heartbeat = [row for row in recent_event_rows if not _is_heartbeat_event(row)]
    received_non_heartbeat = [row for row in received_rows if not _is_heartbeat_event(row)]
    received_stuck = _old_rows(received_non_heartbeat, observed_ms, received_stuck_ms)
    failed_recent = [
        row
        for row in failed_rows
        if not _is_heartbeat_event(row) and _is_within_lookback(row, observed_ms, failed_lookback_ms, updated=True)
    ]
    failed_recent = _latest_rows(failed_recent, updated=True)

    pending_signal_rows_raw = [
        row for row in pending_signal_rows if _is_within_lookback(row, observed_ms, pending_lookback_ms)
    ]
    pending_signal_rows, pending_signal_broker_stats = _filter_pending_signal_rows_for_broker(
        pending_signal_rows_raw,
        runtime_environment=runtime_environment,
    )
    pending_reverse_rows = [row for row in pending_reverse_rows if _is_within_lookback(row, observed_ms, pending_lookback_ms)]
    processed_reverse_rows = [
        row
        for row in processed_reverse_rows
        if _is_within_lookback(row, observed_ms, failed_lookback_ms, updated=True)
    ]
    tv_signal_pending_raw = [row for row in pending_signal_rows if _is_tv_action(row)]
    tv_reverse_pending_raw = [row for row in pending_reverse_rows if _is_tv_action(row)]
    non_tv_signal_pending = [row for row in pending_signal_rows if not _is_tv_action(row)]
    non_tv_reverse_pending = [row for row in pending_reverse_rows if not _is_tv_action(row)]
    tv_reverse_failed_raw = _latest_rows(
        [row for row in processed_reverse_rows if _is_tv_action(row) and _is_failed_processed_tv_action(row)],
        updated=True,
    )
    tv_pending_all_raw = _latest_rows(tv_signal_pending_raw + tv_reverse_pending_raw)
    tv_pending_all, tv_pending_window_stats = _filter_tv_action_window_rows(
        tv_pending_all_raw,
        window=action_window,
    )
    tv_reverse_failed, tv_failed_window_stats = _filter_tv_action_window_rows(
        tv_reverse_failed_raw,
        window=action_window,
        updated=True,
    )
    tv_signal_pending = [row for row in tv_signal_pending_raw if row in tv_pending_all]
    tv_reverse_pending = [row for row in tv_reverse_pending_raw if row in tv_pending_all]
    non_tv_pending_all = _latest_rows(non_tv_signal_pending + non_tv_reverse_pending)
    tv_pending_stuck = _old_rows(tv_pending_all, observed_ms, action_stuck_ms)

    flags: list[dict[str, Any]] = []
    if received_stuck:
        oldest = received_stuck[0]
        flags.append(
            _tv_flow_flag(
                severity="warning",
                code="tv_flow_received_stuck",
                title="TradingView events stuck before routing",
                detail=(
                    f"{len(received_stuck)} received TV event(s) older than {received_stuck_min}m; "
                    f"oldest {_row_label(oldest)} age {_format_age(_age_ms(oldest, observed_ms))}"
                ),
                samples=_sample_rows(received_stuck, now_ms=observed_ms, record_type="tv_event", limit=sample_limit),
            )
        )
    if failed_recent:
        latest = failed_recent[0]
        latest_error = _to_text(latest.get("error_msg")) or "route_failed"
        flags.append(
            _tv_flow_flag(
                severity="error",
                code="tv_flow_route_failed",
                title="TradingView route failed",
                detail=(
                    f"{len(failed_recent)} TV route failure(s) in the last {failed_lookback_min}m; "
                    f"latest {_row_label(latest)}: {latest_error}"
                ),
                samples=_sample_rows(failed_recent, now_ms=observed_ms, record_type="tv_event", limit=sample_limit),
            )
        )
    if tv_pending_stuck:
        oldest = tv_pending_stuck[0]
        stuck_signal_count = sum(1 for row in tv_pending_stuck if row in tv_signal_pending)
        stuck_reverse_count = len(tv_pending_stuck) - stuck_signal_count
        flags.append(
            _tv_flow_flag(
                severity="warning",
                code="tv_flow_tv_action_pending_stuck",
                title="TradingView execution actions pending too long",
                detail=(
                    f"{len(tv_pending_stuck)} TV action(s) pending older than {action_stuck_min}m "
                    f"(signals {stuck_signal_count}, reverse {stuck_reverse_count}); "
                    f"oldest {_row_label(oldest)} age {_format_age(_age_ms(oldest, observed_ms))}"
                ),
                samples=_sample_rows(tv_pending_stuck, now_ms=observed_ms, record_type="action", limit=sample_limit),
            )
        )
    if non_tv_pending_all:
        flags.append(
            _tv_flow_flag(
                severity="info",
                code="tv_flow_non_tv_pending_actions",
                title="Non-TV pending actions present",
                detail=(
                    f"{len(non_tv_pending_all)} non-TV pending action(s) are in scope "
                    f"(signals {len(non_tv_signal_pending)}, reverse {len(non_tv_reverse_pending)})"
                ),
                samples=_sample_rows(non_tv_pending_all, now_ms=observed_ms, record_type="action", limit=sample_limit),
            )
        )
    if tv_reverse_failed:
        latest = tv_reverse_failed[0]
        extra = _as_object(latest.get("extra"))
        runtime_detail = _as_object(extra.get("reverse_runtime_detail"))
        blocked_detail = _as_object(extra.get("reentry_blocked"))
        failure_reason = (
            _to_text(extra.get("flow_error_code"))
            or _to_text(blocked_detail.get("reason"))
            or _to_text(runtime_detail.get("blocked_reason"))
            or _to_text(latest.get("reason"))
            or "execution_action_failed"
        )
        flags.append(
            _tv_flow_flag(
                severity="error",
                code="tv_flow_execution_action_failed",
                title="TradingView execution action failed",
                detail=(
                    f"{len(tv_reverse_failed)} TV execution action failure(s) in the last {failed_lookback_min}m; "
                    f"latest {_row_label(latest)}: {failure_reason}"
                ),
                samples=_sample_rows(tv_reverse_failed, now_ms=observed_ms, record_type="action", limit=sample_limit),
            )
        )

    status = _status_from_flags(flags)
    return {
        "ok": status == "ok",
        "status": status,
        "environment": data_environment,
        "broker_mode": runtime_environment,
        "observed_at_ms": observed_ms,
        "mode": "event_driven",
        "heartbeat_required": False,
        "action_monitor": {
            "active": bool(action_window.get("active")),
            "market_date": action_window.get("market_date"),
            "now_et": action_window.get("now_et"),
            "cutoff_et": action_window.get("cutoff_et"),
            "eod_close_time": action_window.get("eod_close_time"),
        },
        "thresholds": {
            "received_stuck_warn_min": received_stuck_min,
            "action_pending_stuck_warn_min": action_stuck_min,
            "route_failed_lookback_min": failed_lookback_min,
            "pending_action_lookback_min": pending_lookback_min,
            "sample_limit": sample_limit,
        },
        "events": {
            "recent_count": len(recent_non_heartbeat),
            "heartbeat_ignored_count": len(recent_event_rows) - len(recent_non_heartbeat),
            "status_counts": _status_counts(recent_non_heartbeat),
            "event_type_counts": _type_counts(recent_non_heartbeat),
            "latest": _row_sample(recent_non_heartbeat[0], now_ms=observed_ms, record_type="tv_event") if recent_non_heartbeat else {},
            "received_count": len(received_non_heartbeat),
            "received_stuck_count": len(received_stuck),
            "received_oldest_age_ms": _oldest_age_ms(received_non_heartbeat, observed_ms),
            "received_stuck": _sample_rows(received_stuck, now_ms=observed_ms, record_type="tv_event", limit=sample_limit),
            "route_failed_count": len(failed_recent),
            "route_failed": _sample_rows(failed_recent, now_ms=observed_ms, record_type="tv_event", limit=sample_limit),
        },
        "actions": {
            "tv_pending_count": len(tv_pending_all),
            "tv_pending_raw_count": len(tv_pending_all_raw),
            "tv_pending_suppressed_after_eod_count": tv_pending_window_stats["suppressed_after_eod_count"],
            "tv_pending_suppressed_cross_day_count": tv_pending_window_stats["suppressed_cross_day_count"],
            "tv_pending_stuck_count": len(tv_pending_stuck),
            "tv_pending_oldest_age_ms": _oldest_age_ms(tv_pending_all, observed_ms),
            "tv_pending": _sample_rows(tv_pending_all, now_ms=observed_ms, record_type="action", limit=sample_limit),
            "tv_pending_stuck": _sample_rows(tv_pending_stuck, now_ms=observed_ms, record_type="action", limit=sample_limit),
            "tv_failed_count": len(tv_reverse_failed),
            "tv_failed_raw_count": len(tv_reverse_failed_raw),
            "tv_failed_suppressed_after_eod_count": tv_failed_window_stats["suppressed_after_eod_count"],
            "tv_failed_suppressed_cross_day_count": tv_failed_window_stats["suppressed_cross_day_count"],
            "tv_failed": _sample_rows(tv_reverse_failed, now_ms=observed_ms, record_type="action", limit=sample_limit),
            "non_tv_pending_count": len(non_tv_pending_all),
            "non_tv_pending": _sample_rows(non_tv_pending_all, now_ms=observed_ms, record_type="action", limit=sample_limit),
            "signals": {
                "pending_count": len(pending_signal_rows),
                "raw_pending_count": len(pending_signal_rows_raw),
                **pending_signal_broker_stats,
                "tv_pending_count": len(tv_signal_pending),
                "tv_pending_raw_count": len(tv_signal_pending_raw),
                "non_tv_pending_count": len(non_tv_signal_pending),
            },
            "reverse": {
                "pending_count": len(pending_reverse_rows),
                "tv_pending_count": len(tv_reverse_pending),
                "tv_pending_raw_count": len(tv_reverse_pending_raw),
                "non_tv_pending_count": len(non_tv_reverse_pending),
                "processed_recent_count": len(processed_reverse_rows),
                "tv_failed_count": len(tv_reverse_failed),
                "tv_failed_raw_count": len(tv_reverse_failed_raw),
            },
        },
        "flags": flags,
    }


def _bar_pipeline_candidate_disabled(candidate: dict[str, Any]) -> bool:
    status = _to_text(candidate.get("status") or candidate.get("bar_pipeline_status")).lower()
    reason = _to_text(candidate.get("reason") or candidate.get("bar_pipeline_reason")).lower()
    if status in BAR_PIPELINE_DISABLED_STATUSES or reason == "legacy_bar_pipeline_disabled":
        return True
    if candidate.get("enabled") is False or candidate.get("legacy_bar_pipeline_enabled") is False:
        return True
    if _false_text(candidate.get("enabled")) or _false_text(candidate.get("legacy_bar_pipeline_enabled")):
        return True
    return False


def _bar_pipeline_disabled(payload: dict[str, Any]) -> bool:
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    compute = payload.get("compute") if isinstance(payload.get("compute"), dict) else {}
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    candidates = [
        payload.get("bar_pipeline"),
        runtime.get("bar_pipeline"),
        (runtime.get("data_backfill") or {}).get("bar_pipeline") if isinstance(runtime.get("data_backfill"), dict) else None,
        runtime.get("data_backfill"),
        (runtime.get("data_writer") or {}).get("bar_pipeline") if isinstance(runtime.get("data_writer"), dict) else None,
        runtime.get("data_writer"),
        (compute.get("data_backfill") or {}).get("bar_pipeline") if isinstance(compute.get("data_backfill"), dict) else None,
        compute.get("data_backfill"),
    ]
    if any(_bar_pipeline_candidate_disabled(candidate) for candidate in candidates if isinstance(candidate, dict)):
        return True
    if _false_text(config.get("ibkr_legacy_bar_pipeline_enabled")):
        return True
    signal_source = _to_text(config.get("ibkr_signal_source")).lower()
    if signal_source in TV_PRIMARY_SIGNAL_SOURCES and _as_bool(config.get("ibkr_tv_primary_runtime_slim_enabled"), True):
        return True
    runtime_mode = _to_text(runtime.get("runtime_mode") or payload.get("runtime_mode")).lower()
    return "tv_primary" in runtime_mode


def _bar_pipeline_disabled_payload(source: dict[str, Any] | None = None) -> dict[str, Any]:
    reason = _to_text((source or {}).get("reason") or (source or {}).get("bar_pipeline_reason")) or "legacy_bar_pipeline_disabled"
    return {"enabled": False, "status": "disabled_tv_primary", "reason": reason}


def _apply_tv_primary_bar_pipeline_view(payload: dict[str, Any]) -> dict[str, Any]:
    if not _bar_pipeline_disabled(payload):
        return payload
    payload["bar_pipeline"] = _bar_pipeline_disabled_payload(payload.get("bar_pipeline") if isinstance(payload.get("bar_pipeline"), dict) else {})
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    if runtime:
        runtime["bar_pipeline"] = _bar_pipeline_disabled_payload(runtime.get("bar_pipeline") if isinstance(runtime.get("bar_pipeline"), dict) else {})
        canonical = runtime.get("canonical_5m") if isinstance(runtime.get("canonical_5m"), dict) else {}
        runtime["canonical_5m"] = {
            **canonical,
            "enabled": False,
            "status": "disabled_tv_primary",
            "phase": "disabled_tv_primary",
            "running": False,
            "pending_symbols": [],
            "pending_symbols_total": 0,
            "disabled_reason": "legacy_bar_pipeline_disabled",
        }
        data_backfill = runtime.get("data_backfill") if isinstance(runtime.get("data_backfill"), dict) else {}
        runtime["data_backfill"] = {
            **data_backfill,
            "enabled": False,
            "status": "disabled_tv_primary",
            "reason": "legacy_bar_pipeline_disabled",
            "active_requests": 0,
            "active_symbols_total": 0,
            "legacy_bar_pipeline_enabled": False,
            "bar_pipeline": _bar_pipeline_disabled_payload(
                data_backfill.get("bar_pipeline") if isinstance(data_backfill.get("bar_pipeline"), dict) else {}
            ),
        }
        market_universe = runtime.get("market_universe") if isinstance(runtime.get("market_universe"), dict) else {}
        if market_universe:
            market_universe["bar_pipeline_status"] = "disabled_tv_primary"
            if int(market_universe.get("active_target_count") or 0) > 0:
                market_universe["no_active_targets"] = False
            if int(market_universe.get("execution_eligible_target_count") or 0) > 0:
                market_universe["no_execution_eligible_targets"] = False
            runtime["market_universe"] = market_universe
        payload["runtime"] = runtime
    return payload


def _status_from_flags(flags: list[dict[str, Any]]) -> str:
    severities = {_to_text((item or {}).get("severity")).lower() for item in flags if isinstance(item, dict)}
    if "error" in severities:
        return "error"
    if "warning" in severities:
        return "warning"
    return "ok"


def _should_suppress_tv_primary_flag(payload: dict[str, Any], code: str) -> bool:
    normalized_code = _to_text(code).split(":", 1)[0]
    if normalized_code not in TV_PRIMARY_SUPPRESSED_FLAG_CODES:
        return False
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    market_universe = runtime.get("market_universe") if isinstance(runtime.get("market_universe"), dict) else {}
    if normalized_code == "no_active_targets":
        return int(market_universe.get("active_target_count") or 0) > 0
    if normalized_code == "no_execution_eligible_targets":
        return int(market_universe.get("execution_eligible_target_count") or 0) > 0
    return True


def _filter_tv_primary_legacy_flags(payload: dict[str, Any]) -> dict[str, Any]:
    if not _bar_pipeline_disabled(payload):
        return payload
    existing_flags = payload.get("flags") if isinstance(payload.get("flags"), list) else []
    filtered_flags = [
        dict(flag)
        for flag in existing_flags
        if isinstance(flag, dict)
        and not _should_suppress_tv_primary_flag(payload, _to_text(flag.get("code")))
    ]
    payload["flags"] = filtered_flags
    status = _status_from_flags(filtered_flags)
    payload["status"] = status
    if status == "ok":
        payload["ok"] = True
    return payload


def _call_scheduler_status_lite(scheduler_status: SchedulerStatus, environment: str) -> dict[str, Any]:
    try:
        return scheduler_status(environment, lite=True)
    except TypeError as exc:
        if "lite" not in str(exc):
            raise
        return scheduler_status(environment)


def _fallback_service_monitor(environment: str, topology: dict[str, Any]) -> dict[str, Any]:
    services = topology.get("services") if isinstance(topology.get("services"), dict) else {}
    service_map: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for name, raw_item in services.items():
        item = dict(raw_item) if isinstance(raw_item, dict) else {}
        status = str(item.get("status") or "unknown").strip().lower() or "unknown"
        item["status"] = status
        service_map[str(name)] = item
        counts[status] = counts.get(status, 0) + 1
    return {
        "environment": environment,
        "services": service_map,
        "status_counts": counts,
    }


def _merge_monitor_builder_flags(existing_flags: Any, errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in existing_flags if isinstance(existing_flags, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code and code in seen:
            continue
        if code:
            seen.add(code)
        merged.append(dict(item))
    for error in errors:
        flag = _monitor_builder_flag(error)
        code = str(flag.get("code") or "").strip()
        if code in seen:
            continue
        seen.add(code)
        merged.append(flag)
    return merged


def _append_effective_gate_flag(existing_flags: Any, gate: dict[str, Any]) -> list[dict[str, Any]]:
    merged = [dict(item) for item in existing_flags if isinstance(item, dict)] if isinstance(existing_flags, list) else []
    codes = {str(item.get("code") or "").strip() for item in merged if isinstance(item, dict)}
    raw_signal = gate.get("raw_signal_gate") if isinstance(gate.get("raw_signal_gate"), dict) else {}
    raw_snapshot = gate.get("raw_startup_snapshot") if isinstance(gate.get("raw_startup_snapshot"), dict) else {}
    if bool(gate.get("open")) and (raw_signal.get("open") is False or raw_snapshot.get("open") is False):
        code = "trading_gate_snapshot_stale"
        if code not in codes:
            merged.append(
                {
                    "severity": "warning",
                    "code": code,
                    "title": "Trading gate display uses live readiness",
                    "detail": (
                        "Effective gate is open from current readiness; stale signal/startup snapshot "
                        "is diagnostic only and does not block trading."
                    ),
                }
            )
    if not bool(gate.get("open")) and not gate.get("reason"):
        code = "trading_gate_reason_missing"
        if code not in codes:
            merged.append(
                {
                    "severity": "error",
                    "code": code,
                    "title": "Trading gate closed without reason",
                    "detail": "Effective gate is closed but no reason was provided.",
                }
            )
    return merged


def _merge_unique_flags(existing_flags: Any, extra_flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = [dict(item) for item in existing_flags if isinstance(item, dict)] if isinstance(existing_flags, list) else []
    seen = {str(item.get("code") or "").strip() for item in merged if str(item.get("code") or "").strip()}
    for item in extra_flags:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code and code in seen:
            continue
        if code:
            seen.add(code)
        merged.append(dict(item))
    return merged


def _set_status_from_flags(payload: dict[str, Any], flags: list[dict[str, Any]]) -> None:
    severities = {str((item or {}).get("severity") or "").strip().lower() for item in flags if isinstance(item, dict)}
    current_status = str(payload.get("status") or "ok").strip().lower() or "ok"
    if "error" in severities and current_status not in {"offline", "error"}:
        payload["status"] = "error"
        payload["ok"] = False
    elif "warning" in severities and current_status == "ok":
        payload["status"] = "warning"
        payload["ok"] = False


def _elapsed_from_account_snapshot_probe(probe: dict[str, Any], snapshot: dict[str, Any]) -> float:
    elapsed = probe.get("elapsed_ms")
    if elapsed not in (None, ""):
        try:
            return float(elapsed)
        except Exception:
            return 0.0
    diagnostics = snapshot.get("diagnostics") if isinstance(snapshot.get("diagnostics"), dict) else {}
    account = diagnostics.get("account_snapshot") if isinstance(diagnostics.get("account_snapshot"), dict) else {}
    try:
        return float(account.get("total_elapsed_ms") or 0.0)
    except Exception:
        return 0.0


def _account_snapshot_flags(probe: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(probe, dict) or probe.get("skipped"):
        return []
    snapshot = probe.get("payload") if isinstance(probe.get("payload"), dict) else {}
    flags: list[dict[str, Any]] = []
    status_code = int(probe.get("status_code") or 0)
    ok = bool(probe.get("ok")) and status_code < 400 and snapshot.get("ok") is not False
    service_running = snapshot.get("service_running")
    gateway_running = snapshot.get("gateway_running")
    session_authenticated = snapshot.get("session_authenticated")
    if service_running is False or gateway_running is False or session_authenticated is False:
        flags.append(
            {
                "severity": "error",
                "code": "account_runtime_unavailable",
                "title": "Account runtime unavailable",
                "detail": (
                    f"service_running={service_running} gateway_running={gateway_running} "
                    f"session_authenticated={session_authenticated}"
                ),
            }
        )
    if not ok:
        detail = str(probe.get("error") or snapshot.get("error") or snapshot.get("message") or f"status={status_code}")
        flags.append(
            {
                "severity": "warning",
                "code": "account_snapshot_degraded",
                "title": "Account snapshot unavailable",
                "detail": detail,
            }
        )
    elapsed_ms = _elapsed_from_account_snapshot_probe(probe, snapshot)
    if elapsed_ms >= ACCOUNT_SNAPSHOT_WARN_MS:
        flags.append(
            {
                "severity": "warning",
                "code": "account_snapshot_timeout",
                "title": "Account snapshot slow",
                "detail": f"account snapshot took {elapsed_ms:.0f}ms",
            }
        )
    errors = snapshot.get("errors") if isinstance(snapshot.get("errors"), dict) else {}
    blocking_errors = [
        f"{name}: {errors.get(name)}"
        for name in ("summary", "positions", "orders")
        if str(errors.get(name) or "").strip()
    ]
    if blocking_errors:
        flags.append(
            {
                "severity": "warning",
                "code": "account_snapshot_degraded",
                "title": "Account snapshot degraded",
                "detail": " | ".join(blocking_errors[:3]),
            }
        )
    pnl_error = str(errors.get("pnl") or "").strip()
    if pnl_error:
        flags.append(
            {
                "severity": "warning",
                "code": "account_pnl_unavailable",
                "title": "Account PnL unavailable",
                "detail": pnl_error,
            }
        )
    return flags


def _history_backfill_detail(compute: dict[str, Any]) -> str:
    data_backfill = compute.get("data_backfill") if isinstance(compute.get("data_backfill"), dict) else {}
    if not data_backfill:
        return ""
    if _bar_pipeline_candidate_disabled(data_backfill):
        return ""
    parts: list[str] = []
    active_requests = int(data_backfill.get("active_requests") or 0)
    active_symbols_total = int(data_backfill.get("active_symbols_total") or 0)
    if active_requests or active_symbols_total:
        parts.append(f"history active {active_requests}/{active_symbols_total}")
    last_trace = data_backfill.get("last_trace") if isinstance(data_backfill.get("last_trace"), dict) else {}
    if last_trace:
        source = str(last_trace.get("source") or "history").strip()
        duration_s = float(last_trace.get("duration_s") or 0)
        requests = int(last_trace.get("request_count") or 0)
        retry = int(last_trace.get("retry_count") or 0)
        throttle = int(last_trace.get("throttle_count") or 0)
        if duration_s >= 60 or requests >= 100 or retry or throttle:
            parts.append(f"history last {source} {duration_s:.0f}s req {requests} retry {retry} throttle {throttle}")
    return " | ".join(parts)


def _call_console_probe(probe_console_status: ProbeConsoleStatus, console_base_url: str) -> dict[str, Any]:
    try:
        signature = inspect.signature(probe_console_status)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        positional_params = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        variadic = any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        if not positional_params and not variadic:
            return probe_console_status()
    return probe_console_status(console_base_url)


def probe_console_status(console_base_url: str, *, requests_get: RequestsGet = requests.get) -> dict[str, Any]:
    normalized_base_url = str(console_base_url or "").rstrip("/")
    if not normalized_base_url:
        return {
            "ok": False,
            "status_code": 0,
            "target_url": "",
            "error": "console_base_url_missing",
        }
    target_url = f"{normalized_base_url}/index.html"
    try:
        response = requests_get(target_url, timeout=5)
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status_code": 0,
            "target_url": target_url,
            "error": str(exc),
        }
    return {
        "ok": bool(response.ok),
        "status_code": int(response.status_code),
        "target_url": target_url,
        "error": "",
    }



def derive_monitor_service_map(
    environment: str,
    base_payload: dict[str, Any],
    scheduler_summary: dict[str, Any],
    *,
    console_probe: dict[str, Any],
    pb_health: dict[str, Any],
    backtest_health: dict[str, Any] | None = None,
    build_service_topology: BuildServiceTopology,
) -> dict[str, Any]:
    topology = base_payload.get("service_topology") if isinstance(base_payload.get("service_topology"), dict) else build_service_topology()
    services = topology.get("services") if isinstance(topology.get("services"), dict) else {}
    runtime = base_payload.get("runtime") if isinstance(base_payload.get("runtime"), dict) else {}
    gateway = runtime.get("gateway") if isinstance(runtime.get("gateway"), dict) else {}
    compute = base_payload.get("compute") if isinstance(base_payload.get("compute"), dict) else {}
    monitor_status = str(base_payload.get("status") or "").strip().lower()
    monitor_source_unavailable = bool(base_payload.get("monitor_source_unavailable"))

    def _normalize_service_status(raw_status: Any, *, fallback_running: bool) -> str:
        text = str(raw_status or "").strip().lower()
        if text in {"ok", "running", "healthy", "ready"}:
            return "running"
        if text in {"warning", "warn", "degraded", "partial"}:
            return "degraded"
        if text == "error":
            return "degraded" if fallback_running else "offline"
        if text in {"offline", "down", "stopped"}:
            return "offline"
        return "running" if fallback_running else "offline"

    def _topology_meta(name: str) -> dict[str, Any]:
        item = services.get(name) if isinstance(services.get(name), dict) else {}
        return dict(item)

    def _detail_parts(*parts: Any) -> str:
        normalized = [str(part).strip() for part in parts if str(part or "").strip()]
        return " · ".join(normalized)

    def _compute_startup_preload_snapshot() -> dict[str, Any]:
        preload = compute.get("compute_startup_preload") if isinstance(compute.get("compute_startup_preload"), dict) else {}
        if preload:
            return dict(preload)
        root_preload = base_payload.get("compute_startup_preload")
        if isinstance(root_preload, dict):
            return dict(root_preload)
        return {}

    def _compute_startup_preload_active() -> bool:
        preload = _compute_startup_preload_snapshot()
        status = str(preload.get("status") or "").strip().lower()
        return bool(preload.get("running")) or status in {"running", "scheduled"}

    def _scheduler_compute_preload_deferred() -> bool:
        jobs = scheduler_summary.get("jobs") if isinstance(scheduler_summary.get("jobs"), dict) else {}
        compute_job = jobs.get("ibkr_compute_runtime") if isinstance(jobs.get("ibkr_compute_runtime"), dict) else {}
        last_result = compute_job.get("last_result") if isinstance(compute_job.get("last_result"), dict) else {}
        result_payloads = [last_result]
        nested_payload = last_result.get("payload") if isinstance(last_result.get("payload"), dict) else {}
        if nested_payload:
            result_payloads.append(nested_payload)

        for payload in result_payloads:
            reason = str(payload.get("reason") or "").strip().lower()
            if reason == "compute_startup_preload_running":
                return True
            preload = payload.get("compute_startup_preload") if isinstance(payload.get("compute_startup_preload"), dict) else {}
            status = str(preload.get("status") or "").strip().lower()
            if bool(preload.get("running")) or status in {"running", "scheduled"}:
                return True
        return False

    def _scheduler_close_compute_deferred() -> bool:
        reason = str(scheduler_summary.get("dispatch_lag_reason") or "").strip().lower()
        in_progress = (
            bool(scheduler_summary.get("compute_in_progress"))
            or bool(scheduler_summary.get("official_5m_close_in_progress"))
            or reason in {"close_compute_inflight", "official_5m_close_inflight"}
        )
        stalled = bool(
            scheduler_summary.get("compute_in_progress_stalled")
            or scheduler_summary.get("inflight_stalled")
            or scheduler_summary.get("official_5m_close_stalled")
        )
        return bool(in_progress and not stalled)

    observed_at = utc_timestamp()
    backtest_probe = backtest_health if isinstance(backtest_health, dict) else {}
    if not backtest_probe:
        backtest_probe = base_payload.get("backtest_service") if isinstance(base_payload.get("backtest_service"), dict) else {}
    if not backtest_probe:
        backtest_probe = base_payload.get("backtest") if isinstance(base_payload.get("backtest"), dict) else {}
    backtest_meta = _topology_meta("ibkr-backtest")
    backtest_state = (
        derive_backtest_state(backtest_probe, observed_at=observed_at)
        if backtest_probe
        else {
            "service_name": "ibkr-backtest",
            "status": str(backtest_meta.get("status") or "unknown").strip().lower() or "unknown",
            "ready": False,
            "readiness_phase": "unknown",
            "status_source": "topology",
            "last_observed_at": observed_at,
            "stale": False,
            "detail": str(backtest_meta.get("responsibility") or "backtest health not probed").strip(),
        }
    )
    console_meta = _topology_meta("ibkr-console")
    console_running = bool(console_probe.get("ok"))
    pb_meta = _topology_meta("pocketbase")
    pb_disk = ((base_payload.get("pocketbase") or {}).get("disk") or {}) if isinstance(base_payload.get("pocketbase"), dict) else {}
    pb_flags = [item for item in (base_payload.get("flags") or []) if str((item or {}).get("code") or "").startswith("pb_")]
    pb_status = "running" if pb_health.get("ok") else "offline"
    if pb_status == "running" and pb_flags:
        pb_status = "degraded"
    elif pb_status != "running" and pb_disk.get("status") == "partial":
        pb_status = "degraded"

    compute_status = _normalize_service_status(compute.get("status"), fallback_running=bool(compute))
    history_detail = _history_backfill_detail(compute)
    ready_engines = int(compute.get("ready_engines") or 0)
    total_engines = int(compute.get("total_engines") or 0)
    if total_engines > 0 and ready_engines < total_engines and compute_status == "running":
        compute_status = "degraded"
    if monitor_source_unavailable and not compute:
        compute_status = "unknown"
    if not compute and monitor_status in {"warning", "warn", "degraded"}:
        compute_status = "unknown" if monitor_source_unavailable else "degraded"
    if not compute and monitor_status in {"offline", "error"}:
        compute_status = "unknown" if monitor_source_unavailable else "offline"

    runtime_status = _normalize_service_status(runtime.get("status"), fallback_running=bool(runtime))
    runtime_phase = str(runtime.get("runtime_phase") or "").strip().lower()
    session = runtime.get("session") if isinstance(runtime.get("session"), dict) else {}
    websocket = runtime.get("websocket") if isinstance(runtime.get("websocket"), dict) else {}
    gateway_reachable = bool(gateway.get("running") or gateway.get("reachable"))
    websocket_ready = bool(websocket.get("connected") or websocket.get("ready"))
    session_authenticated = bool(session.get("authenticated"))
    if runtime:
        if runtime_phase in {"stopped", "stop_requested", "stopping"}:
            runtime_status = "degraded" if gateway_reachable or session_authenticated or websocket_ready else "offline"
        elif not gateway_reachable or not session_authenticated or not websocket_ready:
            runtime_status = "degraded"
    elif monitor_source_unavailable:
        runtime_status = "unknown"
    elif compute_status != "running":
        runtime_status = "offline"

    gateway_status = (
        "running"
        if bool(gateway.get("running") or gateway.get("reachable"))
        else ("unknown" if monitor_source_unavailable and not gateway else "offline")
    )
    scheduler_status = str(scheduler_summary.get("status") or "").strip().lower() or "unknown"
    scheduler_unavailable = scheduler_status == "unknown" and not bool(scheduler_summary.get("ok", True))
    compute_preload_active = _compute_startup_preload_active() or _scheduler_compute_preload_deferred()
    close_compute_deferred = _scheduler_close_compute_deferred()
    bar_pipeline_disabled = _bar_pipeline_disabled(base_payload)
    scheduler_lag_compute_relevant = bool(scheduler_summary.get("dispatch_lag_compute_relevant", True))
    if (
        scheduler_status == "running"
        and not bar_pipeline_disabled
        and scheduler_lag_compute_relevant
        and float(scheduler_summary.get("dispatch_lag_min") or 0) > 10
        and not compute_preload_active
        and not close_compute_deferred
    ):
        scheduler_status = "degraded"

    service_map = {
        "ibkr-console": {
            **console_meta,
            "status": "running" if console_running else "offline",
            "detail": _detail_parts(
                "static console",
                console_probe.get("target_url"),
                f"http {console_probe.get('status_code')}" if console_probe.get("status_code") else console_probe.get("error"),
            ),
        },
        "ibkr-api": {
            **_topology_meta("ibkr-api"),
            "status": "running",
            "detail": _detail_parts(
                "compat routes active",
                f"env {environment}",
                f"scheduler jobs {int(scheduler_summary.get('job_count') or 0)}",
            ),
        },
        "ibkr-scheduler": {
            **_topology_meta("ibkr-scheduler"),
            "status": scheduler_status,
            "detail": _detail_parts(
                "status unavailable" if scheduler_unavailable else "",
                f"loop {int(float(scheduler_summary.get('loop_interval_seconds') or 0))}s" if scheduler_summary.get("loop_interval_seconds") else "",
                "bar pipeline disabled_tv_primary" if bar_pipeline_disabled else "",
                (
                    f"lag {float(scheduler_summary.get('dispatch_lag_min') or 0):.2f}m"
                    if scheduler_summary.get("latest_ingested_bar_time_ms") and not bar_pipeline_disabled
                    else ("" if bar_pipeline_disabled else "awaiting bars")
                ),
                "non-compute ingest" if scheduler_summary.get("dispatch_lag_reason") == "non_compute_ingest_source" else "",
                "deferred by compute preload" if compute_preload_active else "",
                (
                    f"official close in progress {float(scheduler_summary.get('official_5m_close_age_s') or scheduler_summary.get('inflight_age_s') or 0):.1f}s"
                    if close_compute_deferred and scheduler_summary.get("dispatch_lag_reason") == "official_5m_close_inflight"
                    else (
                        f"close compute in progress {float(scheduler_summary.get('inflight_age_s') or 0):.1f}s"
                        if close_compute_deferred
                        else ""
                    )
                ),
                (
                    f"missing indicators {int(scheduler_summary.get('missing_indicator_symbol_count') or 0)}"
                    if close_compute_deferred and scheduler_summary.get("missing_indicator_symbol_count")
                    else ""
                ),
                (
                    f"busy deferred {int(scheduler_summary.get('deferred_busy_symbol_count') or 0)}"
                    if scheduler_summary.get("deferred_compute_busy")
                    else ""
                ),
                f"jobs {int(scheduler_summary.get('job_count') or 0)}",
            ),
        },
        "ibkr-compute": {
            **_topology_meta("ibkr-compute"),
            "status": compute_status,
            "detail": _detail_parts(
                "monitor source unavailable" if monitor_source_unavailable and not compute else "",
                f"engines {int(compute.get('ready_engines') or 0)}/{int(compute.get('total_engines') or 0)}",
                f"compute {int(compute.get('compute_count') or 0)}",
                f"tracked {int(compute.get('tracked_cursors') or 0)}",
                history_detail,
            ),
        },
        "ibkr-backtest": {
            **backtest_meta,
            **backtest_state,
        },
        "ibkr-runtime": {
            **_topology_meta("ibkr-runtime"),
            "status": runtime_status,
            "detail": _detail_parts(
                "monitor source unavailable" if monitor_source_unavailable and not runtime else "",
                f"phase {runtime.get('runtime_phase') or '--'}" if runtime else "",
                f"session {'AUTHED' if ((runtime.get('session') or {}).get('authenticated')) else 'PENDING'}" if runtime else "",
                f"ws {'READY' if ((runtime.get('websocket') or {}).get('connected')) else 'PENDING'}" if runtime else "",
            ),
        },
        "ibkr-gateway": {
            **_topology_meta("ibkr-gateway"),
            "status": gateway_status,
            "detail": _detail_parts(
                "monitor source unavailable" if monitor_source_unavailable and not gateway else "",
                f"managed_by {gateway.get('managed_by') or '--'}" if gateway else "",
                f"pid {int(gateway.get('pid') or 0)}" if gateway.get("pid") else "",
                ("reachable" if gateway.get("reachable") else "not reachable") if gateway else "",
            ),
        },
        "pocketbase": {
            **pb_meta,
            "status": pb_status,
            "detail": _detail_parts(
                f"pb_data {pb_disk.get('data_path') or '--'}",
                f"size {pb_disk.get('status') or 'unknown'}",
                f"http {pb_health.get('status_code')}" if pb_health.get("status_code") else pb_health.get("error"),
            ),
        },
    }

    if compute:
        compute_state_payload = dict(compute)
        compute_preload = _compute_startup_preload_snapshot()
        if compute_preload and not isinstance(compute_state_payload.get("compute_startup_preload"), dict):
            compute_state_payload["compute_startup_preload"] = compute_preload
        service_map["ibkr-compute"] = {
            **service_map.get("ibkr-compute", {}),
            **derive_compute_state(compute_state_payload, observed_at=observed_at),
        }
        if history_detail:
            service_map["ibkr-compute"]["detail"] = _detail_parts(
                service_map["ibkr-compute"].get("detail"),
                history_detail,
            )
    if runtime:
        service_map["ibkr-runtime"] = {
            **service_map.get("ibkr-runtime", {}),
            **derive_runtime_state(runtime, observed_at=observed_at),
        }
        service_map["ibkr-gateway"] = {
            **service_map.get("ibkr-gateway", {}),
            **derive_gateway_state(runtime, observed_at=observed_at),
        }

    return rebuild_service_monitor(environment, service_map)



def build_system_monitor_payload(
    environment: str,
    *,
    normalize_environment: NormalizeEnvironment,
    fetch_compute_monitor: FetchPayload,
    as_dict: AsDict,
    config_refresh: ConfigRefresh,
    scheduler_status: SchedulerStatus,
    build_cron_payload: BuildCronPayload,
    config: Any,
    build_scheduler_summary: BuildSchedulerSummary,
    augment_scheduler_summary: AugmentSchedulerSummary,
    request_json: RequestJson,
    pb_base_url: str,
    console_base_url: str,
    backtest_base_url: str = "",
    probe_console_status: ProbeConsoleStatus,
    load_effective_config_map: LoadEffectiveConfigMap,
    monitor_config_keys: tuple[str, ...],
    load_recent_system_events: LoadRecentSystemEvents,
    enrich_monitor_payload_with_pocketbase_disk: EnrichMonitorPayload,
    derive_monitor_service_map: DeriveMonitorServiceMap,
    merge_service_topology: MergeServiceTopology,
    build_service_topology: BuildServiceTopology,
    account_snapshot_probe: AccountSnapshotProbe | None = None,
    service_profile: str = "api",
    pocketbase_client: Any | None = None,
    pb_client: Any | None = None,
) -> dict[str, Any]:
    runtime_environment = normalize_broker_mode(environment, configured_broker_mode())
    data_environment = resolve_data_environment(runtime_environment)
    builder_errors: list[dict[str, str]] = []
    base_monitor_result = fetch_compute_monitor(data_environment)
    base_payload = as_dict(base_monitor_result.get("payload"))
    base_monitor_ok = bool(base_monitor_result.get("ok"))
    monitor_source_unavailable = bool(not base_monitor_ok and not base_payload)
    if (not bool(base_monitor_result.get("ok"))) and (
        str(base_monitor_result.get("error") or "").strip() or int(base_monitor_result.get("status_code") or 0) >= 400
    ):
        detail = str(base_monitor_result.get("error") or "").strip() or (
            f"upstream_status={int(base_monitor_result.get('status_code') or 0)} target={base_monitor_result.get('target_url') or ''}"
        )
        severity = "warning" if monitor_source_unavailable else "error"
        builder_errors.append(_monitor_builder_error("compute_monitor", detail, severity=severity))
    try:
        config_refresh()
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("config_refresh", exc))
    try:
        scheduler_payload = _call_scheduler_status_lite(scheduler_status, data_environment)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("scheduler_status", exc))
        scheduler_payload = _fallback_scheduler_payload(data_environment)
    else:
        scheduler_meta = as_dict(scheduler_payload.get("_meta")) if isinstance(scheduler_payload, dict) else {}
        scheduler_status_text = str((scheduler_payload or {}).get("status") or "").strip().lower()
        scheduler_error = str(scheduler_meta.get("error") or "").strip()
        scheduler_status_code = int(scheduler_meta.get("status_code") or 0)
        if isinstance(scheduler_payload, dict) and not bool(scheduler_payload.get("ok", False)) and (
            scheduler_error or scheduler_status_text in {"unknown", "offline", "error"} or scheduler_status_code >= 400
        ):
            detail = scheduler_error or (
                f"status={scheduler_status_text or 'unknown'} status_code={scheduler_status_code}"
            )
            severity = "error" if scheduler_status_text in {"offline", "error"} else "warning"
            builder_errors.append(_monitor_builder_error("scheduler_status", detail, severity=severity))
    scheduler_jobs = scheduler_payload.get("jobs") if isinstance(scheduler_payload.get("jobs"), dict) else {}
    try:
        scheduler_items = build_cron_payload(config, data_environment, scheduler_jobs)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("scheduler_cronz", exc))
        scheduler_items = []
    try:
        scheduler_summary = augment_scheduler_summary(build_scheduler_summary(data_environment, scheduler_payload), scheduler_items)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("scheduler_summary", exc))
        scheduler_summary = _fallback_scheduler_summary(data_environment)
    try:
        pb_health = request_json(pb_base_url, "/api/health", timeout=5)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("pocketbase_health", exc))
        pb_health = {
            "ok": False,
            "status_code": 0,
            "payload": {},
            "error": str(exc),
            "target_url": f"{str(pb_base_url or '').rstrip('/')}/api/health",
        }
    if str(backtest_base_url or "").strip():
        try:
            backtest_health = request_json(backtest_base_url, "/health", params=[("environment", data_environment)], timeout=5)
        except Exception as exc:
            builder_errors.append(_monitor_builder_error("backtest_health", exc))
            backtest_health = {
                "ok": False,
                "status_code": 0,
                "payload": {},
                "error": str(exc),
                "target_url": f"{str(backtest_base_url or '').rstrip('/')}/health",
            }
    else:
        backtest_health = {
            "ok": False,
            "status_code": 0,
            "payload": {},
            "error": "backtest_base_url_missing",
            "target_url": "",
        }
    try:
        console_probe_payload = _call_console_probe(probe_console_status, console_base_url)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("console_probe", exc))
        console_probe_payload = {
            "ok": False,
            "status_code": 0,
            "target_url": f"{str(console_base_url or '').rstrip('/')}/index.html",
            "error": str(exc),
        }
    if callable(account_snapshot_probe):
        try:
            account_snapshot_probe_payload = account_snapshot_probe(runtime_environment)
        except Exception as exc:
            builder_errors.append(_monitor_builder_error("account_snapshot", exc))
            account_snapshot_probe_payload = {
                "ok": False,
                "status_code": 0,
                "payload": {},
                "error": str(exc),
            }
    else:
        account_snapshot_probe_payload = {"ok": True, "skipped": True, "reason": "account_snapshot_probe_not_configured"}

    merged_payload = dict(base_payload)
    merged_payload.setdefault("ok", base_monitor_ok)
    if monitor_source_unavailable:
        merged_payload["monitor_source_unavailable"] = True
        merged_payload["monitor_source_error"] = str(base_monitor_result.get("error") or "").strip()
    merged_payload["status"] = str(
        merged_payload.get("status")
        or ("warning" if monitor_source_unavailable else ("offline" if merged_payload.get("ok") is False else "ok"))
    ).strip().lower() or "ok"
    actual_runtime_environment = normalize_environment(
        merged_payload.get("broker_mode")
        or as_dict(merged_payload.get("runtime")).get("broker_mode")
        or merged_payload.get("environment")
        or as_dict(merged_payload.get("runtime")).get("environment")
        or runtime_environment,
        runtime_environment,
    )
    merged_payload["requested_environment"] = runtime_environment
    merged_payload["broker_mode"] = runtime_environment
    merged_payload["data_environment"] = data_environment
    merged_payload["market_data_environment"] = data_environment
    merged_payload["shared_market_data"] = data_environment == "live"
    merged_payload["actual_runtime_environment"] = actual_runtime_environment
    merged_payload["runtime_environment_mismatch"] = actual_runtime_environment != runtime_environment
    try:
        effective_config_keys = tuple(dict.fromkeys((*monitor_config_keys, *BAR_PIPELINE_CONFIG_KEYS, *TV_FLOW_CONFIG_KEYS)))
        broker_config = load_effective_config_map(runtime_environment, effective_config_keys)
        data_config = load_effective_config_map(data_environment, effective_config_keys)
        merged_payload["config"] = {**data_config, **broker_config}
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("config_map", exc))
        merged_payload["config"] = {}
    merged_payload = _apply_tv_primary_bar_pipeline_view(merged_payload)
    merged_payload = _filter_tv_primary_legacy_flags(merged_payload)
    try:
        merged_payload["recent_events"] = load_recent_system_events(runtime_environment, 20)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("recent_events", exc))
        merged_payload["recent_events"] = []
    tv_flow_pb = pocketbase_client if pocketbase_client is not None else pb_client
    if tv_flow_pb is not None:
        try:
            tv_flow = build_tv_flow_monitor_summary(
                tv_flow_pb,
                data_environment=data_environment,
                runtime_environment=runtime_environment,
                config_map=merged_payload.get("config") if isinstance(merged_payload.get("config"), dict) else {},
            )
            merged_payload["tv_flow"] = tv_flow
            tv_flow_flags = tv_flow.get("flags") if isinstance(tv_flow.get("flags"), list) else []
            if tv_flow_flags:
                merged_payload["flags"] = _merge_unique_flags(merged_payload.get("flags"), tv_flow_flags)
                _set_status_from_flags(merged_payload, tv_flow_flags)
        except Exception as exc:
            builder_errors.append(_monitor_builder_error("tv_flow", exc))
    merged_payload["source"] = "ibkr-api"
    merged_payload["upstream_monitor"] = {
        "ok": base_monitor_ok,
        "status_code": int(base_monitor_result.get("status_code") or 0),
        "target_url": base_monitor_result.get("target_url") or "",
        "error": base_monitor_result.get("error") or "",
        "elapsed_ms": base_monitor_result.get("elapsed_ms"),
        "timeout_s": base_monitor_result.get("timeout_s"),
        "source_unavailable": monitor_source_unavailable,
    }
    merged_payload["account_snapshot_probe"] = account_snapshot_probe_payload
    merged_payload["scheduler"] = scheduler_summary
    merged_payload["backtest_service"] = {
        **backtest_health,
        "payload": as_dict(backtest_health.get("payload")),
    }
    if as_dict(merged_payload["backtest_service"].get("payload")).get("backtest"):
        merged_payload["backtest"] = as_dict(as_dict(merged_payload["backtest_service"].get("payload")).get("backtest"))
    merged_payload["control_plane"] = {
        "api": {
            "ok": True,
            "status": "running",
            "service_profile": str(service_profile or "api"),
        },
        "scheduler": scheduler_summary,
    }
    try:
        merged_payload["service_topology"] = merge_service_topology(
            merged_payload,
            as_dict(merged_payload["backtest_service"].get("payload")),
            build_service_topology(),
        )
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("service_topology", exc))
        merged_payload["service_topology"] = build_service_topology()
    try:
        merged_payload = enrich_monitor_payload_with_pocketbase_disk(merged_payload)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("pocketbase_disk", exc))
    try:
        merged_payload["service_monitor"] = derive_monitor_service_map(
            runtime_environment,
            merged_payload,
            scheduler_summary,
            console_probe=console_probe_payload,
            pb_health=pb_health,
            backtest_health=merged_payload["backtest_service"],
            build_service_topology=build_service_topology,
        )
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("service_monitor", exc))
        merged_payload["service_monitor"] = _fallback_service_monitor(
            runtime_environment,
            merged_payload.get("service_topology") if isinstance(merged_payload.get("service_topology"), dict) else build_service_topology(),
        )
    merged_payload["service_topology"] = apply_service_monitor_to_topology(
        merged_payload.get("service_topology") if isinstance(merged_payload.get("service_topology"), dict) else {},
        merged_payload["service_monitor"],
    )
    try:
        runtime_section = as_dict(merged_payload.get("runtime"))
        gate = build_effective_trading_gate(
            runtime_section,
            live_readiness=as_dict(merged_payload.get("live_readiness")),
        )
        merged_payload["effective_trading_gate"] = gate
        if runtime_section:
            runtime_section["effective_trading_gate"] = gate
            merged_payload["runtime"] = runtime_section
        merged_payload["flags"] = _append_effective_gate_flag(merged_payload.get("flags"), gate)
    except Exception as exc:
        builder_errors.append(_monitor_builder_error("effective_trading_gate", exc))
    account_flags = _account_snapshot_flags(account_snapshot_probe_payload)
    if account_flags:
        merged_payload["flags"] = _merge_unique_flags(merged_payload.get("flags"), account_flags)
        _set_status_from_flags(merged_payload, account_flags)
    if builder_errors:
        merged_payload["monitor_builder_errors"] = builder_errors
        merged_payload["flags"] = _merge_monitor_builder_flags(merged_payload.get("flags"), builder_errors)
        current_status = str(merged_payload.get("status") or "ok").strip().lower() or "ok"
        if current_status == "ok":
            merged_payload["status"] = "warning"
        merged_payload["ok"] = False
    merged_payload = _filter_tv_primary_legacy_flags(merged_payload)
    return merged_payload


__all__ = [
    "build_tv_flow_monitor_summary",
    "build_system_monitor_payload",
    "derive_monitor_service_map",
    "probe_console_status",
]
