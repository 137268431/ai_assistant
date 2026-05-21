from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests


US_TZ = ZoneInfo("America/New_York")
NON_COMPUTE_DISPATCH_SOURCES = {
    "backfill",
    "history_backfill",
    "history_rebuild",
    "history_repair",
    "ibkr_history_backfill",
    "ibkr_history_rebuild",
}
COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS = (0.25, 0.5)
COMPUTE_DISPATCH_RETRYABLE_ERRORS = {"compute_busy"}
COMPUTE_DISPATCH_RETRYABLE_STATUS_CODES = {503}
OFFICIAL_CLOSE_INFLIGHT_GRACE_SECONDS = max(
    60.0,
    float(os.environ.get("IBKR_OFFICIAL_CLOSE_INFLIGHT_GRACE_SEC", "600") or "600"),
)
COMPUTE_DISPATCH_TARGET_STATUSES = {"active", "candidate"}
INDICATOR_5M_INTERVALS = {"5", "5m"}
SIGNAL_DISPATCH_ENVIRONMENTS = {"live", "paper"}
DEFAULT_MARKET_WS_SYMBOLS = "SPY,QQQ,VIX"
DEFAULT_SCHEDULER_ROLLUP_INTERVALS = "15m,30m,1h,4h"
SCHEDULER_ROLLUP_INTERVAL_ORDER = ("15m", "30m", "1h", "4h", "1d")


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


def _normalize_symbols(raw_symbols: Any) -> list[str]:
    if isinstance(raw_symbols, str):
        raw_symbols = raw_symbols.replace("\n", ",").split(",")
    if not isinstance(raw_symbols, (list, tuple, set)):
        return []
    symbols = set()
    for item in raw_symbols:
        symbol = str(item or "").strip().upper()
        if symbol:
            symbols.add(symbol)
    return sorted(symbols)


def _cursor_symbols(bucket: dict[str, Any], key: str = "latest_compute_ingest_symbols") -> list[str]:
    return _normalize_symbols(bucket.get(key))


def _config_value(config: Any, key: str, environment: str, default: str) -> str:
    if config is not None and hasattr(config, "get_for_environment"):
        try:
            return str(config.get_for_environment(key, environment, default) or "")
        except Exception:
            return str(default or "")
    return str(default or "")


def _scheduler_market_ws_symbols(config: Any, environment: str) -> list[str]:
    raw = _config_value(config, "ibkr_market_ws_symbols", environment, DEFAULT_MARKET_WS_SYMBOLS)
    return _normalize_symbols(raw)


def _scheduler_rollup_intervals(config: Any, environment: str) -> list[str]:
    raw = _config_value(
        config,
        "ibkr_scheduler_rollup_intervals",
        environment,
        DEFAULT_SCHEDULER_ROLLUP_INTERVALS,
    )
    requested = [str(item or "").strip().lower() for item in str(raw or "").replace("\n", ",").split(",")]
    allowed = set(SCHEDULER_ROLLUP_INTERVAL_ORDER)
    intervals = []
    for item in requested:
        if item and item in allowed and item not in intervals:
            intervals.append(item)
    return sorted(
        intervals,
        key=lambda value: SCHEDULER_ROLLUP_INTERVAL_ORDER.index(value)
        if value in SCHEDULER_ROLLUP_INTERVAL_ORDER else 99,
    )


def _payload_error_count(payload: dict[str, Any]) -> int:
    try:
        return int((payload or {}).get("errors", 0) or 0)
    except (TypeError, ValueError):
        return 0


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


def _compute_dispatch_chunk_size() -> int:
    return max(1, _coerce_int(os.environ.get("IBKR_COMPUTE_DISPATCH_CHUNK_SIZE"), 8))


def _is_retryable_compute_failure(response: requests.Response, payload: dict[str, Any]) -> bool:
    error = str((payload or {}).get("error") or "").strip().lower()
    return (
        bool((payload or {}).get("retryable"))
        or error in COMPUTE_DISPATCH_RETRYABLE_ERRORS
        or int(getattr(response, "status_code", 0) or 0) in COMPUTE_DISPATCH_RETRYABLE_STATUS_CODES
    )


def _pb_filter_quote(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _market_date_from_bar_time_ms(bar_time_ms: int) -> str:
    try:
        ms = int(bar_time_ms or 0)
    except (TypeError, ValueError):
        ms = 0
    if ms > 0:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(US_TZ).date().isoformat()
    return datetime.now(US_TZ).date().isoformat()


def _load_target_symbol_statuses(pb: Any, environment: str, market_date: str) -> dict[str, str]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    date_token = str(market_date or "").strip()
    if not date_token:
        return {}
    status_filter = " || ".join(f'status = "{status}"' for status in sorted(COMPUTE_DISPATCH_TARGET_STATUSES))
    filter_expr = (
        f'date = "{_pb_filter_quote(date_token)}" && '
        f'environment = "{_pb_filter_quote(runtime_environment)}" && '
        f"({status_filter})"
    )
    try:
        if hasattr(pb, "get_all_records"):
            rows = pb.get_all_records("ibkr_targets", filter=filter_expr, sort="-score,-updated", max_pages=5)
        else:
            rows = pb.get_records("ibkr_targets", filter=filter_expr, sort="-score,-updated", per_page=500, page=1)
    except Exception:
        return {}

    statuses: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        status = str(row.get("status") or "").strip().lower()
        row_environment = str(row.get("environment") or "").strip().lower()
        row_date = str(row.get("date") or "").strip()[:10]
        if (
            symbol
            and status in COMPUTE_DISPATCH_TARGET_STATUSES
            and row_environment == runtime_environment
            and row_date == date_token
        ):
            statuses[symbol] = status
    return statuses


def _chunked_symbols(symbols: list[str], chunk_size: int = 50) -> list[list[str]]:
    safe_chunk_size = max(1, int(chunk_size or 50))
    return [symbols[index : index + safe_chunk_size] for index in range(0, len(symbols), safe_chunk_size)]


def _latest_bar_time_for_symbols(
    pb: Any,
    *,
    environment: str,
    symbols: list[str],
    after_bar_time_ms: int,
    upper_bar_time_ms: int,
) -> int | None:
    normalized_symbols = _normalize_symbols(symbols)
    if not normalized_symbols:
        return 0
    runtime_environment = str(environment or "live").strip().lower() or "live"
    lower_ms = int(after_bar_time_ms or 0)
    upper_ms = int(upper_bar_time_ms or 0)
    latest_ms = 0
    try:
        for chunk in _chunked_symbols(normalized_symbols):
            symbol_filter = " || ".join(f'symbol = "{_pb_filter_quote(symbol)}"' for symbol in chunk)
            filter_expr = (
                f'environment = "{_pb_filter_quote(runtime_environment)}" && '
                'interval = "5m" && '
                f"bar_time_ms > {lower_ms} && "
                f"bar_time_ms <= {upper_ms} && "
                f"({symbol_filter})"
            )
            rows = pb.get_records(
                "ibkr_bars",
                filter=filter_expr,
                sort="-bar_time_ms",
                per_page=200,
                page=1,
            )
            chunk_symbols = set(chunk)
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "").strip().upper()
                interval = str(row.get("interval") or "").strip().lower()
                row_environment = str(row.get("environment") or "").strip().lower()
                try:
                    bar_ms = int(row.get("bar_time_ms") or 0)
                except (TypeError, ValueError):
                    bar_ms = 0
                if (
                    symbol in chunk_symbols
                    and interval == "5m"
                    and row_environment == runtime_environment
                    and bar_ms > lower_ms
                    and (upper_ms <= 0 or bar_ms <= upper_ms)
                ):
                    latest_ms = max(latest_ms, bar_ms)
    except Exception:
        return None
    return latest_ms


def _load_indicator_coverage(
    pb: Any,
    *,
    environment: str,
    symbols: list[str],
    bar_time_ms: int,
) -> dict[str, Any]:
    normalized_symbols = _normalize_symbols(symbols)
    target_ms = _coerce_int(bar_time_ms)
    coverage = {
        "indicator_coverage_status": "unknown",
        "indicator_coverage_bar_time_ms": target_ms,
        "indicator_coverage_symbol_count": len(normalized_symbols),
        "covered_indicator_symbols": [],
        "covered_indicator_symbol_count": 0,
        "missing_indicator_symbols": normalized_symbols,
        "missing_indicator_symbol_count": len(normalized_symbols),
    }
    if not normalized_symbols or target_ms <= 0:
        coverage["indicator_coverage_status"] = "not_applicable"
        coverage["missing_indicator_symbols"] = []
        coverage["missing_indicator_symbol_count"] = 0
        return coverage
    if not hasattr(pb, "get_records"):
        coverage["indicator_coverage_error"] = "pb_get_records_unavailable"
        return coverage

    runtime_environment = str(environment or "live").strip().lower() or "live"
    covered_symbols: set[str] = set()
    try:
        for chunk in _chunked_symbols(normalized_symbols):
            symbol_filter = " || ".join(f'symbol = "{_pb_filter_quote(symbol)}"' for symbol in chunk)
            interval_filter = " || ".join(f'interval = "{interval}"' for interval in sorted(INDICATOR_5M_INTERVALS))
            filter_expr = (
                f'environment = "{_pb_filter_quote(runtime_environment)}" && '
                f"({interval_filter}) && "
                f"bar_time_ms >= {target_ms} && "
                f"({symbol_filter})"
            )
            rows = pb.get_records(
                "ibkr_indicators",
                filter=filter_expr,
                sort="-bar_time_ms",
                per_page=max(200, len(chunk) * 2),
                page=1,
            )
            chunk_symbols = set(chunk)
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "").strip().upper()
                interval = str(row.get("interval") or "").strip().lower()
                row_environment = str(row.get("environment") or "").strip().lower()
                row_ms = _coerce_int(row.get("bar_time_ms"))
                if (
                    symbol in chunk_symbols
                    and interval in INDICATOR_5M_INTERVALS
                    and row_environment == runtime_environment
                    and row_ms >= target_ms
                ):
                    covered_symbols.add(symbol)
    except Exception as exc:
        coverage["indicator_coverage_error"] = str(exc)
        return coverage

    covered = sorted(covered_symbols)
    missing = sorted(symbol for symbol in normalized_symbols if symbol not in covered_symbols)
    coverage.update(
        {
            "indicator_coverage_status": "covered" if not missing else "missing",
            "covered_indicator_symbols": covered,
            "covered_indicator_symbol_count": len(covered),
            "missing_indicator_symbols": missing,
            "missing_indicator_symbol_count": len(missing),
        }
    )
    return coverage


def _load_signal_dispatch_coverage(
    dispatch_5m: dict[str, Any],
    *,
    environment: str,
    symbols: list[str],
    bar_time_ms: int,
) -> dict[str, Any]:
    normalized_symbols = _normalize_symbols(symbols)
    target_ms = _coerce_int(bar_time_ms)
    coverage = {
        "signal_dispatch_status": "unknown",
        "signal_dispatch_bar_time_ms": target_ms,
        "signal_dispatch_symbol_count": len(normalized_symbols),
        "covered_signal_dispatch_symbols": [],
        "covered_signal_dispatch_symbol_count": 0,
        "missing_signal_dispatch_symbols": normalized_symbols,
        "missing_signal_dispatch_symbol_count": len(normalized_symbols),
    }
    runtime_environment = str(environment or "live").strip().lower() or "live"
    if runtime_environment not in SIGNAL_DISPATCH_ENVIRONMENTS or not normalized_symbols or target_ms <= 0:
        coverage["signal_dispatch_status"] = "not_applicable"
        coverage["missing_signal_dispatch_symbols"] = []
        coverage["missing_signal_dispatch_symbol_count"] = 0
        return coverage

    latest_signal_ms = _coerce_int(dispatch_5m.get("latest_signal_dispatch_bar_time_ms"))
    covered_symbols: set[str] = set()
    if latest_signal_ms >= target_ms:
        covered_symbols.update(_cursor_symbols(dispatch_5m, "latest_signal_dispatch_symbols"))

    required = set(normalized_symbols)
    covered = sorted(required & covered_symbols)
    missing = sorted(required - covered_symbols)
    coverage.update(
        {
            "signal_dispatch_status": "covered" if not missing else "missing",
            "covered_signal_dispatch_symbols": covered,
            "covered_signal_dispatch_symbol_count": len(covered),
            "missing_signal_dispatch_symbols": missing,
            "missing_signal_dispatch_symbol_count": len(missing),
        }
    )
    return coverage


def _build_signal_dispatch_success_metadata(
    *,
    environment: str,
    required_symbols: list[str],
    processed_symbols: list[str],
    prior_covered_symbols: list[str],
    bar_time_ms: int,
) -> dict[str, Any]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    required = set(_normalize_symbols(required_symbols))
    if runtime_environment not in SIGNAL_DISPATCH_ENVIRONMENTS or not required:
        return {}
    target_ms = _coerce_int(bar_time_ms)
    if target_ms <= 0:
        return {}

    covered = required & (set(_normalize_symbols(prior_covered_symbols)) | set(_normalize_symbols(processed_symbols)))
    missing = sorted(required - covered)
    if missing:
        return {
            "signal_dispatch_status": "missing",
            "signal_dispatch_bar_time_ms": target_ms,
            "signal_dispatch_symbol_count": len(required),
            "covered_signal_dispatch_symbols": sorted(covered),
            "covered_signal_dispatch_symbol_count": len(covered),
            "missing_signal_dispatch_symbols": missing,
            "missing_signal_dispatch_symbol_count": len(missing),
        }
    return {
        "latest_signal_dispatch_bar_time_ms": target_ms,
        "latest_signal_dispatch_symbols": sorted(required),
        "signal_dispatch_status": "covered",
        "signal_dispatch_bar_time_ms": target_ms,
        "signal_dispatch_symbol_count": len(required),
        "covered_signal_dispatch_symbols": sorted(required),
        "covered_signal_dispatch_symbol_count": len(required),
        "missing_signal_dispatch_symbols": [],
        "missing_signal_dispatch_symbol_count": 0,
    }


def _runtime_realtime_compute_status(status_payload: dict[str, Any]) -> dict[str, Any]:
    payload = status_payload if isinstance(status_payload, dict) else {}
    candidates: list[dict[str, Any]] = []
    for item in (
        payload,
        payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {},
        payload.get("compute") if isinstance(payload.get("compute"), dict) else {},
        payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
    ):
        if isinstance(item, dict):
            candidates.append(item)
    for item in candidates:
        realtime = item.get("realtime_compute")
        if isinstance(realtime, dict) and realtime:
            return dict(realtime)
    return {}


def _runtime_canonical_5m_status(status_payload: dict[str, Any]) -> dict[str, Any]:
    payload = status_payload if isinstance(status_payload, dict) else {}
    candidates: list[dict[str, Any]] = []
    for item in (
        payload,
        payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {},
        payload.get("compute") if isinstance(payload.get("compute"), dict) else {},
        payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
    ):
        if isinstance(item, dict):
            candidates.append(item)
    for item in candidates:
        canonical = item.get("canonical_5m")
        if isinstance(canonical, dict) and canonical:
            return dict(canonical)
    return {}


def _should_wait_for_official_close(
    status_payload: dict[str, Any],
    *,
    target_bar_time_ms: int = 0,
) -> tuple[bool, dict[str, Any]]:
    canonical = _runtime_canonical_5m_status(status_payload)
    running = bool(canonical.get("running"))
    cycle_age_s = _coerce_float(canonical.get("cycle_age_s"), 0.0)
    threshold_s = OFFICIAL_CLOSE_INFLIGHT_GRACE_SECONDS
    current_due_ms = _coerce_int(
        canonical.get("current_due_bucket_ms")
        or canonical.get("last_due_bucket_ms")
    )
    target_ms = _coerce_int(target_bar_time_ms)
    relevant = bool(not target_ms or not current_due_ms or current_due_ms >= target_ms)
    stalled = bool(running and cycle_age_s >= threshold_s)
    wait = bool(running and relevant and not stalled)
    return wait, {
        "official_5m_close_in_progress": bool(running),
        "official_5m_close_relevant": relevant,
        "official_5m_close_stalled": stalled,
        "official_5m_close_age_s": cycle_age_s if running else 0.0,
        "official_5m_close_timeout_threshold_s": threshold_s,
        "canonical_5m": canonical,
    }


def _should_wait_for_inflight(status_payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    realtime = _runtime_realtime_compute_status(status_payload)
    inflight = bool(realtime.get("inflight"))
    stalled = bool(realtime.get("stalled"))
    inflight_age_s = _coerce_float(realtime.get("inflight_age_s"), 0.0)
    threshold_s = _coerce_float(realtime.get("inflight_timeout_threshold_s"), 0.0)
    wait = bool(inflight and not stalled and (threshold_s <= 0.0 or inflight_age_s < threshold_s))
    return wait, {
        "compute_in_progress": bool(inflight),
        "inflight_age_s": inflight_age_s if inflight else 0.0,
        "inflight_timeout_threshold_s": threshold_s,
        "inflight_stalled": bool(stalled),
        "inflight_stall_reason": str(realtime.get("stall_reason") or ""),
        "realtime_compute": realtime,
    }


def _load_runtime_status_payload(compute_base_url: str, environment: str) -> dict[str, Any]:
    try:
        response = requests.get(
            f"{compute_base_url}/ibkr/status",
            params={"environment": environment},
            timeout=5,
        )
        return response.json() if response.content else {}
    except Exception:
        return {}


def _resolve_target_dispatch(
    pb: Any,
    *,
    environment: str,
    ingest_5m: dict[str, Any],
    dispatch_5m: dict[str, Any],
    latest_dispatched_bar_time_ms: int,
) -> dict[str, Any]:
    raw_latest = int(ingest_5m.get("latest_bar_time_ms") or 0)
    explicit_latest = int(ingest_5m.get("latest_compute_ingest_bar_time_ms") or 0)
    latest_dispatched = int(latest_dispatched_bar_time_ms or 0)
    latest_reference_ms = max(raw_latest, explicit_latest, latest_dispatched)
    if latest_reference_ms <= 0:
        return {
            "compute_dispatch_target_symbols": [],
            "compute_dispatch_target_bar_time_ms": 0,
        }

    market_date = _market_date_from_bar_time_ms(latest_reference_ms)
    target_statuses = _load_target_symbol_statuses(pb, environment, market_date)
    all_target_symbols = sorted(target_statuses)
    if not all_target_symbols:
        return {
            "compute_dispatch_target_symbols": [],
            "compute_dispatch_target_bar_time_ms": 0,
            "compute_dispatch_target_date": market_date,
            "compute_dispatch_target_status_counts": {},
        }

    target_latest = _latest_bar_time_for_symbols(
        pb,
        environment=environment,
        symbols=all_target_symbols,
        after_bar_time_ms=0,
        upper_bar_time_ms=latest_reference_ms,
    )
    if target_latest is None:
        # If the read-side check fails, still let compute repair current target symbols.
        target_latest = latest_reference_ms

    previous_dispatch_symbols = set(_cursor_symbols(dispatch_5m, "latest_compute_dispatch_symbols"))
    previous_dispatch_symbols.update(_cursor_symbols(dispatch_5m, "latest_target_dispatch_symbols"))
    previous_dispatch_symbols.update(_cursor_symbols(dispatch_5m, "latest_batch_symbols"))
    previous_target_date = str(dispatch_5m.get("latest_target_dispatch_date") or "").strip()
    previous_target_ms = int(dispatch_5m.get("latest_target_dispatch_bar_time_ms") or 0)
    missing_target_symbols = sorted(symbol for symbol in all_target_symbols if symbol not in previous_dispatch_symbols)
    target_symbols = []
    target_reason = ""
    if target_latest > latest_dispatched:
        target_symbols = all_target_symbols
        target_reason = "target_bar_newer_than_dispatch"
    elif (
        target_latest > 0
        and (previous_target_date != market_date or previous_target_ms < target_latest or missing_target_symbols)
    ):
        target_symbols = missing_target_symbols or all_target_symbols
        target_reason = "target_symbols_not_dispatched"

    status_counts: dict[str, int] = {}
    for symbol in target_symbols:
        status = target_statuses.get(symbol, "")
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "compute_dispatch_target_symbols": target_symbols,
        "compute_dispatch_target_bar_time_ms": int(target_latest or 0),
        "compute_dispatch_target_date": market_date,
        "compute_dispatch_target_status_counts": status_counts,
        "compute_dispatch_target_reason": target_reason,
    }


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
    config: Any = None,
) -> Callable[[str], dict[str, Any]]:
    def _native_compute_due(environment: str) -> tuple[bool, dict[str, Any]]:
        ingest = get_ingest_cursor(environment)
        dispatch = get_dispatch_cursor(environment)
        ingest_intervals = ingest.get("intervals") if isinstance(ingest.get("intervals"), dict) else {}
        dispatch_intervals = dispatch.get("intervals") if isinstance(dispatch.get("intervals"), dict) else {}
        ingest_5m = ingest_intervals.get("5m") if isinstance(ingest_intervals.get("5m"), dict) else {}
        dispatch_5m = dispatch_intervals.get("5m") if isinstance(dispatch_intervals.get("5m"), dict) else {}
        latest_raw_ingested = int(ingest_5m.get("latest_bar_time_ms") or 0)
        latest_dispatched = int((dispatch_5m.get("latest_bar_time_ms") or 0))
        latest_ingested, skip_reason = _resolve_compute_ingest_bar_time_ms(ingest_5m, latest_dispatched)
        target_dispatch = _resolve_target_dispatch(
            pb,
            environment=environment,
            ingest_5m=ingest_5m,
            dispatch_5m=dispatch_5m,
            latest_dispatched_bar_time_ms=latest_dispatched,
        )
        target_bar_time_ms = int(target_dispatch.get("compute_dispatch_target_bar_time_ms") or 0)
        target_symbols = _normalize_symbols(target_dispatch.get("compute_dispatch_target_symbols"))
        target_due = bool(target_symbols) and target_bar_time_ms > 0
        if target_due:
            latest_ingested = max(latest_ingested, target_bar_time_ms)
            skip_reason = ""
        return (latest_ingested > latest_dispatched or target_due), {
            "latest_ingested_bar_time_ms": latest_ingested,
            "latest_raw_ingested_bar_time_ms": latest_raw_ingested,
            "latest_dispatched_bar_time_ms": latest_dispatched,
            "compute_dispatch_skip_reason": skip_reason,
            **target_dispatch,
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
        except Exception:
            status_payload = {}
        if _is_compute_startup_preload_active(status_payload):
            return {
                "ok": True,
                "skipped": True,
                "reason": "compute_startup_preload_running",
                "compute_startup_preload": _extract_compute_startup_preload(status_payload),
                **detail,
            }

        ingest_cursor = detail.get("ingest_cursor") if isinstance(detail.get("ingest_cursor"), dict) else {}
        ingest_intervals = ingest_cursor.get("intervals") if isinstance(ingest_cursor.get("intervals"), dict) else {}
        latest_5m = dict(ingest_intervals.get("5m") or {})
        dispatch_cursor = detail.get("dispatch_cursor") if isinstance(detail.get("dispatch_cursor"), dict) else {}
        dispatch_5m = dict((dispatch_cursor.get("intervals") or {}).get("5m") or {})
        market_ws_symbols = _scheduler_market_ws_symbols(config, environment)
        symbols = sorted(
            set(_cursor_symbols(latest_5m))
            | set(_normalize_symbols(detail.get("compute_dispatch_target_symbols")))
            | set(market_ws_symbols)
        )
        effective_rollup_intervals = _scheduler_rollup_intervals(config, environment) if symbols else []
        compute_payload_base = {
            "source": "ibkr_scheduler",
            "environments": [environment],
            "intervals": ["5m"],
            "rollup_intervals": effective_rollup_intervals,
        }
        latest_dispatch_ms = int(detail.get("latest_ingested_bar_time_ms") or latest_5m.get("latest_bar_time_ms") or 0)
        target_symbols = _normalize_symbols(detail.get("compute_dispatch_target_symbols"))
        coverage_detail: dict[str, Any] = {}
        compute_symbols = list(symbols)

        def _cursor_metadata(
            *,
            source: str,
            coverage: dict[str, Any] | None = None,
            skip_reason: str = "",
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            metadata = {
                "last_dispatched_at_ms": int(time.time() * 1000),
                "dispatch_source": source,
            }
            if skip_reason:
                metadata["dispatch_skip_reason"] = skip_reason
            if symbols:
                metadata["latest_compute_dispatch_symbols"] = symbols
            if target_symbols:
                metadata["latest_target_dispatch_symbols"] = target_symbols
                metadata["latest_target_dispatch_bar_time_ms"] = int(detail.get("compute_dispatch_target_bar_time_ms") or 0)
                metadata["latest_target_dispatch_date"] = str(detail.get("compute_dispatch_target_date") or "")
            if coverage:
                for key in (
                    "indicator_coverage_status",
                    "indicator_coverage_bar_time_ms",
                    "indicator_coverage_symbol_count",
                    "covered_indicator_symbols",
                    "covered_indicator_symbol_count",
                    "missing_indicator_symbols",
                    "missing_indicator_symbol_count",
                    "indicator_coverage_error",
                    "signal_dispatch_status",
                    "signal_dispatch_bar_time_ms",
                    "signal_dispatch_symbol_count",
                    "covered_signal_dispatch_symbols",
                    "covered_signal_dispatch_symbol_count",
                    "missing_signal_dispatch_symbols",
                    "missing_signal_dispatch_symbol_count",
                ):
                    if key in coverage:
                        metadata[key] = coverage[key]
            if extra:
                metadata.update(extra)
            return metadata

        def _save_advanced_dispatch_cursor(
            *,
            source: str,
            compute_result: dict[str, Any] | None = None,
            coverage: dict[str, Any] | None = None,
            skip_reason: str = "",
            extra: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            advanced_5m = dict(latest_5m)
            advanced_5m["latest_bar_time_ms"] = latest_dispatch_ms
            if latest_dispatch_ms == int(advanced_5m.get("latest_compute_ingest_bar_time_ms") or 0):
                advanced_5m["latest_bar_us"] = str(
                    advanced_5m.get("latest_compute_ingest_bar_us") or advanced_5m.get("latest_bar_us") or ""
                )
                advanced_5m["latest_bar_cn"] = str(
                    advanced_5m.get("latest_compute_ingest_bar_cn") or advanced_5m.get("latest_bar_cn") or ""
                )
                advanced_5m["latest_batch_symbols"] = list(
                    advanced_5m.get("latest_compute_ingest_symbols") or advanced_5m.get("latest_batch_symbols") or []
                )
                advanced_5m["latest_sources"] = list(
                    advanced_5m.get("latest_compute_ingest_sources") or advanced_5m.get("latest_sources") or []
                )
            dispatch_intervals = {
                **(dispatch_cursor.get("intervals") or {}),
                "5m": {
                    **advanced_5m,
                    **_cursor_metadata(source=source, coverage=coverage, skip_reason=skip_reason, extra=extra),
                },
            }
            payload = {"intervals": dispatch_intervals}
            if compute_result is not None:
                payload["latest_compute_result"] = compute_result
            return save_dispatch_cursor(environment, payload)

        def _save_partial_dispatch_cursor(
            *,
            compute_result: dict[str, Any],
            coverage: dict[str, Any] | None,
            extra: dict[str, Any],
        ) -> dict[str, Any]:
            dispatch_intervals = {
                **(dispatch_cursor.get("intervals") or {}),
                "5m": {
                    **dispatch_5m,
                    **_cursor_metadata(
                        source="ibkr_scheduler_partial_repair",
                        coverage=coverage,
                        skip_reason="compute_busy_deferred",
                        extra=extra,
                    ),
                },
            }
            return save_dispatch_cursor(
                environment,
                {
                    "intervals": dispatch_intervals,
                    "latest_compute_result": compute_result,
                },
            )

        if symbols and latest_dispatch_ms > 0:
            coverage_detail = _load_indicator_coverage(
                pb,
                environment=environment,
                symbols=symbols,
                bar_time_ms=latest_dispatch_ms,
            )
            signal_dispatch_detail = _load_signal_dispatch_coverage(
                dispatch_5m,
                environment=environment,
                symbols=symbols,
                bar_time_ms=latest_dispatch_ms,
            )
            coverage_detail.update(signal_dispatch_detail)
            if (
                coverage_detail.get("indicator_coverage_status") == "covered"
                and coverage_detail.get("signal_dispatch_status") in {"covered", "not_applicable"}
                and not effective_rollup_intervals
            ):
                saved_dispatch_cursor = _save_advanced_dispatch_cursor(
                    source="ibkr_scheduler_indicator_coverage",
                    coverage=coverage_detail,
                    skip_reason="indicators_already_current",
                )
                return {
                    "ok": True,
                    "skipped": False,
                    "reason": "indicators_already_current",
                    "compute_skipped": True,
                    **detail,
                    **coverage_detail,
                    "latest_dispatched_bar_time_ms": int(
                        ((saved_dispatch_cursor.get("intervals") or {}).get("5m") or {}).get("latest_bar_time_ms") or 0
                    ),
                    "dispatch_cursor": saved_dispatch_cursor,
                }
            compute_symbols = sorted(
                set(_normalize_symbols(coverage_detail.get("missing_indicator_symbols")))
                | set(_normalize_symbols(coverage_detail.get("missing_signal_dispatch_symbols")))
            ) or symbols
            runtime_status_payload = _load_runtime_status_payload(compute_base_url, environment)
            wait_for_official_close, official_close_detail = _should_wait_for_official_close(
                runtime_status_payload,
                target_bar_time_ms=latest_dispatch_ms,
            )
            if compute_symbols and wait_for_official_close:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "official_5m_close_inflight",
                    "compute_in_progress": True,
                    **detail,
                    **coverage_detail,
                    **official_close_detail,
                }
            wait_for_inflight, inflight_detail = _should_wait_for_inflight(runtime_status_payload)
            if compute_symbols and wait_for_inflight:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "close_compute_inflight",
                    **detail,
                    **coverage_detail,
                    **inflight_detail,
                }

        def _post_compute_with_retries(payload: dict[str, Any]) -> tuple[bool, Any, dict[str, Any], int, int]:
            retry_count = 0
            compute_attempts = 0
            response = None
            response_payload: dict[str, Any] = {}
            for attempt_index in range(len(COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS) + 1):
                compute_attempts = attempt_index + 1
                response = requests.post(
                    f"{compute_base_url}/compute",
                    json=payload,
                    timeout=60,
                )
                response_payload = response.json() if response.content else {}
                compute_errors = _payload_error_count(response_payload)
                if response.ok and response_payload.get("ok") is not False and compute_errors <= 0:
                    return True, response, response_payload, retry_count, compute_attempts
                if (
                    _is_retryable_compute_failure(response, response_payload)
                    and attempt_index < len(COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS)
                ):
                    retry_count += 1
                    time.sleep(COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS[attempt_index])
                    continue
                return False, response, response_payload, retry_count, compute_attempts
            return False, response, response_payload, retry_count, compute_attempts  # pragma: no cover

        chunks = _chunked_symbols(compute_symbols, _compute_dispatch_chunk_size()) if compute_symbols else [[]]
        successful_payloads: list[dict[str, Any]] = []
        deferred_busy_symbols: list[str] = []
        deferred_busy_details: list[dict[str, Any]] = []
        compute_retry_count = 0
        compute_attempts = 0
        successful_symbols: list[str] = []
        for chunk in chunks:
            chunk_payload = dict(compute_payload_base)
            if chunk:
                chunk_payload["symbols"] = list(chunk)
            ok, response, payload, retry_count, attempts = _post_compute_with_retries(chunk_payload)
            compute_retry_count += retry_count
            compute_attempts += attempts
            if ok:
                successful_payloads.append(payload)
                successful_symbols.extend(chunk)
                continue
            compute_errors = _payload_error_count(payload)
            if chunk and _is_retryable_compute_failure(response, payload):
                deferred_busy_symbols.extend(chunk)
                deferred_busy_details.append(
                    {
                        "symbols": _normalize_symbols(chunk),
                        "symbol_count": len(_normalize_symbols(chunk)),
                        "status_code": int(getattr(response, "status_code", 0) or 0),
                        "error": str(payload.get("error") or ""),
                        "lock_scope": str(payload.get("lock_scope") or ""),
                        "lock_reason": str(payload.get("lock_reason") or ""),
                        "blocked_by": list(payload.get("blocked_by") or [])[:8]
                        if isinstance(payload.get("blocked_by"), list)
                        else [],
                        "blocked_by_count": _coerce_int(payload.get("blocked_by_count")),
                        "conflict_slots": list(payload.get("conflict_slots") or [])[:20]
                        if isinstance(payload.get("conflict_slots"), list)
                        else [],
                        "conflict_slot_count": _coerce_int(payload.get("conflict_slot_count")),
                    }
                )
                continue
            retry_detail = (
                {
                    "compute_dispatch_retry_count": compute_retry_count,
                    "compute_dispatch_attempts": compute_attempts,
                }
                if compute_retry_count
                else {}
            )
            return {
                "ok": False,
                "status_code": int(getattr(response, "status_code", 0) or 0),
                "error": payload.get("error")
                or ("compute_errors" if compute_errors > 0 else f"http_{int(getattr(response, 'status_code', 0) or 0)}"),
                "compute_errors": compute_errors,
                **retry_detail,
                **detail,
                **coverage_detail,
            }

        if not successful_payloads and deferred_busy_symbols:
            aggregate_payload = {
                "ok": True,
                "deferred_compute_busy": True,
                "deferred_busy_symbols": _normalize_symbols(deferred_busy_symbols),
                "deferred_busy_symbol_count": len(_normalize_symbols(deferred_busy_symbols)),
                "deferred_busy_details": deferred_busy_details[:8],
                "chunks": len(chunks),
                "successful_chunks": 0,
            }
        elif len(successful_payloads) == 1 and not deferred_busy_symbols:
            aggregate_payload = successful_payloads[0]
        else:
            aggregate_payload = {
                "ok": True,
                "chunks": len(chunks),
                "successful_chunks": len(successful_payloads),
                "processed": sum(_coerce_int(payload.get("processed")) for payload in successful_payloads),
                "errors": sum(_payload_error_count(payload) for payload in successful_payloads),
            }
            if deferred_busy_symbols:
                aggregate_payload.update(
                    {
                        "deferred_compute_busy": True,
                        "deferred_busy_symbols": _normalize_symbols(deferred_busy_symbols),
                        "deferred_busy_symbol_count": len(_normalize_symbols(deferred_busy_symbols)),
                        "deferred_busy_details": deferred_busy_details[:8],
                    }
                )

        if deferred_busy_symbols:
            deferred_symbols = _normalize_symbols(deferred_busy_symbols)
            required_signal_symbols = _normalize_symbols(symbols)
            partial_signal_symbols = sorted(
                set(_normalize_symbols(coverage_detail.get("covered_signal_dispatch_symbols")))
                | set(_normalize_symbols(successful_symbols))
            )
            missing_partial_signal_symbols = sorted(set(required_signal_symbols) - set(partial_signal_symbols))
            partial_cursor = _save_partial_dispatch_cursor(
                compute_result=aggregate_payload,
                coverage=coverage_detail,
                extra={
                    "latest_partial_dispatched_bar_time_ms": latest_dispatch_ms,
                    "latest_partial_dispatch_symbols": _normalize_symbols(successful_symbols),
                    "latest_partial_signal_dispatch_bar_time_ms": latest_dispatch_ms,
                    "latest_partial_signal_dispatch_symbols": partial_signal_symbols,
                    "signal_dispatch_status": (
                        "missing"
                        if coverage_detail.get("signal_dispatch_status") != "not_applicable"
                        and missing_partial_signal_symbols
                        else coverage_detail.get("signal_dispatch_status", "unknown")
                    ),
                    "covered_signal_dispatch_symbols": partial_signal_symbols,
                    "covered_signal_dispatch_symbol_count": len(partial_signal_symbols),
                    "missing_signal_dispatch_symbols": missing_partial_signal_symbols,
                    "missing_signal_dispatch_symbol_count": len(missing_partial_signal_symbols),
                    "deferred_compute_busy": True,
                    "deferred_busy_symbols": deferred_symbols,
                    "deferred_busy_symbol_count": len(deferred_symbols),
                    "deferred_busy_details": deferred_busy_details[:8],
                },
            )
            return {
                "ok": True,
                "skipped": False,
                "reason": "compute_busy_deferred",
                "compute": aggregate_payload,
                "deferred_compute_busy": True,
                "deferred_busy_symbols": deferred_symbols,
                "deferred_busy_symbol_count": len(deferred_symbols),
                "deferred_busy_details": deferred_busy_details[:8],
                **(
                    {
                        "compute_dispatch_retry_count": compute_retry_count,
                        "compute_dispatch_attempts": compute_attempts,
                    }
                    if compute_retry_count
                    else {}
                ),
                **detail,
                **coverage_detail,
                "latest_dispatched_bar_time_ms": int((dispatch_5m or {}).get("latest_bar_time_ms") or 0),
                "latest_partial_dispatched_bar_time_ms": latest_dispatch_ms,
                "dispatch_cursor": partial_cursor,
            }

        signal_dispatch_extra = _build_signal_dispatch_success_metadata(
            environment=environment,
            required_symbols=symbols,
            processed_symbols=compute_symbols,
            prior_covered_symbols=_normalize_symbols(coverage_detail.get("covered_signal_dispatch_symbols")),
            bar_time_ms=latest_dispatch_ms,
        )
        saved_dispatch_cursor = _save_advanced_dispatch_cursor(
            source="ibkr_scheduler",
            compute_result=aggregate_payload,
            coverage=coverage_detail,
            extra=signal_dispatch_extra,
        )
        return {
            "ok": True,
            "skipped": False,
            "compute": aggregate_payload,
            **(
                {
                    "compute_dispatch_retry_count": compute_retry_count,
                    "compute_dispatch_attempts": compute_attempts,
                }
                if compute_retry_count
                else {}
            ),
            **{
                **detail,
                **coverage_detail,
                "latest_dispatched_bar_time_ms": int(
                    ((saved_dispatch_cursor.get("intervals") or {}).get("5m") or {}).get("latest_bar_time_ms") or 0
                ),
                "dispatch_cursor": saved_dispatch_cursor,
            },
        }

    return _run_compute_dispatch
