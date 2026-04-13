#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.136}"
IBKR_REMOTE_ROOT="${IBKR_REMOTE_ROOT:-${IBKR_DEPLOY_IBKR_ROOT:-/opt/ibkr_compute}}"
RUNTIME_ROOT="$AI_ASSISTANT_ROOT/runtime/ibkr_compute"
OPS_ROOT="$AI_ASSISTANT_ROOT/ops/ibkr_compute"
GATEWAY_ROOT="$AI_ASSISTANT_ROOT/runtime/ib_gateway"
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

usage() {
  cat <<EOF
Usage: deploy_ibkr_compute_runtime.sh [options]

Options:
  --host <host>         Override SSH target
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

ssh_run() {
  ssh "$REMOTE_HOST" "$@"
}

rsync_common=(
  rsync
  -az
  --human-readable
  --exclude
  .DS_Store
  --exclude
  __pycache__/
  --exclude
  '*.pyc'
  --exclude
  '*.bak.*'
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  rsync_common+=(--dry-run --itemize-changes)
fi

sync_dir() {
  local local_dir="$1"
  local remote_dir="$2"
  [[ -d "$local_dir" ]] || { echo "Missing directory: $local_dir" >&2; exit 1; }
  ssh_run "mkdir -p '$remote_dir'"
  "${rsync_common[@]}" --delete "$local_dir"/ "$REMOTE_HOST:$remote_dir/"
}

sync_file() {
  local local_file="$1"
  local remote_file="$2"
  [[ -f "$local_file" ]] || { echo "Missing file: $local_file" >&2; exit 1; }
  ssh_run "mkdir -p '$(dirname "$remote_file")'"
  "${rsync_common[@]}" "$local_file" "$REMOTE_HOST:$remote_file"
}

ensure_remote_venv() {
  ssh_run "
    set -e
    mkdir -p '$IBKR_REMOTE_ROOT'
    if [ ! -x '$VENV_DIR/bin/python' ]; then
      if ! python3 -m venv '$VENV_DIR' >/dev/null 2>&1; then
        if command -v apt-get >/dev/null 2>&1; then
          apt-get update >/dev/null
          apt-get install -y python3-venv >/dev/null
          python3 -m venv '$VENV_DIR'
        else
          echo 'python3 -m venv failed and apt-get is unavailable' >&2
          exit 1
        fi
      fi
    fi
    '$VENV_DIR/bin/python' -m ensurepip --upgrade >/dev/null 2>&1 || true
    '$VENV_DIR/bin/python' -m pip --version >/dev/null
  "
}

install_requirements() {
  ssh_run "cd '$IBKR_REMOTE_ROOT' && '$VENV_DIR/bin/python' -m pip install --disable-pip-version-check -r requirements.txt -q"
}

check_python_tree() {
  local remote_dir="$1"
  [[ "$SKIP_CHECKS" -eq 1 ]] && return 0
  ssh_run "find '$remote_dir' -type f -name '*.py' -print0 | xargs -0 -r python3 -m py_compile"
}

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  ssh_run "systemctl is-active ibkr-compute ibkr-gateway"
  exit 0
fi

sync_dir "$RUNTIME_ROOT/src" "$IBKR_REMOTE_ROOT/src"
sync_file "$RUNTIME_ROOT/requirements.txt" "$IBKR_REMOTE_ROOT/requirements.txt"

if [[ "$DEPLOY_OPS_TOOLS" -eq 1 ]]; then
  sync_dir "$OPS_ROOT/auth" "$OPS_REMOTE_ROOT/auth"
  sync_dir "$OPS_ROOT/monitor" "$OPS_REMOTE_ROOT/monitor"
fi

if [[ "$SKIP_SYSTEMD" -eq 0 ]]; then
  sync_file "$RUNTIME_ROOT/systemd/ibkr-compute.service" "$SYSTEMD_DIR/ibkr-compute.service"
  if [[ "$DEPLOY_GATEWAY_SERVICE" -eq 1 ]]; then
    sync_file "$GATEWAY_ROOT/systemd/ibkr-gateway.service" "$SYSTEMD_DIR/ibkr-gateway.service"
  fi
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run complete."
  exit 0
fi

check_python_tree "$IBKR_REMOTE_ROOT/src"
if [[ "$DEPLOY_OPS_TOOLS" -eq 1 ]]; then
  check_python_tree "$OPS_REMOTE_ROOT"
fi

if [[ "$SKIP_REQUIREMENTS" -eq 0 ]]; then
  ensure_remote_venv
  install_requirements
fi

if [[ "$SKIP_SYSTEMD" -eq 0 ]]; then
  ssh_run "systemctl daemon-reload"
  ssh_run "systemctl enable ibkr-compute >/dev/null 2>&1 || true"
  if [[ "$DEPLOY_GATEWAY_SERVICE" -eq 1 ]]; then
    ssh_run "systemctl enable ibkr-gateway >/dev/null 2>&1 || true"
  fi
fi

if [[ "$RESTART_SERVICE" -eq 1 ]]; then
  ssh_run "systemctl restart ibkr-compute"
  if [[ "$DEPLOY_GATEWAY_SERVICE" -eq 1 ]]; then
    ssh_run "systemctl restart ibkr-gateway"
  fi
fi

if [[ "$DEPLOY_GATEWAY_SERVICE" -eq 1 ]]; then
  ssh_run "systemctl is-active ibkr-compute ibkr-gateway"
else
  ssh_run "systemctl is-active ibkr-compute"
fi
