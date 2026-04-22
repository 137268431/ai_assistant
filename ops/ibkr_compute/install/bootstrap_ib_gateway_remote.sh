#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
exec bash "$AI_ASSISTANT_ROOT/ops/ib_gateway/install/bootstrap_ib_gateway_remote.sh" "$@"
