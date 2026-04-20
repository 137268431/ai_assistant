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
