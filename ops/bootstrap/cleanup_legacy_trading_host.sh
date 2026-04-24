#!/usr/bin/env bash
set -euo pipefail

SOURCE_HOST="${IBKR_LEGACY_HOST:-root@206.119.171.136}"
STATUS_ONLY=0
PURGE_DATA=0

usage() {
  cat <<EOF
Usage: cleanup_legacy_trading_host.sh [options]

Stops and removes the legacy trading stack from the old host while leaving unrelated services untouched.
This script targets only the trading-system services and directories:
  pocketbase
  ibkr-gateway
  ibkr-display
  ibkr-api
  ibkr-compute
  ibkr-console
  ibkr-runtime
  ibkr-scheduler
  /opt/pocketbase
  /opt/ibkr_compute
  /opt/ibkr_runtime
  /opt/ibkr_api
  /opt/ibkr_scheduler
  /opt/ibkr_console
  /opt/ibc
  /opt/ibgateway
It also disables the legacy trading public-proxy site files on the old host so stale DNS clients
can no longer hit outdated `pb.lzw-glory.top` / `quant.lzw-glory.top` behavior there.

Options:
  --host <host>       Override the legacy SSH target
  --status-only       Print current legacy-host service/process/directory state and exit
  --purge-data        Remove the legacy trading directories after stopping/disabling services
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      SOURCE_HOST="${2:?missing host}"
      shift 2
      ;;
    --status-only)
      STATUS_ONLY=1
      shift
      ;;
    --purge-data)
      PURGE_DATA=1
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

ssh "$SOURCE_HOST" bash -s -- "$STATUS_ONLY" "$PURGE_DATA" <<'REMOTE'
set -euo pipefail

status_only="$1"
purge_data="$2"
services=(
  pocketbase
  ibkr-gateway
  ibkr-display
  ibkr-api
  ibkr-compute
  ibkr-console
  ibkr-runtime
  ibkr-scheduler
)
dirs=(
  /opt/pocketbase
  /opt/ibkr_compute
  /opt/ibkr_runtime
  /opt/ibkr_api
  /opt/ibkr_scheduler
  /opt/ibkr_console
  /opt/ibc
  /opt/ibgateway
)
caddy_site_files=(
  /etc/caddy/conf.d/pb.lzw-glory.top.caddy
  /etc/caddy/conf.d/quant.lzw-glory.top.caddy
  /etc/caddy/conf.d/qc.lzw-glory.top.caddy
)

show_status() {
  for service in "${services[@]}"; do
    systemctl show "$service" --property=Id,ActiveState,SubState,MainPID,UnitFileState --no-pager || true
    printf '%s\n' '---'
  done
  printf '%s\n' 'processes:'
  ps -ef | egrep '3xui|openclaw|crs|pocketbase|ibgateway|IBC|ibkr' | egrep -v 'egrep|grep' || true
  printf '%s\n' '---'
  printf '%s\n' 'docker:'
  docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null | egrep '3xui|openclaw|crs|ibkr|pocketbase|gateway' || true
  printf '%s\n' '---'
  printf '%s\n' 'directories:'
  ls -ld "${dirs[@]}" 2>/dev/null || true
  printf '%s\n' '---'
  printf '%s\n' 'legacy trading caddy site files:'
  ls -l "${caddy_site_files[@]}" 2>/dev/null || true
  printf '%s\n' '---'
  printf '%s\n' 'caddy service:'
  systemctl show caddy --property=Id,ActiveState,SubState,MainPID,UnitFileState --no-pager 2>/dev/null || true
}

disable_legacy_trading_proxy_sites() {
  command -v caddy >/dev/null 2>&1 || return 0
  [[ -d /etc/caddy/conf.d ]] || return 0

  local timestamp moved_file site_file
  local moved_files=()
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"

  for site_file in "${caddy_site_files[@]}"; do
    [[ -f "$site_file" ]] || continue
    moved_file="${site_file}.disabled.${timestamp}"
    mv "$site_file" "$moved_file"
    moved_files+=("$moved_file")
  done

  [[ ${#moved_files[@]} -gt 0 ]] || return 0

  if ! caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    for moved_file in "${moved_files[@]}"; do
      mv "$moved_file" "${moved_file%.disabled.${timestamp}}"
    done
    echo "failed to disable legacy trading caddy sites: caddy validate failed" >&2
    exit 1
  fi

  systemctl reload caddy >/dev/null 2>&1 || true
}

if [[ "$status_only" == "1" ]]; then
  show_status
  exit 0
fi

for service in "${services[@]}"; do
  systemctl stop "$service" >/dev/null 2>&1 || true
  systemctl disable "$service" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/${service}.service"
done
systemctl daemon-reload || true
disable_legacy_trading_proxy_sites

if [[ "$purge_data" == "1" ]]; then
  rm -rf "${dirs[@]}"
fi

show_status
REMOTE
