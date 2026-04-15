#!/usr/bin/env bash

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
  print_status_for_units "${units[@]}"
}
