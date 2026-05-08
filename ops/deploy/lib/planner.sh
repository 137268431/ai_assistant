#!/usr/bin/env bash

init_plan_state() {
  SELECTED_UNITS=()
  INPUT_TOUCHED_PATHS=()
  INPUT_DEPLOYABLE_FILES=()
  CANDIDATE_UNITS=()
  PLAN_UNITS=()
  PLAN_FILES=()
  UNMANAGED_PATHS=()
  DISABLED_SCOPE_PATHS=()
  AUTO_REASONS=()
  NO_MANAGED_CHANGES=0
  HAS_CHANGE_SOURCE=0
  HAS_COMPLEX_CHANGES=0
  INPUT_RECORD_COUNT=0
  FINAL_MODE=""
}

load_selected_units() {
  local target="$1"
  local unit
  SELECTED_UNITS=()
  while IFS= read -r unit; do
    [[ -n "$unit" ]] || continue
    append_unique SELECTED_UNITS "$unit"
  done < <(list_selected_units_for_target "$target")
}

diff_status_kind() {
  case "$1" in
    R*) printf '%s\n' R ;;
    C*) printf '%s\n' C ;;
    *) printf '%s\n' "$1" ;;
  esac
}

collect_cli_file_inputs() {
  local raw
  local rel
  if [[ ${#FILE_ARGS[@]} -eq 0 ]]; then
    return 0
  fi
  for raw in "${FILE_ARGS[@]}"; do
    rel="$(normalize_repo_path "$raw")"
    append_unique INPUT_DEPLOYABLE_FILES "$rel"
    append_unique INPUT_TOUCHED_PATHS "$rel"
    INPUT_RECORD_COUNT=$((INPUT_RECORD_COUNT + 1))
    HAS_CHANGE_SOURCE=1
  done
}

collect_diff_inputs() {
  [[ -n "${DIFF_RANGE:-}" ]] || return 0
  if ! git -C "$AI_ASSISTANT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    deploy_die "Git diff mode requires a git repository: $AI_ASSISTANT_ROOT"
  fi
  HAS_CHANGE_SOURCE=1
  local status path1 path2 kind rel1 rel2
  while IFS=$'\t' read -r status path1 path2; do
    [[ -n "$status" ]] || continue
    kind="$(diff_status_kind "$status")"
    INPUT_RECORD_COUNT=$((INPUT_RECORD_COUNT + 1))
    case "$kind" in
      A|M)
        rel1="$(normalize_repo_path "$path1")"
        append_unique INPUT_DEPLOYABLE_FILES "$rel1"
        append_unique INPUT_TOUCHED_PATHS "$rel1"
        ;;
      D|T|U|B|X)
        HAS_COMPLEX_CHANGES=1
        rel1="$(normalize_repo_path "$path1")"
        append_unique INPUT_TOUCHED_PATHS "$rel1"
        ;;
      R|C)
        HAS_COMPLEX_CHANGES=1
        rel1="$(normalize_repo_path "$path1")"
        rel2="$(normalize_repo_path "$path2")"
        append_unique INPUT_TOUCHED_PATHS "$rel1"
        append_unique INPUT_TOUCHED_PATHS "$rel2"
        ;;
      *)
        HAS_COMPLEX_CHANGES=1
        rel1="$(normalize_repo_path "$path1")"
        append_unique INPUT_TOUCHED_PATHS "$rel1"
        ;;
    esac
  done < <(git -C "$AI_ASSISTANT_ROOT" diff --name-status --find-renames "$DIFF_RANGE" --)
}

register_path_for_target() {
  local target="$1"
  local rel_path="$2"
  local include_file_path="$3"
  local known_unit
  local flag
  local matched=0
  local selected_match=0
  local disabled_matches=()
  while IFS= read -r known_unit; do
    [[ -n "$known_unit" ]] || continue
    if ! unit_matches_path "$known_unit" "$rel_path"; then
      continue
    fi
    matched=1
    if ! is_unit_selected_for_target "$target" "$known_unit"; then
      flag="$(unit_optional_flag "$known_unit")"
      if [[ -n "$flag" ]]; then
        append_unique disabled_matches "$rel_path -> $known_unit (requires --$flag)"
      else
        append_unique disabled_matches "$rel_path -> $known_unit"
      fi
      continue
    fi
    selected_match=1
    append_unique CANDIDATE_UNITS "$known_unit"
    if [[ "$include_file_path" -eq 1 ]]; then
      append_unique PLAN_FILES "$rel_path"
    fi
  done < <(list_all_units_for_target "$target")

  if [[ "$matched" -eq 1 && "$selected_match" -eq 1 ]]; then
    return 0
  fi

  if [[ "$matched" -eq 1 && "$selected_match" -eq 0 ]]; then
    local disabled_entry
    for disabled_entry in "${disabled_matches[@]-}"; do
      append_unique DISABLED_SCOPE_PATHS "$disabled_entry"
    done
    return 0
  fi

  if [[ "${DEPLOY_IGNORE_UNMANAGED:-0}" -eq 1 ]]; then
    return 0
  fi
  append_unique UNMANAGED_PATHS "$rel_path"
}

assert_path_mapping_clean() {
  if [[ ${#DISABLED_SCOPE_PATHS[@]} -gt 0 ]]; then
    print_list_block "Paths require extra deployment scope flags:" "${DISABLED_SCOPE_PATHS[@]}"
    exit 1
  fi
  if [[ ${#UNMANAGED_PATHS[@]} -gt 0 ]]; then
    print_list_block "Paths are outside managed deployment units:" "${UNMANAGED_PATHS[@]}"
    exit 1
  fi
}

collect_candidate_units_for_target() {
  local target="$1"
  local path
  CANDIDATE_UNITS=()
  UNMANAGED_PATHS=()
  DISABLED_SCOPE_PATHS=()
  for path in "${INPUT_TOUCHED_PATHS[@]}"; do
    register_path_for_target "$target" "$path" 0
  done
  assert_path_mapping_clean
}

collect_file_plan_for_target() {
  local target="$1"
  local path
  CANDIDATE_UNITS=()
  PLAN_FILES=()
  UNMANAGED_PATHS=()
  DISABLED_SCOPE_PATHS=()
  for path in "${INPUT_DEPLOYABLE_FILES[@]}"; do
    register_path_for_target "$target" "$path" 1
  done
  assert_path_mapping_clean
}

collect_touched_plan_for_target() {
  local target="$1"
  local path
  CANDIDATE_UNITS=()
  PLAN_FILES=()
  UNMANAGED_PATHS=()
  DISABLED_SCOPE_PATHS=()
  for path in "${INPUT_TOUCHED_PATHS[@]}"; do
    register_path_for_target "$target" "$path" 1
  done
  assert_path_mapping_clean
}

auto_mode_for_target() {
  local target="$1"
  local unit
  local families=()
  local has_runtime=0
  local has_ops=0
  local change_count="${#INPUT_TOUCHED_PATHS[@]}"
  AUTO_REASONS=()
  if [[ "$change_count" -gt 10 ]]; then
    append_unique AUTO_REASONS "change-count>10"
  fi
  if [[ "$HAS_COMPLEX_CHANGES" -eq 1 ]]; then
    append_unique AUTO_REASONS "diff includes delete or rename"
  fi
  for unit in "${CANDIDATE_UNITS[@]}"; do
    append_unique families "$(unit_family "$unit")"
    case "$unit" in
      ibkr_requirements|ibkr_backtest_requirements|ibkr_runtime_requirements)
        append_unique AUTO_REASONS "requirements changed"
        ;;
      ibkr_systemd|ibkr_backtest_systemd|ibkr_runtime_systemd|gateway_display_systemd|gateway_systemd)
        append_unique AUTO_REASONS "systemd changed"
        ;;
      pb_migrations)
        append_unique AUTO_REASONS "migrations changed"
        ;;
    esac
    case "$(unit_category "$unit")" in
      runtime|systemd)
        has_runtime=1
        ;;
      ops)
        has_ops=1
        ;;
    esac
  done
  if [[ "$target" == "all" && ${#families[@]} -gt 1 ]]; then
    append_unique AUTO_REASONS "cross-service changes"
  fi
  if [[ "$has_runtime" -eq 1 && "$has_ops" -eq 1 ]]; then
    append_unique AUTO_REASONS "runtime and ops changed together"
  fi
  if [[ ${#AUTO_REASONS[@]} -gt 0 ]]; then
    printf '%s\n' package
  else
    printf '%s\n' files
  fi
}

print_deploy_plan() {
  local target="$1"
  local checks=()
  local actions=()
  collect_validator_labels checks "${PLAN_UNITS[@]-}"
  collect_post_action_labels actions "${PLAN_UNITS[@]-}"
  deploy_log "Deployment plan"
  deploy_log "  target: $target"
  deploy_log "  requested mode: $REQUESTED_MODE"
  deploy_log "  final mode: $FINAL_MODE"
  [[ -n "${DIFF_RANGE:-}" ]] && deploy_log "  diff: $DIFF_RANGE"
  [[ -n "${PACKAGE_NAME:-}" ]] && deploy_log "  package name: $PACKAGE_NAME"
  print_list_block "  selected units:" "${SELECTED_UNITS[@]-}"
  if [[ ${#AUTO_REASONS[@]} -gt 0 ]]; then
    print_list_block "  auto reasons:" "${AUTO_REASONS[@]-}"
  fi
  print_list_block "  planned units:" "${PLAN_UNITS[@]-}"
  print_list_block "  planned paths:" "${PLAN_FILES[@]-}"
  print_list_block "  checks:" "${checks[@]-}"
  print_list_block "  post actions:" "${actions[@]-}"
}

expand_implicit_package_units() {
  local target="$1"
  if is_unit_selected_for_target "$target" "ibkr_requirements"; then
    if array_contains "ibkr_src" "${PLAN_UNITS[@]-}" || array_contains "ibkr_systemd" "${PLAN_UNITS[@]-}" || array_contains "ibkr_api_src" "${PLAN_UNITS[@]-}" || array_contains "ibkr_api_systemd" "${PLAN_UNITS[@]-}" || array_contains "ibkr_scheduler_src" "${PLAN_UNITS[@]-}" || array_contains "ibkr_scheduler_systemd" "${PLAN_UNITS[@]-}"; then
      append_unique PLAN_UNITS "ibkr_requirements"
    fi
  fi
  if is_unit_selected_for_target "$target" "ibkr_backtest_requirements"; then
    if array_contains "ibkr_backtest_src" "${PLAN_UNITS[@]-}" || array_contains "ibkr_backtest_systemd" "${PLAN_UNITS[@]-}"; then
      append_unique PLAN_UNITS "ibkr_backtest_requirements"
    fi
  fi
  if is_unit_selected_for_target "$target" "ibkr_runtime_requirements"; then
    if array_contains "ibkr_runtime_src" "${PLAN_UNITS[@]-}" || array_contains "ibkr_runtime_systemd" "${PLAN_UNITS[@]-}"; then
      append_unique PLAN_UNITS "ibkr_runtime_requirements"
    fi
  fi
}

prepare_target_plan() {
  local target="$1"
  init_plan_state
  load_selected_units "$target"
  collect_cli_file_inputs
  collect_diff_inputs
  if [[ "${REQUESTED_MODE:-scope}" == "auto" || "${REQUESTED_MODE:-scope}" == "files" ]]; then
    [[ "$HAS_CHANGE_SOURCE" -eq 1 ]] || deploy_die "--mode $REQUESTED_MODE requires --file or --diff"
  fi
  case "${REQUESTED_MODE:-scope}" in
    scope)
      FINAL_MODE=scope
      PLAN_UNITS=( "${SELECTED_UNITS[@]}" )
      return 0
      ;;
    files)
      if [[ "$HAS_COMPLEX_CHANGES" -eq 1 ]]; then
        deploy_die "--mode files only supports pure add/modify changes. Use --mode package for delete/rename changes."
      fi
      FINAL_MODE=files
      collect_file_plan_for_target "$target"
      PLAN_UNITS=( "${CANDIDATE_UNITS[@]}" )
      if [[ ${#PLAN_FILES[@]} -eq 0 ]]; then
        NO_MANAGED_CHANGES=1
      fi
      return 0
      ;;
    package)
      FINAL_MODE=package
      if [[ "$HAS_CHANGE_SOURCE" -eq 1 ]]; then
        collect_touched_plan_for_target "$target"
        PLAN_UNITS=( "${CANDIDATE_UNITS[@]}" )
        expand_implicit_package_units "$target"
        if [[ ${#PLAN_UNITS[@]} -eq 0 ]]; then
          NO_MANAGED_CHANGES=1
        fi
      else
        PLAN_UNITS=( "${SELECTED_UNITS[@]}" )
      fi
      return 0
      ;;
    auto)
      collect_candidate_units_for_target "$target"
      if [[ ${#CANDIDATE_UNITS[@]} -eq 0 ]]; then
        NO_MANAGED_CHANGES=1
        FINAL_MODE=files
        return 0
      fi
      FINAL_MODE="$(auto_mode_for_target "$target")"
      if [[ "$FINAL_MODE" == "files" ]]; then
        collect_file_plan_for_target "$target"
        PLAN_UNITS=( "${CANDIDATE_UNITS[@]}" )
        if [[ ${#PLAN_FILES[@]} -eq 0 ]]; then
          NO_MANAGED_CHANGES=1
        fi
      else
        collect_touched_plan_for_target "$target"
        PLAN_UNITS=( "${CANDIDATE_UNITS[@]}" )
        expand_implicit_package_units "$target"
      fi
      return 0
      ;;
    *)
      deploy_die "Unsupported planning mode: $REQUESTED_MODE"
      ;;
  esac
}
