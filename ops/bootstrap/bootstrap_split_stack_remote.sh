#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.246}"
DEPLOY_MODE="${IBKR_BOOTSTRAP_DEPLOY_MODE:-package}"
SKIP_BASE=0
SKIP_POCKETBASE=0
SKIP_ENV_SEED=0
SKIP_GATEWAY=0
SKIP_DEPLOY=0
SKIP_HEALTH=0
STATUS_ONLY=0
WITH_SCHEMA_STATE=0
LEGACY_SOURCE_HOST="${PB_SOURCE_HOST:-root@206.119.171.136}"
PB_BASE_URL="${PB_BASE_URL:-https://pb.lzw-glory.top}"
PB_SUPERUSER_EMAIL="${PB_SUPERUSER_EMAIL:-}"
PB_SUPERUSER_PASSWORD="${PB_SUPERUSER_PASSWORD:-}"
PB_DELETE_MISSING_SCHEMA=0

usage() {
  cat <<EOF
Usage: bootstrap_split_stack_remote.sh [options]

Bootstraps base software, PocketBase binary, IB Gateway/IBC, and the split-stack deploy.
PocketBase schema import and minimal config/auth migration can also be chained with --with-schema-state.

Options:
  --host <host>         Override SSH target
  --source-host <host>  Source host used by --with-schema-state (default: root@206.119.171.136)
  --mode <mode>         Deploy mode for deploy_runtime_all.sh (default: package)
  --skip-base           Skip base software install
  --skip-pocketbase     Skip PocketBase binary bootstrap
  --skip-env-seed       Skip seeding `.env` files from the legacy host
  --skip-gateway        Skip IB Gateway/IBC bootstrap
  --skip-deploy         Skip runtime payload deploy
  --skip-health         Skip final health check
  --with-schema-state   Chain PocketBase `_superusers` + schema + `config` bootstrap after service deploy
  --pb-base-url <url>   PocketBase base URL used for schema bootstrap (default: https://pb.lzw-glory.top)
  --pb-email <email>    PocketBase superuser email for schema bootstrap
  --pb-password <pwd>   PocketBase superuser password for schema bootstrap
  --pb-delete-missing   Pass --delete-missing to the schema import step
  --status-only         Print current bootstrap-related status and exit
  -h, --help            Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --mode)
      DEPLOY_MODE="${2:?missing mode}"
      shift 2
      ;;
    --source-host)
      LEGACY_SOURCE_HOST="${2:?missing source host}"
      shift 2
      ;;
    --skip-base)
      SKIP_BASE=1
      shift
      ;;
    --skip-pocketbase)
      SKIP_POCKETBASE=1
      shift
      ;;
    --skip-env-seed)
      SKIP_ENV_SEED=1
      shift
      ;;
    --skip-gateway)
      SKIP_GATEWAY=1
      shift
      ;;
    --skip-deploy)
      SKIP_DEPLOY=1
      shift
      ;;
    --skip-health)
      SKIP_HEALTH=1
      shift
      ;;
    --with-schema-state)
      WITH_SCHEMA_STATE=1
      shift
      ;;
    --pb-base-url)
      PB_BASE_URL="${2:?missing pb base url}"
      shift 2
      ;;
    --pb-email)
      PB_SUPERUSER_EMAIL="${2:?missing pb email}"
      shift 2
      ;;
    --pb-password)
      PB_SUPERUSER_PASSWORD="${2:?missing pb password}"
      shift 2
      ;;
    --pb-delete-missing)
      PB_DELETE_MISSING_SCHEMA=1
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
  bash "$AI_ASSISTANT_ROOT/ops/bootstrap/install_base_runtime_remote.sh" --host "$REMOTE_HOST" --status-only
  printf '%s\n' '====='
  bash "$AI_ASSISTANT_ROOT/ops/pocketbase/install/bootstrap_pocketbase_remote.sh" --host "$REMOTE_HOST" --status-only
  printf '%s\n' '====='
  bash "$AI_ASSISTANT_ROOT/ops/deploy/deploy_runtime_all.sh" --host "$REMOTE_HOST" --status-only
  exit 0
fi

if [[ "$SKIP_BASE" -ne 1 ]]; then
  bash "$AI_ASSISTANT_ROOT/ops/bootstrap/install_base_runtime_remote.sh" --host "$REMOTE_HOST"
fi

if [[ "$SKIP_POCKETBASE" -ne 1 ]]; then
  bash "$AI_ASSISTANT_ROOT/ops/pocketbase/install/bootstrap_pocketbase_remote.sh" --host "$REMOTE_HOST"
fi

if [[ "$SKIP_ENV_SEED" -ne 1 ]]; then
  bash "$AI_ASSISTANT_ROOT/ops/bootstrap/seed_split_stack_env_remote.sh" \
    --source-host "$LEGACY_SOURCE_HOST" \
    --target-host "$REMOTE_HOST"
fi

if [[ "$SKIP_GATEWAY" -ne 1 ]]; then
  bash "$AI_ASSISTANT_ROOT/ops/ib_gateway/install/bootstrap_ib_gateway_remote.sh" --host "$REMOTE_HOST"
fi

if [[ "$WITH_SCHEMA_STATE" -eq 1 ]]; then
  schema_args=(
    --source-host "$LEGACY_SOURCE_HOST"
    --target-host "$REMOTE_HOST"
    --base-url "$PB_BASE_URL"
  )
  if [[ -n "$PB_SUPERUSER_EMAIL" ]]; then
    schema_args+=(--email "$PB_SUPERUSER_EMAIL")
  fi
  if [[ -n "$PB_SUPERUSER_PASSWORD" ]]; then
    schema_args+=(--password "$PB_SUPERUSER_PASSWORD")
  fi
  if [[ "$PB_DELETE_MISSING_SCHEMA" -eq 1 ]]; then
    schema_args+=(--delete-missing)
  fi
  bash "$AI_ASSISTANT_ROOT/ops/pocketbase/bootstrap/bootstrap_schema_state_remote.sh" "${schema_args[@]}"
fi

if [[ "$SKIP_DEPLOY" -ne 1 ]]; then
  bash "$AI_ASSISTANT_ROOT/ops/deploy/deploy_runtime_all.sh" --host "$REMOTE_HOST" --mode "$DEPLOY_MODE"
fi

if [[ "$SKIP_HEALTH" -ne 1 ]]; then
  python3 "$AI_ASSISTANT_ROOT/ops/ibkr_stack/health/check_stack.py" --host "$REMOTE_HOST"
fi
