#!/usr/bin/env bash

FILES_LOCAL_TMP=""
FILES_REMOTE_ROOT=""

cleanup_files_artifacts() {
  if [[ -n "${FILES_LOCAL_TMP:-}" && -d "${FILES_LOCAL_TMP:-}" ]]; then
    rm -rf "$FILES_LOCAL_TMP"
  fi
  if [[ -n "${FILES_REMOTE_ROOT:-}" && "${PLAN_ONLY:-0}" -eq 0 && "${DRY_RUN:-0}" -eq 0 ]]; then
    ssh_run "rm -rf '$FILES_REMOTE_ROOT'" >/dev/null 2>&1 || true
  fi
}

append_files_manifest_entry() {
  local manifest_file="$1"
  local stage_rel="$2"
  local remote_file="$3"
  python3 - "$manifest_file" "$stage_rel" "$remote_file" <<'PY'
import json
import sys

manifest_file, stage_rel, remote_file = sys.argv[1:4]
with open(manifest_file, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"stage": stage_rel, "remote": remote_file}, separators=(",", ":")) + "\n")
PY
}

stage_file_for_batch() {
  local payload_root="$1"
  local manifest_file="$2"
  local target="$3"
  local rel_path="$4"
  local unit
  local local_file
  local remote_file
  local stage_rel
  local stage_file
  unit="$(find_known_unit_for_target "$target" "$rel_path")"
  [[ -n "$unit" ]] || return 0
  local_file="$(repo_abs_path "$rel_path")"
  [[ -f "$local_file" ]] || deploy_die "Missing file for files deploy: $local_file"
  remote_file="$(unit_remote_file_for_path "$unit" "$rel_path")"
  stage_rel="files/$rel_path"
  stage_file="$payload_root/$stage_rel"
  mkdir -p "$(dirname "$stage_file")"
  cp -p "$local_file" "$stage_file"
  append_files_manifest_entry "$manifest_file" "$stage_rel" "$remote_file"
}

apply_batched_files_remote() {
  local remote_extract_root="$1"
  ssh "$REMOTE_HOST" python3 - "$remote_extract_root/manifest.jsonl" "$remote_extract_root" <<'PY'
import json
import os
import shutil
import sys

manifest_path = sys.argv[1]
extract_root = sys.argv[2]

with open(manifest_path, "r", encoding="utf-8") as handle:
    for line_number, raw_line in enumerate(handle, 1):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        item = json.loads(raw_line)
        stage = item["stage"]
        remote = item["remote"]
        normalized_stage = os.path.normpath(stage)
        if (
            os.path.isabs(normalized_stage)
            or normalized_stage == ".."
            or normalized_stage.startswith("../")
            or not normalized_stage.startswith("files/")
        ):
            raise SystemExit(f"unsafe staged path at manifest line {line_number}: {stage!r}")
        if not os.path.isabs(remote):
            raise SystemExit(f"remote path must be absolute at manifest line {line_number}: {remote!r}")

        source = os.path.join(extract_root, normalized_stage)
        if not os.path.isfile(source):
            raise SystemExit(f"missing staged file at manifest line {line_number}: {source}")

        remote_dir = os.path.dirname(remote)
        os.makedirs(remote_dir, exist_ok=True)
        tmp_path = os.path.join(remote_dir, f".{os.path.basename(remote)}.deploy-{os.getpid()}.tmp")
        try:
            shutil.copy2(source, tmp_path)
            os.replace(tmp_path, remote)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
PY
}

batch_files_deploy_paths_for_target() {
  local target="$1"
  shift || true
  local paths=( "$@" )
  local release_id
  local payload_root
  local manifest_file
  local package_file
  local remote_extract_root
  local rel_path
  release_id="$(sanitize_release_id "${PACKAGE_NAME:-}")"
  FILES_LOCAL_TMP="$(mktemp -d "${TMPDIR:-/tmp}/ai-assistant-files.XXXXXX")"
  FILES_REMOTE_ROOT="/tmp/ai-assistant-files/$release_id"
  trap cleanup_files_artifacts EXIT
  payload_root="$FILES_LOCAL_TMP/payload"
  manifest_file="$payload_root/manifest.jsonl"
  package_file="$FILES_LOCAL_TMP/$release_id.tar.gz"
  mkdir -p "$payload_root"
  : > "$manifest_file"

  for rel_path in "${paths[@]}"; do
    stage_file_for_batch "$payload_root" "$manifest_file" "$target" "$rel_path"
  done

  if [[ ! -s "$manifest_file" ]]; then
    deploy_log "No deployable files for $target."
    cleanup_files_artifacts
    trap - EXIT
    FILES_LOCAL_TMP=""
    FILES_REMOTE_ROOT=""
    return 0
  fi

  deploy_log "Batch uploading ${#paths[@]} file(s) for $target..."
  COPYFILE_DISABLE=1 COPY_EXTENDED_ATTRIBUTES_DISABLE=1 tar -C "$payload_root" --no-mac-metadata --format=ustar -czf "$package_file" .
  remote_extract_root="$FILES_REMOTE_ROOT/extracted"
  ssh_run "rm -rf '$FILES_REMOTE_ROOT' && mkdir -p '$remote_extract_root'"
  rsync -az --human-readable "$package_file" "$REMOTE_HOST:$FILES_REMOTE_ROOT/package.tar.gz"
  ssh_run "tar -xzf '$FILES_REMOTE_ROOT/package.tar.gz' -C '$remote_extract_root'"
  apply_batched_files_remote "$remote_extract_root"
  cleanup_files_artifacts
  trap - EXIT
  FILES_LOCAL_TMP=""
  FILES_REMOTE_ROOT=""
}

rsync_files_deploy_paths_for_target() {
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
}

files_deploy_paths_for_target() {
  local target="$1"
  shift || true
  local paths=( "$@" )
  if [[ "${DRY_RUN:-0}" -eq 1 || ${#paths[@]} -le 1 ]]; then
    rsync_files_deploy_paths_for_target "$target" "${paths[@]}"
  else
    batch_files_deploy_paths_for_target "$target" "${paths[@]}"
  fi
  if [[ "${DRY_RUN:-0}" -eq 1 ]]; then
    deploy_log "Dry run complete."
    return 0
  fi
  verify_units_live "${PLAN_UNITS[@]}"
  perform_post_actions_for_units "${PLAN_UNITS[@]}"
}
