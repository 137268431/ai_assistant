#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.246}"
DRY_RUN=0

usage() {
  cat <<EOF
Usage: prune_remote_legacy.sh [options]

Options:
  --host <host>       Override SSH target
  --dry-run           Print removals without deleting
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
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

ssh "$REMOTE_HOST" "DRY_RUN=$DRY_RUN bash -s" <<'SH'
set -euo pipefail

run_rm() {
  local target="$1"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "REMOVE $target"
  else
    rm -rf -- "$target"
    echo "REMOVED $target"
  fi
}

remove_path() {
  local target="$1"
  if [[ -e "$target" || -L "$target" ]]; then
    run_rm "$target"
  fi
}

remove_root_pattern() {
  local root="$1"
  local pattern="$2"
  while IFS= read -r -d '' path; do
    run_rm "$path"
  done < <(find "$root" -maxdepth 1 -name "$pattern" -print0 2>/dev/null)
}

remove_recursive_files() {
  local root="$1"
  local pattern="$2"
  while IFS= read -r -d '' path; do
    run_rm "$path"
  done < <(find "$root" -type f -name "$pattern" -print0 2>/dev/null)
}

remove_recursive_dirs() {
  local root="$1"
  local pattern="$2"
  while IFS= read -r -d '' path; do
    run_rm "$path"
  done < <(find "$root" -type d -name "$pattern" -print0 2>/dev/null)
}

remove_crontab_pattern() {
  local pattern="$1"
  local current=""
  if ! current="$(crontab -l 2>/dev/null)"; then
    return 0
  fi
  if ! grep -Fq "$pattern" <<<"$current"; then
    return 0
  fi
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "REMOVE CRONTAB PATTERN $pattern"
    grep -F "$pattern" <<<"$current"
    return 0
  fi

  local next
  next="$(grep -Fv "$pattern" <<<"$current" || true)"
  printf '%s\n' "$next" | crontab -
  echo "REMOVED CRONTAB PATTERN $pattern"
}

remove_path /opt/pocketbase/data
remove_path /opt/pocketbase/deploy.sh
remove_path /opt/pocketbase/pb_hooks_backup
remove_path /opt/pocketbase/pb_public_backup
remove_path /opt/pocketbase/pb_table
remove_path /opt/pocketbase/table_json
remove_path /opt/pocketbase/scripts
remove_path /opt/pocketbase/test
remove_path /opt/pocketbase/pb_migrations
remove_path /opt/pocketbase/pocketbase_old
remove_path /opt/pocketbase/CHANGELOG.md
remove_path /opt/pocketbase/LICENSE.md
remove_root_pattern /opt/pocketbase 'pocketbase_*.zip'
remove_recursive_files /opt/pocketbase '*.bak.*'
remove_recursive_files /opt/pocketbase '._*'
remove_recursive_files /opt/pocketbase '*.pyc'
remove_recursive_dirs /opt/pocketbase '__pycache__'

remove_path /opt/ibkr_compute/deploy
remove_path /opt/ibkr_compute/tests
remove_path /opt/ibkr_compute/server.py
remove_path /opt/ibkr_compute/config.py
remove_path /opt/ibkr_compute/daily_scanner.py
remove_path /opt/ibkr_compute/indicator_engine.py
remove_path /opt/ibkr_compute/pb_client.py
remove_path /opt/ibkr_compute/signal_generator.py
remove_path /opt/ibkr_compute/ibkr_login.py
remove_path /opt/ibkr_compute/ibkr_weekly_reauth.py
remove_path /opt/ibkr_compute/ibkr_keepalive.py
remove_path /opt/ibkr_compute/deploy/ibkr_weekly_reauth.py
remove_path /opt/ibkr_compute/deploy/ibkr_keepalive.py
remove_path /opt/ibkr_compute/e2e_test.py
remove_path /opt/ibkr/clientportal.gw
remove_recursive_files /opt/ibkr_compute/src '*.bak.*'
remove_recursive_files /opt/ibkr_compute/src '._*'
remove_recursive_files /opt/ibkr_compute/src '*.pyc'
remove_recursive_dirs /opt/ibkr_compute/src '__pycache__'
remove_recursive_files /opt/ibkr_compute/ops '*.bak.*'
remove_recursive_files /opt/ibkr_compute/ops '._*'
remove_recursive_files /opt/ibkr_compute/ops '*.pyc'
remove_recursive_dirs /opt/ibkr_compute/ops '__pycache__'
remove_crontab_pattern /opt/ibkr_compute/deploy/ibkr_weekly_reauth.py
remove_crontab_pattern /opt/ibkr_compute/deploy/ibkr_keepalive.py
SH
