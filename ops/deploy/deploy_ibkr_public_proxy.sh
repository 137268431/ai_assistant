#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIB_ROOT="$SCRIPT_DIR/lib"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.246}"
QUANT_PUBLIC_BASE_URL="${IBKR_PUBLIC_BASE_URL:-${PUBLIC_BASE_URL:-https://quant.lzw-glory.top}}"
PB_PUBLIC_BASE_URL="${PB_BASE_URL:-https://pb.lzw-glory.top}"
CADDY_MAIN_PATH="${IBKR_CADDY_MAIN_PATH:-/etc/caddy/Caddyfile}"
CADDY_CONF_DIR="${IBKR_CADDY_CONF_DIR:-/etc/caddy/conf.d}"
LEGACY_CADDY_SITE_FILE="${IBKR_CADDY_SITE_FILE:-}"
QUANT_CADDY_SITE_FILE="${IBKR_QUANT_CADDY_SITE_FILE:-${LEGACY_CADDY_SITE_FILE:-$CADDY_CONF_DIR/quant.lzw-glory.top.caddy}}"
PB_CADDY_SITE_FILE="${IBKR_PB_CADDY_SITE_FILE:-$CADDY_CONF_DIR/pb.lzw-glory.top.caddy}"
CADDY_SERVICE="${IBKR_CADDY_SERVICE:-caddy}"
LOCAL_QUANT_TEMPLATE_PATH="$AI_ASSISTANT_ROOT/ops/templates/caddy/quant.lzw-glory.top.caddy"
LOCAL_PB_TEMPLATE_PATH="$AI_ASSISTANT_ROOT/ops/templates/caddy/pb.lzw-glory.top.caddy"

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
  --public-base-url <url>  Trading system public base URL to verify after reload
  --quant-base-url <url>   Alias for --public-base-url
  --pb-base-url <url>      PocketBase auth/data base URL to verify after reload
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
    --public-base-url|--quant-base-url)
      QUANT_PUBLIC_BASE_URL="${2:?missing url}"
      shift 2
      ;;
    --pb-base-url)
      PB_PUBLIC_BASE_URL="${2:?missing url}"
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
  ssh "$REMOTE_HOST" \
    env \
      CADDY_SERVICE="$CADDY_SERVICE" \
      QUANT_CADDY_SITE_FILE="$QUANT_CADDY_SITE_FILE" \
      PB_CADDY_SITE_FILE="$PB_CADDY_SITE_FILE" \
  'bash -s' <<'REMOTE'
set -euo pipefail
systemctl show "$CADDY_SERVICE" --property=Id,ActiveState,SubState,MainPID,UnitFileState --no-pager || true
printf '%s\n' '---'
for site_file in "$QUANT_CADDY_SITE_FILE" "$PB_CADDY_SITE_FILE"; do
  if [ -f "$site_file" ]; then
    ls -l "$site_file"
    printf '%s\n' '---'
    sed -n '1,220p' "$site_file"
  else
    printf 'missing:%s\n' "$site_file"
  fi
  printf '%s\n' '---'
done
REMOTE
  exit 0
fi

[[ -f "$LOCAL_QUANT_TEMPLATE_PATH" ]] || deploy_die "Missing local caddy template: $LOCAL_QUANT_TEMPLATE_PATH"
[[ -f "$LOCAL_PB_TEMPLATE_PATH" ]] || deploy_die "Missing local caddy template: $LOCAL_PB_TEMPLATE_PATH"

if [[ "$PLAN_ONLY" -eq 0 && "$DRY_RUN" -eq 0 ]]; then
  if ! ssh "$REMOTE_HOST" "command -v caddy >/dev/null 2>&1"; then
    deploy_die "Caddy is not installed on $REMOTE_HOST. Run bash ai_assistant/ops/bootstrap/install_base_runtime_remote.sh first."
  fi
fi

deploy_log "Deployment plan"
deploy_log "  target: ibkr public proxy"
deploy_log "  host: $REMOTE_HOST"
deploy_log "  local quant template: ${LOCAL_QUANT_TEMPLATE_PATH#$AI_ASSISTANT_ROOT/}"
deploy_log "  remote quant template: $QUANT_CADDY_SITE_FILE"
deploy_log "  local pocketbase template: ${LOCAL_PB_TEMPLATE_PATH#$AI_ASSISTANT_ROOT/}"
deploy_log "  remote pocketbase template: $PB_CADDY_SITE_FILE"
deploy_log "  main caddy file: $CADDY_MAIN_PATH"
deploy_log "  public base url: $QUANT_PUBLIC_BASE_URL"
deploy_log "  pocketbase base url: $PB_PUBLIC_BASE_URL"
deploy_log "  reload caddy: $([[ "$SKIP_RELOAD" -eq 1 ]] && printf 'no' || printf 'yes')"

if [[ "$PLAN_ONLY" -eq 1 ]]; then
  exit 0
fi

ensure_remote_dir "$CADDY_CONF_DIR"
sync_file_rsync "$LOCAL_QUANT_TEMPLATE_PATH" "$QUANT_CADDY_SITE_FILE"
sync_file_rsync "$LOCAL_PB_TEMPLATE_PATH" "$PB_CADDY_SITE_FILE"

if [[ "$DRY_RUN" -eq 1 ]]; then
  exit 0
fi

ssh "$REMOTE_HOST" \
  env \
    CADDY_MAIN_PATH="$CADDY_MAIN_PATH" \
    QUANT_CADDY_SITE_FILE="$QUANT_CADDY_SITE_FILE" \
    PB_CADDY_SITE_FILE="$PB_CADDY_SITE_FILE" \
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
site_files = {
    "quant.lzw-glory.top": __import__("os").environ["QUANT_CADDY_SITE_FILE"],
    "pb.lzw-glory.top": __import__("os").environ["PB_CADDY_SITE_FILE"],
}

original = main_path.read_text(encoding="utf-8")
updated = original

if "import /etc/caddy/conf.d/*.caddy" not in updated:
    if not updated.endswith("\n"):
        updated += "\n"
    updated += "\nimport /etc/caddy/conf.d/*.caddy\n"

def replace_inline_site_block(text: str, hostname: str, site_file: str) -> str:
    marker = f"# {hostname} is managed by {site_file}"
    text = re.sub(rf"(?m)^# {re.escape(hostname)} is managed by .*$\n?", "", text)
    match = re.search(rf"(?m)^\s*{re.escape(hostname)}\s*\{{", text)
    if match:
        start = match.start()
        depth = 0
        end = None
        for index in range(match.start(), len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            raise RuntimeError(f"failed to locate end of {hostname} caddy block")
        replacement = marker + "\n\n"
        return text[:start] + replacement + text[end:].lstrip("\n")

    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + marker + "\n"


for hostname, site_file in site_files.items():
    updated = replace_inline_site_block(updated, hostname, site_file)

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
  QUANT_PUBLIC_BASE_URL="$QUANT_PUBLIC_BASE_URL" \
  PB_PUBLIC_BASE_URL="$PB_PUBLIC_BASE_URL" \
  python3 - <<'PY'
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from os import environ

quant_base = environ["QUANT_PUBLIC_BASE_URL"].rstrip("/")
pb_base = environ["PB_PUBLIC_BASE_URL"].rstrip("/")
checks = [
    ("pb_root", f"{pb_base}/", False, True),
    ("quant_console_index", f"{quant_base}/index.html?environment=live", False),
    ("quant_api_health", f"{quant_base}/health", True),
    ("quant_api_runtime_config", f"{quant_base}/api/custom/ibkr/runtime/config?environment=live", True),
    ("quant_api_summaryz", f"{quant_base}/api/custom/system/summaryz?lite=1&environment=live", True),
    ("pb_health", f"{pb_base}/api/health", True),
]

for raw_check in checks:
    if len(raw_check) == 4:
        label, url, expect_json, expect_same_origin = raw_check
    else:
        label, url, expect_json = raw_check
        expect_same_origin = False
    req = urllib.request.Request(url, headers={"User-Agent": "codex-ibkr-public-proxy-check"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status < 200 or resp.status >= 300:
                raise RuntimeError(f"{label} unexpected status {resp.status}")
            final_url = getattr(resp, "geturl", lambda: url)()
            if expect_same_origin and not str(final_url or "").startswith(pb_base):
                raise RuntimeError(f"{label} redirected away from PocketBase origin: {final_url!r}")
            if expect_json:
                payload = json.loads(body)
                if label == "quant_api_runtime_config" and payload.get("source") != "ibkr-api":
                    raise RuntimeError(f"{label} expected source=ibkr-api, got {payload.get('source')!r}")
                if label == "quant_api_summaryz":
                    services = (((payload.get("service_topology") or {}).get("services")) or {})
                    for required in ("ibkr-api", "ibkr-console", "ibkr-scheduler", "pocketbase"):
                        if required not in services:
                            raise RuntimeError(f"{label} missing service topology entry: {required}")
            else:
                lower = body.lower()
                if label == "pb_root":
                    if "pocketbase" not in lower:
                        raise RuntimeError(f"{label} missing pocketbase marker")
                elif "trading flight" not in lower and "ibkr console" not in lower:
                    raise RuntimeError(f"{label} missing console marker")
            print(f"ok:{label}:{url}")
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error:{label}:{url}:{exc}", file=sys.stderr)
        sys.exit(1)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


redirect_req = urllib.request.Request(
    f"{pb_base}/ibkr_runtime.html?environment=live",
    headers={"User-Agent": "codex-ibkr-public-proxy-check"},
)
redirect_opener = urllib.request.build_opener(NoRedirect)
try:
    redirect_opener.open(redirect_req, timeout=20)
    raise RuntimeError("pb_legacy_console_path expected 404 response")
except urllib.error.HTTPError as exc:
    if exc.code != 404:
        raise RuntimeError(f"pb_legacy_console_path unexpected status {exc.code}") from exc
    print("ok:pb_legacy_console_path:404")
PY
fi
