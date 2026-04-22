from __future__ import annotations

import traceback

from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import bucket_start_ms, interval_to_ms, normalize_interval

from ibkr_compute.api.compute.runtime_state.runtime import _api_app
from ibkr_compute.api.compute.runtime_state.timing import get_fetch_since_ms
from .materialize import reset_compute_state_for_symbols


def _normalize_target_intervals(api_app, intervals=None) -> list[str]:
    target_intervals = [normalize_interval(interval) for interval in (intervals or api_app.HIGHER_INTERVALS)]
    return [interval for interval in target_intervals if interval in api_app.HIGHER_INTERVALS]


def _build_rollup_filter(api_app, environment: str, normalized_symbols, since_ms: int | None = None) -> str:
    symbol_filter = api_app.build_symbol_filter(normalized_symbols)
    filter_parts = [
        'interval = "5m"',
        api_app.build_bar_environment_filter(environment, include_legacy_empty=True),
    ]
    if symbol_filter:
        filter_parts.append(symbol_filter)
    if since_ms is not None and int(since_ms) > 0:
        filter_parts.append(f"bar_time_ms >= {int(since_ms)}")
    return " && ".join(filter_parts)


def _latest_targeted_5m_bar_ms(environment: str, normalized_symbols) -> int:
    api_app = _api_app()
    interval_key = (environment, "5m")
    cached_ms = int(api_app.last_interval_fetch_ms.get(interval_key, 0) or 0)
    if cached_ms > 0:
        return cached_ms

    rows = api_app.pb.get_records(
        "ibkr_bars",
        filter=_build_rollup_filter(api_app, environment, normalized_symbols),
        sort="-bar_time_ms",
        per_page=1,
        page=1,
    )
    if not rows:
        return 0
    return int(rows[0].get("bar_time_ms", 0) or 0)


def _recent_rollup_since_ms(environment: str, normalized_symbols, intervals=None) -> int:
    api_app = _api_app()
    target_intervals = _normalize_target_intervals(api_app, intervals)
    if not target_intervals:
        return 0

    latest_5m_ms = _latest_targeted_5m_bar_ms(environment, normalized_symbols)
    if latest_5m_ms <= 0:
        return 0

    max_interval_ms = max(interval_to_ms(interval) for interval in target_intervals)
    # Rebuild two full windows so the builder can correctly close the previous bucket
    # before emitting the next higher-timeframe bar.
    return max(0, latest_5m_ms - (max_interval_ms * 2))


def _incremental_due_intervals(latest_5m_ms: int, intervals=None) -> list[str]:
    api_app = _api_app()
    target_intervals = _normalize_target_intervals(api_app, intervals)
    if latest_5m_ms <= 0:
        return []

    due_intervals = []
    for interval in target_intervals:
        if bucket_start_ms(latest_5m_ms, interval) == int(latest_5m_ms):
            due_intervals.append(interval)
    return due_intervals


def has_interval_bars(environment: str, interval: str, symbols=None) -> bool:
    api_app = _api_app()
    normalized_interval = normalize_interval(interval)
    symbol_filter = api_app.build_symbol_filter(symbols)
    filter_parts = [
        f'interval = "{normalized_interval}"',
        api_app.build_bar_environment_filter(environment, include_legacy_empty=True),
    ]
    if symbol_filter:
        filter_parts.append(symbol_filter)
    try:
        rows = api_app.pb.get_records(
            "ibkr_bars",
            filter=" && ".join(filter_parts),
            sort="-bar_time_ms",
            per_page=1,
            page=1,
        )
        return bool(rows)
    except Exception:
        traceback.print_exc()
        return False


def rebuild_higher_timeframe_bars(environment: str, symbols=None, intervals=None, since_ms: int | None = None) -> dict:
    api_app = _api_app()
    normalized_symbols = api_app.normalize_symbols(symbols)
    target_intervals = _normalize_target_intervals(api_app, intervals)
    base_rows = api_app.pb.get_all_records(
        "ibkr_bars",
        filter=_build_rollup_filter(api_app, environment, normalized_symbols, since_ms=since_ms),
        sort="bar_time_ms",
        max_pages=1000,
    )
    if not base_rows:
        return {
            "processed_5m": 0,
            "written": 0,
            "errors": 0,
            "symbols": normalized_symbols,
            "intervals": target_intervals,
            "since_ms": int(since_ms or 0),
        }

    builder = TimeframeBarBuilder(target_intervals=target_intervals)
    batch = []
    written = 0
    errors = 0

    rows = sorted(
        base_rows,
        key=lambda item: (int(item.get("bar_time_ms", 0) or 0), str(item.get("symbol", "")).upper()),
    )

    def flush_batch():
        nonlocal written, errors, batch
        if not batch:
            return
        try:
            result = api_app.pb.upsert_bars(batch)
            if result.get("ok", False):
                written += int(result.get("created", 0) or 0) + int(result.get("updated", 0) or 0)
            else:
                errors += len(batch)
        except Exception:
            errors += len(batch)
            traceback.print_exc()
        batch = []

    for row in rows:
        base_bar = api_app.normalize_bar_environment(row, environment)
        for derived_bar in builder.consume(base_bar):
            batch.append(api_app.normalize_bar_environment(derived_bar, environment))
            if len(batch) >= api_app.ROLLUP_BATCH_SIZE:
                flush_batch()

    flush_batch()
    return {
        "processed_5m": len(rows),
        "written": written,
        "errors": errors,
        "symbols": normalized_symbols,
        "intervals": target_intervals,
        "since_ms": int(since_ms or 0),
    }


def ensure_higher_timeframe_bars(
    environments,
    force: bool = False,
    symbols=None,
    incremental: bool = False,
    intervals=None,
):
    api_app = _api_app()
    normalized_symbols = api_app.normalize_symbols(symbols)
    target_intervals = _normalize_target_intervals(api_app, intervals)
    results = {}
    for environment in environments:
        if not target_intervals:
            results[environment] = {
                "skipped": True,
                "reason": "no_target_intervals",
                "written": 0,
                "errors": 0,
                "intervals": [],
            }
            continue

        if normalized_symbols:
            effective_intervals = list(target_intervals)
            if incremental:
                latest_5m_ms = _latest_targeted_5m_bar_ms(environment, normalized_symbols)
                if latest_5m_ms <= 0:
                    results[environment] = {
                        "skipped": True,
                        "reason": "no_recent_5m",
                        "written": 0,
                        "errors": 0,
                        "intervals": [],
                    }
                    continue
                effective_intervals = _incremental_due_intervals(
                    latest_5m_ms,
                    intervals=target_intervals,
                )
                if not effective_intervals:
                    results[environment] = {
                        "skipped": True,
                        "reason": "no_due_intervals",
                        "written": 0,
                        "errors": 0,
                        "intervals": [],
                    }
                    continue
                since_ms = _recent_rollup_since_ms(
                    environment,
                    normalized_symbols,
                    intervals=effective_intervals,
                )
            else:
                since_ms = None
            rollup_result = rebuild_higher_timeframe_bars(
                environment,
                symbols=normalized_symbols,
                intervals=effective_intervals,
                since_ms=since_ms,
            )
            rollup_result["targeted"] = True
            rollup_result["incremental"] = bool(incremental and since_ms)
            results[environment] = rollup_result
            continue

        if not force and environment in api_app.rollup_bootstrap_checked:
            results[environment] = {
                "skipped": True,
                "reason": "already_checked",
                "written": 0,
                "errors": 0,
                "intervals": target_intervals,
            }
            continue

        missing_intervals = [
            interval for interval in target_intervals if force or not has_interval_bars(environment, interval)
        ]
        if not missing_intervals:
            api_app.rollup_bootstrap_checked.add(environment)
            results[environment] = {
                "skipped": True,
                "reason": "already_present",
                "written": 0,
                "errors": 0,
                "intervals": target_intervals,
            }
            continue

        rollup_result = rebuild_higher_timeframe_bars(environment, intervals=target_intervals)
        rollup_result["missing_intervals"] = missing_intervals
        results[environment] = rollup_result
        api_app.rollup_bootstrap_checked.add(environment)
    return results


def fetch_interval_bars(environment: str, interval: str, symbols=None, full_scan: bool = False):
    api_app = _api_app()
    normalized_interval = normalize_interval(interval)
    symbol_filter = api_app.build_symbol_filter(symbols)
    filter_parts = [
        f'interval = "{normalized_interval}"',
        api_app.build_bar_environment_filter(environment, include_legacy_empty=True),
    ]
    if symbol_filter:
        filter_parts.append(symbol_filter)
    if not full_scan:
        filter_parts.append(f"bar_time_ms >= {get_fetch_since_ms(environment, normalized_interval)}")
    rows = api_app.pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(filter_parts),
        sort="bar_time_ms",
        max_pages=500,
    )
    if rows:
        api_app.last_interval_fetch_ms[(environment, normalized_interval)] = max(
            int(row.get("bar_time_ms", 0) or 0) for row in rows
        )
    return rows


def repair_symbol_pipeline_from_storage(environment: str, symbols) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbols = api_app.normalize_symbols(symbols)
    if not normalized_symbols:
        return {"ok": True, "symbols": [], "rollup": {}, "compute": {}, "reset": {}}

    rollup_result = rebuild_higher_timeframe_bars(
        runtime_environment,
        symbols=normalized_symbols,
        intervals=api_app.HIGHER_INTERVALS,
    )
    reset_result = reset_compute_state_for_symbols(
        runtime_environment,
        normalized_symbols,
        intervals=api_app.INTERVALS,
    )
    compute_payload = {}
    with api_app.app.test_request_context(
        "/compute",
        method="POST",
        json={
            "source": "history_repair",
            "environments": [runtime_environment],
            "symbols": normalized_symbols,
            "force_rollup": True,
        },
    ):
        response = api_app.compute()
        try:
            compute_payload = response.get_json() or {}
        except Exception:
            compute_payload = {}

    return {
        "ok": bool(compute_payload.get("ok", True)),
        "symbols": normalized_symbols,
        "rollup": rollup_result,
        "reset": reset_result,
        "compute": compute_payload,
    }
