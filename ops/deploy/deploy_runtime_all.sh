#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIB_ROOT="$SCRIPT_DIR/lib"
POCKETBASE_SCRIPT="$SCRIPT_DIR/deploy_pocketbase_runtime.sh"
COMPUTE_SCRIPT="$SCRIPT_DIR/deploy_ibkr_compute_runtime.sh"
RUNTIME_SCRIPT="$SCRIPT_DIR/deploy_ibkr_runtime_service.sh"
CONSOLE_SCRIPT="$SCRIPT_DIR/deploy_ibkr_console.sh"
PUBLIC_PROXY_SCRIPT="$SCRIPT_DIR/deploy_ibkr_public_proxy.sh"

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
IBKR_RUNTIME_REMOTE_ROOT="${IBKR_RUNTIME_REMOTE_ROOT:-${IBKR_DEPLOY_RUNTIME_ROOT:-/opt/ibkr_runtime}}"
IBKR_CONSOLE_REMOTE_ROOT="${IBKR_CONSOLE_REMOTE_ROOT:-${IBKR_DEPLOY_CONSOLE_ROOT:-/opt/ibkr_console}}"
SYSTEMD_DIR="/etc/systemd/system"
OPS_REMOTE_ROOT="$IBKR_REMOTE_ROOT/ops"
VENV_DIR="$IBKR_REMOTE_ROOT/venv"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.246}"
DEPLOY_IGNORE_UNMANAGED=0
HEALTH_CHECK_ENVIRONMENT="${DEPLOY_HEALTH_ENVIRONMENT:-live}"
STACK_HEALTH_TIMEOUT_SECONDS="${DEPLOY_STACK_HEALTH_TIMEOUT_SECONDS:-420}"
STACK_HEALTH_RETRY_INTERVAL_SECONDS="${DEPLOY_STACK_HEALTH_RETRY_INTERVAL_SECONDS:-15}"

source "$LIB_ROOT/common.sh"
source "$LIB_ROOT/cli.sh"
source "$LIB_ROOT/units.sh"
source "$LIB_ROOT/verify.sh"
source "$LIB_ROOT/restart.sh"
source "$LIB_ROOT/planner.sh"

usage() {
  cat <<EOF
Usage: deploy_runtime_all.sh [options]

Compatibility name: this is the split IBKR stack deploy orchestrator. Prefer
deploy_ibkr_stack.sh for new usage. Without --file/--diff, the default scope
deploy can publish/restart compute, api, scheduler, runtime, PocketBase runtime,
console, and public proxy. Gateway units are opt-in to avoid forcing IBKR 2FA.

Options:
  --host <host>       Override SSH target
  --mode <mode>       scope | files | package | auto
  --file <path>       Repo-relative file path, repeatable
  --diff <range>      Git diff range, for example HEAD~1..HEAD
  --plan-only         Print the resolved deployment plan and exit
  --package-name <n>  Override generated package name for package mode
  --migrations        Deploy PocketBase extension migrations
  --ops-tools         Deprecated legacy flag, kept only for CLI compatibility
  --gateway-service   Opt in to syncing/restarting ibkr-display and ibkr-gateway units
  --restart-gateway   Alias for --gateway-service
  --dry-run           Show rsync changes without mutating the remote host
  --skip-checks       Skip remote syntax validation
  --no-restart        Skip service restarts
  --status-only       Show pocketbase / ibkr-console / ibkr-api / ibkr-scheduler / ibkr-runtime / ibkr-compute / ibkr-display / ibkr-gateway status and exit
  -h, --help          Show this help

Main deploy ownership:
  ibkr-compute     runtime/ibkr_compute/src + ibkr-compute.service
  ibkr-api         runtime/ibkr_api/src + ibkr-api.service
  ibkr-scheduler   runtime/ibkr_scheduler/src + ibkr-scheduler.service
  ibkr-runtime     runtime/ibkr_runtime/src + ibkr-runtime.service
  ibkr-gateway     runtime/ib_gateway/systemd/ibkr-gateway.service
  ibkr-display     runtime/ib_gateway/systemd/ibkr-display.service
  ibkr-console     runtime/ibkr_console/static + ibkr-console.service
  pocketbase       runtime/pocketbase/pb_public/pb_hooks + pocketbase.service
  public proxy     Caddy config for quant.lzw-glory.top / pb.lzw-glory.top

Tip: always use --plan-only first when unsure; it prints exact files and
systemd services that will be restarted.
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
    --gateway-service|--restart-gateway)
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
  REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.246}"
fi

declare -a pb_args=()
declare -a compute_args=()
declare -a runtime_args=()
declare -a console_args=()
declare -a proxy_args=()

[[ -n "$REMOTE_HOST" ]] && pb_args+=(--host "$REMOTE_HOST") && compute_args+=(--host "$REMOTE_HOST") && runtime_args+=(--host "$REMOTE_HOST") && console_args+=(--host "$REMOTE_HOST")
[[ "$WITH_MIGRATIONS" -eq 1 ]] && pb_args+=(--migrations)
[[ "$WITH_OPS_TOOLS" -eq 1 ]] && compute_args+=(--ops-tools)
[[ "$WITH_GATEWAY_SERVICE" -eq 1 ]] && runtime_args+=(--gateway-service)
[[ -n "$REMOTE_HOST" ]] && proxy_args+=(--host "$REMOTE_HOST")
[[ "$DRY_RUN" -eq 1 ]] && pb_args+=(--dry-run) && compute_args+=(--dry-run) && runtime_args+=(--dry-run) && console_args+=(--dry-run) && proxy_args+=(--dry-run)
[[ "$SKIP_CHECKS" -eq 1 ]] && pb_args+=(--skip-checks) && compute_args+=(--skip-checks) && runtime_args+=(--skip-checks) && console_args+=(--skip-checks) && proxy_args+=(--skip-checks)
[[ "$NO_RESTART" -eq 1 ]] && pb_args+=(--no-restart) && compute_args+=(--no-restart) && runtime_args+=(--no-restart) && console_args+=(--no-restart)
[[ "$PLAN_ONLY" -eq 1 ]] && pb_args+=(--plan-only) && compute_args+=(--plan-only) && runtime_args+=(--plan-only) && console_args+=(--plan-only) && proxy_args+=(--plan-only)
[[ -n "$PACKAGE_NAME" ]] && pb_args+=(--package-name "$PACKAGE_NAME") && compute_args+=(--package-name "$PACKAGE_NAME") && runtime_args+=(--package-name "$PACKAGE_NAME") && console_args+=(--package-name "$PACKAGE_NAME")

if [[ "$NO_RESTART" -eq 1 ]]; then
  proxy_args+=(--skip-reload)
fi

run_pb() {
  if [[ ${#pb_args[@]} -gt 0 ]]; then
    bash "$POCKETBASE_SCRIPT" "${pb_args[@]}" "$@"
  else
    bash "$POCKETBASE_SCRIPT" "$@"
  fi
}

run_compute() {
  if [[ ${#compute_args[@]} -gt 0 ]]; then
    bash "$COMPUTE_SCRIPT" "${compute_args[@]}" "$@"
  else
    bash "$COMPUTE_SCRIPT" "$@"
  fi
}

run_runtime() {
  if [[ ${#runtime_args[@]} -gt 0 ]]; then
    bash "$RUNTIME_SCRIPT" "${runtime_args[@]}" "$@"
  else
    bash "$RUNTIME_SCRIPT" "$@"
  fi
}

run_console() {
  if [[ ${#console_args[@]} -gt 0 ]]; then
    bash "$CONSOLE_SCRIPT" "${console_args[@]}" "$@"
  else
    bash "$CONSOLE_SCRIPT" "$@"
  fi
}

run_public_proxy() {
  if [[ ${#proxy_args[@]} -gt 0 ]]; then
    bash "$PUBLIC_PROXY_SCRIPT" "${proxy_args[@]}" "$@"
  else
    bash "$PUBLIC_PROXY_SCRIPT" "$@"
  fi
}

public_proxy_needs_deploy() {
  local rel_path
  for rel_path in "${INPUT_TOUCHED_PATHS[@]-}"; do
    case "$rel_path" in
      ops/deploy/deploy_ibkr_public_proxy.sh|ops/templates/caddy/*)
        return 0
        ;;
    esac
  done
  return 1
}

collect_target_file_args() {
  local array_name="$1"
  local target="$2"
  local source_kind="$3"
  eval "$array_name=()"
  local candidate_paths=()
  local rel_path
  local selected_unit
  local matched

  case "$source_kind" in
    deployable)
      candidate_paths=( "${INPUT_DEPLOYABLE_FILES[@]-}" )
      ;;
    touched)
      candidate_paths=( "${INPUT_TOUCHED_PATHS[@]-}" )
      ;;
    *)
      deploy_die "Unsupported target file arg source: $source_kind"
      ;;
  esac

  for rel_path in "${candidate_paths[@]-}"; do
    [[ -n "$rel_path" ]] || continue
    matched=0
    while IFS= read -r selected_unit; do
      [[ -n "$selected_unit" ]] || continue
      if unit_matches_path "$selected_unit" "$rel_path"; then
        matched=1
        break
      fi
    done < <(list_selected_units_for_target "$target")
    [[ "$matched" -eq 1 ]] || continue
    eval "$array_name+=(--file \"\$rel_path\")"
  done
}

wait_for_full_stack_health() {
  [[ "${PLAN_ONLY:-0}" -eq 1 ]] && return 0
  [[ "${DRY_RUN:-0}" -eq 1 ]] && return 0
  [[ "${STATUS_ONLY:-0}" -eq 1 ]] && return 0
  [[ "${NO_RESTART:-0}" -eq 1 ]] && return 0
  command -v python3 >/dev/null 2>&1 || {
    deploy_warn "python3 not found; skipping final stack health wait."
    return 0
  }

  local deadline
  local output=""
  deadline=$(( $(date +%s) + STACK_HEALTH_TIMEOUT_SECONDS ))
  deploy_log "Waiting for full stack health (environment=$HEALTH_CHECK_ENVIRONMENT)..."

  while true; do
    if output="$(python3 "$AI_ASSISTANT_ROOT/ops/ibkr_stack/health/check_stack.py" --environment "$HEALTH_CHECK_ENVIRONMENT" --json 2>&1)"; then
      deploy_log "Full stack health check passed."
      return 0
    fi
    if (( $(date +%s) >= deadline )); then
      deploy_error "Full stack health check timed out."
      printf '%s\n' "$output" >&2
      return 1
    fi
    deploy_log "Stack not ready yet; retrying in ${STACK_HEALTH_RETRY_INTERVAL_SECONDS}s..."
    sleep "$STACK_HEALTH_RETRY_INTERVAL_SECONDS"
  done
}

prepare_target_plan all

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  run_pb --status-only
  run_console --status-only
  run_runtime --status-only
  run_compute --status-only
  run_public_proxy --status-only
  exit 0
fi

PUBLIC_PROXY_NEEDS_DEPLOY=0
if public_proxy_needs_deploy; then
  PUBLIC_PROXY_NEEDS_DEPLOY=1
fi

if [[ "$NO_MANAGED_CHANGES" -eq 1 && "$PUBLIC_PROXY_NEEDS_DEPLOY" -eq 0 ]]; then
  deploy_log "No managed changes for deploy_runtime_all."
  exit 0
fi

if [[ "$REQUESTED_MODE" != "scope" || "$HAS_CHANGE_SOURCE" -eq 1 ]]; then
  local_file_source="deployable"
  if [[ "$FINAL_MODE" == "package" ]]; then
    local_file_source="touched"
  fi

  target_pb_file_args=()
  target_console_file_args=()
  target_compute_file_args=()
  target_runtime_file_args=()
  collect_target_file_args target_pb_file_args pocketbase "$local_file_source"
  collect_target_file_args target_console_file_args ibkr_console "$local_file_source"
  collect_target_file_args target_compute_file_args ibkr_compute "$local_file_source"
  collect_target_file_args target_runtime_file_args ibkr_runtime "$local_file_source"

  pb_args+=(--mode "$FINAL_MODE")
  console_args+=(--mode "$FINAL_MODE")
  compute_args+=(--mode "$FINAL_MODE")
  runtime_args+=(--mode "$FINAL_MODE")
  if [[ ${#target_pb_file_args[@]} -gt 0 ]]; then
    pb_args+=( "${target_pb_file_args[@]}" )
  fi
  if [[ ${#target_console_file_args[@]} -gt 0 ]]; then
    console_args+=( "${target_console_file_args[@]}" )
  fi
  if [[ ${#target_compute_file_args[@]} -gt 0 ]]; then
    compute_args+=( "${target_compute_file_args[@]}" )
  fi
  if [[ ${#target_runtime_file_args[@]} -gt 0 ]]; then
    runtime_args+=( "${target_runtime_file_args[@]}" )
  fi
fi

families=()
for plan_unit in "${PLAN_UNITS[@]}"; do
  append_unique families "$(unit_family "$plan_unit")"
done

if [[ "$REQUESTED_MODE" == "scope" && "$HAS_CHANGE_SOURCE" -eq 0 ]]; then
  run_compute
  run_runtime
  run_pb
  run_console
  run_public_proxy
  wait_for_full_stack_health
  exit 0
fi

if array_contains "ibkr_compute" "${families[@]}"; then
  DEPLOY_IGNORE_UNMANAGED=1 run_compute
fi
if array_contains "ibkr_runtime" "${families[@]}"; then
  DEPLOY_IGNORE_UNMANAGED=1 run_runtime
fi
if array_contains "pocketbase" "${families[@]}"; then
  DEPLOY_IGNORE_UNMANAGED=1 run_pb
fi
if array_contains "ibkr_console" "${families[@]}"; then
  DEPLOY_IGNORE_UNMANAGED=1 run_console
fi
if [[ "$PUBLIC_PROXY_NEEDS_DEPLOY" -eq 1 ]]; then
  DEPLOY_IGNORE_UNMANAGED=1 run_public_proxy
fi

wait_for_full_stack_health
