from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite, table_exists
from ibkr_compute.market.timeframe_utils import interval_to_chart_tf, normalize_interval


TV_AUDIT_SNAPSHOT_TABLE = "tv_indicator_audit_snapshots"
IBKR_BARS_TABLE = "ibkr_bars"
IBKR_INDICATORS_TABLE = "ibkr_indicators"
REQUIRED_TABLES = (TV_AUDIT_SNAPSHOT_TABLE, IBKR_BARS_TABLE, IBKR_INDICATORS_TABLE)

DEFAULT_LIMIT = 100
MAX_LIMIT = 5000
MAX_MISMATCHES = 500

PRICE_TOLERANCE = 1e-4
PERCENT_OSCILLATOR_TOLERANCE = 1e-2

BAR_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "session_type",
)

DEFAULT_INDICATOR_FIELDS = (
    "ema_fast",
    "ema_slow",
    "ema_trend",
    "ema_longest",
    "slope_slow",
    "slope_trend",
    "slope_longest",
    "ema_bull_touch",
    "ema_bear_touch",
    "ema_bullish",
    "ema_bearish",
    "fractal_bull",
    "fractal_bear",
    "sd_reg",
    "sd_std_dev",
    "sd_upper",
    "sd_lower",
    "sd_zone",
    "sd_trend",
    "dtp_avg",
    "dtp_atr",
    "dtp_dir",
    "dtp_phase",
    "dtp_phase_bars",
    "atr",
    "atr_raw",
    "atr_pct",
    "crsi",
    "crsi_db",
    "crsi_ub",
    "crsi_ob",
    "crsi_os",
    "crsi_state",
    "crsi_bull_div",
    "crsi_bear_div",
    "crsi_hid_bull",
    "crsi_hid_bear",
    "obv_rsi",
    "obv_bull_div",
    "obv_bear_div",
    "obv_hid_bull",
    "obv_hid_bear",
    "vwap",
    "vwap_upper1",
    "vwap_lower1",
    "vwap_upper2",
    "vwap_lower2",
    "vwap_dist",
    "vwap_bullish",
    "trend_dir",
    "day_change_pct",
    "prev_close_change_pct",
    "change_7d",
)

FIELD_ALIASES = {
    "dayChangePct": "day_change_pct",
    "prevCloseChangePct": "prev_close_change_pct",
    "change7d": "change_7d",
    "obvRsi": "obv_rsi",
    "emaBullTouch": "ema_bull_touch",
    "emaBearTouch": "ema_bear_touch",
    "emaBullish": "ema_bullish",
    "emaBearish": "ema_bearish",
    "vwapUpper1": "vwap_upper1",
    "vwapLower1": "vwap_lower1",
    "vwapUpper2": "vwap_upper2",
    "vwapLower2": "vwap_lower2",
    "vwapDist": "vwap_dist",
    "sdStdDev": "sd_std_dev",
    "dtpPhaseBars": "dtp_phase_bars",
}

METADATA_FIELDS = {
    "id",
    "symbol",
    "exchange",
    "interval",
    "chart_tf",
    "script_tag",
    "us_time",
    "cn_time",
    "bar_time_ms",
    "bar_index",
    "environment",
    "broker_mode",
    "data_environment",
    "source",
    "created",
    "updated",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "session_type",
}

INT_FIELDS = {"volume", "bar_index", "dtp_phase_bars"}
STRING_FIELDS = {"session_type", "sd_zone", "sd_trend", "dtp_dir", "dtp_phase", "trend_dir", "crsi_state"}
BOOL_FIELDS = {
    "ema_bull_touch",
    "ema_bear_touch",
    "ema_bullish",
    "ema_bearish",
    "fractal_bull",
    "fractal_bear",
    "crsi_bull_div",
    "crsi_bear_div",
    "crsi_hid_bull",
    "crsi_hid_bear",
    "obv_bull_div",
    "obv_bear_div",
    "obv_hid_bull",
    "obv_hid_bear",
    "vwap_bullish",
}
PERCENT_OSCILLATOR_FIELDS = {
    "atr_pct",
    "crsi",
    "crsi_db",
    "crsi_ub",
    "crsi_ob",
    "crsi_os",
    "obv_rsi",
    "vwap_dist",
    "day_change_pct",
    "prev_close_change_pct",
    "change_7d",
}

TV_COLUMNS = (
    "id",
    "symbol",
    "exchange",
    "interval",
    "script_tag",
    "us_time",
    "cn_time",
    "bar_time_ms",
    "bar_index",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "session_type",
    "extra",
    "environment",
    "created",
    "updated",
)

BAR_COLUMNS = (
    "id",
    "symbol",
    "exchange",
    "interval",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "session_type",
    "us_time",
    "cn_time",
    "bar_time_ms",
    "extra",
    "environment",
    "created",
    "updated",
)

INDICATOR_COLUMNS = (
    "id",
    "symbol",
    "exchange",
    "interval",
    "script_tag",
    "us_time",
    "cn_time",
    "bar_time_ms",
    "bar_index",
    "extra",
    "environment",
    "created",
    "updated",
)


@dataclass(frozen=True)
class IntervalSpec:
    input_value: str
    storage_interval: str
    chart_interval: str
    aliases: tuple[str, ...]


def _dedupe(values: Iterable[Any]) -> list[Any]:
    output = []
    seen = set()
    for value in values:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in re.split(r"[,;\s]+", value.strip()) if item]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def normalize_audit_symbols(symbols: Iterable[str] | str | None) -> list[str]:
    output = []
    seen = set()
    for raw in _as_list(symbols):
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        output.append(symbol)
    return output


def _interval_aliases(raw: str, storage_interval: str, chart_interval: str) -> tuple[str, ...]:
    aliases = {
        str(raw or "").strip(),
        str(raw or "").strip().lower(),
        str(raw or "").strip().upper(),
        storage_interval,
        storage_interval.lower(),
        chart_interval,
        chart_interval.lower(),
        chart_interval.upper(),
    }
    if storage_interval == "1h":
        aliases.update({"60", "60m", "1H"})
    if storage_interval == "4h":
        aliases.update({"240", "240m", "4H"})
    if storage_interval == "1d":
        aliases.update({"D", "d", "1D", "1d"})
    if storage_interval.endswith("m"):
        aliases.add(storage_interval[:-1])
    return tuple(item for item in _dedupe(aliases) if item)


def normalize_audit_interval(value: Any) -> IntervalSpec:
    raw = str(value or "").strip() or "5m"
    storage_interval = normalize_interval(raw)
    try:
        chart_interval = interval_to_chart_tf(storage_interval)
    except Exception as exc:
        raise ValueError(f"unsupported_interval:{raw}") from exc
    return IntervalSpec(
        input_value=raw,
        storage_interval=storage_interval,
        chart_interval=chart_interval,
        aliases=_interval_aliases(raw, storage_interval, chart_interval),
    )


def normalize_audit_intervals(intervals: Iterable[str] | str | None) -> list[IntervalSpec]:
    raw_values = _as_list(intervals) or ["5m"]
    output: list[IntervalSpec] = []
    seen = set()
    for raw in raw_values:
        spec = normalize_audit_interval(raw)
        if spec.chart_interval in seen:
            continue
        seen.add(spec.chart_interval)
        output.append(spec)
    return output


def _coerce_limit(value: Any) -> int:
    try:
        limit = int(value or DEFAULT_LIMIT)
    except Exception:
        limit = DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, limit))


def _coerce_ms(value: Any) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return 0


def _resolve_window(window: Any) -> dict[str, int]:
    if window is None:
        return {"start_ms": 0, "end_ms": 0, "before_ms": 0}
    if isinstance(window, Mapping):
        start_ms = _coerce_ms(
            window.get("start_ms")
            or window.get("window_start_ms")
            or window.get("from_ms")
            or window.get("start")
        )
        end_ms = _coerce_ms(
            window.get("end_ms")
            or window.get("window_end_ms")
            or window.get("to_ms")
            or window.get("end")
        )
        before_ms = _coerce_ms(window.get("before_ms") or window.get("before"))
    elif isinstance(window, Sequence) and not isinstance(window, (str, bytes)) and len(window) >= 2:
        start_ms = _coerce_ms(window[0])
        end_ms = _coerce_ms(window[1])
        before_ms = 0
    else:
        start_ms = 0
        end_ms = 0
        before_ms = _coerce_ms(window)
    if start_ms > 0 and end_ms > 0 and start_ms > end_ms:
        start_ms, end_ms = end_ms, start_ms
    return {"start_ms": start_ms, "end_ms": end_ms, "before_ms": before_ms}


@contextmanager
def _sqlite_connection(conn: sqlite3.Connection | None = None, db_path: str | None = None):
    if conn is not None:
        yield conn
        return
    if db_path:
        owned = sqlite3.connect(str(db_path), timeout=30.0)
        owned.row_factory = sqlite3.Row
    else:
        owned = open_pb_sqlite(readonly=True)
    try:
        yield owned
    finally:
        owned.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    output = set()
    for row in rows:
        if isinstance(row, sqlite3.Row):
            output.add(str(row["name"]))
        else:
            output.add(str(row[1]))
    return output


def _execute_dict_rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, tuple(params))
    names = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _build_in_clause(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _fetch_rows(
    conn: sqlite3.Connection,
    table: str,
    desired_columns: Sequence[str],
    *,
    symbols: Sequence[str] | None = None,
    interval_aliases: Sequence[str] | None = None,
    environment: str = "live",
    start_ms: int = 0,
    end_ms: int = 0,
    before_ms: int = 0,
    limit: int = 0,
    descending: bool = False,
) -> list[dict[str, Any]]:
    columns = _table_columns(conn, table)
    selected = [column for column in desired_columns if column in columns]
    if not selected:
        selected = ["rowid"]

    where_parts = []
    params: list[Any] = []
    normalized_symbols = list(symbols or [])
    if normalized_symbols and "symbol" in columns:
        where_parts.append(f"UPPER(COALESCE(symbol, '')) IN ({_build_in_clause(normalized_symbols)})")
        params.extend(normalized_symbols)
    elif normalized_symbols and "symbol" not in columns:
        # The SQL table cannot satisfy a requested symbol filter.
        return []

    aliases = [str(item or "").strip().lower() for item in (interval_aliases or []) if str(item or "").strip()]
    aliases = _dedupe(aliases)
    if aliases and "interval" in columns:
        where_parts.append(f"LOWER(COALESCE(interval, '')) IN ({_build_in_clause(aliases)})")
        params.extend(aliases)

    runtime_environment = str(environment or "live").strip().lower() or "live"
    if "environment" in columns:
        where_parts.append("LOWER(COALESCE(environment, '')) = ?")
        params.append(runtime_environment)

    if "bar_time_ms" in columns:
        if int(start_ms or 0) > 0:
            where_parts.append("bar_time_ms >= ?")
            params.append(int(start_ms or 0))
        if int(end_ms or 0) > 0:
            where_parts.append("bar_time_ms <= ?")
            params.append(int(end_ms or 0))
        if int(before_ms or 0) > 0:
            where_parts.append("bar_time_ms < ?")
            params.append(int(before_ms or 0))

    sql = f"SELECT {', '.join(selected)} FROM {table}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if "bar_time_ms" in columns:
        direction = "DESC" if descending else "ASC"
        order_columns = ["bar_time_ms " + direction]
        if "symbol" in columns:
            order_columns.append("symbol ASC")
        sql += " ORDER BY " + ", ".join(order_columns)
    if int(limit or 0) > 0:
        sql += " LIMIT ?"
        params.append(int(limit or 0))
    return _execute_dict_rows(conn, sql, params)


def _parse_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except Exception:
            return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _camel_to_snake(value: str) -> str:
    text = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", str(value or ""))
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return text.replace("-", "_").strip().lower()


def _canonical_field_name(field: str) -> str:
    text = str(field or "").strip()
    return FIELD_ALIASES.get(text) or _camel_to_snake(text)


def _normalize_extra(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in (extra or {}).items():
        canonical = _canonical_field_name(str(key))
        if not canonical:
            continue
        if canonical == str(key) or canonical not in output:
            output[canonical] = value
    return output


def _value_from_sources(field: str, row: Mapping[str, Any] | None, extra: Mapping[str, Any] | None, *, prefer_extra: bool) -> Any:
    canonical = _canonical_field_name(field)
    row_value = (row or {}).get(canonical)
    extra_value = (extra or {}).get(canonical)
    if prefer_extra:
        return extra_value if not _is_missing(extra_value) else row_value
    return row_value if not _is_missing(row_value) else extra_value


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("%"):
            text = text[:-1]
        text = text.replace(",", "")
        try:
            number = float(text)
        except Exception:
            return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "t", "yes", "y", "1", "on"}:
        return True
    if text in {"false", "f", "no", "n", "0", "off"}:
        return False
    return None


def _format_number(value: float) -> int | float:
    if abs(value - round(value)) < 1e-12:
        return int(round(value))
    return value


def _field_kind(field: str) -> str:
    field = _canonical_field_name(field)
    if field in BOOL_FIELDS or field.endswith("_touch") or field.endswith("_bullish") or field.endswith("_bearish") or "_div" in field:
        return "bool"
    if field in STRING_FIELDS:
        return "string"
    if field in INT_FIELDS:
        return "int"
    if field in PERCENT_OSCILLATOR_FIELDS or field.endswith("_pct") or field.startswith("crsi") or field.startswith("obv"):
        return "percent_oscillator"
    return "price"


def _compare_values(field: str, tv_value: Any, ibkr_value: Any) -> tuple[bool, dict[str, Any]]:
    kind = _field_kind(field)
    detail: dict[str, Any] = {
        "field": _canonical_field_name(field),
        "kind": kind,
        "tv": tv_value,
        "ibkr": ibkr_value,
    }
    if _is_missing(tv_value) and _is_missing(ibkr_value):
        return True, detail
    if _is_missing(tv_value):
        detail["reason"] = "missing_tv"
        return False, detail
    if _is_missing(ibkr_value):
        detail["reason"] = "missing_ibkr"
        return False, detail

    if kind == "bool":
        tv_bool = _to_bool(tv_value)
        ibkr_bool = _to_bool(ibkr_value)
        if tv_bool is not None and ibkr_bool is not None:
            detail["tv"] = tv_bool
            detail["ibkr"] = ibkr_bool
            return tv_bool == ibkr_bool, detail

    if kind == "int":
        tv_num = _to_number(tv_value)
        ibkr_num = _to_number(ibkr_value)
        if tv_num is not None and ibkr_num is not None:
            detail["tv"] = _format_number(tv_num)
            detail["ibkr"] = _format_number(ibkr_num)
            detail["delta"] = _format_number(ibkr_num - tv_num)
            return tv_num == ibkr_num, detail

    if kind in {"price", "percent_oscillator"}:
        tv_num = _to_number(tv_value)
        ibkr_num = _to_number(ibkr_value)
        if tv_num is not None and ibkr_num is not None:
            tolerance = PRICE_TOLERANCE if kind == "price" else PERCENT_OSCILLATOR_TOLERANCE
            delta = ibkr_num - tv_num
            detail.update(
                {
                    "tv": _format_number(tv_num),
                    "ibkr": _format_number(ibkr_num),
                    "delta": delta,
                    "abs_delta": abs(delta),
                    "tolerance": tolerance,
                }
            )
            return abs(delta) <= tolerance + 1e-12, detail

    tv_text = str(tv_value).strip()
    ibkr_text = str(ibkr_value).strip()
    detail["tv"] = tv_text
    detail["ibkr"] = ibkr_text
    return tv_text == ibkr_text, detail


def _is_selected_indicator_field(field: str) -> bool:
    canonical = _canonical_field_name(field)
    if not canonical or canonical in METADATA_FIELDS:
        return False
    if canonical in DEFAULT_INDICATOR_FIELDS:
        return True
    return (
        canonical.startswith(("vwap", "ema", "sd", "dtp", "atr", "crsi", "obv", "fractal", "trend", "slope"))
        or canonical.endswith("_touch")
        or "_div" in canonical
        or canonical in {"day_change_pct", "prev_close_change_pct", "change_7d"}
    )


def _selected_indicator_fields(tv_extra: Mapping[str, Any], ibkr_extra: Mapping[str, Any]) -> list[str]:
    fields = list(DEFAULT_INDICATOR_FIELDS)
    for key in list((tv_extra or {}).keys()) + list((ibkr_extra or {}).keys()):
        canonical = _canonical_field_name(key)
        if _is_selected_indicator_field(canonical):
            fields.append(canonical)
    return [field for field in _dedupe(fields) if _is_selected_indicator_field(field)]


def _row_interval_spec(row: Mapping[str, Any], extra: Mapping[str, Any], fallback: IntervalSpec) -> IntervalSpec:
    raw_interval = row.get("interval") or extra.get("interval") or extra.get("chart_tf") or fallback.chart_interval
    try:
        return normalize_audit_interval(raw_interval)
    except ValueError:
        return fallback


def _normalize_source_row(row: Mapping[str, Any], fallback_spec: IntervalSpec, environment: str) -> dict[str, Any] | None:
    extra = _normalize_extra(_parse_object(row.get("extra")))
    symbol = str(row.get("symbol") or extra.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    bar_time_ms = _coerce_ms(row.get("bar_time_ms") if row.get("bar_time_ms") is not None else extra.get("bar_time_ms"))
    if bar_time_ms <= 0:
        return None
    spec = _row_interval_spec(row, extra, fallback_spec)
    row_env = str(row.get("environment") or extra.get("environment") or environment or "live").strip().lower() or "live"
    normalized = dict(row)
    normalized["_extra"] = extra
    normalized["_symbol"] = symbol
    normalized["_bar_time_ms"] = bar_time_ms
    normalized["_environment"] = row_env
    normalized["_interval"] = spec.storage_interval
    normalized["_chart_interval"] = spec.chart_interval
    normalized["_key"] = (symbol, spec.chart_interval, bar_time_ms, row_env)
    return normalized


def _normalize_rows(
    rows: Iterable[Mapping[str, Any]],
    fallback_spec: IntervalSpec,
    environment: str,
    *,
    symbols: Sequence[str] | None = None,
    start_ms: int = 0,
    end_ms: int = 0,
    before_ms: int = 0,
) -> list[dict[str, Any]]:
    symbol_set = set(symbols or [])
    output = []
    for row in rows:
        normalized = _normalize_source_row(row, fallback_spec, environment)
        if not normalized:
            continue
        if symbol_set and normalized["_symbol"] not in symbol_set:
            continue
        bar_time_ms = int(normalized["_bar_time_ms"] or 0)
        if start_ms > 0 and bar_time_ms < start_ms:
            continue
        if end_ms > 0 and bar_time_ms > end_ms:
            continue
        if before_ms > 0 and bar_time_ms >= before_ms:
            continue
        output.append(normalized)
    return output


def _fetch_tv_snapshots(
    conn: sqlite3.Connection,
    symbols: Sequence[str],
    specs: Sequence[IntervalSpec],
    environment: str,
    *,
    limit: int,
    start_ms: int,
    end_ms: int,
    before_ms: int,
) -> list[dict[str, Any]]:
    output_by_key: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    descending = not (start_ms > 0 or end_ms > 0)
    for spec in specs:
        # Limit is applied per requested interval/symbol bucket.
        symbol_buckets: Sequence[str | None] = list(symbols) if symbols else [None]
        for symbol in symbol_buckets:
            rows = _fetch_rows(
                conn,
                TV_AUDIT_SNAPSHOT_TABLE,
                TV_COLUMNS,
                symbols=[symbol] if symbol else None,
                interval_aliases=spec.aliases,
                environment=environment,
                start_ms=start_ms,
                end_ms=end_ms,
                before_ms=before_ms,
                limit=limit,
                descending=descending,
            )
            normalized_rows = _normalize_rows(
                rows,
                spec,
                environment,
                symbols=symbols,
                start_ms=start_ms,
                end_ms=end_ms,
                before_ms=before_ms,
            )
            for normalized in normalized_rows:
                output_by_key[normalized["_key"]] = normalized
    return sorted(output_by_key.values(), key=lambda item: (item["_symbol"], item["_chart_interval"], item["_bar_time_ms"]))


def _fetch_comparison_map(
    conn: sqlite3.Connection,
    table: str,
    desired_columns: Sequence[str],
    symbols: Sequence[str],
    specs: Sequence[IntervalSpec],
    environment: str,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[tuple[str, str, int, str], dict[str, Any]]:
    output: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    if not symbols or not specs:
        return output
    interval_aliases = _dedupe(alias for spec in specs for alias in spec.aliases)
    fallback_spec = specs[0]
    rows = _fetch_rows(
        conn,
        table,
        desired_columns,
        symbols=symbols,
        interval_aliases=interval_aliases,
        environment=environment,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    for row in _normalize_rows(rows, fallback_spec, environment, symbols=symbols, start_ms=start_ms, end_ms=end_ms):
        output[row["_key"]] = row
    return output


def _mismatch(
    *,
    source_row: Mapping[str, Any],
    group: str,
    field: str,
    detail: Mapping[str, Any] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    item = {
        "symbol": source_row["_symbol"],
        "interval": source_row["_chart_interval"],
        "storage_interval": source_row["_interval"],
        "environment": source_row["_environment"],
        "bar_time_ms": source_row["_bar_time_ms"],
        "group": group,
        "field": field,
    }
    if detail:
        item.update(dict(detail))
    if reason:
        item["reason"] = reason
    return item


def _source_bar_values(row: Mapping[str, Any] | None, extra: Mapping[str, Any] | None, *, prefer_extra: bool) -> dict[str, Any]:
    return {
        field: _value_from_sources(field, row, extra, prefer_extra=prefer_extra)
        for field in BAR_FIELDS
        if not _is_missing(_value_from_sources(field, row, extra, prefer_extra=prefer_extra))
    }


def _source_indicator_values(extra: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {field: extra.get(field) for field in fields if not _is_missing(extra.get(field))}


def _compare_audit_row(
    tv_row: Mapping[str, Any],
    ibkr_bar: Mapping[str, Any] | None,
    ibkr_indicator: Mapping[str, Any] | None,
) -> dict[str, Any]:
    tv_extra = dict(tv_row.get("_extra") or {})
    ibkr_bar_extra = dict((ibkr_bar or {}).get("_extra") or {})
    ibkr_indicator_extra = dict((ibkr_indicator or {}).get("_extra") or {})
    indicator_fields = _selected_indicator_fields(tv_extra, ibkr_indicator_extra)

    mismatches: list[dict[str, Any]] = []
    compared_fields = 0
    matched_fields = 0
    bar_field_mismatches = 0
    indicator_field_mismatches = 0

    if ibkr_bar is None:
        mismatches.append(_mismatch(source_row=tv_row, group="bar", field="_row", reason="missing_ibkr_bar"))
    else:
        for field in BAR_FIELDS:
            tv_value = _value_from_sources(field, tv_row, tv_extra, prefer_extra=True)
            if _is_missing(tv_value):
                continue
            ibkr_value = _value_from_sources(field, ibkr_bar, ibkr_bar_extra, prefer_extra=False)
            compared_fields += 1
            matched, detail = _compare_values(field, tv_value, ibkr_value)
            if matched:
                matched_fields += 1
            else:
                bar_field_mismatches += 1
                mismatches.append(_mismatch(source_row=tv_row, group="bar", field=field, detail=detail))

    if ibkr_indicator is None:
        mismatches.append(_mismatch(source_row=tv_row, group="indicator", field="_row", reason="missing_ibkr_indicator"))
    else:
        for field in indicator_fields:
            tv_value = tv_extra.get(field)
            if _is_missing(tv_value):
                continue
            ibkr_value = ibkr_indicator_extra.get(field)
            compared_fields += 1
            matched, detail = _compare_values(field, tv_value, ibkr_value)
            if matched:
                matched_fields += 1
            else:
                indicator_field_mismatches += 1
                mismatches.append(_mismatch(source_row=tv_row, group="indicator", field=field, detail=detail))

    if ibkr_bar is None and ibkr_indicator is None:
        status = "missing_ibkr"
    elif ibkr_bar is None:
        status = "missing_ibkr_bar"
    elif ibkr_indicator is None:
        status = "missing_ibkr_indicator"
    elif mismatches:
        status = "mismatch"
    else:
        status = "match"

    return {
        "symbol": tv_row["_symbol"],
        "interval": tv_row["_chart_interval"],
        "storage_interval": tv_row["_interval"],
        "environment": tv_row["_environment"],
        "bar_time_ms": tv_row["_bar_time_ms"],
        "us_time": str(tv_row.get("us_time") or tv_extra.get("us_time") or ""),
        "status": status,
        "compared_field_count": compared_fields,
        "matched_field_count": matched_fields,
        "mismatch_count": len(mismatches),
        "bar_field_mismatch_count": bar_field_mismatches,
        "indicator_field_mismatch_count": indicator_field_mismatches,
        "tv": {
            "snapshot_id": str(tv_row.get("id") or ""),
            "bar": _source_bar_values(tv_row, tv_extra, prefer_extra=True),
            "indicator": _source_indicator_values(tv_extra, indicator_fields),
        },
        "ibkr": {
            "bar_id": str((ibkr_bar or {}).get("id") or ""),
            "indicator_id": str((ibkr_indicator or {}).get("id") or ""),
            "bar": _source_bar_values(ibkr_bar, ibkr_bar_extra, prefer_extra=False) if ibkr_bar else None,
            "indicator": _source_indicator_values(ibkr_indicator_extra, indicator_fields) if ibkr_indicator else None,
        },
        "diff": {
            "bar": [item for item in mismatches if item.get("group") == "bar"],
            "indicator": [item for item in mismatches if item.get("group") == "indicator"],
        },
    }


def _status_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {
        "match": 0,
        "mismatch": 0,
        "missing_ibkr": 0,
        "missing_ibkr_bar": 0,
        "missing_ibkr_indicator": 0,
    }
    for row in rows:
        status = str(row.get("status") or "mismatch")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _build_summary(
    *,
    status: str,
    environment: str,
    symbols: Sequence[str],
    specs: Sequence[IntervalSpec],
    tv_rows: Sequence[Mapping[str, Any]],
    ibkr_bar_count: int,
    ibkr_indicator_count: int,
    rows: Sequence[Mapping[str, Any]],
    mismatches: Sequence[Mapping[str, Any]],
    error: str = "",
    missing_tables: Sequence[str] | None = None,
) -> dict[str, Any]:
    status_counts = _status_counts(rows)
    compared_field_count = sum(int(row.get("compared_field_count", 0) or 0) for row in rows)
    matched_field_count = sum(int(row.get("matched_field_count", 0) or 0) for row in rows)
    bar_field_mismatch_count = sum(int(row.get("bar_field_mismatch_count", 0) or 0) for row in rows)
    indicator_field_mismatch_count = sum(int(row.get("indicator_field_mismatch_count", 0) or 0) for row in rows)
    return {
        "status": status,
        "available": status != "unavailable",
        "environment": str(environment or "live").strip().lower() or "live",
        "symbols": list(symbols),
        "intervals": [spec.chart_interval for spec in specs],
        "storage_intervals": [spec.storage_interval for spec in specs],
        "symbol_count": len(symbols),
        "interval_count": len(specs),
        "tv_snapshot_count": len(tv_rows),
        "ibkr_bar_count": ibkr_bar_count,
        "ibkr_indicator_count": ibkr_indicator_count,
        "row_count": len(rows),
        "matched_row_count": status_counts.get("match", 0),
        "mismatch_row_count": len(rows) - status_counts.get("match", 0),
        "missing_ibkr_bar_count": status_counts.get("missing_ibkr", 0) + status_counts.get("missing_ibkr_bar", 0),
        "missing_ibkr_indicator_count": status_counts.get("missing_ibkr", 0) + status_counts.get("missing_ibkr_indicator", 0),
        "compared_field_count": compared_field_count,
        "matched_field_count": matched_field_count,
        "field_mismatch_count": bar_field_mismatch_count + indicator_field_mismatch_count,
        "bar_field_mismatch_count": bar_field_mismatch_count,
        "indicator_field_mismatch_count": indicator_field_mismatch_count,
        "mismatch_count": len(mismatches),
        "status_counts": status_counts,
        "missing_tables": list(missing_tables or []),
        "error": str(error or ""),
    }


def _payload(
    *,
    status: str,
    summary: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]] | None = None,
    mismatches: Sequence[Mapping[str, Any]] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": status != "error",
        "status": status,
        "summary": dict(summary),
        "rows": list(rows or []),
        "mismatches": list(mismatches or []),
        "meta": dict(meta or {}),
    }


def _emit_audit_alert(emit_alert: Callable[[dict[str, Any]], Any] | None, payload: dict[str, Any]) -> None:
    if not emit_alert or payload.get("status") not in {"error", "unavailable"}:
        return
    try:
        emit_alert(
            {
                "type": "tv_indicator_audit",
                "status": payload.get("status"),
                "summary": payload.get("summary") or {},
                "mismatches": (payload.get("mismatches") or [])[:20],
            }
        )
    except Exception:
        # Alert callbacks must not turn a read-only audit into a failed audit.
        return


def _unavailable_payload(
    *,
    environment: str,
    symbols: Sequence[str],
    specs: Sequence[IntervalSpec],
    error: str,
    missing_tables: Sequence[str] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    summary = _build_summary(
        status="unavailable",
        environment=environment,
        symbols=symbols,
        specs=specs,
        tv_rows=[],
        ibkr_bar_count=0,
        ibkr_indicator_count=0,
        rows=[],
        mismatches=[],
        error=error,
        missing_tables=missing_tables,
    )
    return _payload(status="unavailable", summary=summary, meta=meta)


def _error_payload(
    *,
    environment: str,
    symbols: Sequence[str],
    specs: Sequence[IntervalSpec],
    error: str,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    summary = _build_summary(
        status="error",
        environment=environment,
        symbols=symbols,
        specs=specs,
        tv_rows=[],
        ibkr_bar_count=0,
        ibkr_indicator_count=0,
        rows=[],
        mismatches=[],
        error=error,
    )
    return _payload(status="error", summary=summary, meta=meta)


def build_tv_indicator_audit_payload(
    symbols: Iterable[str] | str | None = None,
    intervals: Iterable[str] | str | None = None,
    environment: str = "live",
    limit: int = DEFAULT_LIMIT,
    window: Mapping[str, Any] | Sequence[Any] | int | None = None,
    *,
    emit_alert: Callable[[dict[str, Any]], Any] | None = None,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Build a TV-vs-IBKR indicator audit payload from PocketBase sqlite tables."""

    normalized_symbols = normalize_audit_symbols(symbols)
    try:
        specs = normalize_audit_intervals(intervals)
    except ValueError as exc:
        specs = []
        payload = _error_payload(
            environment=environment,
            symbols=normalized_symbols,
            specs=specs,
            error=str(exc),
            meta={"audit_table": TV_AUDIT_SNAPSHOT_TABLE},
        )
        _emit_audit_alert(emit_alert, payload)
        return payload

    runtime_environment = str(environment or "live").strip().lower() or "live"
    resolved_limit = _coerce_limit(limit)
    resolved_window = _resolve_window(window)
    meta = {
        "audit_table": TV_AUDIT_SNAPSHOT_TABLE,
        "truth_source": "tradingview",
        "limit": resolved_limit,
        "window": dict(resolved_window),
        "tolerances": {
            "price": PRICE_TOLERANCE,
            "percent_oscillator": PERCENT_OSCILLATOR_TOLERANCE,
            "volume_int": "exact",
            "bool_string": "exact",
        },
    }

    try:
        with _sqlite_connection(conn=conn, db_path=db_path) as db:
            missing_tables = [table for table in REQUIRED_TABLES if not table_exists(db, table)]
            if missing_tables:
                payload = _unavailable_payload(
                    environment=runtime_environment,
                    symbols=normalized_symbols,
                    specs=specs,
                    error="missing_tables",
                    missing_tables=missing_tables,
                    meta=meta,
                )
                _emit_audit_alert(emit_alert, payload)
                return payload

            tv_rows = _fetch_tv_snapshots(
                db,
                normalized_symbols,
                specs,
                runtime_environment,
                limit=resolved_limit,
                start_ms=resolved_window["start_ms"],
                end_ms=resolved_window["end_ms"],
                before_ms=resolved_window["before_ms"],
            )
            if not tv_rows:
                payload = _unavailable_payload(
                    environment=runtime_environment,
                    symbols=normalized_symbols,
                    specs=specs,
                    error="no_tv_snapshots",
                    meta=meta,
                )
                _emit_audit_alert(emit_alert, payload)
                return payload

            compare_symbols = sorted({row["_symbol"] for row in tv_rows})
            compare_specs_by_chart = {spec.chart_interval: spec for spec in specs}
            for row in tv_rows:
                if row["_chart_interval"] not in compare_specs_by_chart:
                    compare_specs_by_chart[row["_chart_interval"]] = IntervalSpec(
                        input_value=row["_chart_interval"],
                        storage_interval=row["_interval"],
                        chart_interval=row["_chart_interval"],
                        aliases=_interval_aliases(row["_chart_interval"], row["_interval"], row["_chart_interval"]),
                    )
            compare_specs = list(compare_specs_by_chart.values())
            tv_times = [int(row["_bar_time_ms"] or 0) for row in tv_rows if int(row["_bar_time_ms"] or 0) > 0]
            start_ms = min(tv_times) if tv_times else resolved_window["start_ms"]
            end_ms = max(tv_times) if tv_times else resolved_window["end_ms"]

            ibkr_bars = _fetch_comparison_map(
                db,
                IBKR_BARS_TABLE,
                BAR_COLUMNS,
                compare_symbols,
                compare_specs,
                runtime_environment,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            ibkr_indicators = _fetch_comparison_map(
                db,
                IBKR_INDICATORS_TABLE,
                INDICATOR_COLUMNS,
                compare_symbols,
                compare_specs,
                runtime_environment,
                start_ms=start_ms,
                end_ms=end_ms,
            )

            rows = []
            mismatches: list[dict[str, Any]] = []
            for tv_row in tv_rows:
                key = tv_row["_key"]
                audit_row = _compare_audit_row(tv_row, ibkr_bars.get(key), ibkr_indicators.get(key))
                rows.append(audit_row)
                for group_items in (audit_row["diff"]["bar"], audit_row["diff"]["indicator"]):
                    mismatches.extend(group_items)

            status = "pass"
            if mismatches:
                status = "error"
            elif sum(int(row.get("compared_field_count", 0) or 0) for row in rows) <= 0:
                status = "unavailable"

            summary = _build_summary(
                status=status,
                environment=runtime_environment,
                symbols=normalized_symbols or compare_symbols,
                specs=specs,
                tv_rows=tv_rows,
                ibkr_bar_count=len(ibkr_bars),
                ibkr_indicator_count=len(ibkr_indicators),
                rows=rows,
                mismatches=mismatches,
            )
            payload = _payload(
                status=status,
                summary=summary,
                rows=rows,
                mismatches=mismatches[:MAX_MISMATCHES],
                meta=meta,
            )
            _emit_audit_alert(emit_alert, payload)
            return payload
    except FileNotFoundError as exc:
        payload = _unavailable_payload(
            environment=runtime_environment,
            symbols=normalized_symbols,
            specs=specs,
            error=f"sqlite_unavailable:{exc}",
            meta=meta,
        )
        _emit_audit_alert(emit_alert, payload)
        return payload
    except Exception as exc:
        payload = _error_payload(
            environment=runtime_environment,
            symbols=normalized_symbols,
            specs=specs,
            error=str(exc)[:500],
            meta=meta,
        )
        _emit_audit_alert(emit_alert, payload)
        return payload


__all__ = [
    "BAR_FIELDS",
    "DEFAULT_INDICATOR_FIELDS",
    "IBKR_BARS_TABLE",
    "IBKR_INDICATORS_TABLE",
    "PRICE_TOLERANCE",
    "PERCENT_OSCILLATOR_TOLERANCE",
    "TV_AUDIT_SNAPSHOT_TABLE",
    "build_tv_indicator_audit_payload",
    "normalize_audit_interval",
    "normalize_audit_intervals",
    "normalize_audit_symbols",
]
