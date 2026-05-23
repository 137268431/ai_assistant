from __future__ import annotations

import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from ibkr_compute.core.broker_mode import resolve_data_environment
from ibkr_compute.market.bar_freshness import expected_closed_ms_from_latest_5m
from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite, upsert_bars
from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import bar_close_ms, bucket_start_ms, interval_to_ms, normalize_interval

from ibkr_compute.api.compute.runtime_state.runtime import _api_app
from ibkr_compute.api.compute.runtime_state.timing import get_fetch_since_ms
from .materialize import reset_compute_state_for_symbols


_ROLLUP_WRITE_LOCK = threading.Lock()


BAR_COLUMNS = """
id, symbol, exchange, interval, open, high, low, close, volume,
session_type, us_time, cn_time, bar_time_ms, extra, environment,
created, updated
"""


def _normalize_target_intervals(api_app, intervals=None) -> list[str]:
    source_intervals = api_app.HIGHER_INTERVALS if intervals is None else intervals
    target_intervals = [normalize_interval(interval) for interval in source_intervals]
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


def _runtime_environment(environment: str) -> str:
    return resolve_data_environment(environment)


def _cfg_bool(api_app, key: str, environment: str, default: bool) -> bool:
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_bool_for_environment"):
        try:
            return bool(cfg.get_bool_for_environment(key, environment, default))
        except Exception:
            return bool(default)
    return bool(default)


def _cfg_float(api_app, key: str, environment: str, default: float) -> float:
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_float_for_environment"):
        try:
            return float(cfg.get_float_for_environment(key, environment, default))
        except Exception:
            return float(default)
    return float(default)


def _cfg_int(api_app, key: str, environment: str, default: int) -> int:
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_int_for_environment"):
        try:
            return int(cfg.get_int_for_environment(key, environment, default))
        except Exception:
            return int(default)
    return int(default)


def _direct_sqlite_read_enabled(api_app, environment: str) -> bool:
    return _cfg_bool(api_app, "ibkr_bar_direct_sqlite_read_enabled", environment, True)


def _direct_sqlite_read_fallback_api_enabled(api_app, environment: str) -> bool:
    return _cfg_bool(api_app, "ibkr_bar_direct_sqlite_read_fallback_api_enabled", environment, True)


def _direct_sqlite_write_enabled(api_app, environment: str) -> bool:
    return _cfg_bool(api_app, "ibkr_bar_direct_sqlite_enabled", environment, True)


def _direct_sqlite_write_fallback_api_enabled(api_app, environment: str) -> bool:
    return _cfg_bool(api_app, "ibkr_bar_direct_sqlite_fallback_api_enabled", environment, True)


def _direct_sqlite_read_timeout(api_app, environment: str) -> float:
    fallback = _cfg_float(api_app, "ibkr_bar_direct_sqlite_timeout_sec", environment, 30.0)
    return max(0.5, _cfg_float(api_app, "ibkr_bar_direct_sqlite_read_timeout_sec", environment, fallback))


def _direct_sqlite_write_timeout(api_app, environment: str) -> float:
    return max(1.0, _cfg_float(api_app, "ibkr_bar_direct_sqlite_timeout_sec", environment, 30.0))


def _rollup_parallel_enabled(api_app, environment: str) -> bool:
    return _cfg_bool(api_app, "ibkr_rollup_parallel_enabled", environment, True)


def _rollup_max_workers(api_app, environment: str, target_intervals: list[str]) -> int:
    configured = _cfg_int(api_app, "ibkr_rollup_max_workers", environment, 5)
    return max(1, min(5, len(target_intervals or []), int(configured or 1)))


def _bar_environment_sql(environment: str, *, include_legacy_empty: bool = True) -> tuple[str, list]:
    runtime_environment = _runtime_environment(environment)
    if include_legacy_empty and runtime_environment == "live":
        return "environment IN (?, ?)", [runtime_environment, ""]
    return "environment = ?", [runtime_environment]


def _symbol_sql(api_app, symbols) -> tuple[str, list]:
    normalized_symbols = api_app.normalize_symbols(symbols)
    if not normalized_symbols:
        return "", []
    placeholders = ", ".join("?" for _ in normalized_symbols)
    return f"symbol IN ({placeholders})", list(normalized_symbols)


def _chunked(items: list[str], size: int = 50) -> list[list[str]]:
    safe_size = max(1, int(size or 50))
    return [items[index : index + safe_size] for index in range(0, len(items), safe_size)]


def _sqlite_bar_row(row) -> dict:
    payload = dict(row) if row is not None else {}
    extra = payload.get("extra")
    if isinstance(extra, str) and extra.strip():
        try:
            payload["extra"] = json.loads(extra)
        except Exception:
            payload["extra"] = {}
        if not isinstance(payload["extra"], dict):
            payload["extra"] = {}
    elif not isinstance(extra, dict):
        payload["extra"] = {}
    return payload


def _fetch_bars_from_sqlite(
    api_app,
    environment: str,
    interval: str,
    *,
    symbols=None,
    since_ms: int | None = None,
    sort: str = "bar_time_ms",
    limit: int = 0,
) -> list[dict]:
    normalized_interval = normalize_interval(interval)
    env_sql, env_params = _bar_environment_sql(environment, include_legacy_empty=True)
    where_parts = [
        "interval = ?",
        env_sql,
    ]
    params = [normalized_interval, *env_params]
    symbol_sql, symbol_params = _symbol_sql(api_app, symbols)
    if symbol_sql:
        where_parts.append(symbol_sql)
        params.extend(symbol_params)
    if since_ms is not None and int(since_ms or 0) > 0:
        where_parts.append("bar_time_ms >= ?")
        params.append(int(since_ms or 0))

    order_clause = "bar_time_ms DESC" if str(sort or "").strip() == "-bar_time_ms" else "bar_time_ms ASC"
    limit_clause = ""
    if int(limit or 0) > 0:
        limit_clause = " LIMIT ?"
        params.append(int(limit or 0))

    conn = open_pb_sqlite(readonly=True, timeout=_direct_sqlite_read_timeout(api_app, environment))
    try:
        rows = conn.execute(
            f"""
            SELECT {BAR_COLUMNS}
            FROM ibkr_bars
            WHERE {' AND '.join(where_parts)}
            ORDER BY {order_clause}
            {limit_clause}
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()
    return [_sqlite_bar_row(row) for row in rows]


def _has_bars_in_sqlite(api_app, environment: str, interval: str, symbols=None) -> bool:
    normalized_interval = normalize_interval(interval)
    env_sql, env_params = _bar_environment_sql(environment, include_legacy_empty=True)
    where_parts = [
        "interval = ?",
        env_sql,
    ]
    params = [normalized_interval, *env_params]
    symbol_sql, symbol_params = _symbol_sql(api_app, symbols)
    if symbol_sql:
        where_parts.append(symbol_sql)
        params.extend(symbol_params)

    conn = open_pb_sqlite(readonly=True, timeout=_direct_sqlite_read_timeout(api_app, environment))
    try:
        row = conn.execute(
            f"""
            SELECT 1
            FROM ibkr_bars
            WHERE {' AND '.join(where_parts)}
            ORDER BY bar_time_ms DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _current_interval_symbols_from_sqlite(
    api_app,
    environment: str,
    interval: str,
    symbols,
    expected_ms: int,
) -> set[str]:
    normalized_symbols = api_app.normalize_symbols(symbols)
    if not normalized_symbols:
        return set()
    normalized_interval = normalize_interval(interval)
    env_sql, env_params = _bar_environment_sql(environment, include_legacy_empty=True)
    where_parts = [
        "interval = ?",
        env_sql,
        "bar_time_ms >= ?",
    ]
    params = [normalized_interval, *env_params, int(expected_ms or 0)]
    symbol_sql, symbol_params = _symbol_sql(api_app, normalized_symbols)
    if symbol_sql:
        where_parts.append(symbol_sql)
        params.extend(symbol_params)

    conn = open_pb_sqlite(readonly=True, timeout=_direct_sqlite_read_timeout(api_app, environment))
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT symbol
            FROM ibkr_bars
            WHERE {' AND '.join(where_parts)}
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()
    current_symbols = set()
    for row in rows or []:
        try:
            symbol_value = row["symbol"] if hasattr(row, "keys") else row[0]
        except Exception:
            symbol_value = ""
        symbol = str(symbol_value or "").strip().upper()
        if symbol:
            current_symbols.add(symbol)
    return current_symbols


def _current_interval_symbols_from_api(
    api_app,
    environment: str,
    interval: str,
    symbols,
    expected_ms: int,
) -> set[str]:
    normalized_symbols = api_app.normalize_symbols(symbols)
    if not normalized_symbols:
        return set()
    pb = getattr(api_app, "pb", None)
    if pb is None or not hasattr(pb, "get_records"):
        return set()

    normalized_interval = normalize_interval(interval)
    current_symbols: set[str] = set()
    for chunk in _chunked(normalized_symbols):
        symbol_filter = api_app.build_symbol_filter(chunk)
        filter_parts = [
            f'interval = "{normalized_interval}"',
            api_app.build_bar_environment_filter(environment, include_legacy_empty=True),
            f"bar_time_ms >= {int(expected_ms or 0)}",
        ]
        if symbol_filter:
            filter_parts.append(symbol_filter)
        rows = pb.get_records(
            "ibkr_bars",
            filter=" && ".join(filter_parts),
            sort="-bar_time_ms",
            per_page=max(200, len(chunk) * 2),
            page=1,
        )
        for row in rows or []:
            symbol = str((row or {}).get("symbol") or "").strip().upper()
            if symbol:
                current_symbols.add(symbol)
    return current_symbols


def _all_symbols_current_for_interval(
    api_app,
    environment: str,
    interval: str,
    symbols,
    expected_ms: int,
) -> bool:
    normalized_symbols = set(api_app.normalize_symbols(symbols))
    if not normalized_symbols or int(expected_ms or 0) <= 0:
        return True

    if _direct_sqlite_read_enabled(api_app, environment):
        try:
            current_symbols = _current_interval_symbols_from_sqlite(
                api_app,
                environment,
                interval,
                sorted(normalized_symbols),
                expected_ms,
            )
            return normalized_symbols.issubset(current_symbols)
        except Exception:
            if not _direct_sqlite_read_fallback_api_enabled(api_app, environment):
                return True

    try:
        current_symbols = _current_interval_symbols_from_api(
            api_app,
            environment,
            interval,
            sorted(normalized_symbols),
            expected_ms,
        )
        return normalized_symbols.issubset(current_symbols)
    except Exception:
        return True


def _stale_target_intervals(environment: str, normalized_symbols, latest_5m_ms: int, intervals=None) -> list[str]:
    api_app = _api_app()
    environment = _runtime_environment(environment)
    target_intervals = _normalize_target_intervals(api_app, intervals)
    symbols = api_app.normalize_symbols(normalized_symbols)
    if not symbols or int(latest_5m_ms or 0) <= 0:
        return []

    stale_intervals = []
    for interval in target_intervals:
        expected_groups: dict[int, list[str]] = {}
        for symbol in symbols:
            expected_ms = expected_closed_ms_from_latest_5m(
                int(latest_5m_ms or 0),
                interval,
                symbol=symbol,
            )
            if expected_ms > 0:
                expected_groups.setdefault(expected_ms, []).append(symbol)
        for expected_ms, group_symbols in expected_groups.items():
            if not _all_symbols_current_for_interval(api_app, environment, interval, group_symbols, expected_ms):
                stale_intervals.append(interval)
                break
    return stale_intervals


def _write_rollup_batch(api_app, environment: str, batch: list[dict]) -> dict:
    if _direct_sqlite_write_enabled(api_app, environment):
        try:
            conn = open_pb_sqlite(readonly=False, timeout=_direct_sqlite_write_timeout(api_app, environment))
            try:
                with conn:
                    written = upsert_bars(conn, batch)
            finally:
                conn.close()
            return {
                "ok": True,
                "created": int(written or 0),
                "updated": 0,
                "skipped": max(0, len(batch) - int(written or 0)),
                "write_path": "direct_sqlite",
            }
        except Exception:
            if not _direct_sqlite_write_fallback_api_enabled(api_app, environment):
                traceback.print_exc()
                return {
                    "ok": False,
                    "created": 0,
                    "updated": 0,
                    "skipped": len(batch),
                    "write_path": "direct_sqlite",
                }

    result = api_app.pb.upsert_bars(batch)
    if isinstance(result, dict):
        result.setdefault("write_path", "pocketbase_api")
        return result
    return {"ok": False, "created": 0, "updated": 0, "skipped": len(batch), "write_path": "pocketbase_api"}


def _latest_targeted_5m_bar_ms(environment: str, normalized_symbols) -> int:
    api_app = _api_app()
    environment = _runtime_environment(environment)
    normalized_symbols = api_app.normalize_symbols(normalized_symbols)
    if not normalized_symbols:
        interval_key = (environment, "5m")
        cached_ms = int(api_app.last_interval_fetch_ms.get(interval_key, 0) or 0)
        if cached_ms > 0:
            return cached_ms

    if _direct_sqlite_read_enabled(api_app, environment):
        try:
            rows = _fetch_bars_from_sqlite(
                api_app,
                environment,
                "5m",
                symbols=normalized_symbols,
                sort="-bar_time_ms",
                limit=1,
            )
            if rows:
                return int(rows[0].get("bar_time_ms", 0) or 0)
            return 0
        except Exception:
            if not _direct_sqlite_read_fallback_api_enabled(api_app, environment):
                traceback.print_exc()
                return 0

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
    environment = _runtime_environment(environment)
    target_intervals = _normalize_target_intervals(api_app, intervals)
    if not target_intervals:
        return 0

    latest_5m_ms = _latest_targeted_5m_bar_ms(environment, normalized_symbols)
    if latest_5m_ms <= 0:
        return 0

    since_candidates = []
    non_daily_intervals = [interval for interval in target_intervals if normalize_interval(interval) != "1d"]
    if non_daily_intervals:
        max_interval_ms = max(interval_to_ms(interval) for interval in non_daily_intervals)
        # Rebuild two full windows so the builder can correctly close the previous bucket
        # before emitting the next higher-timeframe bar.
        since_candidates.append(max(0, latest_5m_ms - (max_interval_ms * 2)))
    if "1d" in target_intervals:
        # Daily rollup is synthetic extended-session daily. Start at the expected
        # daily bucket so a bounded incremental rebuild never overwrites a daily
        # bar with only the last few 5m components from a previous day.
        daily_symbols = api_app.normalize_symbols(normalized_symbols) or [""]
        daily_candidates = [
            expected_closed_ms_from_latest_5m(latest_5m_ms, "1d", symbol=symbol)
            for symbol in daily_symbols
        ]
        since_candidates.append(min([value for value in daily_candidates if int(value or 0) > 0] or [0]))
    return min([value for value in since_candidates if int(value or 0) > 0] or [0])


def _incremental_due_intervals(latest_5m_ms: int, intervals=None, symbols=None) -> list[str]:
    api_app = _api_app()
    target_intervals = _normalize_target_intervals(api_app, intervals)
    if latest_5m_ms <= 0:
        return []

    latest_5m_close_ms = int(latest_5m_ms) + interval_to_ms("5m")
    due_symbols = api_app.normalize_symbols(symbols) if symbols else []
    if not isinstance(due_symbols, (list, tuple, set)):
        due_symbols = [str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()]
    due_symbols = due_symbols or [""]
    due_intervals = []
    for interval in target_intervals:
        current_bucket_ms = bucket_start_ms(int(latest_5m_ms), interval)
        if any(
            latest_5m_close_ms >= bar_close_ms(current_bucket_ms, interval, symbol=symbol)
            for symbol in due_symbols
        ):
            due_intervals.append(interval)
    return due_intervals


def has_interval_bars(environment: str, interval: str, symbols=None) -> bool:
    api_app = _api_app()
    environment = _runtime_environment(environment)
    normalized_interval = normalize_interval(interval)
    if _direct_sqlite_read_enabled(api_app, environment):
        try:
            return _has_bars_in_sqlite(api_app, environment, normalized_interval, symbols=symbols)
        except Exception:
            if not _direct_sqlite_read_fallback_api_enabled(api_app, environment):
                traceback.print_exc()
                return False

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


def _flush_rollup_batch(api_app, environment: str, batch: list[dict], *, use_write_lock: bool) -> dict:
    if not batch:
        return {"ok": True, "created": 0, "updated": 0, "skipped": 0}
    if use_write_lock:
        with _ROLLUP_WRITE_LOCK:
            return _write_rollup_batch(api_app, environment, batch)
    return _write_rollup_batch(api_app, environment, batch)


def _rollup_rows_for_intervals(
    api_app,
    environment: str,
    rows: list[dict],
    target_intervals: list[str],
    *,
    use_write_lock: bool = False,
) -> dict:
    builder = TimeframeBarBuilder(target_intervals=target_intervals)
    batch = []
    written = 0
    errors = 0

    def flush_batch():
        nonlocal written, errors, batch
        if not batch:
            return
        try:
            result = _flush_rollup_batch(api_app, environment, batch, use_write_lock=use_write_lock)
            if result.get("ok", False):
                written += int(result.get("created", 0) or 0) + int(result.get("updated", 0) or 0)
            else:
                errors += len(batch)
        except Exception:
            errors += len(batch)
            traceback.print_exc()
        batch = []

    for row in rows:
        for derived_bar in builder.consume(row):
            batch.append(api_app.normalize_bar_environment(derived_bar, environment))
            if len(batch) >= api_app.ROLLUP_BATCH_SIZE:
                flush_batch()

    flush_batch()
    return {
        "processed_5m": len(rows),
        "written": written,
        "errors": errors,
        "intervals": list(target_intervals),
    }


def _parallel_rollup_rows_by_interval(
    api_app,
    environment: str,
    rows: list[dict],
    target_intervals: list[str],
    worker_count: int,
) -> dict:
    interval_results = {}
    written = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="rollup-interval") as executor:
        future_map = {
            executor.submit(
                _rollup_rows_for_intervals,
                api_app,
                environment,
                rows,
                [interval],
                use_write_lock=True,
            ): interval
            for interval in target_intervals
        }
        for future in as_completed(future_map):
            interval = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                traceback.print_exc()
                result = {
                    "processed_5m": len(rows),
                    "written": 0,
                    "errors": 1,
                    "intervals": [interval],
                    "error": str(exc),
                }
            interval_results[interval] = result
            written += int(result.get("written", 0) or 0)
            errors += int(result.get("errors", 0) or 0)

    return {
        "processed_5m": len(rows),
        "written": written,
        "errors": errors,
        "parallel": True,
        "workers": worker_count,
        "interval_results": {interval: interval_results.get(interval, {}) for interval in target_intervals},
    }


def rebuild_higher_timeframe_bars(environment: str, symbols=None, intervals=None, since_ms: int | None = None) -> dict:
    api_app = _api_app()
    environment = _runtime_environment(environment)
    normalized_symbols = api_app.normalize_symbols(symbols)
    target_intervals = _normalize_target_intervals(api_app, intervals)
    if not target_intervals:
        return {
            "processed_5m": 0,
            "written": 0,
            "errors": 0,
            "symbols": normalized_symbols,
            "intervals": [],
            "since_ms": int(since_ms or 0),
            "parallel": False,
            "workers": 0,
            "interval_results": {},
        }
    base_rows = []
    read_from_api = not _direct_sqlite_read_enabled(api_app, environment)
    if not read_from_api:
        try:
            base_rows = _fetch_bars_from_sqlite(
                api_app,
                environment,
                "5m",
                symbols=normalized_symbols,
                since_ms=since_ms,
                sort="bar_time_ms",
            )
        except Exception:
            if not _direct_sqlite_read_fallback_api_enabled(api_app, environment):
                traceback.print_exc()
                base_rows = []
            else:
                base_rows = []
                read_from_api = True

    if read_from_api:
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
            "parallel": False,
            "workers": 0,
            "interval_results": {},
        }

    rows = [
        api_app.normalize_bar_environment(row, environment)
        for row in sorted(
            base_rows,
            key=lambda item: (int(item.get("bar_time_ms", 0) or 0), str(item.get("symbol", "")).upper()),
        )
    ]

    worker_count = _rollup_max_workers(api_app, environment, target_intervals)
    use_parallel = (
        len(target_intervals) > 1
        and worker_count > 1
        and _rollup_parallel_enabled(api_app, environment)
    )
    if use_parallel:
        result = _parallel_rollup_rows_by_interval(api_app, environment, rows, target_intervals, worker_count)
    else:
        result = _rollup_rows_for_intervals(
            api_app,
            environment,
            rows,
            target_intervals,
            use_write_lock=False,
        )
        result["parallel"] = False
        result["workers"] = 1 if target_intervals else 0
        result["interval_results"] = {}

    result.update(
        {
            "symbols": normalized_symbols,
            "intervals": target_intervals,
            "since_ms": int(since_ms or 0),
        }
    )
    return result


def ensure_higher_timeframe_bars(
    environments,
    force: bool = False,
    symbols=None,
    incremental: bool = False,
    intervals=None,
    since_ms: int | None = None,
):
    api_app = _api_app()
    normalized_symbols = api_app.normalize_symbols(symbols)
    target_intervals = _normalize_target_intervals(api_app, intervals)
    requested_since_ms = int(since_ms or 0)
    results = {}
    for environment in [_runtime_environment(item) for item in (environments or [])]:
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
            effective_since_ms = requested_since_ms if requested_since_ms > 0 else None
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
                due_intervals = _incremental_due_intervals(
                    latest_5m_ms,
                    intervals=target_intervals,
                    symbols=normalized_symbols,
                )
                stale_intervals = _stale_target_intervals(
                    environment,
                    normalized_symbols,
                    latest_5m_ms,
                    intervals=target_intervals,
                )
                combined_intervals = []
                for interval in [*due_intervals, *stale_intervals]:
                    if interval not in combined_intervals:
                        combined_intervals.append(interval)
                effective_intervals = _normalize_target_intervals(
                    api_app,
                    combined_intervals,
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
                effective_since_ms = _recent_rollup_since_ms(
                    environment,
                    normalized_symbols,
                    intervals=effective_intervals,
                )
            rollup_result = rebuild_higher_timeframe_bars(
                environment,
                symbols=normalized_symbols,
                intervals=effective_intervals,
                since_ms=effective_since_ms,
            )
            rollup_result["targeted"] = True
            rollup_result["incremental"] = bool(incremental and effective_since_ms)
            if incremental:
                rollup_result["due_intervals"] = due_intervals
                rollup_result["stale_intervals"] = stale_intervals
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
    environment = _runtime_environment(environment)
    normalized_interval = normalize_interval(interval)
    since_ms = None if full_scan else get_fetch_since_ms(environment, normalized_interval)
    rows = []
    read_from_api = not _direct_sqlite_read_enabled(api_app, environment)
    if not read_from_api:
        try:
            rows = _fetch_bars_from_sqlite(
                api_app,
                environment,
                normalized_interval,
                symbols=symbols,
                since_ms=since_ms,
                sort="bar_time_ms",
            )
        except Exception:
            if not _direct_sqlite_read_fallback_api_enabled(api_app, environment):
                traceback.print_exc()
                rows = []
            else:
                rows = []
                read_from_api = True

    if rows:
        api_app.last_interval_fetch_ms[(environment, normalized_interval)] = max(
            int(row.get("bar_time_ms", 0) or 0) for row in rows
        )
        return rows

    if not read_from_api:
        return []

    symbol_filter = api_app.build_symbol_filter(symbols)
    filter_parts = [
        f'interval = "{normalized_interval}"',
        api_app.build_bar_environment_filter(environment, include_legacy_empty=True),
    ]
    if symbol_filter:
        filter_parts.append(symbol_filter)
    if not full_scan:
        filter_parts.append(f"bar_time_ms >= {int(since_ms or 0)}")
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
    runtime_environment = _runtime_environment(environment)
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
