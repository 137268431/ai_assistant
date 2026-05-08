#!/usr/bin/env bash

service_wait_timeout_seconds() {
  case "$1" in
    pocketbase)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_POCKETBASE:-180}"
      ;;
    ibkr-runtime)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_IBKR_RUNTIME:-180}"
      ;;
    ibkr-compute)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_IBKR_COMPUTE:-180}"
      ;;
    ibkr-backtest)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_IBKR_BACKTEST:-180}"
      ;;
    ibkr-api)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_IBKR_API:-120}"
      ;;
    ibkr-scheduler)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_IBKR_SCHEDULER:-120}"
      ;;
    ibkr-console)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_IBKR_CONSOLE:-90}"
      ;;
    ibkr-display|ibkr-gateway)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_GATEWAY:-90}"
      ;;
    *)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_DEFAULT:-60}"
      ;;
  esac
}

service_healthcheck_url() {
  case "$1" in
    pocketbase)
      printf '%s\n' "${DEPLOY_WAIT_URL_POCKETBASE:-http://127.0.0.1:8090/api/health}"
      ;;
    ibkr-runtime)
      printf '%s\n' "${DEPLOY_WAIT_URL_IBKR_RUNTIME:-http://127.0.0.1:5101/health}"
      ;;
    ibkr-compute)
      printf '%s\n' "${DEPLOY_WAIT_URL_IBKR_COMPUTE:-http://127.0.0.1:5100/health}"
      ;;
    ibkr-backtest)
      printf '%s\n' "${DEPLOY_WAIT_URL_IBKR_BACKTEST:-http://127.0.0.1:5105/health}"
      ;;
    ibkr-api)
      printf '%s\n' "${DEPLOY_WAIT_URL_IBKR_API:-http://127.0.0.1:5102/health}"
      ;;
    ibkr-scheduler)
      printf '%s\n' "${DEPLOY_WAIT_URL_IBKR_SCHEDULER:-http://127.0.0.1:5103/health}"
      ;;
    ibkr-console)
      printf '%s\n' "${DEPLOY_WAIT_URL_IBKR_CONSOLE:-http://127.0.0.1:5104/index.html}"
      ;;
    *)
      printf '%s\n' ""
      ;;
  esac
}

wait_for_service_active() {
  local service="$1"
  local timeout_seconds="$2"
  ssh_run "
    set -e
    deadline=\$((\$(date +%s) + $timeout_seconds))
    while true; do
      state=\$(systemctl is-active '$service' 2>/dev/null || true)
      if [ \"\$state\" = 'active' ]; then
        exit 0
      fi
      if [ \$(date +%s) -ge \"\$deadline\" ]; then
        echo 'Timed out waiting for service to become active: $service' >&2
        systemctl status '$service' --no-pager -l >&2 || true
        exit 1
      fi
      sleep 2
    done
  "
}

wait_for_service_health_endpoint() {
  local service="$1"
  local url="$2"
  local timeout_seconds="$3"
  [[ -n "$url" ]] || return 0
  ssh "$REMOTE_HOST" python3 - "$service" "$url" "$timeout_seconds" <<'PY'
import sys
import time
import urllib.error
import urllib.request

service = sys.argv[1]
url = sys.argv[2]
timeout_seconds = int(sys.argv[3])
deadline = time.time() + timeout_seconds
last_error = ""

while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            status = int(getattr(response, "status", 0) or 0)
            if 200 <= status < 300:
                sys.exit(0)
            last_error = f"http_status={status}"
    except Exception as exc:
        last_error = str(exc)
    time.sleep(2)

print(f"Timed out waiting for health endpoint: {service} {url} error={last_error}", file=sys.stderr)
sys.exit(1)
PY
}

wait_for_service_ready() {
  local service="$1"
  local timeout_seconds
  local health_url
  timeout_seconds="$(service_wait_timeout_seconds "$service")"
  health_url="$(service_healthcheck_url "$service")"
  deploy_log "Waiting for $service to become active..."
  wait_for_service_active "$service" "$timeout_seconds"
  if [[ -n "$health_url" ]]; then
    deploy_log "Waiting for $service health endpoint..."
    wait_for_service_health_endpoint "$service" "$health_url" "$timeout_seconds"
  fi
}

collect_wait_groups_for_units() {
  local array_name="$1"
  shift || true
  eval "$array_name=()"
  local restart_groups=()
  if [[ "${RESTART_SERVICE:-1}" -eq 1 ]]; then
    collect_restart_groups restart_groups "$@"
    local restart
    for restart in "${restart_groups[@]-}"; do
      [[ -n "$restart" ]] || continue
      append_unique "$array_name" "$restart"
    done
  fi
  if [[ "${WAIT_FOR_AUTO_RELOAD:-0}" -eq 1 ]]; then
    append_unique "$array_name" "pocketbase"
  fi
  return 0
}

collect_enable_services() {
  local array_name="$1"
  shift || true
  eval "$array_name=()"
  local unit
  local service
  for unit in "$@"; do
    service="$(unit_enable_service "$unit")"
    [[ -n "$service" ]] && append_unique "$array_name" "$service"
  done
  return 0
}

collect_restart_groups() {
  local array_name="$1"
  shift || true
  eval "$array_name=()"
  local unit
  local service
  for unit in "$@"; do
    service="$(unit_restart_group "$unit")"
    [[ -n "$service" ]] && append_unique "$array_name" "$service"
  done
  if array_contains "ibkr_src" "$@"; then
    append_unique "$array_name" "ibkr-api"
    append_unique "$array_name" "ibkr-scheduler"
    # Runtime shares ibkr_compute on PYTHONPATH, but restarting it can disturb
    # IB Gateway/IBC and force a new 2FA. Make that restart an explicit opt-in.
    if [[ "${DEPLOY_RESTART_RUNTIME_FOR_IBKR_SRC:-0}" -eq 1 ]]; then
      append_unique "$array_name" "ibkr-runtime"
    fi
  fi
  if array_contains "ibkr_requirements" "$@"; then
    append_unique "$array_name" "ibkr-api"
    append_unique "$array_name" "ibkr-scheduler"
  fi
  if array_contains "ibkr_scheduler_src" "$@"; then
    append_unique "$array_name" "ibkr-api"
  fi
  return 0
}

units_need_pip_install() {
  local unit
  for unit in "$@"; do
    if unit_needs_pip_install "$unit"; then
      return 0
    fi
  done
  return 1
}

units_need_daemon_reload() {
  local unit
  for unit in "$@"; do
    if unit_needs_daemon_reload "$unit"; then
      return 0
    fi
  done
  return 1
}

collect_post_action_labels() {
  local array_name="$1"
  shift || true
  eval "$array_name=()"
  local enable_services=()
  local restart_groups=()
  if units_need_pip_install "$@" && [[ "${SKIP_REQUIREMENTS:-0}" -eq 0 ]]; then
    append_unique "$array_name" "pip install -r requirements.txt"
  fi
  if units_need_daemon_reload "$@" && [[ "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
    append_unique "$array_name" "systemctl daemon-reload"
  fi
  collect_enable_services enable_services "$@"
  if [[ "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
    local service
    for service in "${enable_services[@]-}"; do
      [[ -n "$service" ]] || continue
      append_unique "$array_name" "systemctl enable $service"
    done
  fi
  collect_restart_groups restart_groups "$@"
  if [[ "${RESTART_SERVICE:-1}" -eq 1 ]]; then
    local restart
    for restart in "${restart_groups[@]-}"; do
      [[ -n "$restart" ]] || continue
      append_unique "$array_name" "systemctl restart $restart"
    done
  else
    append_unique "$array_name" "service restart skipped"
  fi
  return 0
}

print_status_for_units() {
  local restart_groups=()
  local joined=""
  collect_restart_groups restart_groups "$@"
  joined="$(join_by ' ' "${restart_groups[@]-}")"
  [[ -n "$joined" ]] || return 0
  ssh_run "systemctl is-active $joined"
}

prepare_pocketbase_root_if_needed() {
  local units=( "$@" )
  local unit
  local needs_pocketbase_root=0
  local pocketbase_root="${PB_REMOTE_ROOT:-/opt/pocketbase}"

  for unit in "${units[@]-}"; do
    case "$unit" in
      pb_public|pb_hooks|pb_migrations|pb_systemd)
        needs_pocketbase_root=1
        break
        ;;
    esac
  done

  [[ "$needs_pocketbase_root" -eq 1 ]] || return 0

  ssh_run "
    set -e
    mkdir -p \
      '$pocketbase_root' \
      '$pocketbase_root/pb_data' \
      '$pocketbase_root/pb_public' \
      '$pocketbase_root/pb_hooks' \
      '$pocketbase_root/extensions/migrations'
  "
}

prepare_runtime_root_if_needed() {
  local units=( "$@" )
  local unit
  local needs_runtime_root=0
  local runtime_root="${IBKR_RUNTIME_REMOTE_ROOT:-/opt/ibkr_runtime}"
  local legacy_compute_root="${IBKR_RUNTIME_ENV_SEED_ROOT:-${IBKR_DEPLOY_IBKR_ROOT:-/opt/ibkr_compute}}"

  for unit in "${units[@]-}"; do
    case "$unit" in
      ibkr_runtime_src|ibkr_runtime_requirements|ibkr_runtime_systemd|gateway_display_systemd|gateway_systemd)
        needs_runtime_root=1
        break
        ;;
    esac
  done

  [[ "$needs_runtime_root" -eq 1 ]] || return 0

  ssh_run "
    set -e
    mkdir -p '$runtime_root' '$runtime_root/logs/ibgateway'
    if [ ! -f '$runtime_root/.env' ] && [ -f '$legacy_compute_root/.env' ]; then
      cp '$legacy_compute_root/.env' '$runtime_root/.env'
    fi
    if [ ! -f '$runtime_root/.env' ]; then
      : > '$runtime_root/.env'
    fi
    python3 - '$runtime_root/.env' <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding='utf-8', errors='ignore') if path.exists() else ''
updates = {
    'PORT': '5101',
    'IBKR_RUNTIME_MODE': 'remote',
    'IBKR_COMPUTE_INTERNAL_URL': 'http://127.0.0.1:5100',
    'IBKR_RUNTIME_INTERNAL_URL': 'http://127.0.0.1:5101',
    'IBKR_GATEWAY_LOG_DIR': '/opt/ibkr_runtime/logs/ibgateway',
    'CONSOLE_BASE_URL': 'https://quant.lzw-glory.top',
}

output_lines = []
seen = set()
for raw_line in text.splitlines():
    line = raw_line.rstrip('\n')
    stripped = line.strip()
    if not stripped or stripped.startswith('#') or '=' not in line:
        output_lines.append(line)
        continue
    key, _ = line.split('=', 1)
    key = key.strip()
    if key in updates:
        output_lines.append(f'{key}={updates[key]}')
        seen.add(key)
    else:
        output_lines.append(line)

for key, value in updates.items():
    if key not in seen:
        output_lines.append(f'{key}={value}')

path.write_text('\n'.join(output_lines).rstrip() + '\n', encoding='utf-8')
PY
  "
}

prepare_backtest_root_if_needed() {
  local units=( "$@" )
  local unit
  local needs_backtest_root=0
  local backtest_root="${IBKR_BACKTEST_REMOTE_ROOT:-/opt/ibkr_backtest}"
  local compute_env_root="${IBKR_BACKTEST_ENV_SEED_ROOT:-${IBKR_DEPLOY_IBKR_ROOT:-/opt/ibkr_compute}}"
  local backtest_client_id="${IBGW_BACKTEST_CLIENT_ID:-71}"

  for unit in "${units[@]-}"; do
    case "$unit" in
      ibkr_backtest_src|ibkr_backtest_requirements|ibkr_backtest_systemd)
        needs_backtest_root=1
        break
        ;;
    esac
  done

  [[ "$needs_backtest_root" -eq 1 ]] || return 0

  ssh_run "
    set -e
    mkdir -p '$backtest_root' '$backtest_root/src'
    if [ ! -f '$backtest_root/.env' ] && [ -f '$compute_env_root/.env' ]; then
      cp '$compute_env_root/.env' '$backtest_root/.env'
    fi
    if [ ! -f '$backtest_root/.env' ]; then
      : > '$backtest_root/.env'
    fi
    python3 - '$backtest_root/.env' '$backtest_client_id' <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
backtest_client_id = str(sys.argv[2] or '71').strip() or '71'
text = path.read_text(encoding='utf-8', errors='ignore') if path.exists() else ''
updates = {
    'PORT': '5105',
    'IBKR_BACKTEST_PORT': '5105',
    'IBKR_SERVICE_PROFILE': 'backtest',
    'IBKR_RUNTIME_MODE': 'remote',
    'IBKR_BACKTEST_INTERNAL_URL': 'http://127.0.0.1:5105',
    'IBKR_COMPUTE_INTERNAL_URL': 'http://127.0.0.1:5100',
    'IBKR_RUNTIME_INTERNAL_URL': 'http://127.0.0.1:5101',
    'IBKR_API_INTERNAL_URL': 'http://127.0.0.1:5102',
    'IBGW_BACKTEST_CLIENT_ID': backtest_client_id,
}

output_lines = []
seen = set()
for raw_line in text.splitlines():
    line = raw_line.rstrip('\\n')
    stripped = line.strip()
    if not stripped or stripped.startswith('#') or '=' not in line:
        output_lines.append(line)
        continue
    key, _ = line.split('=', 1)
    key = key.strip()
    if key in updates:
        output_lines.append(f'{key}={updates[key]}')
        seen.add(key)
    else:
        output_lines.append(line)

for key, value in updates.items():
    if key not in seen:
        output_lines.append(f'{key}={value}')

path.write_text('\\n'.join(output_lines).rstrip() + '\\n', encoding='utf-8')
PY
  "
}

prepare_api_root_if_needed() {
  local units=( "$@" )
  local unit
  local needs_api_root=0
  local api_root="${IBKR_API_REMOTE_ROOT:-/opt/ibkr_api}"
  local legacy_compute_root="${IBKR_API_ENV_SEED_ROOT:-${IBKR_DEPLOY_IBKR_ROOT:-/opt/ibkr_compute}}"

  for unit in "${units[@]-}"; do
    case "$unit" in
      ibkr_api_src|ibkr_api_systemd)
        needs_api_root=1
        break
        ;;
    esac
  done

  [[ "$needs_api_root" -eq 1 ]] || return 0

  ssh_run "
    set -e
    mkdir -p '$api_root' '$api_root/src'
    if [ ! -f '$api_root/.env' ] && [ -f '$legacy_compute_root/.env' ]; then
      cp '$legacy_compute_root/.env' '$api_root/.env'
    fi
    if [ ! -f '$api_root/.env' ]; then
      : > '$api_root/.env'
    fi
    python3 - '$api_root/.env' <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding='utf-8', errors='ignore') if path.exists() else ''
updates = {
    'PORT': '5102',
    'IBKR_API_PORT': '5102',
    'IBKR_RUNTIME_MODE': 'remote',
    'IBKR_COMPUTE_INTERNAL_URL': 'http://127.0.0.1:5100',
    'IBKR_RUNTIME_INTERNAL_URL': 'http://127.0.0.1:5101',
    'IBKR_API_INTERNAL_URL': 'http://127.0.0.1:5102',
    'IBKR_SCHEDULER_INTERNAL_URL': 'http://127.0.0.1:5103',
    'CONSOLE_BASE_URL': 'https://quant.lzw-glory.top',
}

output_lines = []
seen = set()
for raw_line in text.splitlines():
    line = raw_line.rstrip('\\n')
    stripped = line.strip()
    if not stripped or stripped.startswith('#') or '=' not in line:
        output_lines.append(line)
        continue
    key, _ = line.split('=', 1)
    key = key.strip()
    if key in updates:
        output_lines.append(f'{key}={updates[key]}')
        seen.add(key)
    else:
        output_lines.append(line)

for key, value in updates.items():
    if key not in seen:
        output_lines.append(f'{key}={value}')

path.write_text('\\n'.join(output_lines).rstrip() + '\\n', encoding='utf-8')
PY
  "
}

prepare_scheduler_root_if_needed() {
  local units=( "$@" )
  local unit
  local needs_scheduler_root=0
  local scheduler_root="${IBKR_SCHEDULER_REMOTE_ROOT:-/opt/ibkr_scheduler}"
  local legacy_compute_root="${IBKR_SCHEDULER_ENV_SEED_ROOT:-${IBKR_DEPLOY_IBKR_ROOT:-/opt/ibkr_compute}}"

  for unit in "${units[@]-}"; do
    case "$unit" in
      ibkr_scheduler_src|ibkr_scheduler_systemd)
        needs_scheduler_root=1
        break
        ;;
    esac
  done

  [[ "$needs_scheduler_root" -eq 1 ]] || return 0

  ssh_run "
    set -e
    mkdir -p '$scheduler_root' '$scheduler_root/src'
    if [ ! -f '$scheduler_root/.env' ] && [ -f '$legacy_compute_root/.env' ]; then
      cp '$legacy_compute_root/.env' '$scheduler_root/.env'
    fi
    if [ ! -f '$scheduler_root/.env' ]; then
      : > '$scheduler_root/.env'
    fi
    python3 - '$scheduler_root/.env' <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding='utf-8', errors='ignore') if path.exists() else ''
updates = {
    'PORT': '5103',
    'IBKR_SCHEDULER_PORT': '5103',
    'IBKR_RUNTIME_MODE': 'remote',
    'IBKR_SCHEDULER_AUTOSTART': 'true',
    'IBKR_COMPUTE_INTERNAL_URL': 'http://127.0.0.1:5100',
    'IBKR_RUNTIME_INTERNAL_URL': 'http://127.0.0.1:5101',
    'IBKR_API_INTERNAL_URL': 'http://127.0.0.1:5102',
    'IBKR_SCHEDULER_INTERNAL_URL': 'http://127.0.0.1:5103',
    'CONSOLE_BASE_URL': 'https://quant.lzw-glory.top',
}

output_lines = []
seen = set()
for raw_line in text.splitlines():
    line = raw_line.rstrip('\\n')
    stripped = line.strip()
    if not stripped or stripped.startswith('#') or '=' not in line:
        output_lines.append(line)
        continue
    key, _ = line.split('=', 1)
    key = key.strip()
    if key in updates:
        output_lines.append(f'{key}={updates[key]}')
        seen.add(key)
    else:
        output_lines.append(line)

for key, value in updates.items():
    if key not in seen:
        output_lines.append(f'{key}={value}')

path.write_text('\\n'.join(output_lines).rstrip() + '\\n', encoding='utf-8')
PY
  "
}

prepare_console_root_if_needed() {
  local units=( "$@" )
  local unit
  local needs_console_root=0
  local console_root="${IBKR_CONSOLE_REMOTE_ROOT:-/opt/ibkr_console}"

  for unit in "${units[@]-}"; do
    case "$unit" in
      ibkr_console_static|ibkr_console_systemd)
        needs_console_root=1
        break
        ;;
    esac
  done

  [[ "$needs_console_root" -eq 1 ]] || return 0

  ssh_run "
    set -e
    mkdir -p '$console_root/static'
    if [ ! -f '$console_root/.env' ]; then
      cat > '$console_root/.env' <<'EOF'
PORT=5104
IBKR_CONSOLE_BIND=127.0.0.1
EOF
    fi
  "
}

run_pocketbase_migrations_if_needed() {
  local units=( "$@" )
  array_contains "pb_migrations" "${units[@]}" || return 0
  local pocketbase_root="${PB_REMOTE_ROOT%/}"
  local pocketbase_bin="$pocketbase_root/pocketbase"
  local data_dir="$pocketbase_root/pb_data"
  local hooks_dir="$pocketbase_root/pb_hooks"
  local public_dir="$pocketbase_root/pb_public"
  local migrations_dir="$pocketbase_root/extensions/migrations"

  if [[ "${RESTART_SERVICE:-1}" -eq 1 && "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
    deploy_log "Stopping pocketbase before applying migrations..."
    ssh_run "systemctl stop pocketbase"
  else
    deploy_warn "Applying PocketBase migrations without stopping pocketbase (restart disabled)."
  fi

  deploy_log "Applying PocketBase migrations from $migrations_dir..."
  ssh_run "cd '$pocketbase_root' && '$pocketbase_bin' migrate up --dir '$data_dir' --hooksDir '$hooks_dir' --publicDir '$public_dir' --migrationsDir '$migrations_dir'"
}

perform_post_actions_for_units() {
  if [[ "${PLAN_ONLY:-0}" -eq 1 || "${DRY_RUN:-0}" -eq 1 ]]; then
    return 0
  fi
  local units=( "$@" )
  local enable_services=()
  local restart_groups=()
  local wait_groups=()
  prepare_pocketbase_root_if_needed "${units[@]}"
  prepare_runtime_root_if_needed "${units[@]}"
  prepare_backtest_root_if_needed "${units[@]}"
  prepare_api_root_if_needed "${units[@]}"
  prepare_scheduler_root_if_needed "${units[@]}"
  prepare_console_root_if_needed "${units[@]}"
  if units_need_pip_install "${units[@]}" && [[ "${SKIP_REQUIREMENTS:-0}" -eq 0 ]]; then
    ensure_remote_venv
    install_requirements
  fi
  run_pocketbase_migrations_if_needed "${units[@]}"
  if units_need_daemon_reload "${units[@]}" && [[ "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
    ssh_run "systemctl daemon-reload"
  fi
  if [[ "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
    collect_enable_services enable_services "${units[@]}"
    local service
    for service in "${enable_services[@]-}"; do
      [[ -n "$service" ]] || continue
      ssh_run "systemctl enable $service >/dev/null 2>&1 || true"
    done
  fi
  if [[ "${RESTART_SERVICE:-1}" -eq 1 ]]; then
    collect_restart_groups restart_groups "${units[@]}"
    local restart
    for restart in "${restart_groups[@]-}"; do
      [[ -n "$restart" ]] || continue
      ssh_run "systemctl restart $restart"
    done
  fi
  collect_wait_groups_for_units wait_groups "${units[@]}"
  local wait_group
  for wait_group in "${wait_groups[@]-}"; do
    [[ -n "$wait_group" ]] || continue
    wait_for_service_ready "$wait_group"
  done
  print_status_for_units "${units[@]}"
}
