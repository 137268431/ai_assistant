#!/usr/bin/env bash

files_deploy_paths_for_target() {
  local target="$1"
  shift || true
  local rel_path
  local unit
  local local_file
  local remote_file
  for rel_path in "$@"; do
    unit="$(find_known_unit_for_target "$target" "$rel_path")"
    [[ -n "$unit" ]] || continue
    local_file="$(repo_abs_path "$rel_path")"
    [[ -f "$local_file" ]] || deploy_die "Missing file for files deploy: $local_file"
    remote_file="$(unit_remote_file_for_path "$unit" "$rel_path")"
    sync_file_rsync "$local_file" "$remote_file"
  done
  if [[ "${DRY_RUN:-0}" -eq 1 ]]; then
    deploy_log "Dry run complete."
    return 0
  fi
  verify_units_live "${PLAN_UNITS[@]}"
  perform_post_actions_for_units "${PLAN_UNITS[@]}"
}
