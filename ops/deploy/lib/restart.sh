#!/usr/bin/env bash

service_wait_timeout_seconds() {
  case "$1" in
    pocketbase)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_POCKETBASE:-180}"
      ;;
    ibkr-compute)
      printf '%s\n' "${DEPLOY_WAIT_TIMEOUT_IBKR_COMPUTE:-180}"
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
    ibkr-compute)
      printf '%s\n' "${DEPLOY_WAIT_URL_IBKR_COMPUTE:-http://127.0.0.1:5100/health}"
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

perform_post_actions_for_units() {
  if [[ "${PLAN_ONLY:-0}" -eq 1 || "${DRY_RUN:-0}" -eq 1 ]]; then
    return 0
  fi
  local units=( "$@" )
  local enable_services=()
  local restart_groups=()
  local wait_groups=()
  if units_need_pip_install "${units[@]}" && [[ "${SKIP_REQUIREMENTS:-0}" -eq 0 ]]; then
    ensure_remote_venv
    install_requirements
  fi
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
