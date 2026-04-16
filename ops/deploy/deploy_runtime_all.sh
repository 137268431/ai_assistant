#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIB_ROOT="$SCRIPT_DIR/lib"
POCKETBASE_SCRIPT="$SCRIPT_DIR/deploy_pocketbase_runtime.sh"
IBKR_SCRIPT="$SCRIPT_DIR/deploy_ibkr_compute_runtime.sh"

REMOTE_HOST=""
WITH_MIGRATIONS=0
WITH_OPS_TOOLS=0
WITH_GATEWAY_SERVICE=0
DRY_RUN=0
SKIP_CHECKS=0
NO_RESTART=0
STATUS_ONLY=0
REQUESTED_MODE="scope"
PLAN_ONLY=0
PACKAGE_NAME=""
DIFF_RANGE=""
FILE_ARGS=()
DEPLOY_PUBLIC=1
DEPLOY_HOOKS=1
DEPLOY_MIGRATIONS=0
DEPLOY_OPS_TOOLS=0
DEPLOY_GATEWAY_SERVICE=0
SKIP_SYSTEMD=0
SKIP_REQUIREMENTS=0
PB_REMOTE_ROOT="${PB_REMOTE_ROOT:-${IBKR_DEPLOY_PB_ROOT:-/opt/pocketbase}}"
IBKR_REMOTE_ROOT="${IBKR_REMOTE_ROOT:-${IBKR_DEPLOY_IBKR_ROOT:-/opt/ibkr_compute}}"
SYSTEMD_DIR="/etc/systemd/system"
OPS_REMOTE_ROOT="$IBKR_REMOTE_ROOT/ops"
VENV_DIR="$IBKR_REMOTE_ROOT/venv"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.136}"
DEPLOY_IGNORE_UNMANAGED=0

source "$LIB_ROOT/common.sh"
source "$LIB_ROOT/cli.sh"
source "$LIB_ROOT/units.sh"
source "$LIB_ROOT/verify.sh"
source "$LIB_ROOT/restart.sh"
source "$LIB_ROOT/planner.sh"

usage() {
  cat <<EOF
Usage: deploy_runtime_all.sh [options]

Options:
  --host <host>       Override SSH target
  --mode <mode>       scope | files | package | auto
  --file <path>       Repo-relative file path, repeatable
  --diff <range>      Git diff range, for example HEAD~1..HEAD
  --plan-only         Print the resolved deployment plan and exit
  --package-name <n>  Override generated package name for package mode
  --migrations        Deploy PocketBase extension migrations
  --ops-tools         Deprecated legacy flag, kept only for CLI compatibility
  --gateway-service   Install ibkr-display and ibkr-gateway systemd units
  --dry-run           Show rsync changes without mutating the remote host
  --skip-checks       Skip remote syntax validation
  --no-restart        Skip service restarts
  --status-only       Show pocketbase / ibkr-compute / ibkr-display / ibkr-gateway status and exit
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
    --migrations)
      WITH_MIGRATIONS=1
      DEPLOY_MIGRATIONS=1
      shift
      ;;
    --ops-tools)
      WITH_OPS_TOOLS=1
      DEPLOY_OPS_TOOLS=1
      shift
      ;;
    --gateway-service)
      WITH_GATEWAY_SERVICE=1
      DEPLOY_GATEWAY_SERVICE=1
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
      NO_RESTART=1
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

if [[ -n "${REMOTE_HOST:-}" ]]; then
  :
else
  REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.136}"
fi

declare -a pb_args=()
declare -a ibkr_args=()

[[ -n "$REMOTE_HOST" ]] && pb_args+=(--host "$REMOTE_HOST") && ibkr_args+=(--host "$REMOTE_HOST")
[[ "$WITH_MIGRATIONS" -eq 1 ]] && pb_args+=(--migrations)
[[ "$WITH_OPS_TOOLS" -eq 1 ]] && ibkr_args+=(--ops-tools)
[[ "$WITH_GATEWAY_SERVICE" -eq 1 ]] && ibkr_args+=(--gateway-service)
[[ "$DRY_RUN" -eq 1 ]] && pb_args+=(--dry-run) && ibkr_args+=(--dry-run)
[[ "$SKIP_CHECKS" -eq 1 ]] && pb_args+=(--skip-checks) && ibkr_args+=(--skip-checks)
[[ "$NO_RESTART" -eq 1 ]] && pb_args+=(--no-restart) && ibkr_args+=(--no-restart)
[[ "$PLAN_ONLY" -eq 1 ]] && pb_args+=(--plan-only) && ibkr_args+=(--plan-only)
[[ -n "$PACKAGE_NAME" ]] && pb_args+=(--package-name "$PACKAGE_NAME") && ibkr_args+=(--package-name "$PACKAGE_NAME")

run_pb() {
  if [[ ${#pb_args[@]} -gt 0 ]]; then
    bash "$POCKETBASE_SCRIPT" "${pb_args[@]}" "$@"
  else
    bash "$POCKETBASE_SCRIPT" "$@"
  fi
}

run_ibkr() {
  if [[ ${#ibkr_args[@]} -gt 0 ]]; then
    bash "$IBKR_SCRIPT" "${ibkr_args[@]}" "$@"
  else
    bash "$IBKR_SCRIPT" "$@"
  fi
}

prepare_target_plan all

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  run_pb --status-only
  run_ibkr --status-only
  exit 0
fi

if [[ "$NO_MANAGED_CHANGES" -eq 1 ]]; then
  deploy_log "No managed changes for deploy_runtime_all."
  exit 0
fi

if [[ "$REQUESTED_MODE" != "scope" || "$HAS_CHANGE_SOURCE" -eq 1 ]]; then
  pb_args+=(--mode "$FINAL_MODE")
  ibkr_args+=(--mode "$FINAL_MODE")
  if [[ -n "$DIFF_RANGE" ]]; then
    pb_args+=(--diff "$DIFF_RANGE")
    ibkr_args+=(--diff "$DIFF_RANGE")
  fi
  if [[ ${#FILE_ARGS[@]} -gt 0 ]]; then
    local_file=""
    for local_file in "${FILE_ARGS[@]}"; do
      pb_args+=(--file "$local_file")
      ibkr_args+=(--file "$local_file")
    done
  fi
fi

families=()
for plan_unit in "${PLAN_UNITS[@]}"; do
  append_unique families "$(unit_family "$plan_unit")"
done

if [[ "$REQUESTED_MODE" == "scope" && "$HAS_CHANGE_SOURCE" -eq 0 ]]; then
  run_ibkr
  run_pb
  exit 0
fi

if array_contains "ibkr" "${families[@]}"; then
  DEPLOY_IGNORE_UNMANAGED=1 run_ibkr
fi
if array_contains "pocketbase" "${families[@]}"; then
  DEPLOY_IGNORE_UNMANAGED=1 run_pb
fi
