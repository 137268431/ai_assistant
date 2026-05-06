from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .timeframe_utils import build_bar_close_timestamps, normalize_interval

PB_SQLITE_PATH = os.environ.get("PB_SQLITE_PATH", "/opt/pocketbase/pb_data/data.db")
PB_RECORD_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
PB_RECORD_ID_LENGTH = 15

PB_BAR_UPSERT_SQL = """
INSERT INTO ibkr_bars (
    id,
    symbol,
    exchange,
    interval,
    open,
    high,
    low,
    close,
    volume,
    session_type,
    us_time,
    cn_time,
    bar_time_ms,
    extra,
    environment,
    created,
    updated
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, interval, bar_time_ms, environment) DO UPDATE SET
    exchange = excluded.exchange,
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume,
    session_type = excluded.session_type,
    us_time = excluded.us_time,
    cn_time = excluded.cn_time,
    extra = excluded.extra,
    updated = excluded.updated
"""

PB_STATE_UPSERT_SQL = """
INSERT INTO ibkr_state (
    id,
    state_key,
    date,
    data,
    environment,
    created,
    updated
) VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(state_key, date, environment) DO UPDATE SET
    data = excluded.data,
    updated = excluded.updated
"""


PB_BAR_COVERAGE_DAILY_UPSERT_SQL = """
INSERT INTO ibkr_bar_coverage_daily (
    id,
    environment,
    market_date,
    symbol,
    interval,
    session_mode,
    status,
    hard_gate,
    needs_repair,
    expected_count,
    actual_count,
    missing_count,
    gap_count,
    duplicate_count,
    bad_ohlc_count,
    expected_start_ms,
    expected_end_ms,
    first_bar_ms,
    last_bar_ms,
    last_checked_at,
    last_repair_at,
    missing_windows,
    missing_examples,
    repair_windows,
    expected_mask_hex,
    actual_mask_hex,
    missing_mask_hex,
    source,
    extra,
    created,
    updated
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(environment, market_date, symbol, interval, session_mode) DO UPDATE SET
    status = excluded.status,
    hard_gate = excluded.hard_gate,
    needs_repair = excluded.needs_repair,
    expected_count = excluded.expected_count,
    actual_count = excluded.actual_count,
    missing_count = excluded.missing_count,
    gap_count = excluded.gap_count,
    duplicate_count = excluded.duplicate_count,
    bad_ohlc_count = excluded.bad_ohlc_count,
    expected_start_ms = excluded.expected_start_ms,
    expected_end_ms = excluded.expected_end_ms,
    first_bar_ms = excluded.first_bar_ms,
    last_bar_ms = excluded.last_bar_ms,
    last_checked_at = excluded.last_checked_at,
    last_repair_at = CASE
        WHEN COALESCE(excluded.last_repair_at, '') != '' THEN excluded.last_repair_at
        ELSE ibkr_bar_coverage_daily.last_repair_at
    END,
    missing_windows = excluded.missing_windows,
    missing_examples = excluded.missing_examples,
    repair_windows = excluded.repair_windows,
    expected_mask_hex = excluded.expected_mask_hex,
    actual_mask_hex = excluded.actual_mask_hex,
    missing_mask_hex = excluded.missing_mask_hex,
    source = excluded.source,
    extra = excluded.extra,
    updated = excluded.updated
"""


PB_INDICATOR_UPSERT_SQL = """
INSERT INTO ibkr_indicators (
    id,
    symbol,
    exchange,
    interval,
    script_tag,
    us_time,
    cn_time,
    bar_time_ms,
    bar_index,
    extra,
    environment,
    created,
    updated
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, interval, bar_time_ms, environment) DO UPDATE SET
    exchange = excluded.exchange,
    script_tag = excluded.script_tag,
    us_time = excluded.us_time,
    cn_time = excluded.cn_time,
    bar_index = excluded.bar_index,
    extra = excluded.extra,
    updated = excluded.updated
"""


def pb_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%fZ")


def pb_record_id() -> str:
    return "".join(secrets.choice(PB_RECORD_ID_ALPHABET) for _ in range(PB_RECORD_ID_LENGTH))


def pb_json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"), ensure_ascii=False)


def open_pb_sqlite(*, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    db_path = str(PB_SQLITE_PATH or "").strip()
    if not db_path:
        raise RuntimeError("PB_SQLITE_PATH is empty")
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)

    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_state_payload(
    conn: sqlite3.Connection,
    state_key: str,
    environment: str,
    *,
    date: str = "global",
) -> dict | None:
    row = conn.execute(
        """
        SELECT id, state_key, date, data, environment, created, updated
        FROM ibkr_state
        WHERE state_key = ? AND date = ? AND environment = ?
        LIMIT 1
        """,
        (str(state_key or ""), str(date or "global"), str(environment or "live")),
    ).fetchone()
    if not row:
        return None

    payload = {}
    raw_data = row["data"]
    if isinstance(raw_data, str) and raw_data.strip():
        try:
            payload = json.loads(raw_data)
        except Exception:
            payload = {}
    elif isinstance(raw_data, dict):
        payload = dict(raw_data)

    return {
        "id": str(row["id"] or ""),
        "state_key": str(row["state_key"] or ""),
        "date": str(row["date"] or ""),
        "environment": str(row["environment"] or ""),
        "created": str(row["created"] or ""),
        "updated": str(row["updated"] or ""),
        "data": payload if isinstance(payload, dict) else {},
    }


def upsert_state_payload(
    conn: sqlite3.Connection,
    state_key: str,
    environment: str,
    data: dict,
    *,
    date: str = "global",
) -> None:
    now_text = pb_now_text()
    conn.execute(
        PB_STATE_UPSERT_SQL,
        (
            pb_record_id(),
            str(state_key or ""),
            str(date or "global"),
            pb_json_dumps(data or {}),
            str(environment or "live"),
            now_text,
            now_text,
        ),
    )


def count_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    where: str = "",
    params: Sequence[Any] | None = None,
) -> int:
    sql = f"SELECT COUNT(*) AS total FROM {table}"
    if where:
        sql += f" WHERE {where}"
    row = conn.execute(sql, tuple(params or ())).fetchone()
    return int((row["total"] if row else 0) or 0)


def count_rows_by_environment(conn: sqlite3.Connection, table: str, environment: str) -> int:
    return count_rows(conn, table, where="environment = ?", params=(str(environment or "live"),))


def delete_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    where: str = "",
    params: Sequence[Any] | None = None,
) -> int:
    sql = f"DELETE FROM {table}"
    if where:
        sql += f" WHERE {where}"
    cursor = conn.execute(sql, tuple(params or ()))
    return int(cursor.rowcount or 0)


def delete_rows_by_environment(conn: sqlite3.Connection, table: str, environment: str) -> int:
    return delete_rows(conn, table, where="environment = ?", params=(str(environment or "live"),))


def _bar_environment_values(environment: str, *, include_legacy_empty: bool = True) -> list[str]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    values = [runtime_environment]
    if include_legacy_empty and runtime_environment == "live":
        values.append("")
    return values


def _bar_environment_sql(environment: str, *, include_legacy_empty: bool = True) -> tuple[str, list[Any]]:
    values = _bar_environment_values(environment, include_legacy_empty=include_legacy_empty)
    if len(values) == 1:
        return "environment = ?", values
    placeholders = ", ".join("?" for _ in values)
    return f"environment IN ({placeholders})", values


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def fetch_latest_bar(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
    environment: str,
    *,
    safe_upper_ms: int = 0,
    include_legacy_empty: bool = True,
) -> dict[str, Any]:
    env_sql, env_params = _bar_environment_sql(environment, include_legacy_empty=include_legacy_empty)
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
    if int(safe_upper_ms or 0) > 0:
        where_parts.append("bar_time_ms <= ?")
        params.append(int(safe_upper_ms or 0))

    row = conn.execute(
        f"""
        SELECT id, symbol, exchange, interval, open, high, low, close, volume,
               session_type, us_time, cn_time, bar_time_ms, extra, environment,
               created, updated
        FROM ibkr_bars
        WHERE {' AND '.join(where_parts)}
        ORDER BY bar_time_ms DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return _row_to_dict(row)


def fetch_recent_bars(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
    environment: str,
    *,
    limit: int = 400,
    include_legacy_empty: bool = False,
) -> list[dict[str, Any]]:
    env_sql, env_params = _bar_environment_sql(environment, include_legacy_empty=include_legacy_empty)
    rows = conn.execute(
        f"""
        SELECT id, symbol, exchange, interval, open, high, low, close, volume,
               session_type, us_time, cn_time, bar_time_ms, extra, environment,
               created, updated
        FROM ibkr_bars
        WHERE symbol = ?
          AND interval = ?
          AND {env_sql}
        ORDER BY bar_time_ms DESC
        LIMIT ?
        """,
        (
            str(symbol or "").strip().upper(),
            normalize_interval(interval),
            *env_params,
            max(1, int(limit or 1)),
        ),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def count_recent_bars(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
    environment: str,
    *,
    limit: int = 0,
    include_legacy_empty: bool = True,
) -> int:
    env_sql, env_params = _bar_environment_sql(environment, include_legacy_empty=include_legacy_empty)
    params: list[Any] = [
        str(symbol or "").strip().upper(),
        normalize_interval(interval),
        *env_params,
    ]
    if int(limit or 0) > 0:
        params.append(int(limit or 0))
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM (
                SELECT 1
                FROM ibkr_bars
                WHERE symbol = ?
                  AND interval = ?
                  AND {env_sql}
                ORDER BY bar_time_ms DESC
                LIMIT ?
            )
            """,
            tuple(params),
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM ibkr_bars
            WHERE symbol = ?
              AND interval = ?
              AND {env_sql}
            """,
            tuple(params),
        ).fetchone()
    return int((row["total"] if row else 0) or 0)


def fetch_bar_times_in_range(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
    environment: str,
    *,
    start_ms: int,
    end_ms: int,
    session_type: str = "",
    include_legacy_empty: bool = False,
) -> list[int]:
    env_sql, env_params = _bar_environment_sql(environment, include_legacy_empty=include_legacy_empty)
    where_parts = [
        "symbol = ?",
        "interval = ?",
        env_sql,
        "bar_time_ms >= ?",
        "bar_time_ms <= ?",
    ]
    params: list[Any] = [
        str(symbol or "").strip().upper(),
        normalize_interval(interval),
        *env_params,
        int(start_ms or 0),
        int(end_ms or 0),
    ]
    if str(session_type or "").strip():
        where_parts.append("session_type = ?")
        params.append(str(session_type or "").strip())

    rows = conn.execute(
        f"""
        SELECT bar_time_ms
        FROM ibkr_bars
        WHERE {' AND '.join(where_parts)}
        ORDER BY bar_time_ms
        """,
        tuple(params),
    ).fetchall()
    return [
        int(row["bar_time_ms"] or 0)
        for row in rows
        if int(row["bar_time_ms"] or 0) > 0
    ]


def fetch_bars(
    conn: sqlite3.Connection,
    environment: str,
    interval: str,
    *,
    symbols: Sequence[str] | None = None,
    start_ms: int = 0,
    end_ms: int = 0,
    before_ms: int = 0,
    limit: int = 0,
    descending: bool = False,
    include_legacy_empty: bool = True,
) -> list[dict[str, Any]]:
    env_sql, env_params = _bar_environment_sql(environment, include_legacy_empty=include_legacy_empty)
    where_parts = [
        "interval = ?",
        env_sql,
    ]
    params: list[Any] = [
        normalize_interval(interval),
        *env_params,
    ]
    normalized_symbols = _normalize_symbols(symbols)
    if normalized_symbols:
        placeholders = ", ".join("?" for _ in normalized_symbols)
        where_parts.append(f"symbol IN ({placeholders})")
        params.extend(normalized_symbols)
    if int(start_ms or 0) > 0:
        where_parts.append("bar_time_ms >= ?")
        params.append(int(start_ms or 0))
    if int(end_ms or 0) > 0:
        where_parts.append("bar_time_ms <= ?")
        params.append(int(end_ms or 0))
    if int(before_ms or 0) > 0:
        where_parts.append("bar_time_ms < ?")
        params.append(int(before_ms or 0))

    order_direction = "DESC" if descending else "ASC"
    sql = f"""
        SELECT id, symbol, exchange, interval, open, high, low, close, volume,
               session_type, us_time, cn_time, bar_time_ms, extra, environment,
               created, updated
        FROM ibkr_bars
        WHERE {' AND '.join(where_parts)}
        ORDER BY bar_time_ms {order_direction}, symbol ASC
    """
    if int(limit or 0) > 0:
        sql += " LIMIT ?"
        params.append(int(limit or 0))
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(row) for row in rows]


def prepare_indicator_payload(indicator: dict[str, Any]) -> tuple[Any, ...] | None:
    symbol = str(indicator.get("symbol") or "").strip().upper()
    interval = str(indicator.get("interval") or "").strip()
    environment = str(indicator.get("environment") or "live").strip().lower() or "live"
    bar_time_ms = int(indicator.get("bar_time_ms", 0) or 0)
    if not symbol or not interval or bar_time_ms <= 0:
        return None

    extra = dict(indicator.get("extra") or {})
    extra.update(build_bar_close_timestamps(bar_time_ms, interval))
    extra.setdefault("environment", environment)
    extra.setdefault("source", "ibkr_compute")
    now_text = pb_now_text()
    return (
        pb_record_id(),
        symbol,
        str(indicator.get("exchange") or "").strip().upper(),
        interval,
        str(indicator.get("script_tag") or "").strip(),
        str(indicator.get("us_time") or "").strip(),
        str(indicator.get("cn_time") or "").strip(),
        bar_time_ms,
        int(indicator.get("bar_index")) if indicator.get("bar_index") is not None else 0,
        pb_json_dumps(extra),
        environment,
        now_text,
        now_text,
    )


def _normalize_symbols(symbols: Sequence[str] | None) -> list[str]:
    normalized: list[str] = []
    seen = set()
    for item in symbols or ():
        symbol = str(item or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            normalized.append(symbol)
    return normalized


def _environment_where_clause(environment: str, *, include_legacy_empty: bool = True) -> tuple[str, list[Any]]:
    runtime_environment = str(environment or "live").strip().lower() or "live"
    if include_legacy_empty and runtime_environment == "live":
        return "(environment = ? OR environment = '')", [runtime_environment]
    return "environment = ?", [runtime_environment]


def _symbol_where_clause(symbols: Sequence[str]) -> tuple[str, list[Any]]:
    normalized_symbols = _normalize_symbols(symbols)
    if not normalized_symbols:
        return "1 = 0", []
    placeholders = ", ".join("?" for _ in normalized_symbols)
    return f"symbol IN ({placeholders})", list(normalized_symbols)


def delete_symbol_runtime_data(
    conn: sqlite3.Connection,
    environment: str,
    symbols: Sequence[str],
) -> dict[str, Any]:
    normalized_symbols = _normalize_symbols(symbols)
    if not normalized_symbols:
        return {
            "symbols": [],
            "deleted": {},
            "deleted_signal_ids": [],
            "preserved_signal_ids": [],
        }

    env_where, env_params = _environment_where_clause(environment, include_legacy_empty=True)
    symbol_where, symbol_params = _symbol_where_clause(normalized_symbols)
    base_where = f"{env_where} AND {symbol_where}"
    base_params = [*env_params, *symbol_params]

    linked_signal_ids = [
        str(row["signal_id"] or "").strip()
        for row in conn.execute(
            f"""
            SELECT DISTINCT signal_id
            FROM orders
            WHERE {env_where}
              AND {symbol_where}
              AND COALESCE(signal_id, '') != ''
            """,
            tuple(base_params),
        ).fetchall()
        if str(row["signal_id"] or "").strip()
    ]

    deleted: dict[str, int] = {}
    deleted["ibkr_bars"] = delete_rows(conn, "ibkr_bars", where=base_where, params=base_params)
    deleted["ibkr_indicators"] = delete_rows(conn, "ibkr_indicators", where=base_where, params=base_params)
    deleted["ibkr_reverse_signals"] = delete_rows(conn, "ibkr_reverse_signals", where=base_where, params=base_params)
    deleted["ibkr_bar_integrity"] = delete_rows(conn, "ibkr_bar_integrity", where=base_where, params=base_params)
    deleted["ibkr_bar_truth_audit"] = delete_rows(conn, "ibkr_bar_truth_audit", where=base_where, params=base_params)

    signal_where = f"{base_where} AND LOWER(COALESCE(status, '')) != 'executed'"
    signal_params: list[Any] = list(base_params)
    if linked_signal_ids:
        placeholders = ", ".join("?" for _ in linked_signal_ids)
        signal_where += f" AND (COALESCE(signal_id, '') = '' OR signal_id NOT IN ({placeholders}))"
        signal_params.extend(linked_signal_ids)

    deleted_signal_ids = [
        str(row["signal_id"] or "").strip()
        for row in conn.execute(
            f"""
            SELECT signal_id
            FROM ibkr_signals
            WHERE {signal_where}
            """,
            tuple(signal_params),
        ).fetchall()
        if str(row["signal_id"] or "").strip()
    ]
    deleted["ibkr_signals"] = delete_rows(
        conn,
        "ibkr_signals",
        where=signal_where,
        params=signal_params,
    )

    return {
        "symbols": normalized_symbols,
        "deleted": deleted,
        "deleted_signal_ids": deleted_signal_ids,
        "preserved_signal_ids": linked_signal_ids,
    }


def delete_state_rows(
    conn: sqlite3.Connection,
    environment: str,
    state_keys: Sequence[str],
) -> int:
    normalized_keys = [str(item or "").strip() for item in (state_keys or []) if str(item or "").strip()]
    if not normalized_keys:
        return 0
    placeholders = ", ".join("?" for _ in normalized_keys)
    params = [str(environment or "live"), *normalized_keys]
    cursor = conn.execute(
        f"DELETE FROM ibkr_state WHERE environment = ? AND state_key IN ({placeholders})",
        params,
    )
    return int(cursor.rowcount or 0)


def upsert_bars(conn: sqlite3.Connection, bars: Iterable[dict]) -> int:
    rows = list(bars or [])
    if not rows:
        return 0

    now_text = pb_now_text()
    payload = []
    for bar in rows:
        interval = normalize_interval(bar.get("interval") or "")
        bar_time_ms = int(bar.get("bar_time_ms", 0) or 0)
        extra = dict(bar.get("extra") or {})
        if interval and bar_time_ms > 0:
            extra.update(build_bar_close_timestamps(bar_time_ms, interval))
        payload.append(
            (
                pb_record_id(),
                str(bar.get("symbol") or "").upper(),
                str(bar.get("exchange") or "").upper(),
                interval,
                float(bar.get("open", 0) or 0),
                float(bar.get("high", 0) or 0),
                float(bar.get("low", 0) or 0),
                float(bar.get("close", 0) or 0),
                float(bar.get("volume", 0) or 0),
                str(bar.get("session_type") or ""),
                str(bar.get("us_time") or ""),
                str(bar.get("cn_time") or ""),
                bar_time_ms,
                pb_json_dumps(extra),
                str(bar.get("environment") or "live").strip().lower() or "live",
                now_text,
                now_text,
            )
        )

    conn.executemany(PB_BAR_UPSERT_SQL, payload)
    return len(payload)


def upsert_bar_coverage_daily(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    items = list(rows or [])
    if not items:
        return 0

    now_text = pb_now_text()
    payload = []
    for row in items:
        symbol = str((row or {}).get("symbol") or "").strip().upper()
        market_date = str((row or {}).get("market_date") or "").strip()
        interval = normalize_interval((row or {}).get("interval") or "5m")
        environment = str((row or {}).get("environment") or "live").strip().lower() or "live"
        session_mode = str((row or {}).get("session_mode") or "regular").strip().lower() or "regular"
        if not symbol or not market_date:
            continue
        payload.append(
            (
                pb_record_id(),
                environment,
                market_date,
                symbol,
                interval,
                session_mode,
                str((row or {}).get("status") or "ok").strip() or "ok",
                1 if bool((row or {}).get("hard_gate")) else 0,
                1 if bool((row or {}).get("needs_repair")) else 0,
                int((row or {}).get("expected_count") or 0),
                int((row or {}).get("actual_count") or 0),
                int((row or {}).get("missing_count") or 0),
                int((row or {}).get("gap_count") or 0),
                int((row or {}).get("duplicate_count") or 0),
                int((row or {}).get("bad_ohlc_count") or 0),
                int((row or {}).get("expected_start_ms") or 0),
                int((row or {}).get("expected_end_ms") or 0),
                int((row or {}).get("first_bar_ms") or 0),
                int((row or {}).get("last_bar_ms") or 0),
                str((row or {}).get("last_checked_at") or "").strip(),
                str((row or {}).get("last_repair_at") or "").strip(),
                pb_json_dumps((row or {}).get("missing_windows") or []),
                pb_json_dumps((row or {}).get("missing_examples") or []),
                pb_json_dumps((row or {}).get("repair_windows") or []),
                str((row or {}).get("expected_mask_hex") or "").strip(),
                str((row or {}).get("actual_mask_hex") or "").strip(),
                str((row or {}).get("missing_mask_hex") or "").strip(),
                str((row or {}).get("source") or "manual_scan").strip() or "manual_scan",
                pb_json_dumps((row or {}).get("extra") or {}),
                now_text,
                now_text,
            )
        )
    if not payload:
        return 0
    conn.executemany(PB_BAR_COVERAGE_DAILY_UPSERT_SQL, payload)
    return len(payload)


def upsert_indicators(conn: sqlite3.Connection, indicators: Iterable[dict]) -> int:
    payload = [
        prepared
        for prepared in (prepare_indicator_payload(item) for item in list(indicators or []))
        if prepared is not None
    ]
    if not payload:
        return 0
    conn.executemany(PB_INDICATOR_UPSERT_SQL, payload)
    return len(payload)
