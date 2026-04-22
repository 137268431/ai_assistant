#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIB_ROOT="$SCRIPT_DIR/lib"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.136}"
PUBLIC_BASE_URL="${PB_BASE_URL:-https://pb.lzw-glory.top}"
CADDY_MAIN_PATH="${IBKR_CADDY_MAIN_PATH:-/etc/caddy/Caddyfile}"
CADDY_CONF_DIR="${IBKR_CADDY_CONF_DIR:-/etc/caddy/conf.d}"
CADDY_SITE_FILE="${IBKR_CADDY_SITE_FILE:-$CADDY_CONF_DIR/pb.lzw-glory.top.caddy}"
CADDY_SERVICE="${IBKR_CADDY_SERVICE:-caddy}"
LOCAL_TEMPLATE_PATH="$AI_ASSISTANT_ROOT/ops/templates/caddy/pb.lzw-glory.top.caddy"

DRY_RUN=0
PLAN_ONLY=0
STATUS_ONLY=0
SKIP_RELOAD=0
SKIP_CHECKS=0

source "$LIB_ROOT/common.sh"

usage() {
  cat <<EOF
Usage: deploy_ibkr_public_proxy.sh [options]

Options:
  --host <host>            Override SSH target
  --public-base-url <url>  Public base URL to verify after reload
  --dry-run                Show file sync changes without mutating the remote host
  --plan-only              Print the plan and exit
  --status-only            Show caddy status and current site file info
  --skip-reload            Validate config but skip caddy reload
  --skip-checks            Skip public HTTP verification after reload
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --public-base-url)
      PUBLIC_BASE_URL="${2:?missing url}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --plan-only)
      PLAN_ONLY=1
      shift
      ;;
    --status-only)
      STATUS_ONLY=1
      shift
      ;;
    --skip-reload)
      SKIP_RELOAD=1
      shift
      ;;
    --skip-checks)
      SKIP_CHECKS=1
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

if [[ "$SKIP_RELOAD" -eq 1 ]]; then
  SKIP_CHECKS=1
fi

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  ssh_run "
    set -e
    systemctl is-active '$CADDY_SERVICE'
    printf '%s\n' '---'
    if [ -f '$CADDY_SITE_FILE' ]; then
      ls -l '$CADDY_SITE_FILE'
      printf '%s\n' '---'
      sed -n '1,220p' '$CADDY_SITE_FILE'
    else
      printf '%s\n' 'missing:$CADDY_SITE_FILE'
    fi
  "
  exit 0
fi

[[ -f "$LOCAL_TEMPLATE_PATH" ]] || deploy_die "Missing local caddy template: $LOCAL_TEMPLATE_PATH"

deploy_log "Deployment plan"
deploy_log "  target: ibkr public proxy"
deploy_log "  host: $REMOTE_HOST"
deploy_log "  local template: ${LOCAL_TEMPLATE_PATH#$AI_ASSISTANT_ROOT/}"
deploy_log "  remote template: $CADDY_SITE_FILE"
deploy_log "  main caddy file: $CADDY_MAIN_PATH"
deploy_log "  public base url: $PUBLIC_BASE_URL"
deploy_log "  reload caddy: $([[ "$SKIP_RELOAD" -eq 1 ]] && printf 'no' || printf 'yes')"

if [[ "$PLAN_ONLY" -eq 1 ]]; then
  exit 0
fi

ensure_remote_dir "$CADDY_CONF_DIR"
sync_file_rsync "$LOCAL_TEMPLATE_PATH" "$CADDY_SITE_FILE"

if [[ "$DRY_RUN" -eq 1 ]]; then
  exit 0
fi

ssh "$REMOTE_HOST" \
  env \
    CADDY_MAIN_PATH="$CADDY_MAIN_PATH" \
    CADDY_SITE_FILE="$CADDY_SITE_FILE" \
    CADDY_SERVICE="$CADDY_SERVICE" \
    SKIP_RELOAD="$SKIP_RELOAD" \
  'bash -s' <<'REMOTE'
set -euo pipefail

backup_path="$(python3 - <<'PY'
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

main_path = Path(__import__("os").environ["CADDY_MAIN_PATH"])
site_file = __import__("os").environ["CADDY_SITE_FILE"]
marker = f"# pb.lzw-glory.top is managed by {site_file}"

original = main_path.read_text(encoding="utf-8")
updated = original

if "import /etc/caddy/conf.d/*.caddy" not in updated:
    if not updated.endswith("\n"):
        updated += "\n"
    updated += "\nimport /etc/caddy/conf.d/*.caddy\n"

match = re.search(r"(?m)^\s*pb\.lzw-glory\.top\s*\{", updated)
if match:
    start = match.start()
    depth = 0
    end = None
    for index in range(match.start(), len(updated)):
        char = updated[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise RuntimeError("failed to locate end of pb.lzw-glory.top caddy block")
    replacement = marker + "\n\n"
    updated = updated[:start] + replacement + updated[end:].lstrip("\n")
elif marker not in updated:
    if not updated.endswith("\n"):
        updated += "\n"
    updated += "\n" + marker + "\n"

if updated == original:
    print("__UNCHANGED__")
else:
    backup_path = main_path.with_name(
        f"{main_path.name}.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    backup_path.write_text(original, encoding="utf-8")
    main_path.write_text(updated, encoding="utf-8")
    print(str(backup_path))
PY
)"

if ! caddy validate --config "$CADDY_MAIN_PATH"; then
  if [[ -n "$backup_path" && "$backup_path" != "__UNCHANGED__" && -f "$backup_path" ]]; then
    cp "$backup_path" "$CADDY_MAIN_PATH"
  fi
  exit 1
fi

if [[ "${SKIP_RELOAD:-0}" -eq 0 ]]; then
  caddy reload --config "$CADDY_MAIN_PATH" --force
fi

printf 'backup:%s\n' "$backup_path"
REMOTE

if [[ "$SKIP_CHECKS" -eq 0 ]]; then
  PUBLIC_BASE_URL="$PUBLIC_BASE_URL" python3 - <<'PY'
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from os import environ

base = environ["PUBLIC_BASE_URL"].rstrip("/")
checks = [
    ("console_index", f"{base}/index.html?environment=live", False),
    ("pb_health", f"{base}/api/health", True),
    ("api_health", f"{base}/health", True),
    ("api_runtime_config", f"{base}/api/custom/ibkr/runtime/config?environment=live", True),
    ("api_summaryz", f"{base}/api/custom/system/summaryz?lite=1&environment=live", True),
]

for label, url, expect_json in checks:
    req = urllib.request.Request(url, headers={"User-Agent": "codex-ibkr-public-proxy-check"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status < 200 or resp.status >= 300:
                raise RuntimeError(f"{label} unexpected status {resp.status}")
            if expect_json:
                payload = json.loads(body)
                if label == "api_runtime_config" and payload.get("source") != "ibkr-api":
                    raise RuntimeError(f"{label} expected source=ibkr-api, got {payload.get('source')!r}")
                if label == "api_summaryz":
                    services = (((payload.get("service_topology") or {}).get("services")) or {})
                    for required in ("ibkr-api", "ibkr-console", "ibkr-scheduler", "pocketbase"):
                        if required not in services:
                            raise RuntimeError(f"{label} missing service topology entry: {required}")
            else:
                lower = body.lower()
                if "trading flight" not in lower and "ibkr console" not in lower:
                    raise RuntimeError(f"{label} missing console marker")
            print(f"ok:{label}:{url}")
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error:{label}:{url}:{exc}", file=sys.stderr)
        sys.exit(1)
PY
fi
