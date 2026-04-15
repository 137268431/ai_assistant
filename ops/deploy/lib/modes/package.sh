#!/usr/bin/env bash

PACKAGE_LOCAL_TMP=""
PACKAGE_REMOTE_ROOT=""

cleanup_package_artifacts() {
  if [[ -n "${PACKAGE_LOCAL_TMP:-}" && -d "${PACKAGE_LOCAL_TMP:-}" ]]; then
    rm -rf "$PACKAGE_LOCAL_TMP"
  fi
  if [[ -n "${PACKAGE_REMOTE_ROOT:-}" && "${PLAN_ONLY:-0}" -eq 0 && "${DRY_RUN:-0}" -eq 0 ]]; then
    ssh_run "rm -rf '$PACKAGE_REMOTE_ROOT'" >/dev/null 2>&1 || true
  fi
}

build_package_stage_for_unit() {
  local payload_root="$1"
  local unit="$2"
  local local_rel
  local local_path
  local unit_stage_dir
  local file_name
  local_rel="$(unit_local_rel "$unit")"
  local_path="$(repo_abs_path "$local_rel")"
  unit_stage_dir="$payload_root/$unit"
  if [[ "$(unit_type "$unit")" == "dir" ]]; then
    copy_dir_to_stage "$local_path" "$unit_stage_dir"
    return 0
  fi
  [[ -f "$local_path" ]] || deploy_die "Missing file for package deploy: $local_path"
  mkdir -p "$unit_stage_dir"
  file_name="$(basename "$local_rel")"
  cp "$local_path" "$unit_stage_dir/$file_name"
}

apply_packaged_unit() {
  local remote_extract_root="$1"
  local unit="$2"
  local remote_target
  local staged_path
  remote_target="$(unit_remote_path "$unit")"
  staged_path="$(unit_stage_path "$remote_extract_root" "$unit")"
  if [[ "$(unit_type "$unit")" == "dir" ]]; then
    ssh_run "mkdir -p '$remote_target' && rsync -a --delete '$remote_extract_root/$unit/' '$remote_target/'"
    return 0
  fi
  ssh_run "mkdir -p '$(dirname "$remote_target")' && install -m 0644 '$staged_path' '$remote_target'"
}

package_deploy_units() {
  local units=( "$@" )
  local release_id
  local payload_root
  local package_file
  local remote_extract_root
  local unit
  release_id="$(sanitize_release_id "${PACKAGE_NAME:-}")"
  PACKAGE_LOCAL_TMP="$(mktemp -d "${TMPDIR:-/tmp}/ai-assistant-deploy.XXXXXX")"
  PACKAGE_REMOTE_ROOT="/tmp/ai-assistant-deploy/$release_id"
  trap cleanup_package_artifacts EXIT
  payload_root="$PACKAGE_LOCAL_TMP/payload"
  package_file="$PACKAGE_LOCAL_TMP/$release_id.tar.gz"
  mkdir -p "$payload_root"
  for unit in "${units[@]}"; do
    build_package_stage_for_unit "$payload_root" "$unit"
  done
  tar -C "$payload_root" -czf "$package_file" .
  if [[ "${DRY_RUN:-0}" -eq 1 ]]; then
    deploy_log "Dry run complete."
    cleanup_package_artifacts
    trap - EXIT
    PACKAGE_LOCAL_TMP=""
    PACKAGE_REMOTE_ROOT=""
    return 0
  fi
  remote_extract_root="$PACKAGE_REMOTE_ROOT/extracted"
  ssh_run "rm -rf '$PACKAGE_REMOTE_ROOT' && mkdir -p '$remote_extract_root'"
  rsync -az --human-readable "$package_file" "$REMOTE_HOST:$PACKAGE_REMOTE_ROOT/package.tar.gz"
  ssh_run "tar -xzf '$PACKAGE_REMOTE_ROOT/package.tar.gz' -C '$remote_extract_root'"
  verify_units_staged "$remote_extract_root" "${units[@]}"
  for unit in "${units[@]}"; do
    apply_packaged_unit "$remote_extract_root" "$unit"
  done
  verify_units_live "${units[@]}"
  perform_post_actions_for_units "${units[@]}"
  cleanup_package_artifacts
  trap - EXIT
  PACKAGE_LOCAL_TMP=""
  PACKAGE_REMOTE_ROOT=""
}
