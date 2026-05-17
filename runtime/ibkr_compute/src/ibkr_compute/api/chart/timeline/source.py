from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any

from ibkr_compute.core.broker_mode import resolve_data_environment
from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite
from ibkr_compute.market.timeframe_utils import normalize_interval

from ibkr_compute.api.chart.timeline.runtime import _api_app

logger = logging.getLogger(__name__)

BAR_DIRECT_SQLITE_READ_ENABLED = str(
    os.environ.get("IBKR_BAR_DIRECT_SQLITE_READ_ENABLED", "true")
).strip().lower() in {"1", "true", "yes", "on"}
BAR_DIRECT_SQLITE_READ_FALLBACK_API_ENABLED = str(
    os.environ.get("IBKR_BAR_DIRECT_SQLITE_READ_FALLBACK_API_ENABLED", "true")
).strip().lower() in {"1", "true", "yes", "on"}
BAR_DIRECT_SQLITE_READ_TIMEOUT_SECONDS = max(
    1.0,
    float(os.environ.get("IBKR_BAR_DIRECT_SQLITE_READ_TIMEOUT_SECONDS", "30.0")),
)


def _cfg_bool(api_app, environment: str, key: str, fallback: bool) -> bool:
    cfg = getattr(api_app, "cfg", None)
    if cfg is None or not hasattr(cfg, "get_bool_for_environment"):
        return fallback
    try:
        return bool(cfg.get_bool_for_environment(key, environment, fallback))
    except Exception:
        return fallback


def _cfg_float(api_app, environment: str, key: str, fallback: float) -> float:
    cfg = getattr(api_app, "cfg", None)
    if cfg is None or not hasattr(cfg, "get_float_for_environment"):
        return fallback
    try:
        return float(cfg.get_float_for_environment(key, environment, fallback))
    except Exception:
        return fallback


def _direct_sqlite_read_enabled(api_app, environment: str) -> bool:
    return _cfg_bool(
        api_app,
        environment,
        "ibkr_bar_direct_sqlite_read_enabled",
        BAR_DIRECT_SQLITE_READ_ENABLED,
    )


def _direct_sqlite_read_fallback_api_enabled(api_app, environment: str) -> bool:
    return _cfg_bool(
        api_app,
        environment,
        "ibkr_bar_direct_sqlite_read_fallback_api_enabled",
        BAR_DIRECT_SQLITE_READ_FALLBACK_API_ENABLED,
    )


def _direct_sqlite_read_timeout(api_app, environment: str) -> float:
    return max(
        1.0,
        _cfg_float(
            api_app,
            environment,
            "ibkr_bar_direct_sqlite_read_timeout_sec",
            BAR_DIRECT_SQLITE_READ_TIMEOUT_SECONDS,
        ),
    )


def _bar_environment_sql(environment: str, *, include_legacy_empty: bool = True) -> tuple[str, list[Any]]:
    data_environment = resolve_data_environment(environment)
    values: list[Any] = [data_environment]
    if include_legacy_empty and data_environment == "live":
        values.append("")
    if len(values) == 1:
        return "environment = ?", values
    placeholders = ", ".join("?" for _ in values)
    return f"environment IN ({placeholders})", values


def _sqlite_bar_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    payload = dict(row)
    raw_extra = payload.get("extra")
    if isinstance(raw_extra, str) and raw_extra.strip():
        try:
            payload["extra"] = json.loads(raw_extra)
        except Exception:
            payload["extra"] = {}
    elif raw_extra is None:
        payload["extra"] = {}
    return payload


def _fetch_chart_visible_rows_sqlite(
    conn: sqlite3.Connection,
    *,
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int,
) -> list[dict[str, Any]]:
    env_sql, env_params = _bar_environment_sql(environment, include_legacy_empty=True)
    where_parts = [
        "symbol = ?",
        "interval = ?",
        env_sql,
    ]
    params: list[Any] = [
        str(symbol or "").strip().upper(),
        normalize_interval(interval),
        *env_params,
    ]
    if int(end_ms or 0) > 0:
        where_parts.append("bar_time_ms <= ?")
        params.append(int(end_ms or 0))
    if int(start_ms or 0) > 0:
        where_parts.append("bar_time_ms >= ?")
        params.append(int(start_ms or 0))
    params.append(max(1, int(limit or 1)))

    rows = conn.execute(
        f"""
        SELECT id, symbol, exchange, interval, open, high, low, close, volume,
               session_type, us_time, cn_time, bar_time_ms, extra, environment,
               created, updated
        FROM ibkr_bars
        WHERE {' AND '.join(where_parts)}
        ORDER BY bar_time_ms
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_sqlite_bar_row_to_dict(row) for row in rows]


def _fetch_chart_warmup_rows_sqlite(
    conn: sqlite3.Connection,
    *,
    environment: str,
    symbol: str,
    interval: str,
    end_ms: int,
    anchor_ms: int,
    limit: int,
) -> list[dict[str, Any]]:
    if int(limit or 0) <= 0 or int(anchor_ms or 0) <= 0:
        return []
    env_sql, env_params = _bar_environment_sql(environment, include_legacy_empty=True)
    where_parts = [
        "symbol = ?",
        "interval = ?",
        env_sql,
        "bar_time_ms < ?",
    ]
    params: list[Any] = [
        str(symbol or "").strip().upper(),
        normalize_interval(interval),
        *env_params,
        int(anchor_ms or 0),
    ]
    if int(end_ms or 0) > 0:
        where_parts.append("bar_time_ms <= ?")
        params.append(int(end_ms or 0))
    params.append(max(1, int(limit or 1)))

    rows = conn.execute(
        f"""
        SELECT id, symbol, exchange, interval, open, high, low, close, volume,
               session_type, us_time, cn_time, bar_time_ms, extra, environment,
               created, updated
        FROM ibkr_bars
        WHERE {' AND '.join(where_parts)}
        ORDER BY bar_time_ms DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_sqlite_bar_row_to_dict(row) for row in reversed(rows)]


def _load_chart_timeline_source_bars_sqlite(
    api_app,
    *,
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    warmup_bars: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with open_pb_sqlite(readonly=True, timeout=_direct_sqlite_read_timeout(api_app, environment)) as conn:
        visible_pages = max(1, (api_app.CHART_TIMELINE_VISIBLE_LIMIT + 199) // 200 + 1)
        visible_rows = _fetch_chart_visible_rows_sqlite(
            conn,
            environment=environment,
            symbol=symbol,
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=visible_pages * 200,
        )
        if len(visible_rows) > api_app.CHART_TIMELINE_VISIBLE_LIMIT:
            visible_rows = visible_rows[-api_app.CHART_TIMELINE_VISIBLE_LIMIT:]
        warmup_anchor_ms = int(visible_rows[0].get("bar_time_ms", 0) or 0) if visible_rows else 0
        warmup_rows = _fetch_chart_warmup_rows_sqlite(
            conn,
            environment=environment,
            symbol=symbol,
            interval=interval,
            end_ms=end_ms,
            anchor_ms=warmup_anchor_ms,
            limit=warmup_bars,
        )
    return visible_rows, warmup_rows


def _load_chart_timeline_source_bars_api(
    api_app,
    *,
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    warmup_bars: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    visible_pages = max(1, (api_app.CHART_TIMELINE_VISIBLE_LIMIT + 199) // 200 + 1)
    warmup_pages = max(1, (warmup_bars + 199) // 200 + 1)

    base_filter_parts = [
        f'symbol = "{symbol}"',
        f'interval = "{interval}"',
        api_app.build_bar_environment_filter(environment, include_legacy_empty=True),
    ]
    if end_ms > 0:
        base_filter_parts.append(f"bar_time_ms <= {int(end_ms)}")

    visible_filter_parts = list(base_filter_parts)
    if start_ms > 0:
        visible_filter_parts.append(f"bar_time_ms >= {int(start_ms)}")

    visible_rows = api_app.pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(visible_filter_parts),
        sort="bar_time_ms",
        max_pages=visible_pages,
    )
    if len(visible_rows) > api_app.CHART_TIMELINE_VISIBLE_LIMIT:
        visible_rows = visible_rows[-api_app.CHART_TIMELINE_VISIBLE_LIMIT:]

    warmup_rows = []
    warmup_anchor_ms = int(visible_rows[0].get("bar_time_ms", 0) or 0) if visible_rows else 0
    if warmup_bars > 0 and warmup_anchor_ms > 0:
        warmup_filter_parts = list(base_filter_parts)
        warmup_filter_parts.append(f"bar_time_ms < {warmup_anchor_ms}")
        warmup_rows = api_app.pb.get_all_records(
            "ibkr_bars",
            filter=" && ".join(warmup_filter_parts),
            sort="-bar_time_ms",
            max_pages=warmup_pages,
        )
        warmup_rows = list(reversed(warmup_rows[:warmup_bars]))
    return visible_rows, warmup_rows


def load_chart_timeline_source_bars(
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    api_app = _api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    warmup_bars = int(api_app.BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 260) or 260)
    visible_rows: list[dict[str, Any]] = []
    warmup_rows: list[dict[str, Any]] = []
    sqlite_loaded = False
    if _direct_sqlite_read_enabled(api_app, runtime_environment):
        try:
            visible_rows, warmup_rows = _load_chart_timeline_source_bars_sqlite(
                api_app,
                environment=runtime_environment,
                symbol=normalized_symbol,
                interval=normalized_interval,
                start_ms=start_ms,
                end_ms=end_ms,
                warmup_bars=warmup_bars,
            )
            sqlite_loaded = True
        except Exception as exc:
            if not _direct_sqlite_read_fallback_api_enabled(api_app, runtime_environment):
                logger.warning(
                    "Failed to load chart timeline bars via SQLite for %s/%s: %s",
                    normalized_symbol,
                    normalized_interval,
                    exc,
                )
                raise
            logger.debug(
                "Direct SQLite chart timeline bar read failed for %s/%s, falling back to PocketBase API: %s",
                normalized_symbol,
                normalized_interval,
                exc,
            )

    if not sqlite_loaded:
        visible_rows, warmup_rows = _load_chart_timeline_source_bars_api(
            api_app,
            environment=runtime_environment,
            symbol=normalized_symbol,
            interval=normalized_interval,
            start_ms=start_ms,
            end_ms=end_ms,
            warmup_bars=warmup_bars,
        )

    return {
        "source_rows": warmup_rows + visible_rows,
        "visible_rows": visible_rows,
        "warmup_limit": warmup_bars,
        "warmup_used": len(warmup_rows),
    }


def build_chart_source_window_from_rows(
    rows,
    interval: str,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict:
    api_app = _api_app()
    normalized_interval = normalize_interval(interval)
    warmup_bars = int(api_app.BOOTSTRAP_LOOKBACK_BARS.get(normalized_interval, 260) or 260)
    deduped = {}
    for row in rows or []:
        bar_ms = int((row or {}).get("bar_time_ms", 0) or 0)
        if bar_ms <= 0:
            continue
        if end_ms > 0 and bar_ms > int(end_ms):
            continue
        deduped[bar_ms] = dict(row)

    ordered_rows = [deduped[bar_ms] for bar_ms in sorted(deduped)]
    visible_rows = ordered_rows
    if start_ms > 0:
        visible_rows = [
            row for row in visible_rows
            if int(row.get("bar_time_ms", 0) or 0) >= int(start_ms)
        ]
    if len(visible_rows) > api_app.CHART_TIMELINE_VISIBLE_LIMIT:
        visible_rows = visible_rows[-api_app.CHART_TIMELINE_VISIBLE_LIMIT:]

    warmup_anchor_ms = int(visible_rows[0].get("bar_time_ms", 0) or 0) if visible_rows else 0
    warmup_rows = []
    if warmup_bars > 0 and warmup_anchor_ms > 0:
        warmup_rows = [
            row for row in ordered_rows
            if int(row.get("bar_time_ms", 0) or 0) < warmup_anchor_ms
        ][-warmup_bars:]

    return {
        "source_rows": warmup_rows + visible_rows,
        "visible_rows": visible_rows,
        "warmup_limit": warmup_bars,
        "warmup_used": len(warmup_rows),
    }


__all__ = [
    "build_chart_source_window_from_rows",
    "load_chart_timeline_source_bars",
]
