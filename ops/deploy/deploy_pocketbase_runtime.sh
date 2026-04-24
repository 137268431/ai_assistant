#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIB_ROOT="$SCRIPT_DIR/lib"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.136}"
PB_REMOTE_ROOT="${PB_REMOTE_ROOT:-${IBKR_DEPLOY_PB_ROOT:-/opt/pocketbase}}"

DEPLOY_PUBLIC=1
DEPLOY_HOOKS=1
DEPLOY_MIGRATIONS=0
DRY_RUN=0
RESTART_SERVICE=1
WAIT_FOR_AUTO_RELOAD=0
SKIP_CHECKS=0
STATUS_ONLY=0
PLAN_ONLY=0
REQUESTED_MODE="scope"
PACKAGE_NAME=""
DIFF_RANGE=""
FILE_ARGS=()
DEPLOY_IGNORE_UNMANAGED="${DEPLOY_IGNORE_UNMANAGED:-0}"

source "$LIB_ROOT/common.sh"
source "$LIB_ROOT/cli.sh"
source "$LIB_ROOT/units.sh"
source "$LIB_ROOT/verify.sh"
source "$LIB_ROOT/restart.sh"
source "$LIB_ROOT/planner.sh"
source "$LIB_ROOT/modes/scope.sh"
source "$LIB_ROOT/modes/files.sh"
source "$LIB_ROOT/modes/package.sh"

apply_runtime_restart_policy() {
  local unit
  local skip_restart_for_runtime_files=1
  [[ "${RESTART_SERVICE:-1}" -eq 1 ]] || return 0
  [[ "${FINAL_MODE:-}" == "files" ]] || return 0
  [[ "${DEPLOY_MIGRATIONS:-0}" -eq 0 ]] || return 0
  [[ ${#PLAN_UNITS[@]} -gt 0 ]] || return 0
  for unit in "${PLAN_UNITS[@]}"; do
    case "$unit" in
      pb_public)
        ;;
      pb_hooks)
        skip_restart_for_runtime_files=0
        break
        ;;
      *)
        skip_restart_for_runtime_files=0
        break
        ;;
    esac
  done
  if [[ "$skip_restart_for_runtime_files" -eq 1 ]]; then
    deploy_log "PocketBase landing/redirect-only files deploy detected; skipping explicit systemctl restart."
    WAIT_FOR_AUTO_RELOAD=1
    RESTART_SERVICE=0
  fi
}

usage() {
  cat <<EOF
Usage: deploy_pocketbase_runtime.sh [options]

Options:
  --host <host>       Override SSH target
  --mode <mode>       scope | files | package | auto
  --file <path>       Repo-relative file path, repeatable
  --diff <range>      Git diff range, for example HEAD~1..HEAD
  --plan-only         Print the resolved deployment plan and exit
  --package-name <n>  Override generated package name for package mode
  --public-only       Deploy only the PocketBase landing tree sourced from runtime/pocketbase/pb_public (including legacy redirect shims)
  --hooks-only        Deploy only runtime/pocketbase/pb_hooks
  --migrations        Deploy extensions/pocketbase/migrations to /opt/pocketbase/extensions/migrations
  --dry-run           Show rsync changes without mutating the remote host
  --skip-checks       Skip remote node --check validation
  --no-restart        Skip PocketBase restart
  --status-only       Show pocketbase service status and exit
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --mode)
      REQUESTED_MODE="${2:?missing mode}"
      validate_mode_value "$REQUESTED_MODE"
      shift 2
      ;;
    --file)
      FILE_ARGS+=("${2:?missing file}")
      shift 2
      ;;
    --diff)
      DIFF_RANGE="${2:?missing diff range}"
      shift 2
      ;;
    --plan-only)
      PLAN_ONLY=1
      shift
      ;;
    --package-name)
      PACKAGE_NAME="${2:?missing package name}"
      shift 2
      ;;
    --public-only)
      DEPLOY_PUBLIC=1
      DEPLOY_HOOKS=0
      shift
      ;;
    --hooks-only)
      DEPLOY_PUBLIC=0
      DEPLOY_HOOKS=1
      shift
      ;;
    --migrations)
      DEPLOY_MIGRATIONS=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --skip-checks)
      SKIP_CHECKS=1
      shift
      ;;
    --no-restart)
      RESTART_SERVICE=0
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
  ssh_run "systemctl is-active pocketbase"
  exit 0
fi

if [[ "$DEPLOY_PUBLIC" -eq 0 && "$DEPLOY_HOOKS" -eq 0 && "$DEPLOY_MIGRATIONS" -eq 0 ]]; then
  deploy_error "Nothing selected for deployment."
  usage >&2
  exit 1
fi

prepare_target_plan pocketbase
apply_runtime_restart_policy

if [[ "$NO_MANAGED_CHANGES" -eq 1 ]]; then
  deploy_log "No managed changes for pocketbase."
  exit 0
fi

if [[ "$PLAN_ONLY" -eq 1 ]]; then
  print_deploy_plan pocketbase
  exit 0
fi

case "$FINAL_MODE" in
  scope)
    scope_deploy_units "${PLAN_UNITS[@]}"
    ;;
  files)
    files_deploy_paths_for_target pocketbase "${PLAN_FILES[@]}"
    ;;
  package)
    package_deploy_units "${PLAN_UNITS[@]}"
    ;;
  *)
    deploy_die "Unsupported final mode: $FINAL_MODE"
    ;;
esac
