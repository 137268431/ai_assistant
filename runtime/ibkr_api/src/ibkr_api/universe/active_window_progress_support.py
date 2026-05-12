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
TIMELINE_TODAY_MAX_PAGES = 4
TIMELINE_WARMUP_MAX_PAGES = 2
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

    today_records = load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "5m"',
            build_bar_environment_filter(environment),
            f"bar_time_ms >= {market_start_ms}",
            f"bar_time_ms < {market_end_ms}",
        ],
        symbols=normalized_symbols,
        sort="bar_time_ms",
        max_pages=TIMELINE_TODAY_MAX_PAGES,
    )
    warmup_records = load_records_for_symbols(
        pb,
        "ibkr_bars",
        base_filter_parts=[
            'interval = "5m"',
            build_bar_environment_filter(environment),
            f"bar_time_ms < {market_start_ms}",
        ],
        symbols=normalized_symbols,
        sort="-bar_time_ms",
        max_pages=TIMELINE_WARMUP_MAX_PAGES,
    )

    warmup_by_symbol = _dedupe_timeline_rows(warmup_records, environment)
    today_by_symbol = _dedupe_timeline_rows(today_records, environment)
    result: dict[str, list[dict[str, Any]]] = {}
    for symbol in normalized_symbols:
        warmup = warmup_by_symbol.get(symbol, [])[-TIMELINE_WARMUP_BARS:]
        today = today_by_symbol.get(symbol, [])
        result[symbol] = [*warmup, *today]
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
