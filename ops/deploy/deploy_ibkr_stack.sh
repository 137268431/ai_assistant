#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Clearer alias for the historical deploy_runtime_all.sh entrypoint.
exec bash "$SCRIPT_DIR/deploy_runtime_all.sh" "$@"
