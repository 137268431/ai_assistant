from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlencode

from ibkr_api.orders.values import first_defined, to_float, to_int, to_text
from ibkr_api.universe.maintenance import parse_json_object
from ibkr_api.universe.today_targets_shared import (
    LIVE_ENVIRONMENT,
    TODAY_TARGET_STATUSES,
    build_bar_environment_filter,
    classify_session,
    current_market_date,
    escape_filter,
    format_cn_time,
    format_et_datetime,
    indicator_snapshot,
    interval_to_chart_tf,
    load_records_for_symbols,
    normalize_signal_record,
    normalize_symbols,
    pick_latest_signal,
    effective_target_status,
)
from ibkr_compute.api.market.screener.payload import parse_market_date_bounds_ms
from ibkr_compute.core.active_window_admission import (
    bars_remaining as _admission_bars_remaining,
    build_active_window_trace_for_bars,
    build_active_window_admission_item,
    component_groups as _admission_component_groups,
    component_rollup as _admission_component_rollup,
    side_window as _admission_side_window,
    window_state as _admission_window_state,
    window_status as _admission_window_status,
)
from ibkr_compute.market.timeframe_utils import normalize_interval


NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]

DEFAULT_SIGNAL_WINDOW_MAX_BARS = 12
DEFAULT_LIMIT = 80
TIMELINE_WARMUP_BARS = 300
TIMELINE_TODAY_MAX_PAGES = 20
TIMELINE_WARMUP_MAX_PAGES = 10
SUPPORTED_ENVIRONMENTS = {"live", "paper"}
SUPPORTED_STATUSES = {"active", "candidate", "all"}


def _status_filter(status: str) -> str:
    normalized_status = to_text(status).lower() or "active"
    if normalized_status == "all":
        return '(status = "active" || status = "candidate")'
    return f'status = "{escape_filter(normalized_status)}"'


def _config_rank(environment: Any, runtime_environment: str) -> int:
    normalized = to_text(environment).lower()
    if normalized == runtime_environment:
        return 2
    if normalized == "global":
        return 1
    if not normalized:
        return 0
    return -1


def _pick_config_value(rows: list[dict[str, Any]], key: str, runtime_environment: str) -> str:
    best_value = ""
    best_rank = -1
    for row in rows or []:
        if not isinstance(row, dict) or to_text(row.get("key")) != key:
            continue
        value = row.get("value")
        if value in (None, ""):
            continue
        rank = _config_rank(row.get("environment"), runtime_environment)
        if rank >= best_rank:
            best_rank = rank
            best_value = to_text(value)
    return best_value


def _load_config_rows(pb: Any, runtime_environment: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    get_runtime_config = getattr(pb, "get_runtime_config", None)
    if callable(get_runtime_config):
        try:
            rows = get_runtime_config(scope="all", environment=runtime_environment) or []
        except Exception:
            rows = []
    if not rows:
        get_all_records = getattr(pb, "get_all_records", None)
        if callable(get_all_records):
            try:
                rows = get_all_records("config", sort="sort_order,key", max_pages=20) or []
            except Exception:
                rows = []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _resolve_signal_window_max_bars(pb: Any, runtime_environment: str) -> int:
    value = _pick_config_value(
        _load_config_rows(pb, runtime_environment),
        "signal_window_max_bars",
        to_text(runtime_environment).lower() or LIVE_ENVIRONMENT,
    )
    try:
        return max(0, int(value or DEFAULT_SIGNAL_WINDOW_MAX_BARS))
    except Exception:
        return DEFAULT_SIGNAL_WINDOW_MAX_BARS


def _normalize_timeline_bar(row: dict[str, Any]) -> dict[str, Any]:
    bar_time_ms = to_int(row.get("bar_time_ms"), 0)
    return {
        **dict(row),
        "symbol": to_text(row.get("symbol")).upper(),
        "interval": normalize_interval(to_text(row.get("interval")) or "5m"),
        "exchange": to_text(row.get("exchange")).upper(),
        "bar_time_ms": bar_time_ms,
        "open": to_float(row.get("open")) or 0.0,
        "high": to_float(row.get("high")) or 0.0,
        "low": to_float(row.get("low")) or 0.0,
        "close": to_float(row.get("close")) or 0.0,
        "volume": to_float(row.get("volume")) or 0.0,
        "session_type": to_text(row.get("session_type")).lower() or classify_session(bar_time_ms=bar_time_ms),
        "us_time": to_text(row.get("us_time")) or format_et_datetime(bar_time_ms),
        "cn_time": to_text(row.get("cn_time")) or format_cn_time(bar_time_ms),
    }


def _dedupe_timeline_rows(rows: list[dict[str, Any]], runtime_environment: str) -> dict[str, list[dict[str, Any]]]:
    by_symbol_ms: dict[str, dict[int, dict[str, Any]]] = {}
    ranks: dict[str, dict[int, int]] = {}
    for row in rows or []:
        normalized = _normalize_timeline_bar(row)
        symbol = normalized["symbol"]
        interval = normalized["interval"]
        bar_time_ms = to_int(normalized.get("bar_time_ms"), 0)
        if not symbol or interval != "5m" or bar_time_ms <= 0:
            continue
        rank = _config_rank(normalized.get("environment"), runtime_environment)
        current_rank = ranks.setdefault(symbol, {}).get(bar_time_ms, -1)
        if rank < current_rank:
            continue
        by_symbol_ms.setdefault(symbol, {})[bar_time_ms] = normalized
        ranks[symbol][bar_time_ms] = rank
    return {
        symbol: [rows_by_ms[bar_ms] for bar_ms in sorted(rows_by_ms)]
        for symbol, rows_by_ms in by_symbol_ms.items()
    }


def _empty_active_window_summary(*, market_start_ms: int = 0, market_end_ms: int = 0) -> dict[str, Any]:
    return {
        "total": 0,
        "active_count": 0,
        "candidate_count": 0,
        "target_active_count": 0,
        "target_candidate_count": 0,
        "with_live_bar_count": 0,
        "window_active_count": 0,
        "window_valid_count": 0,
        "candidate_signal_count": 0,
        "current_candidate_signal_count": 0,
        "blocked_count": 0,
        "near_expiry_count": 0,
        "confirmed_count": 0,
        "trace_error_count": 0,
        "trace_stage_counts": {},
        "window_status_counts": {},
        "timeline_data": {
            "market_start_ms": max(0, int(market_start_ms or 0)),
            "market_end_ms": max(0, int(market_end_ms or 0)),
            "warmup_bars_per_symbol": TIMELINE_WARMUP_BARS,
            "symbols_requested": 0,
            "symbols_with_bars_count": 0,
            "symbols_with_today_bars_count": 0,
            "today_bar_count": 0,
            "warmup_bar_count": 0,
            "latest_bar_time_max_ms": 0,
            "latest_bar_time_max_us": "",
            "latest_bar_time_min_ms": 0,
            "latest_bar_time_min_us": "",
            "symbols_with_no_bars": [],
            "symbols_with_no_bars_count": 0,
        },
    }


def _increment_count(counts: dict[str, int], key: Any) -> None:
    normalized_key = to_text(key) or "none"
    counts[normalized_key] = int(counts.get(normalized_key, 0) or 0) + 1


def _timeline_environment_values(environment: str) -> list[str]:
    normalized = to_text(environment).lower() or LIVE_ENVIRONMENT
    values = [normalized]
    if normalized == LIVE_ENVIRONMENT:
        values.append("")
    return values


def _open_pb_sqlite_readonly() -> Callable[..., Any]:
    from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite

    return open_pb_sqlite


def _load_timeline_bars_by_symbol_sqlite(
    *,
    environment: str,
    symbols: list[str],
    interval: str,
    market_start_ms: int,
    market_end_ms: int,
) -> dict[str, list[dict[str, Any]]] | None:
    normalized_symbols = normalize_symbols(symbols)
    normalized_interval = normalize_interval(to_text(interval) or "5m")
    if not normalized_symbols or normalized_interval != "5m":
        return {}
    env_values = _timeline_environment_values(environment)
    symbol_placeholders = ", ".join("?" for _ in normalized_symbols)
    env_placeholders = ", ".join("?" for _ in env_values)
    columns = (
        "id, symbol, exchange, interval, open, high, low, close, volume, "
        "session_type, us_time, cn_time, bar_time_ms, extra, environment, created, updated"
    )
    env_rank_expr = "CASE WHEN environment = ? THEN 2 WHEN environment = '' THEN 0 ELSE -1 END"
    try:
        open_pb_sqlite = _open_pb_sqlite_readonly()
        with open_pb_sqlite(readonly=True, timeout=30.0) as conn:
            today_rows = conn.execute(
                f"""
                WITH candidates AS (
                    SELECT
                        {columns},
                        ROW_NUMBER() OVER (
                            PARTITION BY symbol, bar_time_ms
                            ORDER BY {env_rank_expr} DESC, updated DESC
                        ) AS env_rank
                    FROM ibkr_bars
                    WHERE interval = ?
                      AND symbol IN ({symbol_placeholders})
                      AND environment IN ({env_placeholders})
                      AND bar_time_ms >= ?
                      AND bar_time_ms < ?
                )
                SELECT {columns}
                FROM candidates
                WHERE env_rank = 1
                ORDER BY symbol ASC, bar_time_ms ASC
                """,
                (
                    to_text(environment).lower() or LIVE_ENVIRONMENT,
                    normalized_interval,
                    *normalized_symbols,
                    *env_values,
                    int(market_start_ms or 0),
                    int(market_end_ms or 0),
                ),
            ).fetchall()
            warmup_rows = conn.execute(
                f"""
                WITH candidates AS (
                    SELECT
                        {columns},
                        ROW_NUMBER() OVER (
                            PARTITION BY symbol, bar_time_ms
                            ORDER BY {env_rank_expr} DESC, updated DESC
                        ) AS env_rank
                    FROM ibkr_bars
                    WHERE interval = ?
                      AND symbol IN ({symbol_placeholders})
                      AND environment IN ({env_placeholders})
                      AND bar_time_ms < ?
                ),
                deduped AS (
                    SELECT {columns}
                    FROM candidates
                    WHERE env_rank = 1
                ),
                numbered AS (
                    SELECT
                        {columns},
                        ROW_NUMBER() OVER (
                            PARTITION BY symbol
                            ORDER BY bar_time_ms DESC
                        ) AS symbol_rank
                    FROM deduped
                )
                SELECT {columns}
                FROM numbered
                WHERE symbol_rank <= ?
                ORDER BY symbol ASC, bar_time_ms ASC
                """,
                (
                    to_text(environment).lower() or LIVE_ENVIRONMENT,
                    normalized_interval,
                    *normalized_symbols,
                    *env_values,
                    int(market_start_ms or 0),
                    TIMELINE_WARMUP_BARS,
                ),
            ).fetchall()
    except Exception:
        return None

    grouped = _dedupe_timeline_rows([dict(row) for row in [*warmup_rows, *today_rows]], environment)
    return {symbol: grouped.get(symbol, []) for symbol in normalized_symbols}


def _load_timeline_bars_for_symbol_pb(
    pb: Any,
    *,
    environment: str,
    symbol: str,
    market_start_ms: int,
    market_end_ms: int,
) -> list[dict[str, Any]]:
    filter_symbol = f'symbol = "{escape_filter(symbol)}"'
    today_records = pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(
            [
                'interval = "5m"',
                build_bar_environment_filter(environment),
                f"bar_time_ms >= {market_start_ms}",
                f"bar_time_ms < {market_end_ms}",
                filter_symbol,
            ]
        ),
        sort="bar_time_ms",
        max_pages=TIMELINE_TODAY_MAX_PAGES,
    ) or []
    warmup_records = pb.get_all_records(
        "ibkr_bars",
        filter=" && ".join(
            [
                'interval = "5m"',
                build_bar_environment_filter(environment),
                f"bar_time_ms < {market_start_ms}",
                filter_symbol,
            ]
        ),
        sort="-bar_time_ms",
        max_pages=TIMELINE_WARMUP_MAX_PAGES,
    ) or []
    warmup = _dedupe_timeline_rows(
        [dict(row) for row in warmup_records if isinstance(row, dict)],
        environment,
    ).get(symbol, [])[-TIMELINE_WARMUP_BARS:]
    today = _dedupe_timeline_rows(
        [dict(row) for row in today_records if isinstance(row, dict)],
        environment,
    ).get(symbol, [])
    return [*warmup, *today]


def _load_timeline_bars_by_symbol(
    pb: Any,
    *,
    environment: str,
    symbols: list[str],
    interval: str,
    market_start_ms: int,
    market_end_ms: int,
) -> dict[str, list[dict[str, Any]]]:
    normalized_symbols = normalize_symbols(symbols)
    normalized_interval = normalize_interval(to_text(interval) or "5m")
    if not normalized_symbols or normalized_interval != "5m":
        return {}

    sqlite_result = _load_timeline_bars_by_symbol_sqlite(
        environment=environment,
        symbols=normalized_symbols,
        interval=normalized_interval,
        market_start_ms=market_start_ms,
        market_end_ms=market_end_ms,
    )
    if sqlite_result is not None:
        return sqlite_result

    result: dict[str, list[dict[str, Any]]] = {}
    for symbol in normalized_symbols:
        result[symbol] = _load_timeline_bars_for_symbol_pb(
            pb,
            environment=environment,
            symbol=symbol,
            market_start_ms=market_start_ms,
            market_end_ms=market_end_ms,
        )
    return result


def _pick_latest_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        symbol = to_text(row.get("symbol")).upper()
        bar_time_ms = to_int(row.get("bar_time_ms"), 0)
        if not symbol or bar_time_ms <= 0:
            continue
        current = latest.get(symbol)
        if current is None or bar_time_ms >= to_int(current.get("bar_time_ms"), 0):
            latest[symbol] = dict(row)
    return latest


def _latest_signal_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        normalized_signal = normalize_signal_record(dict(row))
        symbol = to_text(normalized_signal.get("symbol")).upper()
        if not symbol:
            continue
        latest[symbol] = pick_latest_signal(latest.get(symbol), normalized_signal) or latest.get(symbol) or {}
    return latest


def _component_groups(component_flags: dict[str, Any], window_flags: dict[str, Any]) -> dict[str, Any]:
    return _admission_component_groups(component_flags, window_flags)


def _component_rollup(groups: dict[str, Any], window_flags: dict[str, Any]) -> dict[str, Any]:
    return _admission_component_rollup(groups, window_flags)


def _bars_remaining(window_flags: dict[str, Any], max_bars: int) -> int:
    return _admission_bars_remaining(window_flags, max_bars)


def _window_status(
    *,
    signal_state: dict[str, Any],
    window_flags: dict[str, Any],
    bars_remaining: int,
    blocked_reason: str,
) -> str:
    return _admission_window_status(
        signal_state=signal_state,
        window_flags=window_flags,
        bars_remaining_value=bars_remaining,
        blocked_reason=blocked_reason,
    )


def _window_state(window_flags: dict[str, Any]) -> str:
    return _admission_window_state(window_flags)


def _side_window(side: str, window_flags: dict[str, Any], max_bars: int) -> dict[str, Any]:
    return _admission_side_window(side, window_flags, max_bars)

def _build_chart_trace_url(
    *,
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> str:
    query = urlencode(
        {
            "environment": environment,
            "symbol": symbol,
            "interval": interval,
            "start_ms": max(0, int(start_ms or 0)),
            "end_ms": max(0, int(end_ms or 0)),
            "include_signals": "true",
            "include_trace": "true",
        }
    )
    return f"/ibkr_chart.html?{query}"


def _build_chart_trace_request(
    *,
    environment: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    return {
        "path": "/api/custom/ibkr/proxy",
        "method": "POST",
        "body": {
            "action": "chart/timeline",
            "environment": environment,
            "symbol": symbol,
            "interval": interval,
            "start_ms": max(0, int(start_ms or 0)),
            "end_ms": max(0, int(end_ms or 0)),
            "include_signals": True,
            "include_trace": True,
        },
    }


def _build_trace_for_symbol(
    *,
    environment: str,
    symbol: str,
    bars: list[dict[str, Any]],
    signal_window_max_bars: int,
) -> dict[str, Any]:
    del environment
    return build_active_window_trace_for_bars(
        symbol=symbol,
        interval="5m",
        bars=bars,
        signal_window_max_bars=signal_window_max_bars,
    )

__all__ = [name for name in globals() if not name.startswith("__")]
