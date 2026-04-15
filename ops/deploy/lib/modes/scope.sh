#!/usr/bin/env bash

scope_deploy_units() {
  local unit
  local local_path
  local remote_path
  for unit in "$@"; do
    local_path="$(repo_abs_path "$(unit_local_rel "$unit")")"
    remote_path="$(unit_remote_path "$unit")"
    if [[ "$(unit_type "$unit")" == "dir" ]]; then
      sync_dir_scope "$local_path" "$remote_path"
    else
      sync_file_rsync "$local_path" "$remote_path"
    fi
  done
  if [[ "${DRY_RUN:-0}" -eq 1 ]]; then
    deploy_log "Dry run complete."
    return 0
  fi
  verify_units_live "$@"
  perform_post_actions_for_units "$@"
}
