#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${IBKR_DISPLAY:-:1}"

ibc_home="${IBKR_IBC_HOME:-/opt/ibc}"
ibc_ini="${IBKR_IBC_INI:-/opt/ibc/config.ini}"
tws_path="${IBKR_IBG_HOME:-/opt}"
tws_settings="${IBKR_TWS_SETTINGS_PATH:-${IBKR_IBG_USER_DIR:-/opt/ibgateway/userdir}}"
trading_mode="${IBKR_GATEWAY_TRADING_MODE:-live}"
twofa_timeout="${IBKR_2FA_TIMEOUT_ACTION:-exit}"
java_path="${IBKR_GATEWAY_JAVA_HOME:-}"
log_dir="${IBKR_GATEWAY_LOG_DIR:-/opt/ibkr_compute/logs/ibgateway}"
tws_version="${IBKR_TWS_MAJOR_VERSION:-}"

mkdir -p "$tws_settings" "$log_dir"

if [[ -n "$java_path" && ! -x "$java_path/java" && -x "$java_path/bin/java" ]]; then
  java_path="$java_path/bin"
fi

if [[ -d "$tws_path/jars" ]]; then
  install_root="$tws_path"
  for jar in "$install_root"/jars/jts4launch-*.jar "$install_root"/jars/twslaunch-*.jar; do
    [[ -f "$jar" ]] || continue
    parsed_version="$(basename "$jar")"
    parsed_version="${parsed_version#jts4launch-}"
    parsed_version="${parsed_version#twslaunch-}"
    parsed_version="${parsed_version%.jar}"
    [[ "$parsed_version" =~ ^[0-9]+$ ]] || continue
    tws_version="${tws_version:-$parsed_version}"
    ln -sfn "$install_root" "$install_root/$parsed_version"
    tws_path="$(dirname "$install_root")"
    break
  done
fi

if [[ -z "$tws_version" ]]; then
  for candidate in "$tws_path"/ibgateway/[0-9][0-9][0-9][0-9] "$tws_path"/[0-9][0-9][0-9][0-9]; do
    if [[ -d "$candidate/jars" ]]; then
      tws_version="$(basename "$candidate")"
      if [[ "$candidate" == "$tws_path"/[0-9][0-9][0-9][0-9] ]]; then
        mkdir -p "$tws_path/ibgateway"
        ln -sfn "$candidate" "$tws_path/ibgateway/$tws_version"
      fi
      break
    fi
  done
fi

if [[ -z "$tws_version" ]]; then
  echo "Unable to determine IB Gateway major version under $tws_path" >&2
  exit 1
fi

args=(
  "$ibc_home/scripts/ibcstart.sh"
  "$tws_version"
  --gateway
  "--tws-path=$tws_path"
  "--tws-settings-path=$tws_settings"
  "--ibc-path=$ibc_home"
  "--ibc-ini=$ibc_ini"
  "--mode=$trading_mode"
  "--on2fatimeout=$twofa_timeout"
)

if [[ -n "$java_path" ]]; then
  args+=( "--java-path=$java_path" )
fi

exec "${args[@]}"
