#!/usr/bin/env bash

deploy_log() {
  printf '%s\n' "$*"
}

deploy_warn() {
  printf 'WARN: %s\n' "$*" >&2
}

deploy_error() {
  printf 'ERROR: %s\n' "$*" >&2
}

deploy_die() {
  deploy_error "$1"
  exit 1
}

join_by() {
  local delimiter="$1"
  shift || true
  local first=1
  local item
  for item in "$@"; do
    if [[ "$first" -eq 1 ]]; then
      printf '%s' "$item"
      first=0
    else
      printf '%s%s' "$delimiter" "$item"
    fi
  done
}

print_list_block() {
  local label="$1"
  shift || true
  deploy_log "$label"
  local item
  local printed=0
  if [[ $# -eq 0 ]]; then
    deploy_log "  - (none)"
    return 0
  fi
  for item in "$@"; do
    [[ -n "$item" ]] || continue
    deploy_log "  - $item"
    printed=1
  done
  if [[ "$printed" -eq 0 ]]; then
    deploy_log "  - (none)"
  fi
}

array_contains() {
  local needle="$1"
  shift || true
  local item
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

append_unique() {
  local array_name="$1"
  local value="$2"
  eval 'local current_items=( "${'"$array_name"'[@]:-}" )'
  if ! array_contains "$value" "${current_items[@]}"; then
    eval "$array_name+=(\"\$value\")"
  fi
}

normalize_repo_path() {
  local path="$1"
  if [[ -z "$path" ]]; then
    deploy_die "Empty path is not allowed."
  fi
  if [[ "$path" == "$AI_ASSISTANT_ROOT" ]]; then
    deploy_die "Repository root is not a deployable path: $path"
  fi
  if [[ "$path" == "$AI_ASSISTANT_ROOT/"* ]]; then
    path="${path#$AI_ASSISTANT_ROOT/}"
  fi
  path="${path#./}"
  while [[ "$path" == */./* ]]; do
    path="${path//\/.\//\/}"
  done
  case "$path" in
    ""|"."|".."|../*|*/../*|*/..)
      deploy_die "Path must stay inside the repo: $1"
      ;;
  esac
  printf '%s\n' "$path"
}

repo_abs_path() {
  printf '%s/%s\n' "$AI_ASSISTANT_ROOT" "$1"
}

sanitize_release_id() {
  local raw="${1:-}"
  if [[ -z "$raw" ]]; then
    raw="$(date '+%Y%m%d%H%M%S')-$$"
  fi
  raw="$(printf '%s' "$raw" | tr -cs 'A-Za-z0-9._-' '-')"
  raw="${raw#-}"
  raw="${raw%-}"
  [[ -n "$raw" ]] || raw="$(date '+%Y%m%d%H%M%S')-$$"
  printf '%s\n' "$raw"
}

ssh_run() {
  ssh "$REMOTE_HOST" "$@"
}

build_rsync_base() {
  RSYNC_CMD=(
    rsync
    -az
    --human-readable
    --exclude
    .DS_Store
    --exclude
    __pycache__/
    --exclude
    '*.pyc'
    --exclude
    '*.bak.*'
  )
  if [[ "${DRY_RUN:-0}" -eq 1 ]]; then
    RSYNC_CMD+=(--dry-run --itemize-changes)
  fi
}

build_local_copy_rsync_base() {
  LOCAL_COPY_RSYNC_CMD=(
    rsync
    -a
    --exclude
    .DS_Store
    --exclude
    __pycache__/
    --exclude
    '*.pyc'
    --exclude
    '*.bak.*'
  )
}

ensure_remote_dir() {
  local remote_dir="$1"
  if [[ "${PLAN_ONLY:-0}" -eq 1 ]]; then
    return 0
  fi
  if [[ "${DRY_RUN:-0}" -eq 1 ]]; then
    ssh_run "test -d '$remote_dir' || printf 'WOULD CREATE %s\n' '$remote_dir'"
    return 0
  fi
  ssh_run "mkdir -p '$remote_dir'"
}

sync_dir_scope() {
  local local_dir="$1"
  local remote_dir="$2"
  [[ -d "$local_dir" ]] || deploy_die "Missing directory: $local_dir"
  ensure_remote_dir "$remote_dir"
  build_rsync_base
  local cmd=( "${RSYNC_CMD[@]}" --delete "$local_dir"/ "$REMOTE_HOST:$remote_dir/" )
  "${cmd[@]}"
}

sync_file_rsync() {
  local local_file="$1"
  local remote_file="$2"
  [[ -f "$local_file" ]] || deploy_die "Missing file: $local_file"
  ensure_remote_dir "$(dirname "$remote_file")"
  build_rsync_base
  local cmd=( "${RSYNC_CMD[@]}" "$local_file" "$REMOTE_HOST:$remote_file" )
  "${cmd[@]}"
}

copy_dir_to_stage() {
  local source_dir="$1"
  local target_dir="$2"
  [[ -d "$source_dir" ]] || deploy_die "Missing directory: $source_dir"
  mkdir -p "$target_dir"
  build_local_copy_rsync_base
  local cmd=( "${LOCAL_COPY_RSYNC_CMD[@]}" "$source_dir"/ "$target_dir/" )
  "${cmd[@]}"
}
