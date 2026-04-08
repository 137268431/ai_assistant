#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$AI_ASSISTANT_ROOT/.." && pwd)"
WORKSPACE="${PLAYWRIGHT_WORKSPACE:-}"
SMOKE_SCRIPT="${PB_PLAYWRIGHT_SCRIPT:-pb_smoke.js}"
INSTALL_MODE="auto"

usage() {
  cat <<EOF
Usage: run_pb_playwright_smoke.sh [options] [-- <pb_smoke.js args...>]

Options:
  --workspace <path>  Override the Playwright workspace
  --script <name>     Script to run inside the workspace. Default: ${SMOKE_SCRIPT}
  --install           Force npm install before running
  --no-install        Skip npm install even if node_modules is missing
  -h, --help          Show this help
EOF
}

declare -a passthrough=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)
      WORKSPACE="${2:?missing workspace}"
      shift 2
      ;;
    --script)
      SMOKE_SCRIPT="${2:?missing script name}"
      shift 2
      ;;
    --install)
      INSTALL_MODE="always"
      shift
      ;;
    --no-install)
      INSTALL_MODE="never"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      passthrough+=("$@")
      break
      ;;
    *)
      passthrough+=("$1")
      shift
      ;;
  esac
done

resolve_workspace() {
  local candidate
  if [[ -n "$WORKSPACE" ]]; then
    if [[ -f "$WORKSPACE/$SMOKE_SCRIPT" ]]; then
      printf '%s\n' "$WORKSPACE"
      return 0
    fi
    echo "Playwright workspace missing script: $WORKSPACE/$SMOKE_SCRIPT" >&2
    return 1
  fi

  for candidate in "$REPO_ROOT/playwright_pb" "$REPO_ROOT/tmp_playwright"; do
    if [[ -f "$candidate/$SMOKE_SCRIPT" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "Unable to locate Playwright workspace. Set PLAYWRIGHT_WORKSPACE or pass --workspace." >&2
  return 1
}

WORKSPACE="$(resolve_workspace)"

if [[ "$INSTALL_MODE" == "always" ]] || [[ "$INSTALL_MODE" == "auto" && ! -d "$WORKSPACE/node_modules" ]]; then
  (cd "$WORKSPACE" && npm install)
fi

cd "$WORKSPACE"
exec node "$SMOKE_SCRIPT" "${passthrough[@]}"
