#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SOURCE_HOST="${PB_SOURCE_HOST:-root@206.119.171.136}"
TARGET_HOST="${PB_TARGET_HOST:-${IBKR_DEPLOY_HOST:-root@206.119.171.246}}"
SOURCE_DB="${PB_SOURCE_DB:-/opt/pocketbase/pb_data/data.db}"
PB_BASE_URL="${PB_BASE_URL:-https://pb.lzw-glory.top}"
PB_SUPERUSER_EMAIL="${PB_SUPERUSER_EMAIL:-}"
PB_SUPERUSER_PASSWORD="${PB_SUPERUSER_PASSWORD:-}"
DELETE_MISSING=0
SKIP_SUPERUSERS=0
SKIP_SCHEMA=0
SKIP_CONFIG=0
STATUS_ONLY=0
AUTO_TEMP_SUPERUSER=1
TEMP_SUPERUSER_EMAIL=""
TEMP_SUPERUSER_PASSWORD=""
DELETE_TEMP_SUPERUSER=1

usage() {
  cat <<EOF
Usage: bootstrap_schema_state_remote.sh [options]

Bootstraps PocketBase auth/schema/config on the target host with the minimal migration flow:
1. copy `_superusers` from the source host
2. restart PocketBase on the target host
3. import all collection schemas through the PocketBase superuser API
4. copy `config` from the source host
5. restart PocketBase on the target host

Options:
  --source-host <host>   Override the source SSH target
  --target-host <host>   Override the target SSH target
  --source-db <path>     Override the source PocketBase SQLite path used for minimal state copy/status
  --base-url <url>       PocketBase base URL used for schema import (default: https://pb.lzw-glory.top)
  --email <email>        PocketBase superuser email for schema import
  --password <password>  PocketBase superuser password for schema import
  --delete-missing       Pass --delete-missing to collection import
  --skip-superusers      Skip copying `_superusers`
  --skip-schema          Skip schema import
  --skip-config          Skip copying `config`
  --no-auto-temp-superuser  Fail instead of auto-creating a temporary superuser when no schema-import credentials are provided
  --temp-email <email>      Override the temporary superuser email
  --temp-password <pwd>     Override the temporary superuser password
  --keep-temp-superuser     Do not delete the temporary superuser after schema import
  --status-only          Print current PB health plus source/target `_superusers` and `config` row counts
  -h, --help             Show this help
EOF
}

wait_for_pb_health() {
  local base_url="$1"
  python3 - "$base_url" <<'PY'
from __future__ import annotations

import json
import sys
import time
import urllib.request

base_url = sys.argv[1].rstrip("/")
url = f"{base_url}/api/health"
last_error = ""
for _ in range(60):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            if 200 <= response.status < 300 and int(payload.get("code") or 0) == 200:
                raise SystemExit(0)
            last_error = f"unexpected payload: {payload!r}"
    except Exception as exc:
        last_error = str(exc)
    time.sleep(1)
raise SystemExit(last_error or f"PocketBase health check failed: {url}")
PY
}

restart_target_pocketbase() {
  ssh "$TARGET_HOST" "systemctl restart pocketbase"
  wait_for_pb_health "$PB_BASE_URL"
}

ensure_schema_auth_credentials() {
  if [[ -n "$PB_SUPERUSER_EMAIL" && -n "$PB_SUPERUSER_PASSWORD" ]]; then
    return 0
  fi
  if [[ "$AUTO_TEMP_SUPERUSER" -ne 1 ]]; then
    echo "PB superuser email/password are required unless auto temp superuser is enabled" >&2
    exit 1
  fi

  if [[ -z "$TEMP_SUPERUSER_EMAIL" ]]; then
    TEMP_SUPERUSER_EMAIL="codex-bootstrap-$(date +%Y%m%d%H%M%S)@local.dev"
  fi
  if [[ -z "$TEMP_SUPERUSER_PASSWORD" ]]; then
    TEMP_SUPERUSER_PASSWORD="$(
      python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
    )"
  fi

  ssh "$TARGET_HOST" "/opt/pocketbase/pocketbase superuser upsert '$TEMP_SUPERUSER_EMAIL' '$TEMP_SUPERUSER_PASSWORD'"
  PB_SUPERUSER_EMAIL="$TEMP_SUPERUSER_EMAIL"
  PB_SUPERUSER_PASSWORD="$TEMP_SUPERUSER_PASSWORD"
}

cleanup_temp_superuser_if_needed() {
  if [[ "$DELETE_TEMP_SUPERUSER" -eq 1 && -n "$TEMP_SUPERUSER_EMAIL" ]]; then
    ssh "$TARGET_HOST" "/opt/pocketbase/pocketbase superuser delete '$TEMP_SUPERUSER_EMAIL'" >/dev/null 2>&1 || true
  fi
}

show_status() {
  python3 - "$PB_BASE_URL" <<'PY'
from __future__ import annotations

import json
import sys
import urllib.request

base_url = sys.argv[1].rstrip("/")
url = f"{base_url}/api/health"
try:
    with urllib.request.urlopen(url, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
        print(json.dumps({"base_url": base_url, "health": payload}, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({"base_url": base_url, "health_error": str(exc)}, ensure_ascii=False))
PY
  printf '%s\n' '---'
  printf 'source:%s\n' "$SOURCE_HOST"
  ssh "$SOURCE_HOST" "sqlite3 '$SOURCE_DB' \"select 'superusers', count(*) from _superusers union all select 'config', count(*) from config;\" 2>/dev/null || true"
  printf '%s\n' '---'
  printf 'target:%s\n' "$TARGET_HOST"
  ssh "$TARGET_HOST" "sqlite3 '/opt/pocketbase/pb_data/data.db' \"select 'superusers', count(*) from _superusers union all select 'config', count(*) from config;\" 2>/dev/null || true"
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
      SOURCE_DB="${2:?missing source db}"
      shift 2
      ;;
    --base-url)
      PB_BASE_URL="${2:?missing base url}"
      shift 2
      ;;
    --email)
      PB_SUPERUSER_EMAIL="${2:?missing email}"
      shift 2
      ;;
    --password)
      PB_SUPERUSER_PASSWORD="${2:?missing password}"
      shift 2
      ;;
    --delete-missing)
      DELETE_MISSING=1
      shift
      ;;
    --skip-superusers)
      SKIP_SUPERUSERS=1
      shift
      ;;
    --skip-schema)
      SKIP_SCHEMA=1
      shift
      ;;
    --skip-config)
      SKIP_CONFIG=1
      shift
      ;;
    --no-auto-temp-superuser)
      AUTO_TEMP_SUPERUSER=0
      shift
      ;;
    --temp-email)
      TEMP_SUPERUSER_EMAIL="${2:?missing temp email}"
      shift 2
      ;;
    --temp-password)
      TEMP_SUPERUSER_PASSWORD="${2:?missing temp password}"
      shift 2
      ;;
    --keep-temp-superuser)
      DELETE_TEMP_SUPERUSER=0
      shift
      ;;
    --status-only)
      STATUS_ONLY=1
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

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  show_status
  exit 0
fi

if [[ "$SKIP_SUPERUSERS" -ne 1 ]]; then
  bash "$AI_ASSISTANT_ROOT/ops/pocketbase/migrate/migrate_minimal_state_remote.sh" \
    --source-host "$SOURCE_HOST" \
    --target-host "$TARGET_HOST" \
    --superusers-only
  restart_target_pocketbase
fi

if [[ "$SKIP_SCHEMA" -ne 1 ]]; then
  # Create any temporary schema-import superuser only after the superuser copy,
  # because the minimal-state step replaces _superusers on the target.
  ensure_schema_auth_credentials
  import_args=(
    "$AI_ASSISTANT_ROOT/extensions/pocketbase/schema/pb_table/"*.json
    --base-url "$PB_BASE_URL"
    --email "$PB_SUPERUSER_EMAIL"
    --password "$PB_SUPERUSER_PASSWORD"
  )
  if [[ "$DELETE_MISSING" -eq 1 ]]; then
    import_args+=(--delete-missing)
  fi
  python3 "$AI_ASSISTANT_ROOT/extensions/pocketbase/scripts/schema/import_collections.py" "${import_args[@]}"
  cleanup_temp_superuser_if_needed
fi

if [[ "$SKIP_CONFIG" -ne 1 ]]; then
  bash "$AI_ASSISTANT_ROOT/ops/pocketbase/migrate/migrate_minimal_state_remote.sh" \
    --source-host "$SOURCE_HOST" \
    --target-host "$TARGET_HOST" \
    --config-only
fi

restart_target_pocketbase
