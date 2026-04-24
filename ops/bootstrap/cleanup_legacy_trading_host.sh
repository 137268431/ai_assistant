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

if [[ "$purge_data" == "1" ]]; then
  rm -rf "${dirs[@]}"
fi

show_status
REMOTE
