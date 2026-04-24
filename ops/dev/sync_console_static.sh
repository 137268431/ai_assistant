#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SOURCE_DIR="$AI_ASSISTANT_ROOT/runtime/ibkr_console/static"
PB_PUBLIC_DIR="$AI_ASSISTANT_ROOT/runtime/pocketbase/pb_public"

if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "missing console source dir: $SOURCE_DIR" >&2
  exit 1
fi

if [[ ! -d "$PB_PUBLIC_DIR" ]]; then
  echo "missing pocketbase public dir: $PB_PUBLIC_DIR" >&2
  exit 1
fi

echo "ibkr-console and PocketBase public trees are intentionally decoupled."
echo "- console source: $SOURCE_DIR"
echo "- pocketbase landing: $PB_PUBLIC_DIR"
echo "No sync performed. PocketBase keeps only its landing/admin tree."
echo "The old pb_public/common.js and pb_public/assets/** bundle should stay absent."
echo "Use deploy_ibkr_console.sh for console assets and deploy_pocketbase_runtime.sh --public-only for PB landing updates."
