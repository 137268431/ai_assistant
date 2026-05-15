#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIB_ROOT="$SCRIPT_DIR/lib"
POCKETBASE_SCRIPT="$SCRIPT_DIR/deploy_pocketbase_runtime.sh"
COMPUTE_SCRIPT="$SCRIPT_DIR/deploy_ibkr_compute_runtime.sh"
BACKTEST_SCRIPT="$SCRIPT_DIR/deploy_ibkr_backtest_runtime.sh"
RUNTIME_SCRIPT="$SCRIPT_DIR/deploy_ibkr_runtime_service.sh"
CONSOLE_SCRIPT="$SCRIPT_DIR/deploy_ibkr_console.sh"
PUBLIC_PROXY_SCRIPT="$SCRIPT_DIR/deploy_ibkr_public_proxy.sh"

REMOTE_HOST=""
WITH_MIGRATIONS=0
WITH_OPS_TOOLS=0
WITH_GATEWAY_SERVICE=0
WITH_POCKETBASE=0
WITH_POCKETBASE_RESTART=0
WITH_ALLOW_LIVE_MIGRATIONS=0
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
DEPLOY_RESTART_GATEWAY=0
DEPLOY_POCKETBASE=0
SKIP_SYSTEMD=0
SKIP_REQUIREMENTS=0
PB_REMOTE_ROOT="${PB_REMOTE_ROOT:-${IBKR_DEPLOY_PB_ROOT:-/opt/pocketbase}}"
IBKR_REMOTE_ROOT="${IBKR_REMOTE_ROOT:-${IBKR_DEPLOY_IBKR_ROOT:-/opt/ibkr_compute}}"
IBKR_BACKTEST_REMOTE_ROOT="${IBKR_BACKTEST_REMOTE_ROOT:-${IBKR_DEPLOY_BACKTEST_ROOT:-/opt/ibkr_backtest}}"
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
STRICT_GATEWAY_HEALTH="${DEPLOY_STRICT_GATEWAY_HEALTH:-0}"

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
deploy can publish/restart compute, backtest, api, scheduler, runtime,
console, and public proxy. PocketBase and Gateway units are protected opt-ins
to avoid unnecessary datastore restarts and IBKR 2FA disruption.

Options:
  --host <host>       Override SSH target
  --mode <mode>       scope | files | package | auto
  --file <path>       Repo-relative file path, repeatable
  --diff <range>      Git diff range, for example HEAD~1..HEAD
  --plan-only         Print the resolved deployment plan and exit
  --package-name <n>  Override generated package name for package mode
  --migrations        Deploy PocketBase extension migrations; implies --pocketbase
  --allow-live-migrations
                      Allow PocketBase migrations without stopping/restarting PocketBase
  --pocketbase        Opt in to PocketBase runtime deploy
  --restart-pocketbase
                      Opt in to PocketBase runtime deploy and explicit PocketBase restart
  --ops-tools         Deprecated legacy flag, kept only for CLI compatibility
  --gateway-service   Opt in to syncing ibkr-display and ibkr-gateway unit files without restarting them
  --restart-gateway   Explicitly sync and restart ibkr-display / ibkr-gateway
  --dry-run           Show rsync changes without mutating the remote host
  --skip-checks       Skip remote syntax validation
  --no-restart        Skip service restarts
  --status-only       Show pocketbase / ibkr-console / ibkr-api / ibkr-scheduler / ibkr-runtime / ibkr-compute / ibkr-backtest / ibkr-display / ibkr-gateway status and exit
  -h, --help          Show this help

Main deploy ownership:
  ibkr-compute     runtime/ibkr_compute/src + ibkr-compute.service
  ibkr-backtest    runtime/ibkr_compute/src + ibkr-backtest.service
  ibkr-api         runtime/ibkr_api/src + ibkr-api.service
  ibkr-scheduler   runtime/ibkr_scheduler/src + ibkr-scheduler.service
  ibkr-runtime     runtime/ibkr_runtime/src + ibkr-runtime.service
  ibkr-gateway     runtime/ib_gateway/systemd/ibkr-gateway.service (opt-in)
  ibkr-display     runtime/ib_gateway/systemd/ibkr-display.service (opt-in)
  ibkr-console     runtime/ibkr_console/static + ibkr-console.service
  pocketbase       runtime/pocketbase/pb_public/pb_hooks + pocketbase.service (opt-in)
  public proxy     Caddy config for quant.lzw-glory.top / pb.lzw-glory.top

Tip: always use --plan-only first when unsure; it prints exact files and
systemd services that will be restarted.

Environment:
  DEPLOY_STRICT_GATEWAY_HEALTH=1
                      Fail final stack health when IBKR Gateway is unhealthy.
                      Default is 0 so compute/console hotfixes do not fail only
                      because Gateway is already waiting for 2FA.
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
      WITH_POCKETBASE=1
      DEPLOY_POCKETBASE=1
      shift
      ;;
    --allow-live-migrations)
      WITH_ALLOW_LIVE_MIGRATIONS=1
      shift
      ;;
    --pocketbase)
      WITH_POCKETBASE=1
      DEPLOY_POCKETBASE=1
      shift
      ;;
    --restart-pocketbase)
      WITH_POCKETBASE=1
      WITH_POCKETBASE_RESTART=1
      DEPLOY_POCKETBASE=1
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
      if [[ "$1" == "--restart-gateway" ]]; then
        DEPLOY_RESTART_GATEWAY=1
      fi
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
declare -a backtest_args=()
declare -a runtime_args=()
declare -a console_args=()
declare -a proxy_args=()

[[ -n "$REMOTE_HOST" ]] && pb_args+=(--host "$REMOTE_HOST") && compute_args+=(--host "$REMOTE_HOST") && backtest_args+=(--host "$REMOTE_HOST") && runtime_args+=(--host "$REMOTE_HOST") && console_args+=(--host "$REMOTE_HOST")
[[ "$WITH_MIGRATIONS" -eq 1 ]] && pb_args+=(--migrations)
[[ "$WITH_ALLOW_LIVE_MIGRATIONS" -eq 1 ]] && pb_args+=(--allow-live-migrations)
[[ "$WITH_POCKETBASE_RESTART" -eq 1 && "$NO_RESTART" -eq 0 ]] && pb_args+=(--restart-pocketbase)
[[ "$WITH_OPS_TOOLS" -eq 1 ]] && compute_args+=(--ops-tools)
[[ "$WITH_GATEWAY_SERVICE" -eq 1 ]] && runtime_args+=(--gateway-service)
[[ "$DEPLOY_RESTART_GATEWAY" -eq 1 ]] && runtime_args+=(--restart-gateway)
[[ -n "$REMOTE_HOST" ]] && proxy_args+=(--host "$REMOTE_HOST")
[[ "$DRY_RUN" -eq 1 ]] && pb_args+=(--dry-run) && compute_args+=(--dry-run) && backtest_args+=(--dry-run) && runtime_args+=(--dry-run) && console_args+=(--dry-run) && proxy_args+=(--dry-run)
[[ "$SKIP_CHECKS" -eq 1 ]] && pb_args+=(--skip-checks) && compute_args+=(--skip-checks) && backtest_args+=(--skip-checks) && runtime_args+=(--skip-checks) && console_args+=(--skip-checks) && proxy_args+=(--skip-checks)
[[ "$NO_RESTART" -eq 1 ]] && pb_args+=(--no-restart) && compute_args+=(--no-restart) && backtest_args+=(--no-restart) && runtime_args+=(--no-restart) && console_args+=(--no-restart)
[[ "$PLAN_ONLY" -eq 1 ]] && pb_args+=(--plan-only) && compute_args+=(--plan-only) && backtest_args+=(--plan-only) && runtime_args+=(--plan-only) && console_args+=(--plan-only) && proxy_args+=(--plan-only)
[[ -n "$PACKAGE_NAME" ]] && pb_args+=(--package-name "$PACKAGE_NAME") && compute_args+=(--package-name "$PACKAGE_NAME") && backtest_args+=(--package-name "$PACKAGE_NAME") && runtime_args+=(--package-name "$PACKAGE_NAME") && console_args+=(--package-name "$PACKAGE_NAME")

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

run_backtest() {
  if [[ ${#backtest_args[@]} -gt 0 ]]; then
    bash "$BACKTEST_SCRIPT" "${backtest_args[@]}" "$@"
  else
    bash "$BACKTEST_SCRIPT" "$@"
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

path_is_deployable_input() {
  local rel_path="$1"
  array_contains "$rel_path" "${INPUT_DEPLOYABLE_FILES[@]-}"
}

unit_requires_package_mode() {
  case "$1" in
    ibkr_requirements|ibkr_backtest_requirements|ibkr_runtime_requirements)
      return 0
      ;;
    pb_systemd|ibkr_systemd|ibkr_backtest_systemd|ibkr_api_systemd|ibkr_scheduler_systemd|ibkr_runtime_systemd|ibkr_console_systemd|gateway_display_systemd|gateway_systemd)
      return 0
      ;;
    pb_migrations)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

target_needs_package_mode() {
  local target="$1"
  local rel_path
  local selected_unit
  for rel_path in "${INPUT_TOUCHED_PATHS[@]-}"; do
    [[ -n "$rel_path" ]] || continue
    while IFS= read -r selected_unit; do
      [[ -n "$selected_unit" ]] || continue
      if ! unit_matches_path "$selected_unit" "$rel_path"; then
        continue
      fi
      if unit_requires_package_mode "$selected_unit"; then
        return 0
      fi
      if ! path_is_deployable_input "$rel_path"; then
        return 0
      fi
    done < <(list_selected_units_for_target "$target")
  done
  return 1
}

child_mode_for_target() {
  local target="$1"
  if [[ "$REQUESTED_MODE" == "auto" ]]; then
    if target_needs_package_mode "$target"; then
      printf '%s\n' package
    else
      printf '%s\n' auto
    fi
    return 0
  fi
  printf '%s\n' "$FINAL_MODE"
}

file_source_for_child_mode() {
  if [[ "$1" == "package" ]]; then
    printf '%s\n' touched
  else
    printf '%s\n' deployable
  fi
}

stack_health_failure_tolerated() {
  local output="$1"
  [[ "${STRICT_GATEWAY_HEALTH:-0}" != "1" ]] || return 1
  printf '%s' "$output" | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(1)

failures = [str(item) for item in payload.get("failures") or []]
allowed = {"runtime:gateway_unhealthy"}
if failures and set(failures).issubset(allowed):
    sys.exit(0)
sys.exit(1)
'
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
    if stack_health_failure_tolerated "$output"; then
      deploy_warn "Full stack health has only tolerated Gateway failure; deployment completed without restarting Gateway."
      printf '%s\n' "$output" >&2
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
  run_backtest --status-only
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
  pb_mode="$(child_mode_for_target pocketbase)"
  console_mode="$(child_mode_for_target ibkr_console)"
  compute_mode="$(child_mode_for_target ibkr_compute)"
  backtest_mode="$(child_mode_for_target ibkr_backtest)"
  runtime_mode="$(child_mode_for_target ibkr_runtime)"
  pb_file_source="$(file_source_for_child_mode "$pb_mode")"
  console_file_source="$(file_source_for_child_mode "$console_mode")"
  compute_file_source="$(file_source_for_child_mode "$compute_mode")"
  backtest_file_source="$(file_source_for_child_mode "$backtest_mode")"
  runtime_file_source="$(file_source_for_child_mode "$runtime_mode")"

  target_pb_file_args=()
  target_console_file_args=()
  target_compute_file_args=()
  target_backtest_file_args=()
  target_runtime_file_args=()
  collect_target_file_args target_pb_file_args pocketbase "$pb_file_source"
  collect_target_file_args target_console_file_args ibkr_console "$console_file_source"
  collect_target_file_args target_compute_file_args ibkr_compute "$compute_file_source"
  collect_target_file_args target_backtest_file_args ibkr_backtest "$backtest_file_source"
  collect_target_file_args target_runtime_file_args ibkr_runtime "$runtime_file_source"

  pb_args+=(--mode "$pb_mode")
  console_args+=(--mode "$console_mode")
  compute_args+=(--mode "$compute_mode")
  backtest_args+=(--mode "$backtest_mode")
  runtime_args+=(--mode "$runtime_mode")
  if [[ ${#target_pb_file_args[@]} -gt 0 ]]; then
    pb_args+=( "${target_pb_file_args[@]}" )
  fi
  if [[ ${#target_console_file_args[@]} -gt 0 ]]; then
    console_args+=( "${target_console_file_args[@]}" )
  fi
  if [[ ${#target_compute_file_args[@]} -gt 0 ]]; then
    compute_args+=( "${target_compute_file_args[@]}" )
  fi
  if [[ ${#target_backtest_file_args[@]} -gt 0 ]]; then
    backtest_args+=( "${target_backtest_file_args[@]}" )
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
  if [[ "$WITH_POCKETBASE" -eq 1 ]]; then
    run_pb
  else
    deploy_log "Skipping PocketBase deploy by default. Use --pocketbase only for explicit PocketBase changes."
  fi
  run_backtest
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
  if [[ "$WITH_POCKETBASE" -ne 1 ]]; then
    deploy_die "PocketBase changes detected but PocketBase deploy is protected. Re-run with --pocketbase after confirming PocketBase maintenance is acceptable."
  fi
  DEPLOY_IGNORE_UNMANAGED=1 run_pb
fi
if array_contains "ibkr_backtest" "${families[@]}"; then
  DEPLOY_IGNORE_UNMANAGED=1 run_backtest
fi
if array_contains "ibkr_console" "${families[@]}"; then
  DEPLOY_IGNORE_UNMANAGED=1 run_console
fi
if [[ "$PUBLIC_PROXY_NEEDS_DEPLOY" -eq 1 ]]; then
  DEPLOY_IGNORE_UNMANAGED=1 run_public_proxy
fi

wait_for_full_stack_health
