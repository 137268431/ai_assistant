from __future__ import annotations

import time
from datetime import datetime, timedelta

from ibkr_compute.api.market.screener.coercion import coerce_float, coerce_int, parse_json_object
from ibkr_compute.api.market.screener.runtime import get_api_app
from ibkr_compute.api.market.screener.scoring import (
    TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN,
    TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME,
    TRADABILITY_OPERABLE_MIN_SCORE,
    build_tradability_assessment,
)
from ibkr_compute.api.market.screener.watchlist import load_effective_watchlist
from ibkr_compute.core.broker_mode import resolve_data_environment
from ibkr_compute.core.time_utils import ET
from ibkr_compute.market.bar_freshness import (
    BarFreshnessPlanner,
    expected_closed_ms_from_latest_5m,
    latest_expected_extended_5m_ms,
)
from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite
from ibkr_compute.market.timeframe_utils import format_us_time, normalize_interval


MANUAL_TARGET_SOURCES = {
    "ibkr_screener",
    "manual_page",
    "manual_page_add",
    "manual_page_edit",
    "manual_page_remove",
    "screener_targets_tab",
}
CONTEXT_ACTIVE_TARGET_SOURCES = {"daily_scan", "intraday_window_admission"}
TRADINGVIEW_TARGET_SOURCES = {"tradingview", "tv", "tv_webhook", "webhook_tv"}


def _truthy_target_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "active", "passed", "pass"}


def _target_row_is_daily_scan_active(row: dict | None) -> bool:
    extra = parse_json_object((row or {}).get("extra"))
    source = str(extra.get("source") or "").strip().lower()
    if source != "daily_scan":
        return False
    return (
        _truthy_target_value(extra.get("active_gate_passed"))
        or _truthy_target_value(extra.get("context_active"))
        or _truthy_target_value(extra.get("context_gate_passed"))
    )


def _target_row_is_manual_active(row: dict | None) -> bool:
    extra = parse_json_object((row or {}).get("extra"))
    source = str(extra.get("source") or "").strip().lower()
    return source.startswith("manual_") or source in MANUAL_TARGET_SOURCES


def _target_row_is_tradingview_active(row: dict | None) -> bool:
    extra = parse_json_object((row or {}).get("extra"))
    source = str(extra.get("source") or "").strip().lower()
    strategy_policy = extra.get("strategy_policy") if isinstance(extra.get("strategy_policy"), dict) else {}
    return source in TRADINGVIEW_TARGET_SOURCES and bool(
        str(extra.get("event_type") or "").strip().lower() == "entry"
        or _truthy_target_value(extra.get("entry_backfilled_target"))
        or str(extra.get("entry_signal_id") or "").strip()
        or str(extra.get("target_admission_reason") or "").strip().lower() == "entry_signal_backfill"
        or str(strategy_policy.get("setup_type") or "").strip().lower() == "tradingview_entry_backfill"
    )


def _effective_target_status(row: dict | None) -> str:
    status = str((row or {}).get("status") or "").strip().lower()
    extra = parse_json_object((row or {}).get("extra"))
    if status == "active" and _truthy_target_value(extra.get("deactivated_after_close")):
        return "candidate"
    if status == "active" and not (
        _target_row_is_daily_scan_active(row)
        or _target_row_is_manual_active(row)
        or _target_row_is_tradingview_active(row)
    ):
        return "candidate"
    return status


def parse_market_date_bounds_ms(market_date: str) -> tuple[int, int]:
    start_dt = datetime.strptime(str(market_date or "").strip(), "%Y-%m-%d").replace(
        tzinfo=ET,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    end_dt = start_dt + timedelta(days=1)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def _build_symbol_placeholders(symbols: list[str]) -> tuple[str, list[str]]:
    normalized = []
    seen = set()
    for item in symbols or []:
        symbol = str(item or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            normalized.append(symbol)
    if not normalized:
        return "", []
    return ", ".join("?" for _ in normalized), normalized


def _load_latest_bar_times_sqlite(
    *,
    environment: str,
    symbols: list[str],
    intervals: list[str],
) -> dict[str, dict[str, int]]:
    symbol_placeholders, normalized_symbols = _build_symbol_placeholders(symbols)
    normalized_intervals = [
        normalize_interval(item)
        for item in intervals or []
        if str(item or "").strip()
    ]
    normalized_intervals = [item for item in dict.fromkeys(normalized_intervals) if item]
    if not symbol_placeholders or not normalized_intervals:
        return {}
    interval_placeholders = ", ".join("?" for _ in normalized_intervals)
    runtime_environment = str(environment or "live").strip().lower() or "live"
    env_clause = "(environment = ? OR environment = '')" if runtime_environment == "live" else "environment = ?"
    params = [
        runtime_environment,
        *normalized_intervals,
        *normalized_symbols,
    ]
    with open_pb_sqlite(readonly=True, timeout=8.0) as conn:
        rows = conn.execute(
            f"""
            SELECT symbol, interval, MAX(bar_time_ms) AS latest_stored_ms
            FROM ibkr_bars
            WHERE {env_clause}
              AND interval IN ({interval_placeholders})
              AND symbol IN ({symbol_placeholders})
            GROUP BY symbol, interval
            """,
            tuple(params),
        ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        symbol = str(row["symbol"] or "").strip().upper()
        interval = normalize_interval(row["interval"])
        latest_ms = coerce_int(row["latest_stored_ms"])
        if symbol and interval and latest_ms > 0:
            result.setdefault(symbol, {})[interval] = latest_ms
    return result


def _freshness_close_delay_seconds(api_app, environment: str) -> int:
    cfg = getattr(api_app, "cfg", None)
    if cfg is not None and hasattr(cfg, "get_int_for_environment"):
        try:
            return max(0, int(cfg.get_int_for_environment("ibkr_official_5m_close_delay_sec", environment, 8)))
        except Exception:
            return 8
    return 8


def _build_bulk_freshness_payloads(
    api_app,
    *,
    environment: str,
    symbols: list[str],
    intervals: list[str],
) -> dict[str, dict]:
    normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols or [] if str(symbol or "").strip()})
    normalized_intervals = [normalize_interval(item) for item in intervals or [] if str(item or "").strip()]
    normalized_intervals = [item for item in dict.fromkeys(normalized_intervals) if item]
    if not normalized_symbols or not normalized_intervals:
        return {}
    latest_by_symbol = _load_latest_bar_times_sqlite(
        environment=environment,
        symbols=normalized_symbols,
        intervals=normalized_intervals,
    )
    close_delay_seconds = _freshness_close_delay_seconds(api_app, environment)
    payloads: dict[str, dict] = {}
    for symbol in normalized_symbols:
        latest_map = latest_by_symbol.get(symbol) or {}
        latest_5m_ms = int(latest_map.get("5m", 0) or 0)
        expected_5m_ms = latest_expected_extended_5m_ms(
            delay_seconds=close_delay_seconds,
            symbol=symbol,
        )
        interval_payloads: dict[str, dict] = {}
        needs_repair_intervals: list[str] = []
        status_counts: dict[str, int] = {}
        for interval in normalized_intervals:
            latest_ms = int(latest_map.get(interval, 0) or 0)
            expected_ms = (
                expected_5m_ms
                if interval == "5m"
                else expected_closed_ms_from_latest_5m(latest_5m_ms or expected_5m_ms, interval, symbol=symbol)
            )
            reasons: list[str] = []
            if latest_ms <= 0:
                reasons.append("missing_bars")
            elif expected_ms > 0 and latest_ms < expected_ms:
                reasons.append("latest_stale")
            status = "ready"
            if "latest_stale" in reasons:
                status = "stale"
            elif reasons:
                status = "missing"
            if status != "ready":
                needs_repair_intervals.append(interval)
            status_counts[status] = int(status_counts.get(status, 0) or 0) + 1
            interval_payloads[interval] = {
                "environment": environment,
                "symbol": symbol,
                "interval": interval,
                "status": status,
                "ready": status == "ready",
                "needs_repair": status != "ready",
                "reason": ",".join(reasons),
                "reasons": reasons,
                "stored_count": 1 if latest_ms > 0 else 0,
                "required_bars": 0,
                "latest_stored_ms": latest_ms,
                "latest_stored_us": format_us_time(latest_ms) if latest_ms > 0 else "",
                "expected_closed_ms": expected_ms,
                "expected_closed_us": format_us_time(expected_ms) if expected_ms > 0 else "",
                "stale_by_ms": max(0, expected_ms - latest_ms) if expected_ms > 0 and latest_ms > 0 else 0,
                "authoritative_source": "ibkr_history_api",
            }
        aggregate_status = "ready" if not needs_repair_intervals else (
            "stale" if any(interval_payloads[item]["status"] == "stale" for item in needs_repair_intervals) else "missing"
        )
        payloads[symbol] = {
            "ok": True,
            "environment": environment,
            "symbol": symbol,
            "status": aggregate_status,
            "ready": aggregate_status == "ready",
            "needs_repair": aggregate_status != "ready",
            "intervals": interval_payloads,
            "needs_repair_intervals": needs_repair_intervals,
            "status_counts": status_counts,
            "latest_5m_ms": latest_5m_ms,
            "latest_5m_us": format_us_time(latest_5m_ms) if latest_5m_ms > 0 else "",
            "checked_at_ms": int(time.time() * 1000),
            "planner": "sqlite_bulk",
        }
    return payloads


def _build_screener_freshness_payloads(api_app, environment: str, symbols: list[str]) -> dict[str, dict]:
    intervals = ["5m", "15m", "30m", "1h", "4h", "1d"]
    try:
        return _build_bulk_freshness_payloads(
            api_app,
            environment=environment,
            symbols=symbols,
            intervals=intervals,
        )
    except Exception:
        planner = getattr(api_app, "bar_freshness_planner", None) or BarFreshnessPlanner(
            getattr(api_app, "pb", None),
            getattr(api_app, "cfg", None),
            environment=environment,
        )
        return {
            symbol: planner.plan_symbol(
                symbol,
                intervals,
                environment=environment,
                required_bars=0,
            )
            for symbol in symbols
        }


def _load_bar_rows_sqlite(
    *,
    interval: str,
    environment: str,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    order_by: str,
) -> list[dict]:
    placeholders, normalized_symbols = _build_symbol_placeholders(symbols)
    if not placeholders:
        return []
    runtime_environment = str(environment or "live").strip().lower() or "live"
    env_clause = "(environment = ? OR environment = '')" if runtime_environment == "live" else "environment = ?"
    params = [
        str(interval or ""),
        runtime_environment,
        int(start_ms or 0),
        int(end_ms or 0),
        *normalized_symbols,
    ]
    order_sql = "bar_time_ms DESC" if str(order_by or "").lower() == "desc" else "bar_time_ms ASC"
    with open_pb_sqlite(readonly=True, timeout=8.0) as conn:
        rows = conn.execute(
            f"""
            SELECT symbol, exchange, interval, open, high, low, close, volume,
                   session_type, us_time, cn_time, bar_time_ms, extra, environment,
                   created, updated
            FROM ibkr_bars
            WHERE interval = ?
              AND {env_clause}
              AND bar_time_ms >= ?
              AND bar_time_ms < ?
              AND symbol IN ({placeholders})
            ORDER BY {order_sql}
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def _load_daily_rows_from_5m_sqlite(
    *,
    environment: str,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    placeholders, normalized_symbols = _build_symbol_placeholders(symbols)
    if not placeholders:
        return []
    runtime_environment = str(environment or "live").strip().lower() or "live"
    env_clause = "(environment = ? OR environment = '')" if runtime_environment == "live" else "environment = ?"
    params = [
        runtime_environment,
        int(start_ms or 0),
        int(end_ms or 0),
        *normalized_symbols,
    ]
    with open_pb_sqlite(readonly=True, timeout=8.0) as conn:
        rows = conn.execute(
            f"""
            WITH base AS (
                SELECT symbol, exchange, open, high, low, close, volume,
                       session_type, us_time, cn_time, bar_time_ms, environment,
                       substr(us_time, 1, 10) AS day
                FROM ibkr_bars
                WHERE interval = '5m'
                  AND {env_clause}
                  AND bar_time_ms >= ?
                  AND bar_time_ms < ?
                  AND us_time != ''
                  AND symbol IN ({placeholders})
            ),
            daily AS (
                SELECT symbol, day, MIN(bar_time_ms) AS first_ms,
                       MAX(bar_time_ms) AS last_ms, SUM(volume) AS volume,
                       MAX(high) AS high, MIN(low) AS low
                FROM base
                GROUP BY symbol, day
            )
            SELECT d.symbol, last.exchange, '1d' AS interval, first.open,
                   d.high, d.low, last.close, d.volume, last.session_type,
                   last.us_time, last.cn_time, d.first_ms AS bar_time_ms,
                   '{{"source":"5m_daily_fallback"}}' AS extra,
                   last.environment, '' AS created, '' AS updated
            FROM daily d
            JOIN base last
              ON last.symbol = d.symbol
             AND last.day = d.day
             AND last.bar_time_ms = d.last_ms
            JOIN base first
              ON first.symbol = d.symbol
             AND first.day = d.day
             AND first.bar_time_ms = d.first_ms
            ORDER BY d.first_ms ASC, d.symbol ASC
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def _load_indicator_rows_sqlite(
    *,
    interval: str,
    environment: str,
    symbols: list[str],
    start_ms: int,
) -> list[dict]:
    placeholders, normalized_symbols = _build_symbol_placeholders(symbols)
    if not placeholders:
        return []
    runtime_environment = str(environment or "live").strip().lower() or "live"
    params = [
        str(interval or ""),
        runtime_environment,
        int(start_ms or 0),
        *normalized_symbols,
    ]
    with open_pb_sqlite(readonly=True, timeout=8.0) as conn:
        rows = conn.execute(
            f"""
            SELECT id, symbol, exchange, interval, script_tag, us_time, cn_time,
                   bar_index, bar_time_ms, extra, environment, created, updated
            FROM ibkr_indicators
            WHERE interval = ?
              AND environment = ?
              AND bar_time_ms >= ?
              AND symbol IN ({placeholders})
            ORDER BY bar_time_ms DESC
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def _load_rows_with_fallback(sqlite_loader, pb_loader):
    try:
        return sqlite_loader()
    except Exception:
        return pb_loader()


def _build_daily_change_fields(current_close: float, daily_history: list[dict]) -> dict:
    prev_close = coerce_float(daily_history[-1].get("close")) if len(daily_history) >= 1 else 0.0
    prev_prev_close = coerce_float(daily_history[-2].get("close")) if len(daily_history) >= 2 else 0.0
    close_5 = coerce_float(daily_history[-5].get("close")) if len(daily_history) >= 5 else 0.0
    day_change_pct = ((current_close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    prev_close_change_pct = (
        (prev_close - prev_prev_close) / prev_prev_close * 100.0
        if prev_prev_close > 0
        else 0.0
    )
    change_7d = ((current_close - close_5) / close_5 * 100.0) if close_5 > 0 else 0.0
    return {
        "prev_close": round(prev_close, 4),
        "day_change_pct": round(day_change_pct, 2),
        "prev_close_change_pct": round(prev_close_change_pct, 2),
        "change_7d": round(change_7d, 2),
    }


def _daily_row_date(api_app, row: dict) -> str:
    us_time = str((row or {}).get("us_time") or "").strip()
    if len(us_time) >= 10:
        return us_time[:10]
    bar_ms = coerce_int((row or {}).get("bar_time_ms"))
    if bar_ms <= 0:
        return ""
    return api_app.ms_to_et(bar_ms).strftime("%Y-%m-%d")


def _merge_daily_rows_with_fallback(api_app, daily_rows: list[dict], fallback_rows: list[dict]) -> list[dict]:
    merged = []
    seen_keys = set()
    for row in daily_rows or []:
        symbol = str((row or {}).get("symbol", "")).strip().upper()
        row_date = _daily_row_date(api_app, row)
        if symbol and row_date:
            seen_keys.add((symbol, row_date))
        merged.append(row)
    for row in fallback_rows or []:
        symbol = str((row or {}).get("symbol", "")).strip().upper()
        row_date = _daily_row_date(api_app, row)
        if not symbol or not row_date or (symbol, row_date) in seen_keys:
            continue
        merged.append(row)
        seen_keys.add((symbol, row_date))
    return merged


def build_screener_payload(
    environment: str,
    market_date: str | None = None,
    symbols=None,
    limit: int = 0,
) -> dict:
    api_app = get_api_app()
    runtime_environment = str(environment or "live").strip().lower() or "live"
    data_environment = resolve_data_environment(runtime_environment)
    selected_symbols = api_app.normalize_symbols(symbols)
    selected_set = set(selected_symbols)
    market_date = str(market_date or api_app.current_market_date()).strip() or api_app.current_market_date()
    market_start_ms, market_end_ms = parse_market_date_bounds_ms(market_date)
    now_ms = int(time.time() * 1000)

    watchlist_map_all = load_effective_watchlist(runtime_environment)
    watchlist_map = {
        symbol: row
        for symbol, row in watchlist_map_all.items()
        if api_app.normalize_watchlist_symbol_role((row or {}).get("symbol_role")) == api_app.WATCHLIST_SYMBOL_ROLE_TRADE
    }
    market_monitor_symbols = {
        symbol
        for symbol, row in watchlist_map_all.items()
        if api_app.normalize_watchlist_symbol_role((row or {}).get("symbol_role"))
        == api_app.WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
    }
    metadata_map = api_app.refresh_symbol_metadata()

    universe_symbols = selected_symbols or sorted(watchlist_map.keys())
    if selected_set and not universe_symbols:
        universe_symbols = sorted(selected_set)
    universe_set = set(universe_symbols)

    target_rows = api_app.pb.get_all_records(
        "ibkr_targets",
        filter=f'date = "{market_date}" && environment = "{data_environment}"',
        sort="-updated",
        max_pages=20,
    )
    target_by_symbol = {}
    for row in target_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol in market_monitor_symbols:
            continue
        if symbol and symbol not in target_by_symbol:
            target_by_symbol[symbol] = row
            if symbol not in universe_set and not selected_set:
                universe_symbols.append(symbol)
                universe_set.add(symbol)
    freshness_by_symbol = {}
    if not selected_symbols:
        try:
            freshness_by_symbol = _build_screener_freshness_payloads(
                api_app,
                runtime_environment,
                universe_symbols,
            )
        except Exception:
            freshness_by_symbol = {}

    if not universe_symbols:
        timestamps = api_app.build_runtime_timestamps()
        return {
            "ok": True,
            "environment": runtime_environment,
            "market_date": market_date,
            **timestamps,
            "summary": {
                "total": 0,
                "with_live_bars": 0,
                "operable": 0,
                "candidate_targets": 0,
                "active_targets": 0,
            },
            "filters": {
                "exchanges": [],
                "industries": [],
                "target_statuses": [],
                "direction_biases": [],
            },
            "items": [],
        }

    symbol_filter = api_app.build_symbol_filter(universe_symbols)
    lookback_daily_ms = max(0, market_start_ms - api_app.interval_to_ms("1d") * 20)
    daily_filter_parts = [
        'interval = "1d"',
        api_app.build_bar_environment_filter(runtime_environment, include_legacy_empty=True),
        f"bar_time_ms >= {lookback_daily_ms}",
        f"bar_time_ms < {market_end_ms}",
    ]
    if symbol_filter:
        daily_filter_parts.append(symbol_filter)
    daily_rows = _load_rows_with_fallback(
        lambda: _load_bar_rows_sqlite(
            interval="1d",
            environment=runtime_environment,
            symbols=universe_symbols,
            start_ms=lookback_daily_ms,
            end_ms=market_end_ms,
            order_by="asc",
        ),
        lambda: api_app.pb.get_all_records(
            "ibkr_bars",
            filter=" && ".join(daily_filter_parts),
            sort="bar_time_ms",
            max_pages=100,
        ),
    )
    daily_history_symbols = {
        str((row or {}).get("symbol", "")).strip().upper()
        for row in daily_rows
        if str((row or {}).get("symbol", "")).strip().upper() in universe_set
        and _daily_row_date(api_app, row) < market_date
    }
    missing_daily_symbols = [symbol for symbol in universe_symbols if symbol not in daily_history_symbols]
    if missing_daily_symbols:
        fallback_daily_rows = _load_rows_with_fallback(
            lambda: _load_daily_rows_from_5m_sqlite(
                environment=runtime_environment,
                symbols=missing_daily_symbols,
                start_ms=lookback_daily_ms,
                end_ms=market_end_ms,
            ),
            lambda: [],
        )
        daily_rows = _merge_daily_rows_with_fallback(api_app, daily_rows, fallback_daily_rows)

    intraday_filter_parts = [
        'interval = "5m"',
        api_app.build_bar_environment_filter(runtime_environment, include_legacy_empty=True),
        f"bar_time_ms >= {market_start_ms}",
        f"bar_time_ms < {market_end_ms}",
    ]
    if symbol_filter:
        intraday_filter_parts.append(symbol_filter)
    intraday_rows = _load_rows_with_fallback(
        lambda: _load_bar_rows_sqlite(
            interval="5m",
            environment=runtime_environment,
            symbols=universe_symbols,
            start_ms=market_start_ms,
            end_ms=market_end_ms,
            order_by="asc",
        ),
        lambda: api_app.pb.get_all_records(
            "ibkr_bars",
            filter=" && ".join(intraday_filter_parts),
            sort="bar_time_ms",
            max_pages=400,
        ),
    )

    indicator_filter_parts = [
        f'interval = "{api_app.interval_to_chart_tf("5m")}"',
        f'environment = "{runtime_environment}"',
        f"bar_time_ms >= {max(0, market_start_ms - api_app.interval_to_ms('1d') * 5)}",
    ]
    if symbol_filter:
        indicator_filter_parts.append(symbol_filter)
    indicator_rows = _load_rows_with_fallback(
        lambda: _load_indicator_rows_sqlite(
            interval=api_app.interval_to_chart_tf("5m"),
            environment=runtime_environment,
            symbols=universe_symbols,
            start_ms=max(0, market_start_ms - api_app.interval_to_ms("1d") * 5),
        ),
        lambda: api_app.pb.get_all_records(
            "ibkr_indicators",
            filter=" && ".join(indicator_filter_parts),
            sort="-bar_time_ms",
            max_pages=120,
        ),
    )

    daily_bars_by_symbol = {}
    fallback_daily_by_symbol = {}
    for row in daily_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol not in universe_set:
            continue
        bar_ms = coerce_int(row.get("bar_time_ms"))
        close = coerce_float(row.get("close"))
        if bar_ms <= 0 or close <= 0:
            continue
        daily_bars_by_symbol.setdefault(symbol, []).append(row)
        row_date = _daily_row_date(api_app, row)
        if row_date < market_date:
            fallback_daily_by_symbol[symbol] = row

    latest_intraday_by_symbol = {}
    volume_stats_by_symbol = {}
    for row in intraday_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol not in universe_set:
            continue
        bar_ms = coerce_int(row.get("bar_time_ms"))
        if bar_ms <= 0:
            continue
        volume = coerce_float(row.get("volume"))
        session_type = str(row.get("session_type", "") or "").strip().lower() or api_app.classify_session(
            bar_time_ms=bar_ms
        )
        latest_intraday_by_symbol[symbol] = row
        stats = volume_stats_by_symbol.setdefault(symbol, {"premarket": 0.0, "regular": 0.0, "today": 0.0})
        stats["today"] += volume
        if session_type == "premarket":
            stats["premarket"] += volume
        elif session_type == "regular":
            stats["regular"] += volume

    latest_indicator_by_symbol = {}
    for row in indicator_rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol not in universe_set or symbol in latest_indicator_by_symbol:
            continue
        latest_indicator_by_symbol[symbol] = row

    items = []
    exchange_values = set()
    industry_values = set()
    target_status_values = set()
    direction_bias_values = set()
    candidate_targets = 0
    active_targets = 0

    for symbol in universe_symbols:
        base_meta = watchlist_map.get(symbol) or metadata_map.get(symbol) or {}
        latest_intraday = latest_intraday_by_symbol.get(symbol)
        latest_daily = fallback_daily_by_symbol.get(symbol)
        latest_target = target_by_symbol.get(symbol)
        latest_indicator = latest_indicator_by_symbol.get(symbol)
        indicator_extra = parse_json_object((latest_indicator or {}).get("extra"))

        intraday_bar_ms = coerce_int((latest_intraday or {}).get("bar_time_ms"))
        daily_bar_ms = coerce_int((latest_daily or {}).get("bar_time_ms"))
        price = coerce_float((latest_intraday or {}).get("close"))
        price_source = "5m"
        if price <= 0:
            price = coerce_float((latest_daily or {}).get("close"))
            price_source = "1d_close" if price > 0 else ""

        compare_bar_ms = intraday_bar_ms or market_start_ms
        latest_bar_time_ms = intraday_bar_ms or daily_bar_ms
        latest_us_time = str((latest_intraday or {}).get("us_time") or (latest_daily or {}).get("us_time") or "")
        latest_session_type = str((latest_intraday or {}).get("session_type") or "").strip().lower()
        if intraday_bar_ms > 0 and not latest_session_type:
            latest_session_type = api_app.classify_session(bar_time_ms=intraday_bar_ms)

        daily_history = [
            row
            for row in daily_bars_by_symbol.get(symbol, [])
            if _daily_row_date(api_app, row) < market_date
        ]
        last_10_daily = daily_history[-10:]
        avg_10d_volume = (
            round(
                sum(coerce_float(row.get("volume")) for row in last_10_daily) / len(last_10_daily),
                2,
            )
            if last_10_daily
            else 0.0
        )

        daily_fields = _build_daily_change_fields(price, daily_history) if price > 0 else {
            "day_change_pct": 0.0,
            "prev_close_change_pct": 0.0,
            "change_7d": 0.0,
        }

        target_status = _effective_target_status(latest_target)
        direction_bias = str((latest_target or {}).get("direction_bias") or "neutral").strip().lower() or "neutral"
        target_score = round(coerce_float((latest_target or {}).get("score")), 2)
        scan_reason = str((latest_target or {}).get("scan_reason") or "").strip()
        if target_status == "candidate":
            candidate_targets += 1
        elif target_status == "active":
            active_targets += 1

        volume_stats = volume_stats_by_symbol.get(symbol, {})
        freshness_min = None
        if intraday_bar_ms > 0:
            freshness_min = max(0, int((now_ms - intraday_bar_ms) // 60000))

        exchange = (
            str(
                (base_meta or {}).get("exchange")
                or (latest_intraday or {}).get("exchange")
                or (latest_target or {}).get("exchange")
                or ""
            )
            .strip()
            .upper()
        )
        industry = str((base_meta or {}).get("industry") or "").strip()
        note = str((base_meta or {}).get("note") or "").strip()
        atr_pct = round(coerce_float(indicator_extra.get("atr_pct", (latest_indicator or {}).get("atr_pct"))), 2)

        row = {
            "symbol": symbol,
            "exchange": exchange,
            "industry": industry,
            "note": note,
            "price": round(price, 4) if price > 0 else 0.0,
            "price_source": price_source,
            "atr_pct": atr_pct,
            "avg_10d_volume": avg_10d_volume,
            "premarket_volume": round(coerce_float(volume_stats.get("premarket")), 2),
            "regular_volume": round(coerce_float(volume_stats.get("regular")), 2),
            "today_volume": round(coerce_float(volume_stats.get("today")), 2),
            "latest_bar_time_ms": latest_bar_time_ms,
            "latest_intraday_bar_time_ms": intraday_bar_ms,
            "latest_us_time": latest_us_time,
            "latest_session_type": latest_session_type,
            "freshness_min": freshness_min,
            "has_live_bar": intraday_bar_ms > 0,
            "target_status": target_status,
            "target_score": target_score,
            "direction_bias": direction_bias,
            "scan_reason": scan_reason,
            "data_quality": freshness_by_symbol.get(symbol) or {},
            **daily_fields,
        }
        tradability_score, operable_reasons = build_tradability_assessment(row)
        row["tradability_score"] = tradability_score
        row["operable_reasons"] = operable_reasons
        row["is_operable"] = bool(
            row["has_live_bar"]
            and row["price"] > 0
            and row["avg_10d_volume"] >= TRADABILITY_OPERABLE_MIN_AVG_10D_VOLUME
            and row["tradability_score"] >= TRADABILITY_OPERABLE_MIN_SCORE
            and isinstance(row["freshness_min"], int)
            and row["freshness_min"] <= TRADABILITY_OPERABLE_MAX_FRESHNESS_MIN
        )
        items.append(row)

        if exchange:
            exchange_values.add(exchange)
        if industry:
            industry_values.add(industry)
        if target_status:
            target_status_values.add(target_status)
        if direction_bias:
            direction_bias_values.add(direction_bias)

    items.sort(
        key=lambda item: (
            0 if item.get("is_operable") else 1,
            -coerce_float(item.get("tradability_score")),
            -coerce_float(item.get("target_score")),
            -coerce_float(item.get("premarket_volume")),
            -coerce_float(item.get("avg_10d_volume")),
            item.get("symbol", ""),
        )
    )
    if limit > 0:
        items = items[:limit]

    timestamps = api_app.build_runtime_timestamps()
    return {
        "ok": True,
        "environment": runtime_environment,
        "market_date": market_date,
        **timestamps,
        "summary": {
            "total": len(items),
            "with_live_bars": sum(1 for item in items if item.get("has_live_bar")),
            "operable": sum(1 for item in items if item.get("is_operable")),
            "data_completeness": "repairing" if any(bool((item.get("data_quality") or {}).get("needs_repair")) for item in items) else "ready",
            "incomplete_data": sum(1 for item in items if bool((item.get("data_quality") or {}).get("needs_repair"))),
            "repairing_count": sum(1 for item in items if bool((item.get("data_quality") or {}).get("needs_repair"))),
            "candidate_targets": candidate_targets,
            "active_targets": active_targets,
            "avg_premarket_volume": round(
                sum(coerce_float(item.get("premarket_volume")) for item in items) / len(items),
                2,
            )
            if items
            else 0.0,
        },
        "filters": {
            "exchanges": sorted(exchange_values),
            "industries": sorted(industry_values),
            "target_statuses": sorted(target_status_values),
            "direction_biases": sorted(direction_bias_values),
        },
        "items": items,
    }


__all__ = ["build_screener_payload", "parse_market_date_bounds_ms"]
