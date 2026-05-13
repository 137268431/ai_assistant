#!/usr/bin/env bash
set -euo pipefail

DEFAULT_HOST="${PB_SQLITE_HOST:-${IBKR_DEPLOY_HOST:-root@206.119.171.246}}"
DEFAULT_PB_ROOT="${PB_REMOTE_ROOT:-/opt/pocketbase}"
DEFAULT_DB="${PB_DB_PATH:-${DEFAULT_PB_ROOT%/}/pb_data/data.db}"
HOST="$DEFAULT_HOST"
DB=""
MODE="readonly"
FORMAT="box"
BACKUP=""
ACTION="sql"
SQL=""
TABLE=""

usage() {
  cat <<EOF
Usage:
  remote_pb_sqlite.sh --tables
  remote_pb_sqlite.sh --schema <table>
  remote_pb_sqlite.sh --sql "<query>"
  remote_pb_sqlite.sh --write --backup auto --sql "<delete/update/insert>"
  remote_pb_sqlite.sh --write --backup db:auto --sql "<high-risk write>"

Options:
  --host <host>       SSH target. Default: ${DEFAULT_HOST}
  --db <path>         Remote PocketBase data.db path. Default: ${DEFAULT_DB}
  --tables            List remote sqlite tables
  --schema <table>    Show schema and indexes for one table
  --sql <sql>         Run a SQL statement
  --format <mode>     sqlite output mode for reads. Default: box
  --write             Allow write statements
  --backup <mode>     Backup before writes. "auto" creates lightweight row/table snapshots
                      for protected tables; "db:auto" creates a full sqlite backup.
  --help              Show this help

Notes:
  - Read-only is the default.
  - Write mode bypasses PocketBase hooks and validation.
  - "auto" is intentionally not a full database backup. Full DB backups must be
    explicit with "db:auto" or an absolute backup path.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="${2:?missing host}"
      shift 2
      ;;
    --db)
      DB="${2:?missing db path}"
      shift 2
      ;;
    --tables)
      ACTION="tables"
      shift
      ;;
    --schema)
      ACTION="schema"
      TABLE="${2:?missing table}"
      shift 2
      ;;
    --sql)
      ACTION="sql"
      SQL="${2:?missing sql}"
      shift 2
      ;;
    --format)
      FORMAT="${2:?missing format}"
      shift 2
      ;;
    --write)
      MODE="write"
      shift
      ;;
    --backup)
      BACKUP="${2:?missing backup path}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$DB" ]]; then
  DB="$DEFAULT_DB"
fi

case "$ACTION" in
  tables)
    SQL="select name, type from sqlite_master where type in ('table','view') and name not like 'sqlite_%' order by type, name;"
    ;;
  schema)
    if [[ -z "$TABLE" ]]; then
      echo "--schema requires a table name" >&2
      exit 1
    fi
    printf -v SQL "%s\n%s\n%s\n%s" \
      "select sql from sqlite_master where type = 'table' and name = '$TABLE';" \
      "pragma table_info('$TABLE');" \
      "pragma index_list('$TABLE');" \
      "select name, sql from sqlite_master where type = 'index' and tbl_name = '$TABLE' order by name;"
    ;;
  sql)
    if [[ -z "$SQL" ]]; then
      echo "--sql requires a statement" >&2
      exit 1
    fi
    ;;
  *)
    echo "Unsupported action: $ACTION" >&2
    exit 1
    ;;
esac

SQL_B64="$(printf '%s' "$SQL" | base64 | tr -d '\n')"
MODE_B64="$(printf '%s' "$MODE" | base64 | tr -d '\n')"
FORMAT_B64="$(printf '%s' "$FORMAT" | base64 | tr -d '\n')"
BACKUP_B64="$(printf '%s' "$BACKUP" | base64 | tr -d '\n')"
DB_B64="$(printf '%s' "$DB" | base64 | tr -d '\n')"

ssh "$HOST" \
  "DB_B64='$DB_B64' SQL_B64='$SQL_B64' MODE_B64='$MODE_B64' FORMAT_B64='$FORMAT_B64' BACKUP_B64='$BACKUP_B64' python3 -" <<'PY'
import base64
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

db = base64.b64decode(os.environ['DB_B64']).decode()
sql = base64.b64decode(os.environ['SQL_B64']).decode()
mode = base64.b64decode(os.environ['MODE_B64']).decode()
fmt = base64.b64decode(os.environ['FORMAT_B64']).decode()
backup = base64.b64decode(os.environ['BACKUP_B64']).decode()
auto_backup_keep = max(1, int(os.getenv('PB_SQLITE_AUTO_BACKUP_KEEP', '1') or '1'))
light_backup_keep = max(1, int(os.getenv('PB_SQLITE_LIGHT_BACKUP_KEEP', '30') or '30'))
light_backup_max_rows = max(1, int(os.getenv('PB_SQLITE_LIGHT_BACKUP_MAX_ROWS', '5000') or '5000'))
light_backup_dir = os.getenv('PB_SQLITE_LIGHT_BACKUP_DIR', '').strip()
if not light_backup_dir:
    light_backup_dir = os.path.join(os.path.dirname(db), 'maintenance_row_backups')
small_backup_tables = {
    item.strip()
    for item in os.getenv(
        'PB_SQLITE_SMALL_BACKUP_TABLES',
        'config,watchlist,ibkr_state,ibkr_targets,orders,ibkr_order_details',
    ).split(',')
    if item.strip()
}
protected_backup_tables = {
    item.strip()
    for item in os.getenv(
        'PB_SQLITE_PROTECTED_BACKUP_TABLES',
        'config,watchlist,ibkr_state,ibkr_targets,orders,ibkr_order_details,ibkr_bars,ibkr_indicators,ibkr_signals',
    ).split(',')
    if item.strip()
}

AUTO_BACKUP_SIDECARS = ('-journal', '-wal', '-shm')

if not os.path.exists(db):
    print(f'Database not found: {db}', file=sys.stderr)
    sys.exit(1)

def run_sqlite(args, script):
    proc = subprocess.run(
        args,
        input=script,
        text=True,
        capture_output=True,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        sys.exit(proc.returncode)

def prune_auto_backups(db_path, keep_count):
    pattern = f'{db_path}.backup.*'
    matches = sorted(glob.glob(pattern))
    base_backups = [
        path for path in matches
        if os.path.isfile(path) and not path.endswith(AUTO_BACKUP_SIDECARS)
    ]
    removable = base_backups[:-keep_count] if keep_count > 0 else base_backups
    removed = []
    for base_path in removable:
        candidates = [base_path] + [f'{base_path}{suffix}' for suffix in AUTO_BACKUP_SIDECARS]
        for candidate in candidates:
            if not os.path.exists(candidate):
                continue
            try:
                os.remove(candidate)
                removed.append(candidate)
            except FileNotFoundError:
                continue
    return removed

def utc_stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

def normalize_identifier(value):
    text = str(value or '').strip().strip(chr(96) + '"[]')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', text):
        return ''
    return text

def quote_identifier(value):
    text = normalize_identifier(value)
    if not text:
        raise ValueError(f'Unsafe sqlite identifier: {value!r}')
    return '"' + text.replace('"', '""') + '"'

def touched_tables(statement):
    tables = set()
    ident = r'[A-Za-z_][A-Za-z0-9_]*'
    open_quote = '[' + re.escape(chr(96) + '"[') + ']'
    close_quote = '[' + re.escape(chr(96) + '"]') + ']'
    ident_token = open_quote + '?' + ident + close_quote + '?'
    patterns = [
        r'\binsert\s+(?:or\s+\w+\s+)?into\s+(' + ident_token + ')',
        r'\breplace\s+into\s+(' + ident_token + ')',
        r'\bupdate\s+(' + ident_token + ')',
        r'\bdelete\s+from\s+(' + ident_token + ')',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, statement, flags=re.IGNORECASE):
            table = normalize_identifier(match.group(1))
            if table:
                tables.add(table)
    return sorted(tables)

def extract_single_table_where(statement, table):
    safe = re.escape(table)
    open_quote = '[' + re.escape(chr(96) + '"[') + ']'
    close_quote = '[' + re.escape(chr(96) + '"]') + ']'
    table_token = open_quote + '?' + safe + close_quote + '?'
    patterns = [
        r'\bupdate\s+' + table_token + r'\s+set\b.+?\bwhere\b(.+?)(?:;|$)',
        r'\bdelete\s+from\s+' + table_token + r'\s+\bwhere\b(.+?)(?:;|$)',
    ]
    for pattern in patterns:
        match = re.search(pattern, statement, flags=re.IGNORECASE | re.DOTALL)
        if match:
            clause = match.group(1).strip()
            if clause:
                return clause
    return ''

def write_light_backup(statement):
    tables = [table for table in touched_tables(statement) if table in protected_backup_tables]
    if not tables:
        print('light_backup: skipped no protected table touched', file=sys.stderr)
        return []

    os.makedirs(light_backup_dir, exist_ok=True)
    created = []
    stamp = utc_stamp()
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for table in tables:
            where_clause = ''
            if table not in small_backup_tables:
                where_clause = extract_single_table_where(statement, table)
                if not where_clause:
                    raise RuntimeError(
                        f'auto light backup refused broad write to large/protected table {table}; '
                        'use a narrower WHERE clause or explicit --backup db:auto during a maintenance window'
                    )
            quoted_table = quote_identifier(table)
            filter_sql = f' where {where_clause}' if where_clause else ''
            count_sql = f'select count(*) from {quoted_table}{filter_sql}'
            count = int(conn.execute(count_sql).fetchone()[0] or 0)
            if count > light_backup_max_rows:
                raise RuntimeError(
                    f'auto light backup refused {table}: {count} rows exceeds '
                    f'PB_SQLITE_LIGHT_BACKUP_MAX_ROWS={light_backup_max_rows}; '
                    'narrow the write or use explicit --backup db:auto during a maintenance window'
                )
            output_path = os.path.join(light_backup_dir, f'{stamp}_{table}.jsonl')
            query_sql = f'select * from {quoted_table}{filter_sql}'
            with open(output_path, 'w', encoding='utf-8') as handle:
                handle.write(json.dumps({
                    '_meta': {
                        'table': table,
                        'row_count': count,
                        'where': where_clause,
                        'created_at': stamp,
                        'source': 'remote_pb_sqlite_light_backup',
                    }
                }, ensure_ascii=False) + '\n')
                for row in conn.execute(query_sql):
                    handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + '\n')
            created.append(output_path)
            print(f'light_backup: {output_path} rows={count}', file=sys.stderr)
    finally:
        conn.close()

    backups = sorted(
        glob.glob(os.path.join(light_backup_dir, '*.jsonl')),
        key=lambda path: os.path.getmtime(path),
    )
    removable = backups[:-light_backup_keep]
    for path in removable:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    if removable:
        print(f'pruned_light_backups: keep={light_backup_keep} removed={len(removable)}', file=sys.stderr)
    return created

if mode == 'write':
    if backup:
        if backup in ('auto', 'light:auto', 'rows:auto', 'table:auto'):
            write_light_backup(sql)
        elif backup == 'db:auto':
            stamp = utc_stamp()
            backup = f'{db}.backup.{stamp}'
            backup_dir = os.path.dirname(backup)
            if backup_dir:
                os.makedirs(backup_dir, exist_ok=True)
            run_sqlite(['sqlite3', db], f".timeout 5000\n.backup {backup}\n")
            print(f'db_backup: {backup}', file=sys.stderr)
            removed = prune_auto_backups(db, auto_backup_keep)
            if removed:
                print(
                    f'pruned_db_backups: keep={auto_backup_keep} removed={len(removed)}',
                    file=sys.stderr,
                )
        else:
            backup_dir = os.path.dirname(backup)
            if backup_dir:
                os.makedirs(backup_dir, exist_ok=True)
            run_sqlite(['sqlite3', db], f".timeout 5000\n.backup {backup}\n")
            print(f'db_backup: {backup}', file=sys.stderr)
    run_sqlite(['sqlite3', db], f".timeout 5000\nBEGIN;\n{sql}\nCOMMIT;\n")
else:
    run_sqlite(
        ['sqlite3', '-readonly', db],
        f".headers on\n.mode {fmt}\n.nullvalue NULL\n.timeout 5000\n{sql}\n",
    )
PY
