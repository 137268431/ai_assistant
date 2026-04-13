#!/usr/bin/env python3
"""
Clone a PocketBase collection directly in the SQLite database.

This is useful when the runtime already uses direct SQLite-backed collections and
we need a new table that mirrors an existing collection with a new name/id.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now_text() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def load_schema(path: str | None) -> dict | None:
    if not path:
        return None
    payload = json.loads(Path(path).read_text())
    if isinstance(payload, list):
        if not payload:
            raise ValueError(f"schema file is empty: {path}")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError(f"schema file must contain an object or single-item list: {path}")
    return payload


def fetch_collection(cur: sqlite3.Cursor, name: str):
    row = cur.execute(
        """
        select id, system, type, name, fields, indexes,
               listRule, viewRule, createRule, updateRule, deleteRule,
               options, created, updated
        from _collections
        where name = ?
        """,
        (name,),
    ).fetchone()
    if not row:
        return None
    keys = [
        "id",
        "system",
        "type",
        "name",
        "fields",
        "indexes",
        "listRule",
        "viewRule",
        "createRule",
        "updateRule",
        "deleteRule",
        "options",
        "created",
        "updated",
    ]
    return dict(zip(keys, row))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="PocketBase sqlite database path")
    parser.add_argument("--source", required=True, help="Source collection name")
    parser.add_argument("--target", required=True, help="Target collection name")
    parser.add_argument("--target-id", required=True, help="Target collection id")
    parser.add_argument("--schema-json", help="Optional PB schema json for target collection")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    schema = load_schema(args.schema_json)

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    source_collection = fetch_collection(cur, args.source)
    if not source_collection:
        raise SystemExit(f"source collection not found: {args.source}")
    if fetch_collection(cur, args.target):
        raise SystemExit(f"target collection already exists: {args.target}")

    source_table_sql = cur.execute(
        "select sql from sqlite_master where type='table' and name=?",
        (args.source,),
    ).fetchone()
    if not source_table_sql or not source_table_sql[0]:
        raise SystemExit(f"source table sql not found: {args.source}")

    source_indexes = cur.execute(
        "select name, sql from sqlite_master where type='index' and tbl_name=? and sql is not null order by name",
        (args.source,),
    ).fetchall()

    now_text = utc_now_text()
    fields_json = json.dumps(schema.get("fields") if schema else json.loads(source_collection["fields"]))
    indexes_json = json.dumps(schema.get("indexes") if schema else json.loads(source_collection["indexes"]))
    options_json = json.dumps(schema.get("options", {}) if schema else json.loads(source_collection["options"] or "{}"))
    list_rule = schema.get("listRule", source_collection["listRule"]) if schema else source_collection["listRule"]
    view_rule = schema.get("viewRule", source_collection["viewRule"]) if schema else source_collection["viewRule"]
    create_rule = schema.get("createRule", source_collection["createRule"]) if schema else source_collection["createRule"]
    update_rule = schema.get("updateRule", source_collection["updateRule"]) if schema else source_collection["updateRule"]
    delete_rule = schema.get("deleteRule", source_collection["deleteRule"]) if schema else source_collection["deleteRule"]
    collection_type = schema.get("type", source_collection["type"]) if schema else source_collection["type"]
    collection_system = int(bool(schema.get("system", source_collection["system"]))) if schema else source_collection["system"]

    table_sql = source_table_sql[0].replace(f"`{args.source}`", f"`{args.target}`")
    index_sqls = [sql.replace(args.source, args.target) for _, sql in source_indexes]

    if args.dry_run:
        print(table_sql)
        for sql in index_sqls:
            print(sql)
        print(fields_json)
        return

    cur.execute(
        """
        insert into _collections (
            id, system, type, name, fields, indexes,
            listRule, viewRule, createRule, updateRule, deleteRule,
            options, created, updated
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            args.target_id,
            collection_system,
            collection_type,
            args.target,
            fields_json,
            indexes_json,
            list_rule,
            view_rule,
            create_rule,
            update_rule,
            delete_rule,
            options_json,
            now_text,
            now_text,
        ),
    )
    cur.execute(table_sql)
    for sql in index_sqls:
        cur.execute(sql)

    conn.commit()
    print(f"cloned {args.source} -> {args.target}")


if __name__ == "__main__":
    main()
