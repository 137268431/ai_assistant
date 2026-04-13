#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.136}"
PB_REMOTE_ROOT="${PB_REMOTE_ROOT:-${IBKR_DEPLOY_PB_ROOT:-/opt/pocketbase}}"
RUNTIME_ROOT="$AI_ASSISTANT_ROOT/runtime/pocketbase"
EXTENSIONS_ROOT="$AI_ASSISTANT_ROOT/extensions/pocketbase"

DEPLOY_PUBLIC=1
DEPLOY_HOOKS=1
DEPLOY_MIGRATIONS=0
DRY_RUN=0
RESTART_SERVICE=1
SKIP_CHECKS=0
STATUS_ONLY=0

usage() {
  cat <<EOF
Usage: deploy_pocketbase_runtime.sh [options]

Options:
  --host <host>       Override SSH target
  --public-only       Deploy only runtime/pocketbase/pb_public
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

check_js_tree() {
  local remote_dir="$1"
  [[ "$SKIP_CHECKS" -eq 1 ]] && return 0
  ssh_run "if command -v node >/dev/null 2>&1; then find '$remote_dir' -type f -name '*.js' -print0 | xargs -0 -r node --check; fi"
}

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  ssh_run "systemctl is-active pocketbase"
  exit 0
fi

if [[ "$DEPLOY_PUBLIC" -eq 0 && "$DEPLOY_HOOKS" -eq 0 && "$DEPLOY_MIGRATIONS" -eq 0 ]]; then
  echo "Nothing selected for deployment." >&2
  usage >&2
  exit 1
fi

if [[ "$DEPLOY_PUBLIC" -eq 1 ]]; then
  sync_dir "$RUNTIME_ROOT/pb_public" "$PB_REMOTE_ROOT/pb_public"
fi

if [[ "$DEPLOY_HOOKS" -eq 1 ]]; then
  sync_dir "$RUNTIME_ROOT/pb_hooks" "$PB_REMOTE_ROOT/pb_hooks"
fi

if [[ "$DEPLOY_MIGRATIONS" -eq 1 ]]; then
  sync_dir "$EXTENSIONS_ROOT/migrations" "$PB_REMOTE_ROOT/extensions/migrations"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run complete."
  exit 0
fi

if [[ "$DEPLOY_PUBLIC" -eq 1 ]]; then
  check_js_tree "$PB_REMOTE_ROOT/pb_public"
fi

if [[ "$DEPLOY_HOOKS" -eq 1 ]]; then
  check_js_tree "$PB_REMOTE_ROOT/pb_hooks"
fi

if [[ "$DEPLOY_MIGRATIONS" -eq 1 ]]; then
  check_js_tree "$PB_REMOTE_ROOT/extensions/migrations"
fi

if [[ "$RESTART_SERVICE" -eq 1 ]]; then
  ssh_run "systemctl restart pocketbase"
fi

ssh_run "systemctl is-active pocketbase"
