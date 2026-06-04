#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REMOTE_HOST="${MONITORING_DEPLOY_HOST:-${IBKR_DEPLOY_HOST:-root@206.119.171.246}}"
SKIP_INSTALL=0
SKIP_DEPLOY=0
SKIP_HEALTH=0
STATUS_ONLY=0
PLAN_ONLY=0
DRY_RUN=0
NO_RESTART=0
SKIP_CADDY=0
SKIP_PUBLIC_CHECK=0

usage() {
  cat <<EOF_USAGE
Usage: bootstrap_monitoring_stack_remote.sh [options]

Bootstraps the monitoring-only stack. It installs monitoring binaries/packages,
deploys monitoring config/systemd/Caddy files, and runs the monitoring health
check. It does not restart or modify IBKR Gateway, PocketBase, or app services.

Options:
  --host <host>        Override SSH target
  --skip-install       Skip monitoring runtime install
  --skip-deploy        Skip monitoring config deploy
  --skip-health        Skip final monitoring health check
  --skip-caddy         Do not deploy/reload the public Grafana Caddy site
  --skip-public-check  Do not require the public Grafana URL to return Basic Auth
  --dry-run            Show rsync changes without mutating the remote host
  --plan-only          Print deploy plan only; also skips install and health
  --no-restart         Deploy files but do not restart monitoring services or reload Caddy
  --status-only        Print monitoring install/deploy/health status and exit
  -h, --help           Show this help
EOF_USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --skip-install)
      SKIP_INSTALL=1
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
    --skip-caddy)
      SKIP_CADDY=1
      SKIP_PUBLIC_CHECK=1
      shift
      ;;
    --skip-public-check)
      SKIP_PUBLIC_CHECK=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      SKIP_INSTALL=1
      SKIP_HEALTH=1
      shift
      ;;
    --plan-only)
      PLAN_ONLY=1
      SKIP_INSTALL=1
      SKIP_HEALTH=1
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

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  bash "$AI_ASSISTANT_ROOT/ops/bootstrap/install_monitoring_runtime_remote.sh" --host "$REMOTE_HOST" --status-only
  printf '%s\n' '====='
  bash "$AI_ASSISTANT_ROOT/ops/deploy/deploy_monitoring_stack.sh" --host "$REMOTE_HOST" --status-only
  printf '%s\n' '====='
  health_args=(--host "$REMOTE_HOST")
  [[ "$SKIP_PUBLIC_CHECK" -eq 1 ]] && health_args+=(--skip-public)
  [[ "$SKIP_CADDY" -eq 1 ]] && health_args+=(--skip-caddy)
  python3 "$AI_ASSISTANT_ROOT/ops/monitoring/health/check_monitoring_stack.py" "${health_args[@]}" || true
  exit 0
fi

if [[ "$SKIP_INSTALL" -ne 1 ]]; then
  bash "$AI_ASSISTANT_ROOT/ops/bootstrap/install_monitoring_runtime_remote.sh" --host "$REMOTE_HOST"
fi

if [[ "$SKIP_DEPLOY" -ne 1 ]]; then
  deploy_args=(--host "$REMOTE_HOST")
  [[ "$DRY_RUN" -eq 1 ]] && deploy_args+=(--dry-run)
  [[ "$PLAN_ONLY" -eq 1 ]] && deploy_args+=(--plan-only)
  [[ "$NO_RESTART" -eq 1 ]] && deploy_args+=(--no-restart)
  [[ "$SKIP_CADDY" -eq 1 ]] && deploy_args+=(--skip-caddy)
  [[ "$SKIP_PUBLIC_CHECK" -eq 1 ]] && deploy_args+=(--skip-public-check)
  bash "$AI_ASSISTANT_ROOT/ops/deploy/deploy_monitoring_stack.sh" "${deploy_args[@]}"
fi

if [[ "$SKIP_HEALTH" -ne 1 && "$DRY_RUN" -ne 1 && "$PLAN_ONLY" -ne 1 ]]; then
  health_args=(--host "$REMOTE_HOST")
  [[ "$SKIP_PUBLIC_CHECK" -eq 1 ]] && health_args+=(--skip-public)
  [[ "$SKIP_CADDY" -eq 1 ]] && health_args+=(--skip-caddy)
  python3 "$AI_ASSISTANT_ROOT/ops/monitoring/health/check_monitoring_stack.py" "${health_args[@]}"
fi
