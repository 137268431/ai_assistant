#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SOURCE_DIR="$AI_ASSISTANT_ROOT/runtime/ibkr_console/static"
LEGACY_DIR="$AI_ASSISTANT_ROOT/runtime/pocketbase/pb_public"

if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "missing source dir: $SOURCE_DIR" >&2
  exit 1
fi

mkdir -p "$LEGACY_DIR"
rsync -a --delete \
  --exclude '.DS_Store' \
  --exclude 'Thumbs.db' \
  "$SOURCE_DIR/" "$LEGACY_DIR/"

echo "synced: $SOURCE_DIR -> $LEGACY_DIR"
