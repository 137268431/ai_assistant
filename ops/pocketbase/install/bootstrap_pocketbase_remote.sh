#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.246}"
PB_REMOTE_ROOT="${PB_REMOTE_ROOT:-${IBKR_DEPLOY_PB_ROOT:-/opt/pocketbase}}"
SYSTEMD_PATH="${PB_SYSTEMD_PATH:-/etc/systemd/system/pocketbase.service}"
LOCAL_SYSTEMD_TEMPLATE="$AI_ASSISTANT_ROOT/runtime/pocketbase/systemd/pocketbase.service"
POCKETBASE_VERSION="${POCKETBASE_VERSION:-}"
POCKETBASE_ASSET_URL="${POCKETBASE_ASSET_URL:-}"
SKIP_START=0
STATUS_ONLY=0

usage() {
  cat <<EOF
Usage: bootstrap_pocketbase_remote.sh [options]

Options:
  --host <host>          Override SSH target
  --root <path>          Remote PocketBase root (default: /opt/pocketbase)
  --version <version>    Install a specific PocketBase version (default: latest)
  --asset-url <url>      Override the PocketBase linux_amd64 asset URL
  --skip-start           Install/update files but do not restart the service
  --status-only          Print PocketBase binary/service status and exit
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --root)
      PB_REMOTE_ROOT="${2:?missing root}"
      shift 2
      ;;
    --version)
      POCKETBASE_VERSION="${2:?missing version}"
      shift 2
      ;;
    --asset-url)
      POCKETBASE_ASSET_URL="${2:?missing url}"
      shift 2
      ;;
    --skip-start)
      SKIP_START=1
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
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

[[ -f "$LOCAL_SYSTEMD_TEMPLATE" ]] || {
  echo "Missing local PocketBase systemd template: $LOCAL_SYSTEMD_TEMPLATE" >&2
  exit 1
}

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  ssh "$REMOTE_HOST" bash -s -- "$PB_REMOTE_ROOT" <<'REMOTE'
set -euo pipefail
root="$1"
systemctl show pocketbase --property=Id,ActiveState,SubState,MainPID,UnitFileState --no-pager || true
printf '%s\n' '---'
if [[ -x "$root/pocketbase" ]]; then
  "$root/pocketbase" --version || true
else
  printf 'PocketBase binary missing: %s/pocketbase\n' "$root"
fi
printf '%s\n' '---'
ls -la "$root" || true
REMOTE
  exit 0
fi

rsync -az "$LOCAL_SYSTEMD_TEMPLATE" "$REMOTE_HOST:$SYSTEMD_PATH"

ssh "$REMOTE_HOST" \
  env \
    PB_ROOT="$PB_REMOTE_ROOT" \
    PB_SYSTEMD_PATH="$SYSTEMD_PATH" \
    PB_REQUESTED_VERSION="$POCKETBASE_VERSION" \
    PB_REQUESTED_ASSET_URL="$POCKETBASE_ASSET_URL" \
    PB_SKIP_START="$SKIP_START" \
  'bash -s' <<'REMOTE'
set -euo pipefail

pb_root="${PB_ROOT:?missing PB_ROOT}"
systemd_path="${PB_SYSTEMD_PATH:?missing PB_SYSTEMD_PATH}"
requested_version="${PB_REQUESTED_VERSION:-}"
requested_asset_url="${PB_REQUESTED_ASSET_URL:-}"
skip_start="${PB_SKIP_START:-0}"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
archive_path="$tmp_dir/pocketbase.zip"
timestamp="$(date +%Y%m%d%H%M%S)"

resolve_asset() {
  python3 - "$requested_version" "$requested_asset_url" <<'PY'
from __future__ import annotations

import json
import sys
import urllib.request

requested_version = sys.argv[1].strip()
requested_asset_url = sys.argv[2].strip()

if requested_asset_url:
    version = requested_version or "custom"
    print(version)
    print(requested_asset_url)
    raise SystemExit(0)

if requested_version:
    api_url = f"https://api.github.com/repos/pocketbase/pocketbase/releases/tags/v{requested_version}"
else:
    api_url = "https://api.github.com/repos/pocketbase/pocketbase/releases/latest"

with urllib.request.urlopen(api_url, timeout=20) as response:
    data = json.load(response)

version = str(data.get("tag_name") or "").lstrip("v")
for asset in data.get("assets", []):
    name = str(asset.get("name") or "")
    if name.endswith("_linux_amd64.zip"):
        print(version)
        print(asset["browser_download_url"])
        break
else:
    raise SystemExit("Unable to resolve PocketBase linux_amd64 asset")
PY
}

mapfile -t resolved < <(resolve_asset)
resolved_version="${resolved[0]}"
resolved_asset_url="${resolved[1]}"

mkdir -p \
  "$pb_root" \
  "$pb_root/pb_data" \
  "$pb_root/pb_public" \
  "$pb_root/pb_hooks" \
  "$pb_root/extensions/migrations"

curl -fsSL "$resolved_asset_url" -o "$archive_path"
unzip -qo "$archive_path" -d "$tmp_dir/extracted"

if [[ -f "$pb_root/pocketbase" ]]; then
  cp "$pb_root/pocketbase" "$pb_root/pocketbase.bak.$timestamp"
fi
install -m 0755 "$tmp_dir/extracted/pocketbase" "$pb_root/pocketbase"

systemctl daemon-reload
systemctl enable pocketbase >/dev/null 2>&1 || true

if [[ "$skip_start" != "1" ]]; then
  systemctl restart pocketbase
  python3 - <<'PY'
from __future__ import annotations

import json
import time
import urllib.request

url = "http://127.0.0.1:8090/api/health"
last_error = None
for _ in range(40):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            if response.status == 200 and payload.get("code") == 200:
                raise SystemExit(0)
            last_error = f"unexpected health payload: {payload!r}"
    except Exception as exc:
        last_error = str(exc)
    time.sleep(1)
raise SystemExit(last_error or "PocketBase health check failed")
PY
fi

printf 'pocketbase_service=%s\n' "$systemd_path"
printf 'pocketbase_version=%s\n' "$resolved_version"
printf 'pocketbase_asset_url=%s\n' "$resolved_asset_url"
"$pb_root/pocketbase" --version
REMOTE
