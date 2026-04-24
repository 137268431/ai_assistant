#!/usr/bin/env bash
set -euo pipefail

SOURCE_HOST="${IBKR_LEGACY_HOST:-root@206.119.171.136}"
TARGET_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.246}"
STATUS_ONLY=0

usage() {
  cat <<EOF
Usage: seed_split_stack_env_remote.sh [options]

Seed split-stack `.env` files on the target host from the legacy host.
This copies service env files without printing secret values.

Options:
  --source-host <host>  Override the legacy/source SSH target
  --target-host <host>  Override the new/target SSH target
  --status-only         Show whether source/target env files exist and exit
  -h, --help            Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-host)
      SOURCE_HOST="${2:?missing source host}"
      shift 2
      ;;
    --target-host)
      TARGET_HOST="${2:?missing target host}"
      shift 2
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
  while IFS='|' read -r source_path target_path; do
    [[ -n "$source_path" ]] || continue
    printf 'source %s -> ' "$source_path"
    ssh -n "$SOURCE_HOST" "test -f '$source_path' && printf present || printf missing"
    printf '\n'
    printf 'target %s -> ' "$target_path"
    ssh -n "$TARGET_HOST" "test -f '$target_path' && printf present || printf missing"
    printf '\n---\n'
  done <<'EOF'
/opt/ibkr_runtime/.env|/opt/ibkr_runtime/.env
/opt/ibkr_compute/.env|/opt/ibkr_compute/.env
/opt/ibkr_api/.env|/opt/ibkr_api/.env
/opt/ibkr_scheduler/.env|/opt/ibkr_scheduler/.env
/opt/ibkr_console/.env|/opt/ibkr_console/.env
EOF
  exit 0
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

copy_one() {
  local source_path="$1"
  local target_path="$2"
  local fallback_source_path="${3:-}"
  local tmp_file="$tmp_dir/$(basename "$(dirname "$target_path")").env"

  if ssh "$SOURCE_HOST" "test -f '$source_path'"; then
    ssh "$SOURCE_HOST" "cat '$source_path'" > "$tmp_file"
  elif [[ -n "$fallback_source_path" ]] && ssh "$SOURCE_HOST" "test -f '$fallback_source_path'"; then
    ssh "$SOURCE_HOST" "cat '$fallback_source_path'" > "$tmp_file"
  else
    return 0
  fi

  chmod 600 "$tmp_file"
  ssh "$TARGET_HOST" "mkdir -p '$(dirname "$target_path")'"
  scp "$tmp_file" "$TARGET_HOST:$target_path" >/dev/null
  ssh "$TARGET_HOST" "chmod 600 '$target_path'"
}

copy_one "/opt/ibkr_runtime/.env" "/opt/ibkr_runtime/.env" "/opt/ibkr_compute/.env"
copy_one "/opt/ibkr_compute/.env" "/opt/ibkr_compute/.env"
copy_one "/opt/ibkr_api/.env" "/opt/ibkr_api/.env" "/opt/ibkr_compute/.env"
copy_one "/opt/ibkr_scheduler/.env" "/opt/ibkr_scheduler/.env" "/opt/ibkr_compute/.env"
copy_one "/opt/ibkr_console/.env" "/opt/ibkr_console/.env"
