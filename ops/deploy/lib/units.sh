#!/usr/bin/env bash

list_all_units_for_target() {
  case "$1" in
    pocketbase)
      # PocketBase deploys still carry the empty pb_hooks placeholder directory.
      printf '%s\n' pb_public pb_hooks pb_migrations pb_systemd
      ;;
    console|ibkr_console)
      printf '%s\n' ibkr_console_static ibkr_console_systemd
      ;;
    ibkr|ibkr_compute)
      printf '%s\n' ibkr_src ibkr_requirements ibkr_systemd ibkr_api_src ibkr_api_systemd ibkr_scheduler_src ibkr_scheduler_systemd gateway_display_systemd gateway_systemd
      ;;
    runtime|ibkr_runtime)
      printf '%s\n' ibkr_runtime_src ibkr_runtime_requirements ibkr_runtime_systemd gateway_display_systemd gateway_systemd
      ;;
    all)
      list_all_units_for_target pocketbase
      list_all_units_for_target ibkr_console
      list_all_units_for_target ibkr_compute
      list_all_units_for_target ibkr_runtime
      ;;
    *)
      deploy_die "Unknown unit target: $1"
      ;;
  esac
}

list_selected_units_for_target() {
  case "$1" in
    pocketbase)
      if [[ "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
        printf '%s\n' pb_systemd
      fi
      [[ "${DEPLOY_PUBLIC:-1}" -eq 1 ]] && printf '%s\n' pb_public
      # pb_hooks no longer carries JS entrypoints, but we still sync the placeholder directory.
      [[ "${DEPLOY_HOOKS:-1}" -eq 1 ]] && printf '%s\n' pb_hooks
      [[ "${DEPLOY_MIGRATIONS:-0}" -eq 1 ]] && printf '%s\n' pb_migrations
      return 0
      ;;
    console|ibkr_console)
      printf '%s\n' ibkr_console_static
      if [[ "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
        printf '%s\n' ibkr_console_systemd
      fi
      return 0
      ;;
    ibkr|ibkr_compute)
      printf '%s\n' ibkr_src
      printf '%s\n' ibkr_requirements
      printf '%s\n' ibkr_api_src
      printf '%s\n' ibkr_scheduler_src
      if [[ "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
        printf '%s\n' ibkr_systemd
        printf '%s\n' ibkr_api_systemd
        printf '%s\n' ibkr_scheduler_systemd
      fi
      if [[ "${DEPLOY_GATEWAY_SERVICE:-0}" -eq 1 && "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
        printf '%s\n' gateway_display_systemd
        printf '%s\n' gateway_systemd
      fi
      return 0
      ;;
    runtime|ibkr_runtime)
      printf '%s\n' ibkr_runtime_src
      printf '%s\n' ibkr_runtime_requirements
      if [[ "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
        printf '%s\n' ibkr_runtime_systemd
      fi
      if [[ "${DEPLOY_GATEWAY_SERVICE:-0}" -eq 1 && "${SKIP_SYSTEMD:-0}" -eq 0 ]]; then
        printf '%s\n' gateway_display_systemd
        printf '%s\n' gateway_systemd
      fi
      return 0
      ;;
    all)
      list_selected_units_for_target pocketbase
      list_selected_units_for_target ibkr_console
      list_selected_units_for_target ibkr_compute
      list_selected_units_for_target ibkr_runtime
      return 0
      ;;
    *)
      deploy_die "Unknown selected-unit target: $1"
      ;;
  esac
}

unit_local_rel() {
  case "$1" in
    pb_public) printf '%s\n' runtime/pocketbase/pb_public ;;
    pb_hooks) printf '%s\n' runtime/pocketbase/pb_hooks ;;
    pb_migrations) printf '%s\n' extensions/pocketbase/migrations ;;
    pb_systemd) printf '%s\n' runtime/pocketbase/systemd/pocketbase.service ;;
    ibkr_console_static) printf '%s\n' runtime/ibkr_console/static ;;
    ibkr_console_systemd) printf '%s\n' runtime/ibkr_console/systemd/ibkr-console.service ;;
    ibkr_src) printf '%s\n' runtime/ibkr_compute/src ;;
    ibkr_requirements) printf '%s\n' runtime/ibkr_compute/requirements.txt ;;
    ibkr_systemd) printf '%s\n' runtime/ibkr_compute/systemd/ibkr-compute.service ;;
    ibkr_api_src) printf '%s\n' runtime/ibkr_api/src ;;
    ibkr_api_systemd) printf '%s\n' runtime/ibkr_api/systemd/ibkr-api.service ;;
    ibkr_scheduler_src) printf '%s\n' runtime/ibkr_scheduler/src ;;
    ibkr_scheduler_systemd) printf '%s\n' runtime/ibkr_scheduler/systemd/ibkr-scheduler.service ;;
    ibkr_runtime_src) printf '%s\n' runtime/ibkr_runtime/src ;;
    ibkr_runtime_requirements) printf '%s\n' runtime/ibkr_runtime/requirements.txt ;;
    ibkr_runtime_systemd) printf '%s\n' runtime/ibkr_runtime/systemd/ibkr-runtime.service ;;
    gateway_display_systemd) printf '%s\n' runtime/ib_gateway/systemd/ibkr-display.service ;;
    gateway_systemd) printf '%s\n' runtime/ib_gateway/systemd/ibkr-gateway.service ;;
    *)
      deploy_die "Unknown unit: $1"
      ;;
  esac
}

unit_remote_path() {
  case "$1" in
    pb_public) printf '%s\n' "$PB_REMOTE_ROOT/pb_public" ;;
    pb_hooks) printf '%s\n' "$PB_REMOTE_ROOT/pb_hooks" ;;
    pb_migrations) printf '%s\n' "$PB_REMOTE_ROOT/extensions/migrations" ;;
    pb_systemd) printf '%s\n' "$SYSTEMD_DIR/pocketbase.service" ;;
    ibkr_console_static) printf '%s\n' "${IBKR_CONSOLE_REMOTE_ROOT:-/opt/ibkr_console}/static" ;;
    ibkr_console_systemd) printf '%s\n' "$SYSTEMD_DIR/ibkr-console.service" ;;
    ibkr_src) printf '%s\n' "$IBKR_REMOTE_ROOT/src" ;;
    ibkr_requirements) printf '%s\n' "$IBKR_REMOTE_ROOT/requirements.txt" ;;
    ibkr_systemd) printf '%s\n' "$SYSTEMD_DIR/ibkr-compute.service" ;;
    ibkr_api_src) printf '%s\n' "${IBKR_API_REMOTE_ROOT:-/opt/ibkr_api}/src" ;;
    ibkr_api_systemd) printf '%s\n' "$SYSTEMD_DIR/ibkr-api.service" ;;
    ibkr_scheduler_src) printf '%s\n' "${IBKR_SCHEDULER_REMOTE_ROOT:-/opt/ibkr_scheduler}/src" ;;
    ibkr_scheduler_systemd) printf '%s\n' "$SYSTEMD_DIR/ibkr-scheduler.service" ;;
    ibkr_runtime_src) printf '%s\n' "${IBKR_RUNTIME_REMOTE_ROOT:-/opt/ibkr_runtime}/src" ;;
    ibkr_runtime_requirements) printf '%s\n' "${IBKR_RUNTIME_REMOTE_ROOT:-/opt/ibkr_runtime}/requirements.txt" ;;
    ibkr_runtime_systemd) printf '%s\n' "$SYSTEMD_DIR/ibkr-runtime.service" ;;
    gateway_display_systemd) printf '%s\n' "$SYSTEMD_DIR/ibkr-display.service" ;;
    gateway_systemd) printf '%s\n' "$SYSTEMD_DIR/ibkr-gateway.service" ;;
    *)
      deploy_die "Unknown unit remote path: $1"
      ;;
  esac
}

unit_type() {
  case "$1" in
    pb_public|pb_hooks|pb_migrations|ibkr_console_static|ibkr_src|ibkr_api_src|ibkr_scheduler_src|ibkr_runtime_src)
      printf '%s\n' dir
      ;;
    pb_systemd|ibkr_requirements|ibkr_systemd|ibkr_api_systemd|ibkr_scheduler_systemd|ibkr_runtime_requirements|ibkr_runtime_systemd|ibkr_console_systemd|gateway_display_systemd|gateway_systemd)
      printf '%s\n' file
      ;;
    *)
      deploy_die "Unknown unit type: $1"
      ;;
  esac
}

unit_validator() {
  case "$1" in
    pb_public|pb_hooks|pb_migrations|ibkr_console_static)
      printf '%s\n' js_tree
      ;;
    ibkr_src|ibkr_api_src|ibkr_scheduler_src|ibkr_runtime_src)
      printf '%s\n' python_tree
      ;;
    pb_systemd|ibkr_requirements|ibkr_systemd|ibkr_api_systemd|ibkr_scheduler_systemd|ibkr_runtime_requirements|ibkr_runtime_systemd|ibkr_console_systemd|gateway_display_systemd|gateway_systemd)
      printf '%s\n' none
      ;;
    *)
      deploy_die "Unknown unit validator: $1"
      ;;
  esac
}

unit_family() {
  case "$1" in
    pb_public|pb_hooks|pb_migrations|pb_systemd)
      printf '%s\n' pocketbase
      ;;
    ibkr_console_static|ibkr_console_systemd)
      printf '%s\n' ibkr_console
      ;;
    ibkr_src|ibkr_requirements|ibkr_systemd)
      printf '%s\n' ibkr_compute
      ;;
    ibkr_api_src|ibkr_api_systemd)
      printf '%s\n' ibkr_api
      ;;
    ibkr_scheduler_src|ibkr_scheduler_systemd)
      printf '%s\n' ibkr_scheduler
      ;;
    ibkr_runtime_src|ibkr_runtime_requirements|ibkr_runtime_systemd|gateway_display_systemd|gateway_systemd)
      printf '%s\n' ibkr_runtime
      ;;
    *)
      deploy_die "Unknown unit family: $1"
      ;;
  esac
}

unit_category() {
  case "$1" in
    pb_public|pb_hooks|ibkr_console_static|ibkr_src|ibkr_requirements|ibkr_api_src|ibkr_scheduler_src|ibkr_runtime_src|ibkr_runtime_requirements)
      printf '%s\n' runtime
      ;;
    pb_systemd|ibkr_systemd|ibkr_api_systemd|ibkr_scheduler_systemd|ibkr_runtime_systemd|ibkr_console_systemd|gateway_display_systemd|gateway_systemd)
      printf '%s\n' systemd
      ;;
    pb_migrations)
      printf '%s\n' extensions
      ;;
    *)
      deploy_die "Unknown unit category: $1"
      ;;
  esac
}

unit_restart_group() {
  case "$1" in
    pb_public|pb_hooks|pb_migrations|pb_systemd)
      printf '%s\n' pocketbase
      ;;
    ibkr_src|ibkr_requirements|ibkr_systemd)
      printf '%s\n' ibkr-compute
      ;;
    ibkr_api_src|ibkr_api_systemd)
      printf '%s\n' ibkr-api
      ;;
    ibkr_scheduler_src|ibkr_scheduler_systemd)
      printf '%s\n' ibkr-scheduler
      ;;
    ibkr_runtime_src|ibkr_runtime_requirements|ibkr_runtime_systemd)
      printf '%s\n' ibkr-runtime
      ;;
    ibkr_console_static)
      printf '%s\n' ""
      ;;
    ibkr_console_systemd)
      printf '%s\n' ibkr-console
      ;;
    gateway_display_systemd)
      printf '%s\n' ibkr-display
      ;;
    gateway_systemd)
      printf '%s\n' ibkr-gateway
      ;;
    *)
      printf '%s\n' ""
      ;;
  esac
}

unit_enable_service() {
  case "$1" in
    pb_systemd)
      printf '%s\n' pocketbase
      ;;
    ibkr_systemd)
      printf '%s\n' ibkr-compute
      ;;
    ibkr_api_systemd)
      printf '%s\n' ibkr-api
      ;;
    ibkr_scheduler_systemd)
      printf '%s\n' ibkr-scheduler
      ;;
    ibkr_runtime_systemd)
      printf '%s\n' ibkr-runtime
      ;;
    ibkr_console_systemd)
      printf '%s\n' ibkr-console
      ;;
    gateway_display_systemd)
      printf '%s\n' ibkr-display
      ;;
    gateway_systemd)
      printf '%s\n' ibkr-gateway
      ;;
    *)
      printf '%s\n' ""
      ;;
  esac
}

unit_needs_pip_install() {
  [[ "$1" == "ibkr_requirements" || "$1" == "ibkr_runtime_requirements" ]] && return 0
  return 1
}

unit_needs_daemon_reload() {
  case "$1" in
    pb_systemd|ibkr_systemd|ibkr_runtime_systemd|gateway_display_systemd|gateway_systemd)
      return 0
      ;;
    ibkr_api_systemd|ibkr_scheduler_systemd|ibkr_console_systemd)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

unit_optional_flag() {
  case "$1" in
    pb_migrations)
      printf '%s\n' migrations
      ;;
    gateway_display_systemd|gateway_systemd)
      printf '%s\n' gateway-service
      ;;
    *)
      printf '%s\n' ""
      ;;
  esac
}

unit_matches_path() {
  local unit="$1"
  local rel_path="$2"
  local local_rel
  local_rel="$(unit_local_rel "$unit")"
  if [[ "$(unit_type "$unit")" == "dir" ]]; then
    case "$rel_path" in
      "$local_rel"/*)
        return 0
        ;;
      *)
        return 1
        ;;
    esac
  fi
  [[ "$rel_path" == "$local_rel" ]]
}

find_known_unit_for_target() {
  local target="$1"
  local rel_path="$2"
  local unit
  while IFS= read -r unit; do
    [[ -n "$unit" ]] || continue
    if unit_matches_path "$unit" "$rel_path"; then
      printf '%s\n' "$unit"
      return 0
    fi
  done < <(list_all_units_for_target "$target")
  return 1
}

is_unit_selected_for_target() {
  local target="$1"
  local unit="$2"
  local selected
  while IFS= read -r selected; do
    [[ "$selected" == "$unit" ]] && return 0
  done < <(list_selected_units_for_target "$target")
  return 1
}

unit_remote_file_for_path() {
  local unit="$1"
  local rel_path="$2"
  local local_rel
  local remote_target
  local suffix
  local_rel="$(unit_local_rel "$unit")"
  remote_target="$(unit_remote_path "$unit")"
  if [[ "$(unit_type "$unit")" == "file" ]]; then
    printf '%s\n' "$remote_target"
    return 0
  fi
  suffix="${rel_path#$local_rel/}"
  printf '%s/%s\n' "$remote_target" "$suffix"
}

unit_stage_path() {
  local stage_root="$1"
  local unit="$2"
  if [[ "$(unit_type "$unit")" == "dir" ]]; then
    printf '%s/%s\n' "$stage_root" "$unit"
    return 0
  fi
  printf '%s/%s/%s\n' "$stage_root" "$unit" "$(basename "$(unit_local_rel "$unit")")"
}
