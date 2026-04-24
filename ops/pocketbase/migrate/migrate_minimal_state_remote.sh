#!/usr/bin/env bash
set -euo pipefail

SOURCE_HOST="${PB_SOURCE_HOST:-root@206.119.171.136}"
TARGET_HOST="${PB_TARGET_HOST:-${IBKR_DEPLOY_HOST:-root@206.119.171.246}}"
SOURCE_DB="${PB_SOURCE_DB:-/opt/pocketbase/pb_data/data.db}"
TARGET_DB="${PB_TARGET_DB:-/opt/pocketbase/pb_data/data.db}"
BACKUP_TARGET=1
MODE="all"

usage() {
  cat <<EOF
Usage: migrate_minimal_state_remote.sh [options]

Moves the minimal PocketBase state from the source host to the target host.
Supported tables are `_superusers` and `config`.

Options:
  --source-host <host>   Override the source SSH target
  --target-host <host>   Override the target SSH target
  --source-db <path>     Override the source PocketBase SQLite path
  --target-db <path>     Override the target PocketBase SQLite path
  --superusers-only      Copy only `_superusers`
  --config-only          Copy only `config`
  --no-backup            Skip the automatic target DB backup
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-host)
      SOURCE_HOST="${2:?missing source host}"
      shift 2
      ;;
    --target-host)
      TARGET_HOST="${2:?missing target host}"
      shift 2
      ;;
    --source-db)
      SOURCE_DB="${2:?missing source db path}"
      shift 2
      ;;
    --target-db)
      TARGET_DB="${2:?missing target db path}"
      shift 2
      ;;
    --superusers-only)
      MODE="superusers"
      shift
      ;;
    --config-only)
      MODE="config"
      shift
      ;;
    --no-backup)
      BACKUP_TARGET=0
      shift
      ;;
    -h|--help)
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

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
remote_tmp_dir="/tmp/codex-pb-migrate-$$"
local_source_copy="$tmp_dir/source.db"
remote_source_copy="$remote_tmp_dir/source.db"

tables=()
case "$MODE" in
  all)
    tables=(_superusers config)
    ;;
  superusers)
    tables=(_superusers)
    ;;
  config)
    tables=(config)
    ;;
  *)
    echo "Unknown mode: $MODE" >&2
    exit 1
    ;;
esac

scp "$SOURCE_HOST:$SOURCE_DB" "$local_source_copy"
ssh "$TARGET_HOST" "mkdir -p '$remote_tmp_dir'"
scp "$local_source_copy" "$TARGET_HOST:$remote_source_copy"

ssh "$TARGET_HOST" bash -s -- "$TARGET_DB" "$remote_source_copy" "$BACKUP_TARGET" "$MODE" <<'REMOTE'
set -euo pipefail

target_db="$1"
source_db="$2"
backup_target="$3"
mode="$4"
timestamp="$(date +%Y%m%d%H%M%S)"

if [[ "$backup_target" == "1" && -f "$target_db" ]]; then
  cp "$target_db" "$target_db.bak.$timestamp"
fi

case "$mode" in
  all)
    sqlite3 "$target_db" <<SQL
PRAGMA foreign_keys = OFF;
ATTACH DATABASE '$source_db' AS source_db;
BEGIN IMMEDIATE;
DELETE FROM _superusers;
DELETE FROM config;
INSERT INTO _superusers SELECT * FROM source_db._superusers;
INSERT INTO config SELECT * FROM source_db.config;
COMMIT;
DETACH DATABASE source_db;
PRAGMA foreign_keys = ON;
SQL
    sqlite3 "$target_db" "select 'superusers', count(*) from _superusers union all select 'config', count(*) from config;"
    rm -f "$source_db"
    ;;
  superusers)
    sqlite3 "$target_db" <<SQL
PRAGMA foreign_keys = OFF;
ATTACH DATABASE '$source_db' AS source_db;
BEGIN IMMEDIATE;
DELETE FROM _superusers;
INSERT INTO _superusers SELECT * FROM source_db._superusers;
COMMIT;
DETACH DATABASE source_db;
PRAGMA foreign_keys = ON;
SQL
    sqlite3 "$target_db" "select 'superusers', count(*) from _superusers;"
    rm -f "$source_db"
    ;;
  config)
    sqlite3 "$target_db" <<SQL
PRAGMA foreign_keys = OFF;
ATTACH DATABASE '$source_db' AS source_db;
BEGIN IMMEDIATE;
DELETE FROM config;
INSERT INTO config SELECT * FROM source_db.config;
COMMIT;
DETACH DATABASE source_db;
PRAGMA foreign_keys = ON;
SQL
    sqlite3 "$target_db" "select 'config', count(*) from config;"
    rm -f "$source_db"
    ;;
esac
REMOTE
