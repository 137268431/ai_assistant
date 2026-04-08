#!/usr/bin/env bash
set -euo pipefail

DEFAULT_HOST="${PB_SQLITE_HOST:-${IBKR_DEPLOY_HOST:-root@206.119.171.136}}"
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

Options:
  --host <host>       SSH target. Default: ${DEFAULT_HOST}
  --db <path>         Remote PocketBase data.db path. Default: ${DEFAULT_DB}
  --tables            List remote sqlite tables
  --schema <table>    Show schema and indexes for one table
  --sql <sql>         Run a SQL statement
  --format <mode>     sqlite output mode for reads. Default: box
  --write             Allow write statements
  --backup <path>     Backup path before writes; use "auto" for timestamped backup
  --help              Show this help

Notes:
  - Read-only is the default.
  - Write mode bypasses PocketBase hooks and validation.
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

ssh "$HOST" "python3 - <<'PY'
import base64
import os
import subprocess
import sys
from datetime import datetime

db = base64.b64decode('$DB_B64').decode()
sql = base64.b64decode('$SQL_B64').decode()
mode = base64.b64decode('$MODE_B64').decode()
fmt = base64.b64decode('$FORMAT_B64').decode()
backup = base64.b64decode('$BACKUP_B64').decode()

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

if mode == 'write':
    if backup:
        if backup == 'auto':
            stamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
            backup = f'{db}.backup.{stamp}'
        backup_dir = os.path.dirname(backup)
        if backup_dir:
            os.makedirs(backup_dir, exist_ok=True)
        run_sqlite(['sqlite3', db], f\".timeout 5000\\n.backup {backup}\\n\")
        print(f'backup: {backup}', file=sys.stderr)
    run_sqlite(['sqlite3', db], f\".timeout 5000\\nBEGIN;\\n{sql}\\nCOMMIT;\\n\")
else:
    run_sqlite(
        ['sqlite3', '-readonly', db],
        f\".headers on\\n.mode {fmt}\\n.nullvalue NULL\\n.timeout 5000\\n{sql}\\n\",
    )
PY"
