"""Aggregate bar freshness for system summaries.

The evaluator treats ``bar_time_ms`` as the bar start and uses canonical close
boundaries for synthetic extended-session daily bars.  Other intervals use
``extra.bar_close_time_ms`` when present.  Higher intervals are only considered
overdue after their own close boundary is reachable from a mature 5m close.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any, Iterable

from ibkr_compute.core.time_utils import ET
from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite
from ibkr_compute.market.timeframe_utils import (
    COMPUTE_INTERVALS,
    EXTENDED_OPEN_MINUTE,
    REGULAR_OPEN_MINUTE,
    bar_close_ms,
    bucket_start_ms,
    extended_close_minute_for_date,
    extended_session_close_ms_for_date,
    format_us_time,
    interval_to_ms,
    is_nyse_trading_day,
    normalize_interval,
    previous_intraday_bucket_start_ms,
    previous_trading_day,
    regular_close_minute_for_date,
    regular_session_close_ms_for_date,
)

DEFAULT_CLOSE_DELAY_SECONDS = 3
DEFAULT_SQLITE_TIMEOUT_SECONDS = 2.0

RUNTIME_UNIVERSE_KEYS = (
    "active_trade_symbols",
    "market_ws_symbols",
    "data_symbols",
    "active_subscription_symbols",
    "market_ws_subscribed_symbols",
)

PENDING_SYMBOL_KEYS = (
    ("canonical_5m", "pending_symbols"),
    ("warmup", "pending_symbols"),
    ("warmup", "integrity_pending_symbols"),
    ("data_backfill", "pending_symbols"),
)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_symbols(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    normalized = [_normalize_symbol(item) for item in values]
    return [item for item in normalized if item]


def _parse_extra(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _escape_pb_filter(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _environment_filter(environment: str) -> str:
    env = str(environment or "live").strip().lower() or "live"
    clauses = [f'environment = "{_escape_pb_filter(env)}"']
    if env == "live":
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})" if len(clauses) > 1 else clauses[0]


def _get_bool_setting(config: Any, key: str, environment: str, default: bool) -> bool:
    if config is not None and hasattr(config, "get_bool_for_environment"):
        try:
            return bool(config.get_bool_for_environment(key, environment, default))
        except Exception:
            return bool(default)
    return bool(default)


def _get_float_setting(config: Any, key: str, environment: str, default: float) -> float:
    if config is not None and hasattr(config, "get_float_for_environment"):
        try:
            return float(config.get_float_for_environment(key, environment, default))
        except Exception:
            return float(default)
    if config is not None and hasattr(config, "get_for_environment"):
        try:
            return float(config.get_for_environment(key, environment, default))
        except Exception:
            return float(default)
    return float(default)


def _close_delay_seconds(config: Any, environment: str) -> int:
    if config is not None and hasattr(config, "get_int_for_environment"):
        try:
            return max(
                0,
                int(
                    config.get_int_for_environment(
                        "ibkr_official_5m_close_delay_sec",
                        environment,
                        DEFAULT_CLOSE_DELAY_SECONDS,
                    )
                ),
            )
        except Exception:
            return DEFAULT_CLOSE_DELAY_SECONDS
    return DEFAULT_CLOSE_DELAY_SECONDS


def _sqlite_timeout_seconds(config: Any, environment: str) -> float:
    fallback = _get_float_setting(config, "ibkr_bar_direct_sqlite_timeout_sec", environment, 30.0)
    timeout = _get_float_setting(
        config,
        "ibkr_summary_freshness_sqlite_timeout_sec",
        environment,
        DEFAULT_SQLITE_TIMEOUT_SECONDS,
    )
    return max(
        0.5,
        min(
            float(fallback or DEFAULT_SQLITE_TIMEOUT_SECONDS),
            float(timeout or DEFAULT_SQLITE_TIMEOUT_SECONDS),
        ),
    )


def _parse_us_time_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text[:19], fmt).replace(tzinfo=ET).timestamp() * 1000)
        except Exception:
            continue
    try:
        return int(datetime.fromisoformat(text).astimezone(ET).timestamp() * 1000)
    except Exception:
        return 0


def _now_ms(*, now_ms: int | None = None, now_us: str = "") -> int:
    explicit = _to_int(now_ms, 0)
    if explicit > 0:
        return explicit
    parsed = _parse_us_time_ms(now_us)
    if parsed > 0:
        return parsed
    return int(time.time() * 1000)


def _dt_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, ET)


def _ms_from_dt(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _daily_close_time_ms_from_start(start_ms: int) -> int:
    if int(start_ms or 0) <= 0:
        return 0
    return extended_session_close_ms_for_date(_dt_from_ms(start_ms))


def _previous_extended_close(value: datetime) -> datetime:
    day = previous_trading_day(value)
    close_ms = extended_session_close_ms_for_date(day)
    return _dt_from_ms(close_ms)


def _previous_regular_close(value: datetime) -> datetime:
    day = previous_trading_day(value)
    close_ms = regular_session_close_ms_for_date(day)
    return _dt_from_ms(close_ms)


def _session_kind(now_ms: int) -> str:
    dt = _dt_from_ms(now_ms)
    if not is_nyse_trading_day(dt):
        return "closed"
    minutes = dt.hour * 60 + dt.minute
    regular_close = regular_close_minute_for_date(dt)
    extended_close = extended_close_minute_for_date(dt)
    if minutes < EXTENDED_OPEN_MINUTE:
        return "closed"
    if minutes < REGULAR_OPEN_MINUTE:
        return "premarket"
    if minutes < regular_close:
        return "regular"
    if minutes < extended_close:
        return "afterhours"
    return "closed"


def _session_start_ms(now_ms: int, session: str) -> int:
    dt = _dt_from_ms(now_ms)
    if session == "premarket":
        start = dt.replace(hour=4, minute=0, second=0, microsecond=0)
    elif session == "regular":
        start = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    elif session == "afterhours":
        regular_close = regular_close_minute_for_date(dt)
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=regular_close)
    else:
        return 0
    return _ms_from_dt(start)


def _quiet_extended_start_ms(now_ms: int, session: str) -> int:
    dt = _dt_from_ms(now_ms)
    if session in {"premarket", "afterhours"}:
        return _session_start_ms(now_ms, session)
    if session != "closed":
        return 0

    minutes = dt.hour * 60 + dt.minute
    extended_close = extended_close_minute_for_date(dt)
    if is_nyse_trading_day(dt) and minutes >= extended_close:
        regular_close = regular_close_minute_for_date(dt)
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=regular_close)
        return _ms_from_dt(start)
    if is_nyse_trading_day(dt) and minutes < EXTENDED_OPEN_MINUTE:
        return _ms_from_dt(_previous_regular_close(dt))
    if not is_nyse_trading_day(dt):
        return _ms_from_dt(_previous_regular_close(dt))
    return 0


def _latest_mature_5m_close_ms(now_ms: int, *, delay_seconds: int) -> int:
    effective = _dt_from_ms(now_ms) - timedelta(seconds=max(0, int(delay_seconds or 0)))
    if not is_nyse_trading_day(effective):
        return _ms_from_dt(_previous_extended_close(effective))

    minutes = effective.hour * 60 + effective.minute
    first_close = EXTENDED_OPEN_MINUTE + 5
    if minutes < first_close:
        return _ms_from_dt(_previous_extended_close(effective))
    extended_close = extended_close_minute_for_date(effective)
    if minutes >= extended_close:
        return extended_session_close_ms_for_date(effective)

    close_minutes = minutes - (minutes % 5)
    close_dt = effective.replace(
        hour=close_minutes // 60,
        minute=close_minutes % 60,
        second=0,
        microsecond=0,
    )
    if close_minutes < first_close:
        return _ms_from_dt(_previous_extended_close(effective))
    return _ms_from_dt(close_dt)


def _latest_intraday_expected_close(mature_5m_close_ms: int, interval: str) -> dict[str, Any]:
    normalized = normalize_interval(interval)
    if normalized == "5m":
        close_ms = int(mature_5m_close_ms or 0)
        return {
            "expected_close_ms": close_ms,
            "expected_close_us": format_us_time(close_ms) if close_ms > 0 else "",
            "current_due": True,
        }

    latest_5m_start_ms = int(mature_5m_close_ms or 0) - interval_to_ms("5m")
    current_bucket_ms = bucket_start_ms(latest_5m_start_ms, normalized)
    current_close_ms = bar_close_ms(current_bucket_ms, normalized)
    if int(mature_5m_close_ms or 0) < current_close_ms:
        previous_start_ms = previous_intraday_bucket_start_ms(current_bucket_ms, normalized)
        previous_close_ms = bar_close_ms(previous_start_ms, normalized)
        previous = _dt_from_ms(previous_close_ms)
        return {
            "expected_close_ms": previous_close_ms,
            "expected_close_us": previous.strftime("%Y-%m-%d %H:%M:%S"),
            "current_due": False,
        }
    return {
        "expected_close_ms": current_close_ms,
        "expected_close_us": format_us_time(current_close_ms),
        "current_due": True,
    }


def _latest_daily_expected_close(mature_5m_close_ms: int) -> dict[str, Any]:
    mature_dt = _dt_from_ms(mature_5m_close_ms)
    current_close_ms = extended_session_close_ms_for_date(mature_dt)
    if is_nyse_trading_day(mature_dt) and int(mature_5m_close_ms or 0) >= current_close_ms:
        close_dt = _dt_from_ms(current_close_ms)
        current_due = True
    else:
        close_dt = _previous_extended_close(mature_dt)
        current_due = False
    return {
        "expected_close_ms": _ms_from_dt(close_dt),
        "expected_close_us": close_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "current_due": current_due,
    }


def _expected_close_payload(mature_5m_close_ms: int, interval: str) -> dict[str, Any]:
    normalized = normalize_interval(interval)
    if normalized == "1d":
        return _latest_daily_expected_close(mature_5m_close_ms)
    return _latest_intraday_expected_close(mature_5m_close_ms, normalized)


def extract_bar_close_time_ms(row: dict[str, Any] | None, interval: str) -> int:
    payload = dict(row or {})
    if not payload:
        return 0
    normalized = normalize_interval(interval or payload.get("interval") or "5m")
    start_ms = _to_int(payload.get("bar_time_ms"), 0)
    if normalized == "1d" and start_ms > 0:
        return _daily_close_time_ms_from_start(start_ms)

    extra = _parse_extra(payload.get("extra"))
    close_ms = _to_int(extra.get("bar_close_time_ms"), 0)
    if close_ms > 0:
        return close_ms

    if start_ms <= 0:
        return 0
    return start_ms + interval_to_ms(normalized)


def _bar_payload(row: dict[str, Any] | None, interval: str) -> dict[str, Any]:
    payload = dict(row or {})
    start_ms = _to_int(payload.get("bar_time_ms"), 0)
    close_ms = extract_bar_close_time_ms(payload, interval)
    return {
        "row": payload,
        "latest_start_ms": start_ms,
        "latest_start_us": format_us_time(start_ms) if start_ms > 0 else "",
        "latest_close_ms": close_ms,
        "latest_close_us": format_us_time(close_ms) if close_ms > 0 else "",
    }


def _runtime_universe_symbols(runtime_payload: dict[str, Any]) -> list[str]:
    runtime = dict(runtime_payload or {})
    market_universe = runtime.get("market_universe") if isinstance(runtime.get("market_universe"), dict) else {}
    symbols: list[str] = []
    for key in RUNTIME_UNIVERSE_KEYS:
        symbols.extend(_normalize_symbols(market_universe.get(key)))
    return sorted(dict.fromkeys(symbols))


def _runtime_market_universe(runtime_payload: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(runtime_payload or {})
    market_universe = runtime.get("market_universe")
    return dict(market_universe) if isinstance(market_universe, dict) else {}


def _resolve_scope_symbols(runtime_payload: dict[str, Any], symbols: list[str]) -> dict[str, list[str]]:
    all_symbols = sorted(dict.fromkeys(_normalize_symbols(symbols)))
    all_set = set(all_symbols)
    market_universe = _runtime_market_universe(runtime_payload)
    active = _normalize_symbols(
        market_universe.get("active_trade_symbols")
        or market_universe.get("active_target_symbols")
        or []
    )
    market_symbols = _normalize_symbols(market_universe.get("market_ws_symbols"))
    market_symbols.extend(_normalize_symbols(market_universe.get("market_ws_subscribed_symbols")))
    subscription_symbols = _normalize_symbols(market_universe.get("active_subscription_symbols"))

    active = [symbol for symbol in active if symbol in all_set]
    market_symbols = [symbol for symbol in dict.fromkeys(market_symbols) if symbol in all_set]
    subscription_symbols = [symbol for symbol in subscription_symbols if symbol in all_set]
    if not active and subscription_symbols:
        active = [symbol for symbol in subscription_symbols if symbol not in set(market_symbols)] or subscription_symbols

    realtime = sorted(dict.fromkeys(active + market_symbols))
    if not realtime:
        realtime = subscription_symbols or all_symbols
    active_scope = active or realtime
    realtime_set = set(realtime)
    watchlist = [symbol for symbol in all_symbols if symbol not in realtime_set]
    return {
        "all": all_symbols,
        "active": sorted(dict.fromkeys(active_scope)),
        "realtime": sorted(dict.fromkeys(realtime)),
        "watchlist": watchlist,
    }


def _runtime_pending_symbols(runtime_payload: dict[str, Any]) -> set[str]:
    runtime = dict(runtime_payload or {})
    symbols: list[str] = []
    for section, key in PENDING_SYMBOL_KEYS:
        section_payload = runtime.get(section) if isinstance(runtime.get(section), dict) else {}
        symbols.extend(_normalize_symbols(section_payload.get(key)))
    return set(symbols)


def _load_today_target_symbols_sqlite(environment: str, market_date: str, *, timeout: float) -> list[str]:
    if not market_date:
        return []
    rows: list[str] = []
    with open_pb_sqlite(readonly=True, timeout=timeout) as conn:
        query = """
            SELECT symbol
            FROM ibkr_targets
            WHERE environment = ?
              AND date = ?
        """
        for row in conn.execute(query, (str(environment or "live"), str(market_date))):
            symbol = _normalize_symbol(row["symbol"] if "symbol" in row.keys() else "")
            if symbol:
                rows.append(symbol)
    return sorted(dict.fromkeys(rows))


def _load_today_target_symbols_pb(pb_client: Any, environment: str, market_date: str) -> list[str]:
    if pb_client is None or not market_date:
        return []
    filter_text = f'environment = "{_escape_pb_filter(environment)}" && date = "{_escape_pb_filter(market_date)}"'
    try:
        if hasattr(pb_client, "get_all_records"):
            rows = pb_client.get_all_records(
                "ibkr_targets",
                filter=filter_text,
                sort="-score,-updated",
                max_pages=10,
            ) or []
        else:
            rows = pb_client.get_records(
                "ibkr_targets",
                filter=filter_text,
                sort="-score,-updated",
                per_page=200,
                page=1,
            ) or []
    except Exception:
        rows = []
    return sorted(
        dict.fromkeys(
            _normalize_symbol(row.get("symbol"))
            for row in rows
            if _normalize_symbol(row.get("symbol"))
        )
    )


def _resolve_universe_symbols(
    *,
    pb_client: Any,
    runtime_payload: dict[str, Any],
    environment: str,
    market_date: str,
    config: Any,
) -> tuple[list[str], str]:
    runtime_symbols = _runtime_universe_symbols(runtime_payload)
    if runtime_symbols:
        return runtime_symbols, "runtime_market_universe"

    timeout = _sqlite_timeout_seconds(config, environment)
    if _get_bool_setting(config, "ibkr_summary_freshness_direct_sqlite_enabled", environment, True):
        try:
            symbols = _load_today_target_symbols_sqlite(environment, market_date, timeout=timeout)
            if symbols:
                return symbols, "today_targets_sqlite"
        except Exception:
            pass

    symbols = _load_today_target_symbols_pb(pb_client, environment, market_date)
    return symbols, "today_targets"


def _latest_bars_sqlite(
    symbols: Iterable[str],
    intervals: Iterable[str],
    environment: str,
    *,
    timeout: float,
) -> dict[str, dict[str, dict[str, Any]]]:
    normalized_symbols = sorted(dict.fromkeys(_normalize_symbols(list(symbols or []))))
    normalized_intervals = [normalize_interval(item) for item in intervals if str(item or "").strip()]
    if not normalized_symbols or not normalized_intervals:
        return {}

    env_values = [str(environment or "live").strip().lower() or "live"]
    if env_values[0] == "live":
        env_values.append("")
    symbol_placeholders = ", ".join("?" for _ in normalized_symbols)
    env_placeholders = ", ".join("?" for _ in env_values)
    payload: dict[str, dict[str, dict[str, Any]]] = {}
    with open_pb_sqlite(readonly=True, timeout=timeout) as conn:
        for interval in normalized_intervals:
            params = [interval, *env_values, *normalized_symbols]
            rows = conn.execute(
                f"""
                WITH latest AS (
                    SELECT symbol, MAX(bar_time_ms) AS latest_bar_time_ms
                    FROM ibkr_bars
                    WHERE interval = ?
                      AND environment IN ({env_placeholders})
                      AND symbol IN ({symbol_placeholders})
                    GROUP BY symbol
                )
                SELECT b.id, b.symbol, b.interval, b.session_type, b.us_time, b.cn_time,
                       b.bar_time_ms, b.extra, b.environment, b.created, b.updated
                FROM ibkr_bars b
                JOIN latest l
                  ON l.symbol = b.symbol
                 AND l.latest_bar_time_ms = b.bar_time_ms
                WHERE b.interval = ?
                  AND b.environment IN ({env_placeholders})
                ORDER BY b.symbol ASC, b.environment DESC
                """,
                tuple(params + [interval, *env_values]),
            ).fetchall()
            for row in rows:
                symbol = _normalize_symbol(row["symbol"])
                if symbol:
                    payload.setdefault(symbol, {}).setdefault(interval, dict(row))
    return payload


def _source_5m_bucket_counts_sqlite(
    symbols: Iterable[str],
    expected_by_interval: dict[str, dict[str, Any]],
    environment: str,
    *,
    timeout: float,
) -> dict[str, dict[str, int]]:
    normalized_symbols = sorted(dict.fromkeys(_normalize_symbols(list(symbols or []))))
    if not normalized_symbols:
        return {}

    env_values = [str(environment or "live").strip().lower() or "live"]
    if env_values[0] == "live":
        env_values.append("")
    symbol_placeholders = ", ".join("?" for _ in normalized_symbols)
    env_placeholders = ", ".join("?" for _ in env_values)
    payload: dict[str, dict[str, int]] = {}
    with open_pb_sqlite(readonly=True, timeout=timeout) as conn:
        for interval, expected in (expected_by_interval or {}).items():
            normalized = normalize_interval(interval)
            if normalized in {"5m", "1d"} or not bool((expected or {}).get("current_due")):
                continue
            expected_close_ms = _to_int((expected or {}).get("expected_close_ms"), 0)
            window_start_ms = max(0, expected_close_ms - interval_to_ms(normalized))
            if expected_close_ms <= 0 or window_start_ms <= 0:
                continue
            rows = conn.execute(
                f"""
                SELECT symbol, COUNT(*) AS source_count
                FROM ibkr_bars
                WHERE interval = '5m'
                  AND environment IN ({env_placeholders})
                  AND symbol IN ({symbol_placeholders})
                  AND bar_time_ms >= ?
                  AND bar_time_ms < ?
                GROUP BY symbol
                """,
                tuple([*env_values, *normalized_symbols, window_start_ms, expected_close_ms]),
            ).fetchall()
            counts = {symbol: 0 for symbol in normalized_symbols}
            for row in rows:
                symbol = _normalize_symbol(row["symbol"] if "symbol" in row.keys() else "")
                if symbol:
                    counts[symbol] = _to_int(row["source_count"] if "source_count" in row.keys() else 0, 0)
            payload[normalized] = counts
    return payload


def _latest_bar_pb(pb_client: Any, symbol: str, interval: str, environment: str) -> dict[str, Any]:
    if pb_client is None:
        return {}
    filter_text = (
        f'symbol = "{_escape_pb_filter(symbol)}" && '
        f'interval = "{_escape_pb_filter(interval)}" && '
        f'{_environment_filter(environment)}'
    )
    try:
        if hasattr(pb_client, "get_first_record"):
            return dict(pb_client.get_first_record("ibkr_bars", filter=filter_text, sort="-bar_time_ms") or {})
        if hasattr(pb_client, "get_all_records"):
            rows = pb_client.get_all_records("ibkr_bars", filter=filter_text, sort="-bar_time_ms", max_pages=1) or []
            return dict(rows[0]) if rows else {}
        rows = pb_client.get_records("ibkr_bars", filter=filter_text, sort="-bar_time_ms", per_page=1, page=1) or []
        return dict(rows[0]) if rows else {}
    except Exception:
        return {}


def _latest_bars_pb(
    pb_client: Any,
    symbols: Iterable[str],
    intervals: Iterable[str],
    environment: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    payload: dict[str, dict[str, dict[str, Any]]] = {}
    if pb_client is None:
        return payload
    for symbol in sorted(dict.fromkeys(_normalize_symbols(list(symbols or [])))):
        for interval in [normalize_interval(item) for item in intervals if str(item or "").strip()]:
            row = _latest_bar_pb(pb_client, symbol, interval, environment)
            if row:
                payload.setdefault(symbol, {})[interval] = row
    return payload


def _load_latest_bars(
    *,
    pb_client: Any,
    symbols: list[str],
    intervals: list[str],
    environment: str,
    config: Any,
) -> tuple[dict[str, dict[str, dict[str, Any]]], str]:
    if not symbols:
        return {}, "none"

    if _get_bool_setting(config, "ibkr_summary_freshness_direct_sqlite_enabled", environment, True):
        try:
            rows = _latest_bars_sqlite(
                symbols,
                intervals,
                environment,
                timeout=_sqlite_timeout_seconds(config, environment),
            )
            return rows, "sqlite"
        except Exception:
            pass
    return _latest_bars_pb(pb_client, symbols, intervals, environment), "pocketbase"


def _load_5m_source_bucket_counts(
    *,
    symbols: list[str],
    expected_by_interval: dict[str, dict[str, Any]],
    environment: str,
    config: Any,
) -> dict[str, dict[str, int]]:
    if not symbols or not expected_by_interval:
        return {}
    if not _get_bool_setting(config, "ibkr_summary_freshness_direct_sqlite_enabled", environment, True):
        return {}
    try:
        return _source_5m_bucket_counts_sqlite(
            symbols,
            expected_by_interval,
            environment,
            timeout=_sqlite_timeout_seconds(config, environment),
        )
    except Exception:
        return {}


def _evaluate_symbol_interval(
    *,
    symbol: str,
    interval: str,
    rows: dict[str, dict[str, Any]],
    expected: dict[str, Any],
    latest_5m: dict[str, Any],
    session: str,
    session_start_ms: int,
    pending_symbols: set[str],
    now_ms: int,
    source_5m_count: int | None = None,
) -> dict[str, Any]:
    normalized = normalize_interval(interval)
    row_payload = _bar_payload(rows.get(normalized), normalized)
    latest_5m_close_ms = _to_int(latest_5m.get("latest_close_ms"), 0)
    expected_close_ms = _to_int(expected.get("expected_close_ms"), 0)
    latest_close_ms = _to_int(row_payload.get("latest_close_ms"), 0)
    current_due = bool(expected.get("current_due"))

    has_current_extended_5m = latest_5m_close_ms > 0 and session_start_ms > 0 and latest_5m_close_ms > session_start_ms
    extended_quiet = (
        normalized != "1d"
        and session_start_ms > 0
        and session in {"premarket", "afterhours", "closed"}
        and not has_current_extended_5m
        and symbol not in pending_symbols
    )

    status = "ready"
    reason = "ready"
    if expected_close_ms <= 0:
        status = "not_due"
        reason = "not_due"
    elif extended_quiet:
        status = "quiet_extended"
        reason = "quiet_extended"
    elif (
        normalized not in {"5m", "1d"}
        and current_due
        and source_5m_count == 0
        and symbol not in pending_symbols
    ):
        status = "not_due"
        reason = "no_source_5m_window"
    elif normalized not in {"5m", "1d"} and current_due and latest_5m_close_ms < expected_close_ms:
        status = "waiting_5m"
        reason = "waiting_5m"
    elif not current_due and latest_close_ms >= expected_close_ms:
        status = "not_due"
        reason = "not_due"
    elif latest_close_ms >= expected_close_ms:
        status = "ready"
        reason = "ready"
    elif latest_close_ms <= 0:
        status = "missing"
        reason = "missing"
    else:
        status = "overdue"
        reason = "overdue"

    overdue_ms = 0
    if status in {"overdue", "missing"} and expected_close_ms > 0:
        overdue_ms = max(0, int(now_ms) - expected_close_ms)

    return {
        "symbol": symbol,
        "interval": normalized,
        "status": status,
        "reason": reason,
        "ready": status == "ready",
        "expected_close_ms": expected_close_ms,
        "expected_close_us": str(expected.get("expected_close_us") or ""),
        "current_due": current_due,
        "latest_close_ms": latest_close_ms,
        "latest_close_us": str(row_payload.get("latest_close_us") or ""),
        "latest_start_ms": _to_int(row_payload.get("latest_start_ms"), 0),
        "latest_start_us": str(row_payload.get("latest_start_us") or ""),
        "latest_5m_close_ms": latest_5m_close_ms,
        "latest_5m_close_us": str(latest_5m.get("latest_close_us") or ""),
        "source_5m_count": source_5m_count,
        "overdue_ms": overdue_ms,
        "overdue_min": round(overdue_ms / 60000.0, 1) if overdue_ms > 0 else 0.0,
    }


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 100.0
    return round((int(numerator) / int(denominator)) * 100.0, 1)


def _interval_status(counts: dict[str, int], due_symbols: int) -> str:
    if due_symbols <= 0:
        if counts.get("quiet_extended"):
            return "quiet_extended"
        if counts.get("not_due"):
            return "not_due"
        return "empty"
    if counts.get("overdue") or counts.get("missing"):
        return "overdue"
    if counts.get("waiting_5m"):
        return "waiting_5m"
    return "ready"


def _sample(items: list[dict[str, Any]], statuses: set[str], limit: int = 8) -> list[dict[str, Any]]:
    rows = [item for item in items if str(item.get("status") or "") in statuses]
    rows.sort(key=lambda item: (-_to_int(item.get("overdue_ms"), 0), str(item.get("symbol") or "")))
    result = []
    for item in rows[: max(0, int(limit or 0))]:
        result.append(
            {
                "symbol": item.get("symbol"),
                "status": item.get("status"),
                "reason": item.get("reason"),
                "latest_close_us": item.get("latest_close_us"),
                "expected_close_us": item.get("expected_close_us"),
                "overdue_min": item.get("overdue_min"),
            }
        )
    return result


def _aggregate_interval(
    *,
    interval: str,
    items: list[dict[str, Any]],
    expected: dict[str, Any],
    total_symbols: int,
    session: str,
) -> dict[str, Any]:
    counts = {
        "ready": 0,
        "overdue": 0,
        "missing": 0,
        "waiting_5m": 0,
        "not_due": 0,
        "quiet_extended": 0,
        "closed_session": 0,
    }
    for item in items:
        status = str(item.get("status") or "")
        counts[status] = int(counts.get(status, 0) or 0) + 1
    due_symbols = max(
        0,
        int(total_symbols)
        - int(counts.get("quiet_extended", 0))
        - int(counts.get("not_due", 0))
        - int(counts.get("closed_session", 0)),
    )
    covered_symbols = max(0, int(total_symbols) - int(counts.get("quiet_extended", 0)))
    return {
        "interval": normalize_interval(interval),
        "session": session,
        "status": _interval_status(counts, due_symbols),
        "total_symbols": int(total_symbols),
        "due_symbols": due_symbols,
        "covered_symbols": covered_symbols,
        "ready_pct": _pct(int(counts.get("ready", 0)), due_symbols),
        "coverage_pct": _pct(covered_symbols, int(total_symbols)),
        "ready": int(counts.get("ready", 0)),
        "overdue": int(counts.get("overdue", 0)),
        "missing": int(counts.get("missing", 0)),
        "waiting_5m": int(counts.get("waiting_5m", 0)),
        "not_due": int(counts.get("not_due", 0)),
        "quiet_extended": int(counts.get("quiet_extended", 0)),
        "closed_session": int(counts.get("closed_session", 0)),
        "expected_close_ms": _to_int(expected.get("expected_close_ms"), 0),
        "expected_close_us": str(expected.get("expected_close_us") or ""),
        "current_due": bool(expected.get("current_due")),
        "sample_lag_symbols": _sample(items, {"overdue", "missing", "waiting_5m"}),
        "reason_counts": {key: int(value or 0) for key, value in sorted(counts.items()) if int(value or 0) > 0},
    }


def _overall_status(counts: dict[str, int], due_checks: int) -> str:
    if due_checks <= 0:
        if counts.get("quiet_extended"):
            return "quiet_extended"
        if counts.get("not_due"):
            return "not_due"
        return "empty"
    if counts.get("overdue") or counts.get("missing"):
        return "overdue"
    if counts.get("waiting_5m"):
        return "waiting_5m"
    return "ready"


def _aggregate_scope(
    *,
    name: str,
    label: str,
    symbols: list[str],
    intervals: list[str],
    items_by_interval: dict[str, dict[str, dict[str, Any]]],
    expected_by_interval: dict[str, dict[str, Any]],
    session: str,
    checked_at_ms: int,
    critical: bool,
    best_effort: bool = False,
) -> dict[str, Any]:
    normalized_symbols = sorted(dict.fromkeys(_normalize_symbols(symbols)))
    normalized_intervals = [
        normalize_interval(interval)
        for interval in intervals
        if normalize_interval(interval) in items_by_interval
    ]
    normalized_intervals = list(dict.fromkeys(normalized_intervals))
    interval_payloads: list[dict[str, Any]] = []
    counts = {
        "ready": 0,
        "overdue": 0,
        "missing": 0,
        "waiting_5m": 0,
        "not_due": 0,
        "quiet_extended": 0,
        "closed_session": 0,
    }
    samples: list[dict[str, Any]] = []
    for interval in normalized_intervals:
        by_symbol = items_by_interval.get(interval) or {}
        items = [
            by_symbol[symbol]
            for symbol in normalized_symbols
            if symbol in by_symbol
        ]
        for item in items:
            status = str(item.get("status") or "")
            counts[status] = int(counts.get(status, 0) or 0) + 1
        expected = expected_by_interval.get(interval) or {}
        interval_payloads.append(
            _aggregate_interval(
                interval=interval,
                items=items,
                expected=expected,
                total_symbols=len(normalized_symbols),
                session=session,
            )
        )
        samples.extend(_sample(items, {"overdue", "missing", "waiting_5m"}, limit=4))

    total_checks = len(normalized_symbols) * len(normalized_intervals)
    due_checks = max(
        0,
        total_checks
        - int(counts.get("quiet_extended", 0))
        - int(counts.get("not_due", 0))
        - int(counts.get("closed_session", 0)),
    )
    covered_checks = max(0, total_checks - int(counts.get("quiet_extended", 0)))
    overall = {
        "status": _overall_status(counts, due_checks),
        "ready_pct": _pct(int(counts.get("ready", 0)), due_checks),
        "coverage_pct": _pct(covered_checks, total_checks),
        "total_checks": total_checks,
        "due_checks": due_checks,
        "total_symbols": len(normalized_symbols),
        "symbols_total": len(normalized_symbols),
        "ready": int(counts.get("ready", 0)),
        "overdue": int(counts.get("overdue", 0)),
        "missing": int(counts.get("missing", 0)),
        "waiting_5m": int(counts.get("waiting_5m", 0)),
        "not_due": int(counts.get("not_due", 0)),
        "quiet_extended": int(counts.get("quiet_extended", 0)),
        "closed_session": int(counts.get("closed_session", 0)),
        "checked_at_ms": checked_at_ms,
        "checked_at_us": format_us_time(checked_at_ms),
        "sample_lag_symbols": samples[:8],
        "reason_counts": {key: int(value or 0) for key, value in sorted(counts.items()) if int(value or 0) > 0},
    }
    return {
        "name": name,
        "label": label,
        "status": str(overall.get("status") or "empty"),
        "critical": bool(critical),
        "best_effort": bool(best_effort),
        "symbols": normalized_symbols[:50],
        "symbols_total": len(normalized_symbols),
        "intervals": interval_payloads,
        "interval_names": normalized_intervals,
        "overall": overall,
    }


def build_data_freshness_summary(
    *,
    pb_client: Any = None,
    runtime_payload: dict[str, Any] | None = None,
    environment: str = "live",
    market_date: str = "",
    config: Any = None,
    now_ms: int | None = None,
    now_us: str = "",
    intervals: Iterable[str] | None = None,
) -> dict[str, Any]:
    runtime = dict(runtime_payload or {})
    data_environment = str(environment or "live").strip().lower() or "live"
    checked_at_ms = _now_ms(now_ms=now_ms, now_us=now_us)
    delay_seconds = _close_delay_seconds(config, data_environment)
    normalized_intervals = [
        normalize_interval(item)
        for item in (intervals or COMPUTE_INTERVALS)
        if str(item or "").strip()
    ]
    normalized_intervals = list(dict.fromkeys(normalized_intervals))
    if "5m" not in normalized_intervals:
        normalized_intervals.insert(0, "5m")

    symbols, universe_source = _resolve_universe_symbols(
        pb_client=pb_client,
        runtime_payload=runtime,
        environment=data_environment,
        market_date=market_date,
        config=config,
    )
    session = _session_kind(checked_at_ms)
    mature_5m_close_ms = _latest_mature_5m_close_ms(checked_at_ms, delay_seconds=delay_seconds)
    session_start_ms = _quiet_extended_start_ms(checked_at_ms, session)
    expected_by_interval = {
        interval: _expected_close_payload(mature_5m_close_ms, interval)
        for interval in normalized_intervals
    }
    if not symbols:
        return {
            "ok": True,
            "status": "empty",
            "environment": data_environment,
            "market_date": str(market_date or ""),
            "universe_source": universe_source,
            "total_symbols": 0,
            "symbols": [],
            "session": session,
            "checked_at_ms": checked_at_ms,
            "checked_at_us": format_us_time(checked_at_ms),
            "mature_5m_close_ms": mature_5m_close_ms,
            "mature_5m_close_us": format_us_time(mature_5m_close_ms) if mature_5m_close_ms > 0 else "",
            "intervals": [],
            "overall": {
                "status": "empty",
                "ready_pct": 100.0,
                "coverage_pct": 100.0,
                "total_checks": 0,
                "due_checks": 0,
                "ready": 0,
                "overdue": 0,
                "missing": 0,
                "waiting_5m": 0,
                "not_due": 0,
                "quiet_extended": 0,
                "checked_at_ms": checked_at_ms,
            },
            "source": "ibkr-api",
        }

    latest_rows, storage_source = _load_latest_bars(
        pb_client=pb_client,
        symbols=symbols,
        intervals=normalized_intervals,
        environment=data_environment,
        config=config,
    )
    source_5m_bucket_counts = _load_5m_source_bucket_counts(
        symbols=symbols,
        expected_by_interval=expected_by_interval,
        environment=data_environment,
        config=config,
    )
    pending_symbols = _runtime_pending_symbols(runtime)

    intervals_payload: list[dict[str, Any]] = []
    items_by_interval: dict[str, dict[str, dict[str, Any]]] = {}
    overall_counts = {
        "ready": 0,
        "overdue": 0,
        "missing": 0,
        "waiting_5m": 0,
        "not_due": 0,
        "quiet_extended": 0,
        "closed_session": 0,
    }
    overall_samples: list[dict[str, Any]] = []
    for interval in normalized_intervals:
        expected = expected_by_interval[interval]
        items: list[dict[str, Any]] = []
        interval_items_by_symbol: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            rows = latest_rows.get(symbol) if isinstance(latest_rows.get(symbol), dict) else {}
            latest_5m = _bar_payload(rows.get("5m"), "5m")
            item = _evaluate_symbol_interval(
                symbol=symbol,
                interval=interval,
                rows=rows,
                expected=expected,
                latest_5m=latest_5m,
                session=session,
                session_start_ms=session_start_ms,
                pending_symbols=pending_symbols,
                now_ms=checked_at_ms,
                source_5m_count=(source_5m_bucket_counts.get(interval) or {}).get(symbol),
            )
            items.append(item)
            interval_items_by_symbol[symbol] = item
            status = str(item.get("status") or "")
            overall_counts[status] = int(overall_counts.get(status, 0) or 0) + 1
        items_by_interval[interval] = interval_items_by_symbol
        interval_payload = _aggregate_interval(
            interval=interval,
            items=items,
            expected=expected,
            total_symbols=len(symbols),
            session=session,
        )
        intervals_payload.append(interval_payload)
        overall_samples.extend(_sample(items, {"overdue", "missing", "waiting_5m"}, limit=4))

    total_checks = len(symbols) * len(normalized_intervals)
    due_checks = max(
        0,
        total_checks
        - int(overall_counts.get("quiet_extended", 0))
        - int(overall_counts.get("not_due", 0))
        - int(overall_counts.get("closed_session", 0)),
    )
    covered_checks = max(0, total_checks - int(overall_counts.get("quiet_extended", 0)))
    overall = {
        "status": _overall_status(overall_counts, due_checks),
        "ready_pct": _pct(int(overall_counts.get("ready", 0)), due_checks),
        "coverage_pct": _pct(covered_checks, total_checks),
        "total_checks": total_checks,
        "due_checks": due_checks,
        "total_symbols": len(symbols),
        "ready": int(overall_counts.get("ready", 0)),
        "overdue": int(overall_counts.get("overdue", 0)),
        "missing": int(overall_counts.get("missing", 0)),
        "waiting_5m": int(overall_counts.get("waiting_5m", 0)),
        "not_due": int(overall_counts.get("not_due", 0)),
        "quiet_extended": int(overall_counts.get("quiet_extended", 0)),
        "closed_session": int(overall_counts.get("closed_session", 0)),
        "checked_at_ms": checked_at_ms,
        "checked_at_us": format_us_time(checked_at_ms),
        "sample_lag_symbols": overall_samples[:8],
        "reason_counts": {key: int(value or 0) for key, value in sorted(overall_counts.items()) if int(value or 0) > 0},
    }
    scope_symbols = _resolve_scope_symbols(runtime, symbols)
    higher_intervals = [
        interval
        for interval in ("15m", "30m", "1h", "4h")
        if interval in normalized_intervals
    ]
    active_trading_intervals = [
        interval
        for interval in ("5m", "15m", "30m", "1h", "4h")
        if interval in normalized_intervals
    ]
    scopes = {
        "realtime_5m": _aggregate_scope(
            name="realtime_5m",
            label="Realtime 5m",
            symbols=scope_symbols["realtime"],
            intervals=["5m"] if "5m" in normalized_intervals else [],
            items_by_interval=items_by_interval,
            expected_by_interval=expected_by_interval,
            session=session,
            checked_at_ms=checked_at_ms,
            critical=True,
        ),
        "active_trading": _aggregate_scope(
            name="active_trading",
            label="Active Trading",
            symbols=scope_symbols["active"] or scope_symbols["realtime"],
            intervals=active_trading_intervals,
            items_by_interval=items_by_interval,
            expected_by_interval=expected_by_interval,
            session=session,
            checked_at_ms=checked_at_ms,
            critical=True,
        ),
        "active_higher_timeframes": _aggregate_scope(
            name="active_higher_timeframes",
            label="Active HTF 15m-4h",
            symbols=scope_symbols["active"] or scope_symbols["realtime"],
            intervals=higher_intervals,
            items_by_interval=items_by_interval,
            expected_by_interval=expected_by_interval,
            session=session,
            checked_at_ms=checked_at_ms,
            critical=True,
        ),
        "watchlist_higher_timeframes": _aggregate_scope(
            name="watchlist_higher_timeframes",
            label="Watchlist HTF best-effort",
            symbols=scope_symbols["watchlist"],
            intervals=higher_intervals,
            items_by_interval=items_by_interval,
            expected_by_interval=expected_by_interval,
            session=session,
            checked_at_ms=checked_at_ms,
            critical=False,
            best_effort=True,
        ),
        "daily_1d": _aggregate_scope(
            name="daily_1d",
            label="Daily 1d",
            symbols=scope_symbols["all"],
            intervals=["1d"] if "1d" in normalized_intervals else [],
            items_by_interval=items_by_interval,
            expected_by_interval=expected_by_interval,
            session=session,
            checked_at_ms=checked_at_ms,
            critical=False,
        ),
    }
    primary_scope = "active_trading" if scopes["active_trading"]["symbols_total"] > 0 else "realtime_5m"
    display_overall = dict(scopes[primary_scope]["overall"])
    display_overall["scope"] = primary_scope
    display_overall["label"] = scopes[primary_scope]["label"]
    return {
        "ok": True,
        "status": str(overall.get("status") or "empty"),
        "primary_status": str(display_overall.get("status") or "empty"),
        "primary_scope": primary_scope,
        "display_overall": display_overall,
        "environment": data_environment,
        "market_date": str(market_date or ""),
        "universe_source": universe_source,
        "storage_source": storage_source,
        "total_symbols": len(symbols),
        "symbols": symbols[:50],
        "symbols_total": len(symbols),
        "session": session,
        "checked_at_ms": checked_at_ms,
        "checked_at_us": format_us_time(checked_at_ms),
        "mature_5m_close_ms": mature_5m_close_ms,
        "mature_5m_close_us": format_us_time(mature_5m_close_ms) if mature_5m_close_ms > 0 else "",
        "close_delay_sec": delay_seconds,
        "intervals": intervals_payload,
        "overall": overall,
        "scope_symbols": {key: len(value or []) for key, value in scope_symbols.items()},
        "scopes": scopes,
        "source": "ibkr-api",
    }


__all__ = [
    "build_data_freshness_summary",
    "extract_bar_close_time_ms",
]
