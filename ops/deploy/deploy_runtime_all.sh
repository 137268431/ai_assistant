#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
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

usage() {
  cat <<EOF
Usage: deploy_runtime_all.sh [options]

Options:
  --host <host>       Override SSH target
  --migrations        Deploy PocketBase extension migrations
  --ops-tools         Deploy IBKR operational auth/monitor tools
  --gateway-service   Install ibkr-gateway systemd unit
  --dry-run           Show rsync changes without mutating the remote host
  --skip-checks       Skip remote syntax validation
  --no-restart        Skip service restarts
  --status-only       Show pocketbase / ibkr-compute / ibkr-gateway status and exit
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --migrations)
      WITH_MIGRATIONS=1
      shift
      ;;
    --ops-tools)
      WITH_OPS_TOOLS=1
      shift
      ;;
    --gateway-service)
      WITH_GATEWAY_SERVICE=1
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

declare -a pb_args=()
declare -a ibkr_args=()

[[ -n "$REMOTE_HOST" ]] && pb_args+=(--host "$REMOTE_HOST") && ibkr_args+=(--host "$REMOTE_HOST")
[[ "$WITH_MIGRATIONS" -eq 1 ]] && pb_args+=(--migrations)
[[ "$WITH_OPS_TOOLS" -eq 1 ]] && ibkr_args+=(--ops-tools)
[[ "$WITH_GATEWAY_SERVICE" -eq 1 ]] && ibkr_args+=(--gateway-service)
[[ "$DRY_RUN" -eq 1 ]] && pb_args+=(--dry-run) && ibkr_args+=(--dry-run)
[[ "$SKIP_CHECKS" -eq 1 ]] && pb_args+=(--skip-checks) && ibkr_args+=(--skip-checks)
[[ "$NO_RESTART" -eq 1 ]] && pb_args+=(--no-restart) && ibkr_args+=(--no-restart)

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

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  run_pb --status-only
  run_ibkr --status-only
  exit 0
fi

run_ibkr
run_pb
