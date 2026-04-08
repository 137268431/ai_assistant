#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT_DEFAULT="$(cd "$SCRIPT_DIR/../.." && pwd)"
AI_ASSISTANT_ROOT="${AI_ASSISTANT_ROOT:-${IBKR_DEPLOY_LOCAL_ROOT:-$AI_ASSISTANT_ROOT_DEFAULT}}"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.136}"
IBKR_REMOTE_ROOT="${IBKR_REMOTE_ROOT:-${IBKR_DEPLOY_IBKR_ROOT:-/opt/ibkr_compute}}"
PB_REMOTE_ROOT="${PB_REMOTE_ROOT:-${IBKR_DEPLOY_PB_ROOT:-/opt/pocketbase}}"
RESTART_MODE="auto"
DRY_RUN=0
SKIP_CHECKS=0
STATUS_ONLY=0

usage() {
  cat <<USAGE
Usage: deploy_ibkr_pb.sh [options] [file ...]

Options:
  --restart <auto|none|ibkr-compute|pocketbase|both|all>
  --dry-run
  --skip-checks
  --status-only
  -h, --help
USAGE
}

realpath_py() {
  python3 - "$1" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
}

normalize_local_path() {
  local raw="$1"
  if [[ "$raw" == /* ]]; then
    realpath_py "$raw"
  elif [[ -e "$raw" ]]; then
    realpath_py "$raw"
  else
    realpath_py "$AI_ASSISTANT_ROOT/$raw"
  fi
}

declare -a restart_targets=()
declare -a files=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart)
      RESTART_MODE="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --skip-checks)
      SKIP_CHECKS=1
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
      files+=("$1")
      shift
      ;;
  esac
done

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  ssh "$REMOTE_HOST" 'systemctl is-active ibkr-compute pocketbase'
  exit 0
fi

file_count=0
if [[ -n "${files[*]-}" ]]; then
  file_count="${#files[@]}"
fi

if [[ "$file_count" -eq 0 && "$RESTART_MODE" == "auto" ]]; then
  echo "No files supplied and restart mode is auto." >&2
  usage >&2
  exit 1
fi

declare -a scp_pairs=()
declare -a remote_py=()
declare -a remote_js=()
need_ibkr=0
need_pb=0

if [[ "$file_count" -gt 0 ]]; then
  for raw in "${files[@]}"; do
    local_path="$(normalize_local_path "$raw")"
    if [[ ! -f "$local_path" ]]; then
      echo "Missing file: $raw -> $local_path" >&2
      exit 1
    fi

    case "$local_path" in
      "$AI_ASSISTANT_ROOT"/ibkr_compute/*)
        rel_path="${local_path#"$AI_ASSISTANT_ROOT"/ibkr_compute/}"
        remote_path="$IBKR_REMOTE_ROOT/$rel_path"
        need_ibkr=1
        [[ "$remote_path" == *.py ]] && remote_py+=("$remote_path")
        ;;
      "$AI_ASSISTANT_ROOT"/pocketbase/*)
        rel_path="${local_path#"$AI_ASSISTANT_ROOT"/pocketbase/}"
        remote_path="$PB_REMOTE_ROOT/$rel_path"
        need_pb=1
        [[ "$remote_path" == *.js ]] && remote_js+=("$remote_path")
        ;;
      *)
        echo "Unsupported path outside mapped roots: $local_path" >&2
        echo "Expected files under: $AI_ASSISTANT_ROOT/ibkr_compute or $AI_ASSISTANT_ROOT/pocketbase" >&2
        exit 1
        ;;
    esac

    scp_pairs+=("$local_path::$remote_path")
  done
fi

case "$RESTART_MODE" in
  auto)
    ;;
  none)
    need_ibkr=0
    need_pb=0
    ;;
  ibkr-compute)
    need_ibkr=1
    need_pb=0
    ;;
  pocketbase)
    need_ibkr=0
    need_pb=1
    ;;
  both|all)
    need_ibkr=1
    need_pb=1
    ;;
  *)
    echo "Unsupported restart mode: $RESTART_MODE" >&2
    exit 1
    ;;
esac

if [[ -n "${scp_pairs[*]-}" ]]; then
  for pair in "${scp_pairs[@]}"; do
    local_path="${pair%%::*}"
    remote_path="${pair##*::}"
    echo "Deploy: $local_path -> $remote_path"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      continue
    fi
    ssh "$REMOTE_HOST" "mkdir -p '$(dirname "$remote_path")'"
    scp "$local_path" "$REMOTE_HOST:$remote_path"
  done
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry run complete."
  exit 0
fi

if [[ "$SKIP_CHECKS" -eq 0 ]]; then
  if [[ -n "${remote_py[*]-}" ]]; then
    py_cmd="python3 -m py_compile"
    for path in "${remote_py[@]}"; do
      py_cmd+=" '$path'"
    done
    ssh "$REMOTE_HOST" "$py_cmd"
  fi

  if [[ -n "${remote_js[*]-}" ]]; then
    js_cmd="if command -v node >/dev/null 2>&1; then"
    for path in "${remote_js[@]}"; do
      js_cmd+=" node --check '$path' &&"
    done
    js_cmd+=" true; fi"
    ssh "$REMOTE_HOST" "$js_cmd"
  fi
fi

declare -a restart_cmds=()
[[ "$need_ibkr" -eq 1 ]] && restart_cmds+=("systemctl restart ibkr-compute")
[[ "$need_pb" -eq 1 ]] && restart_cmds+=("systemctl restart pocketbase")

if [[ -n "${restart_cmds[*]-}" ]]; then
  ssh "$REMOTE_HOST" "$(printf '%s && ' "${restart_cmds[@]}") sleep 2"
fi

declare -a status_query=()
[[ "$need_ibkr" -eq 1 || "$RESTART_MODE" == "both" || "$RESTART_MODE" == "all" || "$RESTART_MODE" == "ibkr-compute" ]] && status_query+=("ibkr-compute")
[[ "$need_pb" -eq 1 || "$RESTART_MODE" == "both" || "$RESTART_MODE" == "all" || "$RESTART_MODE" == "pocketbase" ]] && status_query+=("pocketbase")

if [[ -z "${status_query[*]-}" ]]; then
  status_query+=("ibkr-compute" "pocketbase")
fi

ssh "$REMOTE_HOST" "systemctl is-active ${status_query[*]}"
