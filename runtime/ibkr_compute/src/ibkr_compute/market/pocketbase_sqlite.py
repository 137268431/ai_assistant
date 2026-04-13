from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

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
        payload.append(
            (
                pb_record_id(),
                str(bar.get("symbol") or "").upper(),
                str(bar.get("exchange") or "").upper(),
                str(bar.get("interval") or "").strip().lower(),
                float(bar.get("open", 0) or 0),
                float(bar.get("high", 0) or 0),
                float(bar.get("low", 0) or 0),
                float(bar.get("close", 0) or 0),
                float(bar.get("volume", 0) or 0),
                str(bar.get("session_type") or ""),
                str(bar.get("us_time") or ""),
                str(bar.get("cn_time") or ""),
                int(bar.get("bar_time_ms", 0) or 0),
                pb_json_dumps(bar.get("extra") or {}),
                str(bar.get("environment") or "live").strip().lower() or "live",
                now_text,
                now_text,
            )
        )

    conn.executemany(PB_BAR_UPSERT_SQL, payload)
    return len(payload)
