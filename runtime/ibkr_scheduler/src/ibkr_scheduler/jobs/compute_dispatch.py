from __future__ import annotations

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
COMPUTE_DISPATCH_TARGET_STATUSES = {"active", "candidate"}


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


def _payload_error_count(payload: dict[str, Any]) -> int:
    try:
        return int((payload or {}).get("errors", 0) or 0)
    except (TypeError, ValueError):
        return 0


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

        compute_payload = {
            "source": "ibkr_scheduler",
            "environments": [environment],
            "intervals": ["5m"],
            "rollup_intervals": [],
        }
        ingest_cursor = detail.get("ingest_cursor") if isinstance(detail.get("ingest_cursor"), dict) else {}
        ingest_intervals = ingest_cursor.get("intervals") if isinstance(ingest_cursor.get("intervals"), dict) else {}
        latest_5m = dict(ingest_intervals.get("5m") or {})
        symbols = sorted(
            set(_cursor_symbols(latest_5m))
            | set(_normalize_symbols(detail.get("compute_dispatch_target_symbols")))
        )
        if symbols:
            compute_payload["symbols"] = symbols

        retry_count = 0
        compute_attempts = 0
        for attempt_index in range(len(COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS) + 1):
            compute_attempts = attempt_index + 1
            response = requests.post(
                f"{compute_base_url}/compute",
                json=compute_payload,
                timeout=60,
            )
            payload = response.json() if response.content else {}
            compute_errors = _payload_error_count(payload)
            if response.ok and payload.get("ok") is not False and compute_errors <= 0:
                break
            if (
                _is_retryable_compute_failure(response, payload)
                and attempt_index < len(COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS)
            ):
                retry_count += 1
                time.sleep(COMPUTE_DISPATCH_RETRY_BACKOFF_SECONDS[attempt_index])
                continue
            retry_detail = (
                {
                    "compute_dispatch_retry_count": retry_count,
                    "compute_dispatch_attempts": compute_attempts,
                }
                if retry_count
                else {}
            )
            return {
                "ok": False,
                "status_code": response.status_code,
                "error": payload.get("error")
                or ("compute_errors" if compute_errors > 0 else f"http_{response.status_code}"),
                "compute_errors": compute_errors,
                **retry_detail,
                **detail,
            }

        latest_dispatch_ms = int(detail.get("latest_ingested_bar_time_ms") or latest_5m.get("latest_bar_time_ms") or 0)
        latest_5m["latest_bar_time_ms"] = latest_dispatch_ms
        if latest_dispatch_ms == int(latest_5m.get("latest_compute_ingest_bar_time_ms") or 0):
            latest_5m["latest_bar_us"] = str(latest_5m.get("latest_compute_ingest_bar_us") or latest_5m.get("latest_bar_us") or "")
            latest_5m["latest_bar_cn"] = str(latest_5m.get("latest_compute_ingest_bar_cn") or latest_5m.get("latest_bar_cn") or "")
            latest_5m["latest_batch_symbols"] = list(latest_5m.get("latest_compute_ingest_symbols") or latest_5m.get("latest_batch_symbols") or [])
            latest_5m["latest_sources"] = list(latest_5m.get("latest_compute_ingest_sources") or latest_5m.get("latest_sources") or [])
        if symbols:
            latest_5m["latest_compute_dispatch_symbols"] = symbols
        target_symbols = _normalize_symbols(detail.get("compute_dispatch_target_symbols"))
        if target_symbols:
            latest_5m["latest_target_dispatch_symbols"] = target_symbols
            latest_5m["latest_target_dispatch_bar_time_ms"] = int(detail.get("compute_dispatch_target_bar_time_ms") or 0)
            latest_5m["latest_target_dispatch_date"] = str(detail.get("compute_dispatch_target_date") or "")
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
            **(
                {
                    "compute_dispatch_retry_count": retry_count,
                    "compute_dispatch_attempts": compute_attempts,
                }
                if retry_count
                else {}
            ),
            **{
                **detail,
                "latest_dispatched_bar_time_ms": int((dispatch_intervals.get("5m") or {}).get("latest_bar_time_ms") or 0),
                "dispatch_cursor": saved_dispatch_cursor,
            },
        }

    return _run_compute_dispatch
