#!/usr/bin/env python3
"""
Move legacy TradingView rows from ibkr_* tables into tv_* tables.

This is intended for the IBKR-only migration where historical TradingView
records were previously stored in `ibkr_signals` / `ibkr_indicators` and now
need to live in `tv_signals` / `tv_indicators`.
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Iterable


def table_columns(cur: sqlite3.Cursor, table: str) -> list[str]:
    return [row[1] for row in cur.execute(f"pragma table_info({table})").fetchall()]


def move_rows(
    cur: sqlite3.Cursor,
    source_table: str,
    target_table: str,
    source_value: str,
    delete_source: bool,
) -> tuple[int, int]:
    source_columns = table_columns(cur, source_table)
    target_columns = set(table_columns(cur, target_table))
    shared_columns = [column for column in source_columns if column in target_columns]
    if not shared_columns:
        raise ValueError(f"no shared columns between {source_table} and {target_table}")

    column_sql = ", ".join(shared_columns)
    params = {"source_value": source_value}
    insert_sql = f"""
        insert or ignore into {target_table} ({column_sql})
        select {column_sql}
        from {source_table}
        where coalesce(json_extract(extra, '$.source'), '') = :source_value
    """
    cur.execute(insert_sql, params)
    moved = cur.rowcount if cur.rowcount is not None else 0

    deleted = 0
    if delete_source:
        delete_sql = f"""
            delete from {source_table}
            where coalesce(json_extract(extra, '$.source'), '') = :source_value
        """
        cur.execute(delete_sql, params)
        deleted = cur.rowcount if cur.rowcount is not None else 0

    return moved, deleted


def cleanup_test_signals(cur: sqlite3.Cursor, values: Iterable[str]) -> int:
    deleted = 0
    for source_value in values:
        cur.execute(
            """
            delete from ibkr_signals
            where coalesce(json_extract(extra, '$.source'), '') = ?
            """,
            (source_value,),
        )
        deleted += cur.rowcount if cur.rowcount is not None else 0
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="PocketBase sqlite database path")
    parser.add_argument("--source-value", default="tradingview", help="extra.source value to migrate")
    parser.add_argument("--delete-source", action="store_true", help="delete migrated rows from ibkr_* tables")
    parser.add_argument(
        "--cleanup-test-source",
        action="append",
        default=[],
        help="extra.source values to delete from ibkr_signals after migration (repeatable)",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    indicator_moved, indicator_deleted = move_rows(
        cur,
        "ibkr_indicators",
        "tv_indicators",
        args.source_value,
        args.delete_source,
    )
    signal_moved, signal_deleted = move_rows(
        cur,
        "ibkr_signals",
        "tv_signals",
        args.source_value,
        args.delete_source,
    )
    cleanup_deleted = cleanup_test_signals(cur, args.cleanup_test_source)

    conn.commit()
    print(
        "migrated"
        f" indicators={indicator_moved}"
        f" signals={signal_moved}"
        f" deleted_indicators={indicator_deleted}"
        f" deleted_signals={signal_deleted}"
        f" cleanup_signals={cleanup_deleted}"
    )


if __name__ == "__main__":
    main()
