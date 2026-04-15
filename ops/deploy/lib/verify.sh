#!/usr/bin/env bash

validator_label_for_unit() {
  case "$(unit_validator "$1")" in
    python_tree)
      printf '%s\n' "python syntax ($1)"
      ;;
    js_tree)
      printf '%s\n' "node --check ($1)"
      ;;
    none)
      printf '%s\n' ""
      ;;
  esac
}

collect_validator_labels() {
  local array_name="$1"
  shift || true
  eval "$array_name=()"
  local unit
  local label
  for unit in "$@"; do
    label="$(validator_label_for_unit "$unit")"
    [[ -n "$label" ]] && append_unique "$array_name" "$label"
  done
  return 0
}

check_python_tree_remote() {
  local remote_dir="$1"
  [[ "${SKIP_CHECKS:-0}" -eq 1 ]] && return 0
  ssh_run "find '$remote_dir' -type f -name '*.py' -print0 | xargs -0 -r python3 -m py_compile"
}

check_python_file_remote() {
  local remote_file="$1"
  [[ "${SKIP_CHECKS:-0}" -eq 1 ]] && return 0
  ssh_run "python3 -m py_compile '$remote_file'"
}

check_js_tree_remote() {
  local remote_dir="$1"
  [[ "${SKIP_CHECKS:-0}" -eq 1 ]] && return 0
  ssh_run "if command -v node >/dev/null 2>&1; then find '$remote_dir' -type f -name '*.js' -print0 | xargs -0 -r node --check; fi"
}

check_js_file_remote() {
  local remote_file="$1"
  [[ "${SKIP_CHECKS:-0}" -eq 1 ]] && return 0
  ssh_run "if command -v node >/dev/null 2>&1; then node --check '$remote_file'; fi"
}

verify_remote_path() {
  local validator="$1"
  local target_path="$2"
  case "$validator" in
    python_tree)
      check_python_tree_remote "$target_path"
      ;;
    python_file)
      check_python_file_remote "$target_path"
      ;;
    js_tree)
      check_js_tree_remote "$target_path"
      ;;
    js_file)
      check_js_file_remote "$target_path"
      ;;
    none)
      ;;
    *)
      deploy_die "Unknown validator: $validator"
      ;;
  esac
}

verify_units_live() {
  local unit
  local validator
  local remote_path
  for unit in "$@"; do
    validator="$(unit_validator "$unit")"
    remote_path="$(unit_remote_path "$unit")"
    verify_remote_path "$validator" "$remote_path"
  done
}

verify_units_staged() {
  local stage_root="$1"
  shift || true
  local unit
  local validator
  local remote_path
  for unit in "$@"; do
    validator="$(unit_validator "$unit")"
    remote_path="$(unit_stage_path "$stage_root" "$unit")"
    verify_remote_path "$validator" "$remote_path"
  done
}

ensure_remote_venv() {
  ssh_run "
    set -e
    mkdir -p '$IBKR_REMOTE_ROOT'
    if [ ! -x '$VENV_DIR/bin/python' ]; then
      if ! python3 -m venv '$VENV_DIR' >/dev/null 2>&1; then
        if command -v apt-get >/dev/null 2>&1; then
          apt-get update >/dev/null
          apt-get install -y python3-venv >/dev/null
          python3 -m venv '$VENV_DIR'
        else
          echo 'python3 -m venv failed and apt-get is unavailable' >&2
          exit 1
        fi
      fi
    fi
    '$VENV_DIR/bin/python' -m ensurepip --upgrade >/dev/null 2>&1 || true
    '$VENV_DIR/bin/python' -m pip --version >/dev/null
  "
}

install_requirements() {
  ssh_run "cd '$IBKR_REMOTE_ROOT' && '$VENV_DIR/bin/python' -m pip install --disable-pip-version-check -r requirements.txt -q"
}
