#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WORKSPACE="${PLAYWRIGHT_WORKSPACE:-}"
SMOKE_SCRIPT="${PB_PLAYWRIGHT_SCRIPT:-pb_smoke.js}"
INSTALL_MODE="auto"
DEFAULT_WORKSPACE="$AI_ASSISTANT_ROOT/ops/ui/playwright"
NPM_CACHE_DIR="${NPM_CONFIG_CACHE:-${npm_config_cache:-${TMPDIR:-/tmp}/ai_assistant_npm_cache}}"

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
  if [[ -n "$WORKSPACE" ]]; then
    if [[ -f "$WORKSPACE/$SMOKE_SCRIPT" ]]; then
      printf '%s\n' "$WORKSPACE"
      return 0
    fi
    echo "Playwright workspace missing script: $WORKSPACE/$SMOKE_SCRIPT" >&2
    return 1
  fi

  if [[ -f "$DEFAULT_WORKSPACE/$SMOKE_SCRIPT" ]]; then
    printf '%s\n' "$DEFAULT_WORKSPACE"
    return 0
  fi

  echo "Unable to locate the repo Playwright workspace: $DEFAULT_WORKSPACE" >&2
  echo "Set PLAYWRIGHT_WORKSPACE or pass --workspace to override." >&2
  return 1
}

WORKSPACE="$(resolve_workspace)"

if [[ "$INSTALL_MODE" == "always" ]] || [[ "$INSTALL_MODE" == "auto" && ! -d "$WORKSPACE/node_modules" ]]; then
  mkdir -p "$NPM_CACHE_DIR"
  (cd "$WORKSPACE" && npm_config_cache="$NPM_CACHE_DIR" npm install)
fi

cd "$WORKSPACE"
if [[ ${#passthrough[@]} -gt 0 ]]; then
  exec node "$SMOKE_SCRIPT" "${passthrough[@]}"
fi

exec node "$SMOKE_SCRIPT"
