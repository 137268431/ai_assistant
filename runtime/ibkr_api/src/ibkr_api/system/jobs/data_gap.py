from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ibkr_api.orders.values import parse_boolean
from ibkr_compute.core.broker_mode import resolve_market_data_mode


BAR_INTERVAL_MS = 5 * 60 * 1000
DEFAULT_BAR_LAG_ALERT_MIN = 20
DEFAULT_INDICATOR_LAG_ALERT_MIN = 30
DEFAULT_GAP_ALERT_COOLDOWN_MIN = 30
DEFAULT_INDICATOR_REQUIRES_TARGETS = True
DEFAULT_DATA_GAP_INTERVALS = ("5m",)
SUPPORTED_DATA_GAP_INTERVALS = ("5m", "15m", "30m", "1h", "4h", "1d")
GAP_MONITOR_STATE_KEY = "system_gap_monitor"
GAP_ALERT_EXCLUDED_SYMBOLS = {"VIX"}
ET = ZoneInfo("America/New_York")

try:  # Prefer the shared evaluator when another workspace provides it.
    from ibkr_compute.market import freshness_evaluator as _freshness_evaluator
except Exception:  # pragma: no cover - exercised in forks without the shared module.
    _freshness_evaluator = None

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
EmitSystemEvent = Callable[..., dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _normalize_interval(value: Any) -> str:
    text = _to_text(value).lower()
    mapping = {
        "5": "5m",
        "5m": "5m",
        "15": "15m",
        "15m": "15m",
        "30": "30m",
        "30m": "30m",
        "60": "1h",
        "1h": "1h",
        "240": "4h",
        "4h": "4h",
        "d": "1d",
        "1d": "1d",
    }
    return mapping.get(text, text or "5m")


def _interval_to_ms(interval: str) -> int:
    normalized = _normalize_interval(interval)
    minutes = {
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "4h": 240,
        "1d": 1440,
    }.get(normalized, 5)
    return minutes * 60 * 1000


def _parse_intervals(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple, set)):
        candidates = list(raw)
    else:
        text = _to_text(raw)
        if text.lower() in {"", "default"}:
            candidates = list(DEFAULT_DATA_GAP_INTERVALS)
        elif text.lower() == "all":
            candidates = list(SUPPORTED_DATA_GAP_INTERVALS)
        else:
            candidates = [item.strip() for item in text.replace(";", ",").split(",")]
    output: list[str] = []
    for candidate in candidates:
        interval = _normalize_interval(candidate)
        if interval in SUPPORTED_DATA_GAP_INTERVALS and interval not in output:
            output.append(interval)
    return output or list(DEFAULT_DATA_GAP_INTERVALS)


def _config_int(
    config_value: ConfigValue | None,
    key: str,
    default: int,
    environment: str,
    *,
    minimum: int = 1,
) -> int:
    try:
        raw = config_value(key, str(default), environment) if config_value else default
    except Exception:
        raw = default
    return max(int(minimum), _to_int(raw, default))


def _config_bool(config_value: ConfigValue | None, key: str, default: bool, environment: str) -> bool:
    try:
        raw = config_value(key, "TRUE" if default else "FALSE", environment) if config_value else default
    except Exception:
        raw = default
    return parse_boolean(raw, default)


def _config_intervals(config_value: ConfigValue | None, key: str, default: tuple[str, ...], environment: str) -> list[str]:
    try:
        raw = config_value(key, ",".join(default), environment) if config_value else default
    except Exception:
        raw = default
    return _parse_intervals(raw)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _unique_sorted(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values or []:
        item = _to_text(value).upper()
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    output.sort()
    return output


def _alertable_symbols(symbols: list[Any]) -> list[str]:
    return [symbol for symbol in _unique_sorted(symbols) if symbol not in GAP_ALERT_EXCLUDED_SYMBOLS]


def _is_regular_session(row: dict[str, Any]) -> bool:
    return _to_text(row.get("session_type")).lower() == "regular"


def _get_state_data(pb: Any, state_key: str, environment: str, date_token: str) -> dict[str, Any]:
    try:
        record = pb.get_state(state_key, environment, date=date_token)
    except Exception:
        record = None
    return _as_dict((record or {}).get("data") if isinstance(record, dict) else {})


def _load_watchlist_symbols(pb: Any, environment: str) -> list[str]:
    filter_expr = f'(environment = "{environment}" || environment = "global" || environment = "") && symbol != ""'
    rows = pb.get_records("watchlist", filter=filter_expr, sort="symbol", per_page=500, page=1)
    return _unique_sorted([row.get("symbol") for row in rows or [] if isinstance(row, dict)])


def _load_target_symbols(pb: Any, environment: str, date_token: str) -> list[str]:
    return [row["symbol"] for row in _load_target_rows(pb, environment, date_token)]


def _load_target_rows(pb: Any, environment: str, date_token: str) -> list[dict[str, Any]]:
    filter_expr = (
        f'date = "{date_token}" && environment = "{environment}" && '
        '(status = "candidate" || status = "active")'
    )
    rows = pb.get_records("ibkr_targets", filter=filter_expr, sort="-score,-updated", per_page=500, page=1)
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = _to_text(row.get("symbol")).upper()
        if not symbol:
            continue
        by_symbol[symbol] = {
            "symbol": symbol,
            "status": _to_text(row.get("status")).lower(),
            "score": row.get("score"),
            "updated": _to_text(row.get("updated")),
        }
    return [by_symbol[symbol] for symbol in sorted(by_symbol)]


def _today_bounds_ms(today_start: str) -> tuple[int, int]:
    token = _to_text(today_start)
    if len(token) >= 10:
        token = token[:10]
    dt = datetime.strptime(token, "%Y-%m-%d").replace(tzinfo=ET, hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp() * 1000), int((dt + timedelta(days=1)).timestamp() * 1000)


def _parse_extra(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _parse_us_time_ms(value: Any) -> int:
    text = _to_text(value)
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text[:19], fmt).replace(tzinfo=ET).timestamp() * 1000)
        except Exception:
            continue
    return 0


def _format_us_time_ms(value: int) -> str:
    if int(value or 0) <= 0:
        return ""
    return datetime.fromtimestamp(int(value) / 1000, ET).strftime("%Y-%m-%d %H:%M:%S")


def _shared_bar_close_time_ms(row: dict[str, Any], interval: str) -> int:
    evaluator = _freshness_evaluator
    if evaluator is None:
        return 0
    for name in (
        "extract_bar_close_time_ms",
        "parse_bar_close_ms",
        "parse_bar_close_time_ms",
        "bar_close_time_ms",
        "get_bar_close_time_ms",
    ):
        func = getattr(evaluator, name, None)
        if not callable(func):
            continue
        for args, kwargs in (
            ((row,), {"interval": interval}),
            ((row, interval), {}),
            ((row,), {}),
        ):
            try:
                value = func(*args, **kwargs)
            except Exception:
                continue
            if isinstance(value, dict):
                value = value.get("bar_close_time_ms") or value.get("close_time_ms") or value.get("close_ms")
            close_ms = _to_int(value, 0)
            if close_ms > 0:
                return close_ms
    return 0


def _daily_close_from_start_ms(start_ms: int) -> int:
    if start_ms <= 0:
        return 0
    start = datetime.fromtimestamp(start_ms / 1000, ET)
    return int(start.replace(hour=16, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _fallback_bar_close_time_ms(row: dict[str, Any], interval: str) -> int:
    normalized = _normalize_interval(interval)
    start_ms = _parse_us_time_ms(row.get("us_time"))
    if start_ms <= 0:
        start_ms = _to_int(row.get("bar_time_ms"), 0)
    if start_ms <= 0:
        return 0
    if normalized == "1d":
        return _daily_close_from_start_ms(start_ms)
    return start_ms + _interval_to_ms(normalized)


def _bar_close_time_ms(row: dict[str, Any], interval: str) -> int:
    item = _as_dict(row)
    normalized = _normalize_interval(item.get("interval") or interval)
    extra = _parse_extra(item.get("extra"))
    for key in ("bar_close_time_ms", "close_time_ms", "close_ms"):
        close_ms = _to_int(extra.get(key), 0)
        if close_ms > 0:
            return close_ms
    close_us_ms = _parse_us_time_ms(extra.get("bar_close_us_time") or extra.get("close_us_time"))
    if close_us_ms > 0:
        return close_us_ms
    if extra:
        shared_ms = _shared_bar_close_time_ms(item, normalized)
        if shared_ms > 0:
            return shared_ms
    return _fallback_bar_close_time_ms(item, normalized)


def _bar_close_us_time(row: dict[str, Any], interval: str) -> str:
    item = _as_dict(row)
    extra = _parse_extra(item.get("extra"))
    close_us = _to_text(extra.get("bar_close_us_time") or extra.get("close_us_time"))
    if close_us:
        return close_us
    close_ms = _bar_close_time_ms(item, interval)
    return _format_us_time_ms(close_ms) if close_ms > 0 else _to_text(item.get("us_time"))


def _previous_business_day(value: datetime) -> datetime:
    cursor = value - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _expected_daily_close_ms_from_close(close_ms: int) -> int:
    if close_ms <= 0:
        return 0
    close_dt = datetime.fromtimestamp(close_ms / 1000, ET)
    if close_dt.hour * 60 + close_dt.minute >= 16 * 60:
        day = close_dt
    else:
        day = _previous_business_day(close_dt)
    return int(day.replace(hour=16, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _expected_interval_close_from_5m(latest_5m_close_ms: int, interval: str) -> int:
    normalized = _normalize_interval(interval)
    latest_close = _to_int(latest_5m_close_ms, 0)
    if latest_close <= 0:
        return 0
    if normalized == "5m":
        return latest_close
    if normalized == "1d":
        return _expected_daily_close_ms_from_close(latest_close)
    close_dt = datetime.fromtimestamp(latest_close / 1000, ET)
    interval_minutes = _interval_to_ms(normalized) // 60000
    total_minutes = close_dt.hour * 60 + close_dt.minute
    boundary_minutes = total_minutes - (total_minutes % interval_minutes)
    boundary = close_dt.replace(
        hour=boundary_minutes // 60,
        minute=boundary_minutes % 60,
        second=0,
        microsecond=0,
    )
    return int(boundary.timestamp() * 1000)


def _expected_interval_close_from_now(now_ms: int, interval: str) -> int:
    normalized = _normalize_interval(interval)
    now_value = _to_int(now_ms, 0)
    if now_value <= 0:
        return 0
    if normalized == "1d":
        return _expected_daily_close_ms_from_close(now_value)
    now_dt = datetime.fromtimestamp(now_value / 1000, ET)
    interval_minutes = _interval_to_ms(normalized) // 60000
    total_minutes = now_dt.hour * 60 + now_dt.minute
    boundary_minutes = total_minutes - (total_minutes % interval_minutes)
    boundary = now_dt.replace(
        hour=boundary_minutes // 60,
        minute=boundary_minutes % 60,
        second=0,
        microsecond=0,
    )
    return int(boundary.timestamp() * 1000)


def _indicator_interval_values(collection: str, interval: str) -> list[str]:
    text = _normalize_interval(interval)
    values = [text] if text else []
    if collection == "ibkr_indicators":
        aliases = {
            "5m": ["5"],
            "15m": ["15"],
            "30m": ["30"],
            "1h": ["60"],
            "4h": ["240"],
            "1d": ["D", "d"],
        }
        values.extend(aliases.get(text, []))
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output or [text]


def _pb_filter_quote(value: Any) -> str:
    return _to_text(value).replace("\\", "\\\\").replace('"', '\\"')


def _load_recent_rows_by_symbol_sqlite(collection: str, environment: str, interval: str, today_start: str, limit: int) -> dict[str, Any] | None:
    if collection not in {"ibkr_bars", "ibkr_indicators"}:
        return None
    try:
        from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite

        start_ms, end_ms = _today_bounds_ms(today_start)
        session_expr = "session_type" if collection == "ibkr_bars" else "'' AS session_type"
        index_hint = "INDEXED BY idx_ibkr_indicators_bartimems" if collection == "ibkr_indicators" else ""
        interval_values = _indicator_interval_values(collection, interval)
        interval_placeholders = ", ".join("?" for _ in interval_values)
        per_symbol_limit = max(1, min(12, int(limit or 12)))
        with open_pb_sqlite(readonly=True, timeout=2.0) as conn:
            rows = conn.execute(
                f"""
                WITH ranked AS (
                SELECT symbol, interval, bar_time_ms, us_time, {session_expr}, extra
                FROM {collection} {index_hint}
                WHERE environment = ?
                  AND interval IN ({interval_placeholders})
                  AND bar_time_ms >= ?
                  AND bar_time_ms < ?
                ),
                numbered AS (
                    SELECT
                        symbol,
                        interval,
                        bar_time_ms,
                        us_time,
                        session_type,
                        extra,
                        ROW_NUMBER() OVER (
                            PARTITION BY symbol
                            ORDER BY bar_time_ms DESC
                        ) AS row_rank
                    FROM ranked
                )
                SELECT symbol, interval, bar_time_ms, us_time, session_type, extra
                FROM numbered
                WHERE row_rank <= ?
                ORDER BY bar_time_ms DESC, symbol ASC
                """,
                (str(environment or "live"), *interval_values, start_ms, end_ms, per_symbol_limit),
            ).fetchall()
        return _rows_by_symbol_payload([dict(row) for row in rows], default_interval=interval)
    except Exception:
        return None


def _rows_by_symbol_payload(rows: list[dict[str, Any]], *, default_interval: str = "5m") -> dict[str, Any]:
    latest_by_symbol: dict[str, dict[str, Any]] = {}
    series_by_symbol: dict[str, list[dict[str, Any]]] = {}
    normalized_default = _normalize_interval(default_interval)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = _to_text(row.get("symbol")).upper()
        if not symbol:
            continue
        interval = _normalize_interval(row.get("interval") or normalized_default)
        close_ms = _bar_close_time_ms(row, interval)
        close_us = _bar_close_us_time(row, interval)
        extra = _parse_extra(row.get("extra"))
        if symbol not in latest_by_symbol:
            latest_by_symbol[symbol] = {
                "interval": interval,
                "bar_time_ms": _to_int(row.get("bar_time_ms"), 0),
                "bar_close_time_ms": close_ms,
                "bar_close_us_time": close_us,
                "us_time": _to_text(row.get("us_time")),
                "session_type": _to_text(row.get("session_type")).lower(),
                "extra": extra,
            }
        series = series_by_symbol.setdefault(symbol, [])
        if len(series) < 12:
            series.append(
                {
                    "interval": interval,
                    "bar_time_ms": _to_int(row.get("bar_time_ms"), 0),
                    "bar_close_time_ms": close_ms,
                    "bar_close_us_time": close_us,
                    "us_time": _to_text(row.get("us_time")),
                    "session_type": _to_text(row.get("session_type")).lower(),
                    "extra": extra,
                }
            )
    return {"latest_by_symbol": latest_by_symbol, "series_by_symbol": series_by_symbol}


def _load_recent_rows_by_symbol(pb: Any, collection: str, environment: str, interval: str, today_start: str, limit: int) -> dict[str, Any]:
    sqlite_payload = _load_recent_rows_by_symbol_sqlite(collection, environment, interval, today_start, limit)
    if sqlite_payload is not None:
        return sqlite_payload
    interval_values = _indicator_interval_values(collection, interval)
    if len(interval_values) == 1:
        interval_filter = f'interval = "{_pb_filter_quote(interval_values[0])}"'
    else:
        interval_filter = "(" + " || ".join(f'interval = "{_pb_filter_quote(item)}"' for item in interval_values) + ")"
    rows = pb.get_records(
        collection,
        filter=(
            f'environment = "{environment}" && '
            f'{interval_filter} && '
            f'us_time >= "{today_start}"'
        ),
        sort="-bar_time_ms",
        per_page=max(1, limit),
        page=1,
    )
    normalized_values = {_normalize_interval(value) for value in interval_values}
    filtered_rows = [
        row
        for row in list(rows or [])
        if _normalize_interval(_as_dict(row).get("interval") or interval) in normalized_values
    ]
    return _rows_by_symbol_payload(filtered_rows, default_interval=interval)


def _empty_cross_environment_bar_payload() -> dict[str, Any]:
    return {
        "count": 0,
        "environments": {},
        "symbols": [],
        "latest_us_time": "",
        "samples": [],
    }


def _cross_environment_bar_payload(rows: list[dict[str, Any]], samples: list[dict[str, Any]]) -> dict[str, Any]:
    environments: dict[str, int] = {}
    symbols: set[str] = set()
    latest_ms = 0
    latest_us_time = ""
    total = 0
    for row in rows or []:
        env = _to_text(_as_dict(row).get("environment")).lower()
        count = _to_int(_as_dict(row).get("count"), 0)
        if not env or count <= 0:
            continue
        environments[env] = environments.get(env, 0) + count
        total += count
        row_latest_ms = _to_int(_as_dict(row).get("latest_bar_time_ms"), 0)
        if row_latest_ms >= latest_ms:
            latest_ms = row_latest_ms
            latest_us_time = _to_text(_as_dict(row).get("latest_us_time"))
    sample_rows: list[dict[str, Any]] = []
    for row in samples or []:
        item = _as_dict(row)
        symbol = _to_text(item.get("symbol")).upper()
        if symbol:
            symbols.add(symbol)
        sample_rows.append(
            {
                "environment": _to_text(item.get("environment")).lower(),
                "symbol": symbol,
                "us_time": _to_text(item.get("us_time")),
            }
        )
    return {
        "count": total,
        "environments": environments,
        "symbols": sorted(symbols),
        "latest_us_time": latest_us_time,
        "samples": sample_rows,
    }


def _load_cross_environment_bars_sqlite(environment: str, interval: str, today_start: str, limit: int) -> dict[str, Any] | None:
    data_environment = _to_text(environment).lower()
    if data_environment not in {"live", "paper"}:
        return _empty_cross_environment_bar_payload()
    try:
        from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite

        start_ms, end_ms = _today_bounds_ms(today_start)
        sample_limit = max(1, min(12, int(limit or 12)))
        with open_pb_sqlite(readonly=True, timeout=2.0) as conn:
            rows = conn.execute(
                """
                SELECT
                    environment,
                    COUNT(*) AS count,
                    MAX(bar_time_ms) AS latest_bar_time_ms,
                    MAX(us_time) AS latest_us_time
                FROM ibkr_bars
                WHERE environment IN ('live', 'paper')
                  AND environment != ?
                  AND interval = ?
                  AND bar_time_ms >= ?
                  AND bar_time_ms < ?
                GROUP BY environment
                """,
                (data_environment, interval, start_ms, end_ms),
            ).fetchall()
            samples = conn.execute(
                """
                SELECT environment, symbol, us_time
                FROM ibkr_bars
                WHERE environment IN ('live', 'paper')
                  AND environment != ?
                  AND interval = ?
                  AND bar_time_ms >= ?
                  AND bar_time_ms < ?
                ORDER BY bar_time_ms DESC, symbol ASC
                LIMIT ?
                """,
                (data_environment, interval, start_ms, end_ms, sample_limit),
            ).fetchall()
        return _cross_environment_bar_payload([dict(row) for row in rows], [dict(row) for row in samples])
    except Exception:
        return None


def _load_cross_environment_bars(pb: Any, environment: str, interval: str, today_start: str, limit: int) -> dict[str, Any]:
    sqlite_payload = _load_cross_environment_bars_sqlite(environment, interval, today_start, limit)
    if sqlite_payload is not None:
        return sqlite_payload
    data_environment = _to_text(environment).lower()
    if data_environment not in {"live", "paper"}:
        return _empty_cross_environment_bar_payload()
    rows = pb.get_records(
        "ibkr_bars",
        filter=(
            '(environment = "live" || environment = "paper") && '
            f'environment != "{_pb_filter_quote(data_environment)}" && '
            f'interval = "{_pb_filter_quote(interval)}" && '
            f'us_time >= "{today_start}"'
        ),
        sort="-bar_time_ms",
        per_page=max(1, limit),
        page=1,
    )
    sample_rows = []
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        item = _as_dict(row)
        env = _to_text(item.get("environment")).lower()
        if not env or env == data_environment or env not in {"live", "paper"}:
            continue
        bucket = grouped.setdefault(env, {"environment": env, "count": 0, "latest_bar_time_ms": 0, "latest_us_time": ""})
        bucket["count"] += 1
        bar_ms = _to_int(item.get("bar_time_ms"), 0)
        if bar_ms >= _to_int(bucket.get("latest_bar_time_ms"), 0):
            bucket["latest_bar_time_ms"] = bar_ms
            bucket["latest_us_time"] = _to_text(item.get("us_time"))
        if len(sample_rows) < max(1, min(12, int(limit or 12))):
            sample_rows.append(item)
    return _cross_environment_bar_payload(list(grouped.values()), sample_rows)


def _build_gap_fingerprint(summary: dict[str, Any]) -> str:
    return str(
        {
            "latest_bar_time_ms": summary.get("latest_bar_time_ms") or 0,
            "latest_bar_close_time_ms": summary.get("latest_bar_close_time_ms") or 0,
            "bar_lag_symbols": list(summary.get("bar_lag_symbols") or [])[:12],
            "bar_lag_intervals": list(summary.get("bar_lag_intervals") or [])[:12],
            "indicator_lag_symbols": list(summary.get("indicator_lag_symbols") or [])[:12],
            "indicator_lag_intervals": list(summary.get("indicator_lag_intervals") or [])[:12],
            "sequence_gap_examples": list(summary.get("sequence_gap_examples") or [])[:6],
            "cross_environment_bar_count": summary.get("cross_environment_bar_count") or 0,
            "cross_environment_environments": summary.get("cross_environment_environments") or {},
        }
    )


def _count_target_statuses(target_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in target_rows or []:
        status = _to_text(_as_dict(row).get("status")).lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _build_indicator_lag_reason_hint(details: list[dict[str, Any]]) -> str:
    if not details:
        return ""
    statuses = {_to_text(_as_dict(item).get("target_status")).lower() for item in details}
    reasons = {_to_text(_as_dict(item).get("reason")).lower() for item in details}
    if statuses == {"candidate"} and reasons == {"missing_today"}:
        return "全部为 candidate 且缺当日到期指标；通常表示候选标的只有 bars 回补，没有进入实时 compute/指标物化。"
    if "missing_today" in reasons:
        return "存在缺当日到期指标；优先检查该标的是否进入 compute 调度、materialize 是否开启、指标写入是否失败。"
    return "已有指标但落后对应周期 bar close；优先检查 compute 调度、indicator_flush 和 compute_busy/队列积压。"


def _format_indicator_lag_sample(details: list[dict[str, Any]], limit: int = 6) -> str:
    parts: list[str] = []
    for item in details[: max(1, int(limit or 1))]:
        detail = _as_dict(item)
        symbol = _to_text(detail.get("symbol"))
        status = _to_text(detail.get("target_status")) or "target"
        interval = _to_text(detail.get("interval")) or "5m"
        bar_time = _to_text(detail.get("bar_close_us_time") or detail.get("bar_us_time"))[-8:-3] or "bar?"
        indicator_time = _to_text(detail.get("indicator_close_us_time") or detail.get("indicator_us_time"))[-8:-3]
        reason = _to_text(detail.get("reason")).lower()
        if reason == "missing_today":
            parts.append(f"{symbol}({status}/{interval}): 缺当日指标，bar close {bar_time}")
            continue
        lag_min = _to_int(detail.get("lag_min"), 0)
        parts.append(f"{symbol}({status}/{interval}): {lag_min}分钟 {indicator_time or '指标?'}->{bar_time}")
    return "; ".join(part for part in parts if part)


def _format_max_indicator_lag(gaps: dict[str, Any]) -> str:
    parts: list[str] = []
    max_lag = _to_int(gaps.get("max_indicator_lag_min"), 0)
    missing = _to_int(gaps.get("indicator_missing_count"), 0)
    if max_lag > 0:
        parts.append(f"{max_lag}分钟")
    if missing > 0:
        parts.append(f"缺当日到期指标 {missing}")
    return " / ".join(parts) if parts else "0分钟"


def load_data_gap_summary(
    pb: Any,
    *,
    environment: str,
    date_token: str,
    today_start: str,
    bar_lag_alert_min: int = DEFAULT_BAR_LAG_ALERT_MIN,
    indicator_lag_alert_min: int = DEFAULT_INDICATOR_LAG_ALERT_MIN,
    indicator_requires_targets: bool = DEFAULT_INDICATOR_REQUIRES_TARGETS,
    intervals: list[str] | tuple[str, ...] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    watchlist_symbols = _load_watchlist_symbols(pb, environment)
    target_rows = _load_target_rows(pb, environment, date_token)
    target_symbols = [row["symbol"] for row in target_rows]
    target_status_by_symbol = {
        _to_text(row.get("symbol")).upper(): _to_text(row.get("status")).lower()
        for row in target_rows
        if _to_text(row.get("symbol"))
    }
    monitored_intervals = _parse_intervals(intervals or DEFAULT_DATA_GAP_INTERVALS)
    if "5m" not in monitored_intervals:
        monitored_intervals.insert(0, "5m")
    bars_by_interval = {
        interval: _load_recent_rows_by_symbol(pb, "ibkr_bars", environment, interval, today_start, 1200)
        for interval in monitored_intervals
    }
    indicators_by_interval = {
        interval: _load_recent_rows_by_symbol(pb, "ibkr_indicators", environment, interval, today_start, 1200)
        for interval in monitored_intervals
    }
    bars = bars_by_interval.get("5m") or {"latest_by_symbol": {}, "series_by_symbol": {}}
    cross_environment_bars = _load_cross_environment_bars(pb, environment, "5m", today_start, 12)
    latest_bar_by_symbol = _as_dict(bars.get("latest_by_symbol"))
    monitored_symbols = target_symbols or list(latest_bar_by_symbol.keys())
    symbols = _alertable_symbols(list(monitored_symbols))
    excluded_symbols = _unique_sorted(
        [
            symbol
            for symbol in list(monitored_symbols)
            if _to_text(symbol).upper() in GAP_ALERT_EXCLUDED_SYMBOLS
        ]
    )
    bar_lag_alert_ms = max(1, int(bar_lag_alert_min or DEFAULT_BAR_LAG_ALERT_MIN)) * 60 * 1000
    indicator_lag_alert_ms = max(1, int(indicator_lag_alert_min or DEFAULT_INDICATOR_LAG_ALERT_MIN)) * 60 * 1000
    indicator_symbols = symbols
    if indicator_requires_targets and not target_symbols:
        indicator_symbols = []
    indicator_symbol_set = set(indicator_symbols)

    latest_bar_time_ms = 0
    latest_bar_close_time_ms = 0
    latest_bar_symbol = ""
    ignored_non_regular_symbols: list[str] = []
    for symbol in symbols:
        bucket = _as_dict(latest_bar_by_symbol.get(symbol))
        bar_close_ms = _to_int(bucket.get("bar_close_time_ms"), 0)
        if bar_close_ms > 0 and not _is_regular_session(bucket):
            ignored_non_regular_symbols.append(symbol)
            continue
        if bar_close_ms > latest_bar_close_time_ms:
            latest_bar_close_time_ms = bar_close_ms
            latest_bar_time_ms = _to_int(bucket.get("bar_time_ms"), 0)
            latest_bar_symbol = symbol

    bar_lag_symbols: list[str] = []
    indicator_lag_symbols: list[str] = []
    indicator_missing_symbols: list[str] = []
    indicator_lag_details: list[dict[str, Any]] = []
    bar_lag_details: list[dict[str, Any]] = []
    sequence_gap_examples: list[dict[str, Any]] = []
    max_bar_lag_ms = 0
    max_indicator_lag_ms = 0
    interval_summaries: dict[str, dict[str, Any]] = {}
    for interval in monitored_intervals:
        interval_bars = bars_by_interval.get(interval) or {"latest_by_symbol": {}, "series_by_symbol": {}}
        interval_indicators = indicators_by_interval.get(interval) or {"latest_by_symbol": {}, "series_by_symbol": {}}
        interval_latest_bars = _as_dict(interval_bars.get("latest_by_symbol"))
        interval_latest_indicators = _as_dict(interval_indicators.get("latest_by_symbol"))
        expected_close_ms = _expected_interval_close_from_5m(latest_bar_close_time_ms, interval)
        wall_expected_close_ms = _expected_interval_close_from_now(_to_int(now_ms, 0), interval)
        waiting_5m = bool(interval != "5m" and wall_expected_close_ms > 0 and wall_expected_close_ms > expected_close_ms)
        interval_bar_lag_symbols: list[str] = []
        interval_indicator_lag_symbols: list[str] = []
        interval_indicator_missing_symbols: list[str] = []
        interval_quiet_symbols: list[str] = []
        interval_missing_bar_symbols: list[str] = []
        interval_waiting_5m_symbols: list[str] = []
        interval_max_bar_lag_ms = 0
        interval_max_indicator_lag_ms = 0

        for symbol in symbols:
            latest_bar = _as_dict(interval_latest_bars.get(symbol))
            bar_close_ms = _to_int(latest_bar.get("bar_close_time_ms"), 0)
            if bar_close_ms > 0 and not _is_regular_session(latest_bar):
                interval_quiet_symbols.append(symbol)
                if interval == "5m":
                    ignored_non_regular_symbols.append(symbol)
                continue
            if waiting_5m:
                interval_waiting_5m_symbols.append(symbol)
                continue
            if interval != "5m" and expected_close_ms > 0:
                symbol_5m_bar = _as_dict(latest_bar_by_symbol.get(symbol))
                symbol_5m_close_ms = _to_int(symbol_5m_bar.get("bar_close_time_ms"), 0)
                symbol_expected_close_ms = _expected_interval_close_from_5m(symbol_5m_close_ms, interval)
                if symbol_expected_close_ms < expected_close_ms:
                    interval_waiting_5m_symbols.append(symbol)
                    continue
            if expected_close_ms > 0:
                if bar_close_ms <= 0:
                    interval_missing_bar_symbols.append(symbol)
                    lag_ms = (
                        max(0, _to_int(now_ms, 0) - expected_close_ms)
                        if _to_int(now_ms, 0) > 0
                        else bar_lag_alert_ms + 1
                    )
                else:
                    lag_ms = expected_close_ms - bar_close_ms
                if lag_ms >= bar_lag_alert_ms:
                    interval_bar_lag_symbols.append(symbol)
                    max_bar_lag_ms = max(max_bar_lag_ms, lag_ms)
                    interval_max_bar_lag_ms = max(interval_max_bar_lag_ms, lag_ms)
                    bar_lag_details.append(
                        {
                            "symbol": symbol,
                            "interval": interval,
                            "expected_close_ms": expected_close_ms,
                            "expected_close_us_time": _format_us_time_ms(expected_close_ms),
                            "bar_close_time_ms": bar_close_ms,
                            "bar_close_us_time": _to_text(latest_bar.get("bar_close_us_time")),
                            "lag_min": round(lag_ms / 60000) if bar_close_ms > 0 else None,
                            "reason": "missing_today" if bar_close_ms <= 0 else "lag_over_threshold",
                        }
                    )

            if symbol in indicator_symbol_set and bar_close_ms > 0 and (expected_close_ms <= 0 or bar_close_ms >= expected_close_ms):
                latest_indicator = _as_dict(interval_latest_indicators.get(symbol))
                indicator_close_ms = _to_int(latest_indicator.get("bar_close_time_ms"), 0)
                indicator_lag_ms = bar_close_ms - indicator_close_ms
                if indicator_lag_ms > indicator_lag_alert_ms:
                    interval_indicator_lag_symbols.append(symbol)
                    target_status = _to_text(target_status_by_symbol.get(symbol)) or "unknown"
                    if indicator_close_ms > 0:
                        max_indicator_lag_ms = max(max_indicator_lag_ms, indicator_lag_ms)
                        interval_max_indicator_lag_ms = max(interval_max_indicator_lag_ms, indicator_lag_ms)
                        reason = "lag_over_threshold"
                        lag_min: int | None = round(indicator_lag_ms / 60000)
                    else:
                        interval_indicator_missing_symbols.append(symbol)
                        indicator_missing_symbols.append(symbol)
                        reason = "missing_today"
                        lag_min = None
                    indicator_lag_details.append(
                        {
                            "symbol": symbol,
                            "interval": interval,
                            "target_status": target_status,
                            "bar_us_time": _to_text(latest_bar.get("us_time")),
                            "bar_close_us_time": _to_text(latest_bar.get("bar_close_us_time")),
                            "indicator_us_time": _to_text(latest_indicator.get("us_time")),
                            "indicator_close_us_time": _to_text(latest_indicator.get("bar_close_us_time")),
                            "lag_min": lag_min,
                            "reason": reason,
                        }
                    )

        bar_lag_symbols.extend(interval_bar_lag_symbols)
        indicator_lag_symbols.extend(interval_indicator_lag_symbols)
        interval_summaries[interval] = {
            "interval": interval,
            "expected_close_ms": expected_close_ms,
            "expected_close_us_time": _format_us_time_ms(expected_close_ms),
            "wall_expected_close_ms": wall_expected_close_ms,
            "wall_expected_close_us_time": _format_us_time_ms(wall_expected_close_ms),
            "waiting_5m": waiting_5m,
            "waiting_5m_symbols": _unique_sorted(interval_waiting_5m_symbols),
            "waiting_5m_count": len(set(interval_waiting_5m_symbols)),
            "bar_lag_symbols": _unique_sorted(interval_bar_lag_symbols),
            "bar_lag_count": len(set(interval_bar_lag_symbols)),
            "missing_bar_symbols": _unique_sorted(interval_missing_bar_symbols),
            "missing_bar_count": len(set(interval_missing_bar_symbols)),
            "indicator_lag_symbols": _unique_sorted(interval_indicator_lag_symbols),
            "indicator_lag_count": len(set(interval_indicator_lag_symbols)),
            "indicator_missing_symbols": _unique_sorted(interval_indicator_missing_symbols),
            "indicator_missing_count": len(set(interval_indicator_missing_symbols)),
            "quiet_extended_symbols": _unique_sorted(interval_quiet_symbols),
            "quiet_extended_count": len(set(interval_quiet_symbols)),
            "max_bar_lag_min": round(interval_max_bar_lag_ms / 60000) if interval_max_bar_lag_ms else 0,
            "max_indicator_lag_min": round(interval_max_indicator_lag_ms / 60000) if interval_max_indicator_lag_ms else 0,
        }

    bar_lag_symbols = _unique_sorted(bar_lag_symbols)
    indicator_lag_symbols = _unique_sorted(indicator_lag_symbols)
    indicator_missing_symbols = _unique_sorted(indicator_missing_symbols)

    for symbol in symbols:
        if len(sequence_gap_examples) >= 6:
            continue
        series = list(_as_dict(bars.get("series_by_symbol")).get(symbol) or [])
        if len(series) < 3:
            continue
        series.sort(key=lambda item: _to_int(_as_dict(item).get("bar_close_time_ms"), 0))
        for index in range(1, len(series)):
            prev = _as_dict(series[index - 1])
            curr = _as_dict(series[index])
            if _to_text(prev.get("session_type")).lower() != "regular" or _to_text(curr.get("session_type")).lower() != "regular":
                continue
            delta_ms = _to_int(curr.get("bar_close_time_ms"), 0) - _to_int(prev.get("bar_close_time_ms"), 0)
            if BAR_INTERVAL_MS < delta_ms <= 6 * BAR_INTERVAL_MS:
                sequence_gap_examples.append(
                    {
                        "symbol": symbol,
                        "prev_us_time": _to_text(prev.get("bar_close_us_time") or prev.get("us_time")),
                        "next_us_time": _to_text(curr.get("bar_close_us_time") or curr.get("us_time")),
                        "missing_points": max(round(delta_ms / BAR_INTERVAL_MS) - 1, 1),
                    }
                )
                break

    summary = {
        "watchlist_count": len(watchlist_symbols),
        "target_count": len(target_symbols),
        "target_status_counts": _count_target_statuses(target_rows),
        "monitored_symbol_count": len(monitored_symbols),
        "alertable_symbol_count": len(symbols),
        "indicator_monitored_symbol_count": len(indicator_symbols),
        "excluded_gap_symbols": excluded_symbols,
        "ignored_non_regular_symbols": _unique_sorted(ignored_non_regular_symbols),
        "today_bar_symbol_count": len(latest_bar_by_symbol),
        "bar_lag_alert_min": round(bar_lag_alert_ms / 60000),
        "indicator_lag_alert_min": round(indicator_lag_alert_ms / 60000),
        "indicator_requires_targets": bool(indicator_requires_targets),
        "monitored_intervals": monitored_intervals,
        "latest_bar_time_ms": latest_bar_time_ms,
        "latest_bar_close_time_ms": latest_bar_close_time_ms,
        "latest_bar_symbol": latest_bar_symbol,
        "latest_bar_us_time": _to_text(
            _as_dict(latest_bar_by_symbol.get(latest_bar_symbol)).get("bar_close_us_time")
            or _as_dict(latest_bar_by_symbol.get(latest_bar_symbol)).get("us_time")
        ),
        "bar_lag_symbols": bar_lag_symbols,
        "bar_lag_details": bar_lag_details,
        "bar_lag_intervals": sorted({_to_text(item.get("interval")) for item in bar_lag_details if _to_text(item.get("interval"))}),
        "indicator_lag_symbols": indicator_lag_symbols,
        "indicator_lag_intervals": sorted({_to_text(item.get("interval")) for item in indicator_lag_details if _to_text(item.get("interval"))}),
        "indicator_missing_symbols": indicator_missing_symbols,
        "indicator_lag_details": indicator_lag_details,
        "interval_summaries": interval_summaries,
        "sequence_gap_examples": sequence_gap_examples,
        "bar_lag_count": len(bar_lag_symbols),
        "indicator_lag_count": len(indicator_lag_symbols),
        "indicator_missing_count": len(indicator_missing_symbols),
        "sequence_gap_count": len(sequence_gap_examples),
        "max_bar_lag_min": round(max_bar_lag_ms / 60000) if max_bar_lag_ms else 0,
        "max_indicator_lag_min": round(max_indicator_lag_ms / 60000) if max_indicator_lag_ms else 0,
        "market_activity_detected": latest_bar_close_time_ms > 0,
        "cross_environment_bar_count": _to_int(cross_environment_bars.get("count"), 0),
        "cross_environment_environments": _as_dict(cross_environment_bars.get("environments")),
        "cross_environment_symbols": list(cross_environment_bars.get("symbols") or []),
        "cross_environment_latest_us_time": _to_text(cross_environment_bars.get("latest_us_time")),
        "cross_environment_samples": list(cross_environment_bars.get("samples") or []),
    }
    summary["indicator_lag_reason_hint"] = _build_indicator_lag_reason_hint(indicator_lag_details)
    summary["has_issue"] = bool(
        summary["cross_environment_bar_count"]
        or (
            summary["market_activity_detected"]
            and (summary["bar_lag_count"] or summary["indicator_lag_count"] or summary["sequence_gap_count"])
        )
    )
    summary["fingerprint"] = _build_gap_fingerprint(summary)
    return summary


def build_data_gap_guard_response(
    pb: Any,
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    emit_system_event: EmitSystemEvent,
    config_value: ConfigValue | None = None,
) -> tuple[dict[str, Any], int]:
    request_payload = payload or {}
    data_environment = resolve_market_data_mode(request_payload.get("market_data_mode"))
    environment = data_environment
    times = time_strings()
    event_now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    scan_now_ms = _parse_us_time_ms(times.get("us")) or event_now_ms
    today_start = f"{times['date']} 00:00:00"
    bar_lag_alert_min = _config_int(
        config_value,
        "system_data_gap_bar_lag_alert_min",
        DEFAULT_BAR_LAG_ALERT_MIN,
        environment,
    )
    indicator_lag_alert_min = _config_int(
        config_value,
        "system_data_gap_indicator_lag_alert_min",
        DEFAULT_INDICATOR_LAG_ALERT_MIN,
        environment,
    )
    alert_cooldown_min = _config_int(
        config_value,
        "system_data_gap_alert_cooldown_min",
        DEFAULT_GAP_ALERT_COOLDOWN_MIN,
        environment,
    )
    indicator_requires_targets = _config_bool(
        config_value,
        "system_data_gap_indicator_requires_targets",
        DEFAULT_INDICATOR_REQUIRES_TARGETS,
        environment,
    )
    monitored_intervals = _config_intervals(
        config_value,
        "system_data_gap_intervals",
        DEFAULT_DATA_GAP_INTERVALS,
        environment,
    )
    gaps = load_data_gap_summary(
        pb,
        environment=data_environment,
        date_token=times["date"],
        today_start=today_start,
        bar_lag_alert_min=bar_lag_alert_min,
        indicator_lag_alert_min=indicator_lag_alert_min,
        indicator_requires_targets=indicator_requires_targets,
        intervals=monitored_intervals,
        now_ms=scan_now_ms,
    )
    gaps["data_environment"] = data_environment
    gaps["alert_cooldown_min"] = alert_cooldown_min
    next_state = {
        "last_gap_scan_at": times["us"],
        "last_gap_fingerprint": gaps.get("fingerprint") or "",
    }
    current_state = _get_state_data(pb, GAP_MONITOR_STATE_KEY, environment, times["date"])
    event_result: dict[str, Any] = {}

    if not gaps.get("has_issue"):
        next_state["last_gap_issue_at"] = ""
        pb.upsert_state(GAP_MONITOR_STATE_KEY, environment, next_state, date=times["date"])
        return {
            "ok": True,
            "environment": environment,
            "summary": gaps,
            "state": next_state,
            "source": "ibkr-api",
            "job_id": "system_data_gap_guard",
        }, 200

    last_alert_hash = _to_text(current_state.get("last_gap_alert_hash"))
    last_alert_ms = _to_int(current_state.get("last_gap_alert_ms"), 0)
    should_notify = (
        gaps.get("fingerprint") != last_alert_hash
        or last_alert_ms <= 0
        or (event_now_ms - last_alert_ms) >= alert_cooldown_min * 60 * 1000
    )
    if should_notify:
        indicator_intervals = list(gaps.get("indicator_lag_intervals") or [])
        bar_intervals = list(gaps.get("bar_lag_intervals") or [])
        detail = {
            "检查时间": times["us"],
            "最新bar时间": gaps.get("latest_bar_us_time") or "unknown",
            "最新bar close": gaps.get("latest_bar_us_time") or "unknown",
            "bars缺口数": str(gaps.get("bar_lag_count") or 0),
            "bars最大滞后": f"{_to_int(gaps.get('max_bar_lag_min'), 0)}分钟",
            "指标滞后数": str(gaps.get("indicator_lag_count") or 0),
            "指标周期": ", ".join(indicator_intervals) if indicator_intervals else "5m",
            "指标滞后判定": f"缺当日到期指标 或 指标 close 落后对应周期 bar close >{_to_int(gaps.get('indicator_lag_alert_min'), DEFAULT_INDICATOR_LAG_ALERT_MIN)}分钟",
            "指标最大滞后": _format_max_indicator_lag(gaps),
            "序列缺口数": str(gaps.get("sequence_gap_count") or 0),
        }
        if bar_intervals:
            detail["bars异常周期"] = ", ".join(bar_intervals)
        cross_environment_count = _to_int(gaps.get("cross_environment_bar_count"), 0)
        if cross_environment_count > 0:
            detail["跨环境bars"] = str(cross_environment_count)
            cross_envs = _as_dict(gaps.get("cross_environment_environments"))
            if cross_envs:
                detail["跨环境来源"] = ", ".join(f"{key}:{cross_envs[key]}" for key in sorted(cross_envs))
            if _to_text(gaps.get("cross_environment_latest_us_time")):
                detail["跨环境最新bar"] = _to_text(gaps.get("cross_environment_latest_us_time"))
            samples = [_as_dict(item) for item in list(gaps.get("cross_environment_samples") or [])[:6]]
            if samples:
                detail["跨环境样本"] = "; ".join(
                    f"{_to_text(item.get('environment'))}/{_to_text(item.get('symbol'))} {_to_text(item.get('us_time'))}"
                    for item in samples
                    if _to_text(item.get("environment")) and _to_text(item.get("symbol"))
                )
            detail["跨环境说明"] = "共享行情应只落到 data_environment；若 broker=paper 但 data_environment=live，paper bars 代表落库环境被 broker mode 污染。"
        target_status_counts = _as_dict(gaps.get("target_status_counts"))
        if target_status_counts:
            detail["目标范围"] = ", ".join(
                f"{key}:{target_status_counts[key]}"
                for key in sorted(target_status_counts)
            )
        bar_lag_symbols = list(gaps.get("bar_lag_symbols") or [])
        indicator_lag_symbols = list(gaps.get("indicator_lag_symbols") or [])
        indicator_lag_details = list(gaps.get("indicator_lag_details") or [])
        sequence_gap_examples = list(gaps.get("sequence_gap_examples") or [])
        if bar_lag_symbols:
            detail["bars异常样本"] = ", ".join(bar_lag_symbols[:10])
        if indicator_lag_symbols:
            detail["指标异常样本"] = ", ".join(indicator_lag_symbols[:10])
        if indicator_lag_details:
            detail["指标滞后明细"] = _format_indicator_lag_sample(indicator_lag_details)
        reason_hint = _to_text(gaps.get("indicator_lag_reason_hint"))
        if reason_hint:
            detail["排查提示"] = reason_hint
        if sequence_gap_examples:
            first = _as_dict(sequence_gap_examples[0])
            detail["序列缺口样本"] = f"{_to_text(first.get('symbol'))}: {_to_text(first.get('prev_us_time'))} -> {_to_text(first.get('next_us_time'))} ({_to_int(first.get('missing_points'), 0)})"
        event_result = emit_system_event(
            event_type="alert",
            level="warning",
            source="ibkr_compute",
            title="IBKR 数据缺口告警",
            detail=detail,
            environment=environment,
        )
        next_state.update(
            {
                "last_gap_issue_at": times["us"],
                "last_gap_alert_ms": event_now_ms,
                "last_gap_alert_hash": gaps.get("fingerprint") or "",
            }
        )
    pb.upsert_state(GAP_MONITOR_STATE_KEY, environment, next_state, date=times["date"])
    return {
        "ok": True,
        "environment": environment,
        "summary": gaps,
        "state": next_state,
        "notified": bool(event_result.get("notified")),
        "source": "ibkr-api",
        "job_id": "system_data_gap_guard",
    }, 200
