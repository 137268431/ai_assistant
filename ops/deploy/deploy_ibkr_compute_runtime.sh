#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIB_ROOT="$SCRIPT_DIR/lib"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.136}"
IBKR_REMOTE_ROOT="${IBKR_REMOTE_ROOT:-${IBKR_DEPLOY_IBKR_ROOT:-/opt/ibkr_compute}}"
SYSTEMD_DIR="/etc/systemd/system"
VENV_DIR="$IBKR_REMOTE_ROOT/venv"
OPS_REMOTE_ROOT="$IBKR_REMOTE_ROOT/ops"

DEPLOY_OPS_TOOLS=0
DEPLOY_GATEWAY_SERVICE=0
DRY_RUN=0
SKIP_CHECKS=0
SKIP_REQUIREMENTS=0
SKIP_SYSTEMD=0
RESTART_SERVICE=1
STATUS_ONLY=0
PLAN_ONLY=0
REQUESTED_MODE="scope"
PACKAGE_NAME=""
DIFF_RANGE=""
FILE_ARGS=()
PB_REMOTE_ROOT="${PB_REMOTE_ROOT:-${IBKR_DEPLOY_PB_ROOT:-/opt/pocketbase}}"
DEPLOY_PUBLIC=1
DEPLOY_HOOKS=1
DEPLOY_MIGRATIONS=0
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

usage() {
  cat <<EOF
Usage: deploy_ibkr_compute_runtime.sh [options]

Options:
  --host <host>         Override SSH target
  --mode <mode>         scope | files | package | auto
  --file <path>         Repo-relative file path, repeatable
  --diff <range>        Git diff range, for example HEAD~1..HEAD
  --plan-only           Print the resolved deployment plan and exit
  --package-name <n>    Override generated package name for package mode
  --ops-tools           Deploy ops/ibkr_compute/auth and ops/ibkr_compute/monitor
  --gateway-service     Install runtime/ib_gateway/systemd/ibkr-gateway.service
  --skip-requirements   Skip remote pip install -r requirements.txt
  --skip-systemd        Skip systemd unit sync
  --dry-run             Show rsync changes without mutating the remote host
  --skip-checks         Skip remote python syntax validation
  --no-restart          Skip service restart
  --status-only         Show ibkr-compute and ibkr-gateway status and exit
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
    --ops-tools)
      DEPLOY_OPS_TOOLS=1
      shift
      ;;
    --gateway-service)
      DEPLOY_GATEWAY_SERVICE=1
      shift
      ;;
    --skip-requirements)
      SKIP_REQUIREMENTS=1
      shift
      ;;
    --skip-systemd)
      SKIP_SYSTEMD=1
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
  ssh_run "systemctl is-active ibkr-compute ibkr-gateway"
  exit 0
fi

prepare_target_plan ibkr

if [[ "$NO_MANAGED_CHANGES" -eq 1 ]]; then
  deploy_log "No managed changes for ibkr."
  exit 0
fi

if [[ "$PLAN_ONLY" -eq 1 ]]; then
  print_deploy_plan ibkr
  exit 0
fi

case "$FINAL_MODE" in
  scope)
    scope_deploy_units "${PLAN_UNITS[@]}"
    ;;
  files)
    files_deploy_paths_for_target ibkr "${PLAN_FILES[@]}"
    ;;
  package)
    package_deploy_units "${PLAN_UNITS[@]}"
    ;;
  *)
    deploy_die "Unsupported final mode: $FINAL_MODE"
    ;;
esac
