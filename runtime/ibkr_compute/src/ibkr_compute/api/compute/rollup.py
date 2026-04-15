from __future__ import annotations

import traceback

from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import normalize_interval

from ibkr_compute.api.compute.runtime_state.runtime import _api_app
from ibkr_compute.api.compute.runtime_state.timing import get_fetch_since_ms
from .materialize import reset_compute_state_for_symbols


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


def rebuild_higher_timeframe_bars(environment: str, symbols=None, intervals=None) -> dict:
    api_app = _api_app()
    normalized_symbols = api_app.normalize_symbols(symbols)
    target_intervals = [normalize_interval(interval) for interval in (intervals or api_app.HIGHER_INTERVALS)]
    target_intervals = [interval for interval in target_intervals if interval in api_app.HIGHER_INTERVALS]
    symbol_filter = api_app.build_symbol_filter(normalized_symbols)
    filter_parts = [
        'interval = "5m"',
        api_app.build_bar_environment_filter(environment, include_legacy_empty=True),
    ]
    if symbol_filter:
        filter_parts.append(symbol_filter)
    base_rows = api_app.pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(filter_parts),
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
    }


def ensure_higher_timeframe_bars(environments, force: bool = False, symbols=None):
    api_app = _api_app()
    normalized_symbols = api_app.normalize_symbols(symbols)
    results = {}
    for environment in environments:
        if normalized_symbols:
            rollup_result = rebuild_higher_timeframe_bars(
                environment,
                symbols=normalized_symbols,
                intervals=api_app.HIGHER_INTERVALS,
            )
            rollup_result["targeted"] = True
            results[environment] = rollup_result
            continue

        if not force and environment in api_app.rollup_bootstrap_checked:
            results[environment] = {
                "skipped": True,
                "reason": "already_checked",
                "written": 0,
                "errors": 0,
            }
            continue

        missing_intervals = [
            interval for interval in api_app.HIGHER_INTERVALS if force or not has_interval_bars(environment, interval)
        ]
        if not missing_intervals:
            api_app.rollup_bootstrap_checked.add(environment)
            results[environment] = {
                "skipped": True,
                "reason": "already_present",
                "written": 0,
                "errors": 0,
            }
            continue

        rollup_result = rebuild_higher_timeframe_bars(environment)
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
