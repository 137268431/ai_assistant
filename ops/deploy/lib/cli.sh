#!/usr/bin/env bash

validate_mode_value() {
  case "$1" in
    scope|files|package|auto)
      ;;
    *)
      deploy_die "Unknown mode: $1"
      ;;
  esac
}
