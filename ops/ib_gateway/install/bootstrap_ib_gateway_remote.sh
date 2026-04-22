#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.136}"
REMOTE_ROOT="${IBKR_RUNTIME_REMOTE_ROOT:-${IBKR_DEPLOY_RUNTIME_ROOT:-/opt/ibkr_runtime}}"
IBG_INSTALL_ROOT="${IBKR_BOOTSTRAP_IBG_ROOT:-/opt/ibgateway}"
IBC_HOME="${IBKR_BOOTSTRAP_IBC_HOME:-/opt/ibc}"
DISPLAY_VALUE="${IBKR_BOOTSTRAP_DISPLAY:-:1}"
DISPLAY_SCREEN="${IBKR_BOOTSTRAP_DISPLAY_SCREEN:-1280x800x24}"
API_HOST="${IBKR_BOOTSTRAP_IBGW_HOST:-127.0.0.1}"
API_PORT="${IBKR_BOOTSTRAP_IBGW_PORT:-4001}"
CLIENT_ID="${IBKR_BOOTSTRAP_IBGW_CLIENT_ID:-31}"
INSTALLER_URL="${IBKR_BOOTSTRAP_INSTALLER_URL:-https://download2.interactivebrokers.com/installers/ibgateway/latest-standalone/ibgateway-latest-standalone-linux-x64.sh}"
IBC_URL="${IBKR_BOOTSTRAP_IBC_URL:-}"

usage() {
  cat <<EOF
Usage: bootstrap_ib_gateway_remote.sh [options]

Options:
  --host <host>             Override SSH target
  --remote-root <path>      Remote ibkr_runtime root (default: /opt/ibkr_runtime)
  --ibg-root <path>         Remote IB Gateway install root (default: /opt/ibgateway)
  --ibc-home <path>         Remote IBC install root (default: /opt/ibc)
  --display <display>       X display value (default: :1)
  --screen <spec>           Xvfb screen spec (default: 1280x800x24)
  --api-host <host>         IB socket host stored in .env (default: 127.0.0.1)
  --api-port <port>         IB socket port stored in .env (default: 4001)
  --client-id <id>          IB socket client id stored in .env (default: 31)
  --installer-url <url>     Override IB Gateway Linux installer URL
  --ibc-url <url>           Override IBC Linux zip URL
  -h, --help                Show this help
EOF
}

resolve_latest_ibc_url() {
  python3 - <<'PY'
import json
import urllib.request

url = "https://api.github.com/repos/IbcAlpha/IBC/releases/latest"
with urllib.request.urlopen(url, timeout=20) as response:
    data = json.load(response)
for asset in data.get("assets", []):
    name = asset.get("name", "")
    if name.startswith("IBCLinux-") and name.endswith(".zip"):
        print(asset["browser_download_url"])
        break
else:
    raise SystemExit("Unable to resolve latest IBCLinux release asset")
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --remote-root)
      REMOTE_ROOT="${2:?missing remote root}"
      shift 2
      ;;
    --ibg-root)
      IBG_INSTALL_ROOT="${2:?missing ibg root}"
      shift 2
      ;;
    --ibc-home)
      IBC_HOME="${2:?missing ibc home}"
      shift 2
      ;;
    --display)
      DISPLAY_VALUE="${2:?missing display}"
      shift 2
      ;;
    --screen)
      DISPLAY_SCREEN="${2:?missing screen}"
      shift 2
      ;;
    --api-host)
      API_HOST="${2:?missing api host}"
      shift 2
      ;;
    --api-port)
      API_PORT="${2:?missing api port}"
      shift 2
      ;;
    --client-id)
      CLIENT_ID="${2:?missing client id}"
      shift 2
      ;;
    --installer-url)
      INSTALLER_URL="${2:?missing installer url}"
      shift 2
      ;;
    --ibc-url)
      IBC_URL="${2:?missing ibc url}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$IBC_URL" ]]; then
  IBC_URL="$(resolve_latest_ibc_url)"
fi

echo "Bootstrap target: $REMOTE_HOST"
echo "Remote root: $REMOTE_ROOT"
echo "IB Gateway installer: $INSTALLER_URL"
echo "IBC package: $IBC_URL"

ssh "$REMOTE_HOST" bash -s -- \
  "$REMOTE_ROOT" \
  "$IBG_INSTALL_ROOT" \
  "$IBC_HOME" \
  "$DISPLAY_VALUE" \
  "$DISPLAY_SCREEN" \
  "$API_HOST" \
  "$API_PORT" \
  "$CLIENT_ID" \
  "$INSTALLER_URL" \
  "$IBC_URL" <<'SH'
set -euo pipefail

remote_root="$1"
ibg_install_root="$2"
ibc_home="$3"
display_value="$4"
display_screen="$5"
api_host="$6"
api_port="$7"
client_id="$8"
installer_url="$9"
ibc_url="${10}"

env_file="${remote_root}/.env"
config_file="${ibc_home}/config.ini"
user_dir="${ibg_install_root}/userdir"
log_dir="${remote_root}/logs/ibgateway"
timestamp="$(date +%Y%m%d%H%M%S)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

log() {
  printf '[bootstrap] %s\n' "$*"
}

require_file() {
  local path="$1"
  [[ -f "$path" ]] || {
    echo "Required file is missing: $path" >&2
    exit 1
  }
}

install_packages() {
  local packages=(
    ca-certificates
    curl
    libnss3
    libx11-6
    libxcb1
    libxext6
    libxi6
    libxrandr2
    libxrender1
    libxtst6
    procps
    psmisc
    unzip
    xvfb
  )
  local audio_pkg=""
  if apt-cache show libasound2t64 >/dev/null 2>&1; then
    audio_pkg="libasound2t64"
  elif apt-cache show libasound2 >/dev/null 2>&1; then
    audio_pkg="libasound2"
  fi
  if [[ -n "$audio_pkg" ]]; then
    packages+=("$audio_pkg")
  fi
  log "Installing system packages"
  DEBIAN_FRONTEND=noninteractive apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
}

discover_gateway_layout() {
  local root="$1"
  local version_dir=""
  local base=""
  local version=""

  if [[ -d "$root/jars" ]]; then
    local ibgateway_dir="$root"
    while IFS= read -r jar_path; do
      [[ -f "$jar_path" ]] || continue
      version="$(basename "$jar_path")"
      version="${version#jts4launch-}"
      version="${version#twslaunch-}"
      version="${version%.jar}"
      [[ "$version" =~ ^[0-9]+$ ]] || continue
      if [[ "$(basename "$ibgateway_dir")" != "ibgateway" ]]; then
        ln -sfn "$ibgateway_dir" "$(dirname "$ibgateway_dir")/ibgateway"
        ibgateway_dir="$(dirname "$ibgateway_dir")/ibgateway"
      fi
      ln -sfn "$ibgateway_dir" "$ibgateway_dir/$version"
      printf '%s\n%s\n' "$(dirname "$ibgateway_dir")" "$version"
      return 0
    done < <(find "$root/jars" -maxdepth 1 -type f \( -name 'jts4launch-*.jar' -o -name 'twslaunch-*.jar' \) | sort)
  fi

  while IFS= read -r candidate; do
    [[ -d "$candidate/jars" ]] || continue
    version_dir="$candidate"
    break
  done < <(find "$root" -type d -path '*/ibgateway/[0-9][0-9][0-9][0-9]*' | sort)

  if [[ -n "$version_dir" ]]; then
    version="$(basename "$version_dir")"
    base="${version_dir%/ibgateway/$version}"
    printf '%s\n%s\n' "$base" "$version"
    return 0
  fi

  while IFS= read -r candidate; do
    [[ -d "$candidate/jars" ]] || continue
    version_dir="$candidate"
    break
  done < <(find "$root" -mindepth 1 -maxdepth 4 -type d -regex '.*/[0-9][0-9][0-9][0-9]+' | sort)

  if [[ -n "$version_dir" ]]; then
    version="$(basename "$version_dir")"
    base="$(dirname "$version_dir")"
    mkdir -p "$base/ibgateway"
    ln -sfn "$version_dir" "$base/ibgateway/$version"
    printf '%s\n%s\n' "$base" "$version"
    return 0
  fi

  return 1
}

install_ib_gateway() {
  local installer="${tmp_dir}/ibgateway-installer.sh"
  local java_bin=""
  local java_home=""
  java_bin="$(readlink -f "$(command -v java)")"
  java_home="$(dirname "$(dirname "$java_bin")")"

  log "Downloading IB Gateway installer"
  curl -fsSL -o "$installer" "$installer_url"
  chmod +x "$installer"

  mkdir -p "$ibg_install_root"
  log "Installing IB Gateway into $ibg_install_root"
  INSTALL4J_JAVA_HOME_OVERRIDE="$java_home" "$installer" -q -console -overwrite -dir "$ibg_install_root"
}

install_ibc() {
  local archive="${tmp_dir}/ibc.zip"
  local extract_dir="${tmp_dir}/ibc"

  log "Downloading IBC package"
  curl -fsSL -o "$archive" "$ibc_url"
  rm -rf "$extract_dir"
  mkdir -p "$extract_dir"
  unzip -q "$archive" -d "$extract_dir"

  rm -rf "$ibc_home"
  mkdir -p "$ibc_home"
  cp -R "$extract_dir"/. "$ibc_home"/
  find "$ibc_home" -type d -exec chmod 755 {} +
  find "$ibc_home" -type f -exec chmod 644 {} +
  find "$ibc_home" -path '*/scripts/*' -type f -exec chmod 755 {} +
  [[ -f "$ibc_home/gatewaystart.sh" ]] && chmod 755 "$ibc_home/gatewaystart.sh"
  [[ -f "$ibc_home/twsstart.sh" ]] && chmod 755 "$ibc_home/twsstart.sh"
}

backup_env() {
  require_file "$env_file"
  cp "$env_file" "${env_file}.bak.${timestamp}"
}

migrate_env() {
  local detected_ibg_home="$1"
  local detected_tws_version="$2"
  python3 - "$env_file" "$remote_root" "$detected_ibg_home" "$user_dir" "$log_dir" "$display_value" "$display_screen" "$api_host" "$api_port" "$client_id" "$ibc_home" "$config_file" "$detected_tws_version" <<'PY'
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
remote_root = sys.argv[2]
detected_ibg_home = sys.argv[3]
user_dir = sys.argv[4]
log_dir = sys.argv[5]
display_value = sys.argv[6]
display_screen = sys.argv[7]
api_host = sys.argv[8]
api_port = sys.argv[9]
client_id = sys.argv[10]
ibc_home = sys.argv[11]
config_file = sys.argv[12]
detected_tws_version = sys.argv[13]

data = {}
order = []
for raw_line in env_path.read_text().splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = raw_line.split("=", 1)
    key = key.strip()
    data[key] = value
    if key not in order:
        order.append(key)

username = data.get("IBKR_USERNAME", "").strip()
password = data.get("IBKR_PASSWORD", "").strip()
if not username or not password:
    raise SystemExit("IBKR_USERNAME and IBKR_PASSWORD must exist in the remote .env before bootstrap")

trading_mode = (data.get("IBKR_GATEWAY_TRADING_MODE") or data.get("IBKR_ENVIRONMENT") or "live").strip() or "live"
twofa_action = (data.get("IBKR_2FA_TIMEOUT_ACTION") or "exit").strip() or "exit"
gateway_tz = (data.get("IBKR_GATEWAY_TZ") or "America/New_York").strip() or "America/New_York"
auto_restart_time = data.get("IBKR_AUTO_RESTART_TIME", "08:05 PM")
if auto_restart_time is None:
    auto_restart_time = "08:05 PM"
auto_restart_time = str(auto_restart_time).strip()
java_home = data.get("IBKR_GATEWAY_JAVA_HOME", "").strip()
if java_home in {
    "/usr/lib/jvm/java-17-openjdk-amd64",
    "/usr/lib/jvm/java-17-openjdk-amd64/bin",
}:
    java_home = ""

updates = {
    "IBKR_LOGIN_TIMEOUT": data.get("IBKR_LOGIN_TIMEOUT", "180"),
    "IBKR_2FA_WAIT": data.get("IBKR_2FA_WAIT", "180"),
    "IBGW_HOST": api_host,
    "IBGW_PORT": api_port,
    "IBGW_CLIENT_ID": client_id,
    "IBKR_GATEWAY_SYSTEMD_SERVICE": "ibkr-gateway",
    "IBKR_DISPLAY_SYSTEMD_SERVICE": "ibkr-display",
    "IBKR_GATEWAY_COOKIE_FILE": data.get("IBKR_GATEWAY_COOKIE_FILE", "/tmp/ibkr_gateway_cookies.json"),
    "IBKR_DISPLAY": display_value,
    "IBKR_DISPLAY_SCREEN": display_screen,
    "IBKR_IBC_HOME": ibc_home,
    "IBKR_IBC_INI": config_file,
    "IBKR_IBG_HOME": detected_ibg_home,
    "IBKR_IBG_USER_DIR": user_dir,
    "IBKR_TWS_SETTINGS_PATH": user_dir,
    "IBKR_TWS_MAJOR_VERSION": detected_tws_version,
    "IBKR_GATEWAY_TZ": gateway_tz,
    "IBKR_AUTO_RESTART_TIME": auto_restart_time,
    "IBKR_GATEWAY_TRADING_MODE": trading_mode,
    "IBKR_2FA_TIMEOUT_ACTION": twofa_action,
    "IBKR_GATEWAY_JAVA_HOME": java_home,
    "IBKR_GATEWAY_LOG_DIR": log_dir,
}

remove_keys = {
    "IBKR_GATEWAY_URL",
    "IBKR_GATEWAY_DIR",
    "IBKR_GATEWAY_CONF",
    "IBKR_GATEWAY_JAVA_OPTS",
}

for key in remove_keys:
    data.pop(key, None)

data.update(updates)

preferred_order = [
    "IBKR_USERNAME",
    "IBKR_PASSWORD",
    "IBKR_ACCOUNT_ID",
    "IBKR_PAPER_ACCOUNT_ID",
    "IBKR_ENVIRONMENT",
    "IBKR_LOGIN_TIMEOUT",
    "IBKR_2FA_WAIT",
    "IBKR_EOD_KEEP_SYMBOLS",
    "IBGW_HOST",
    "IBGW_PORT",
    "IBGW_CLIENT_ID",
    "IBKR_GATEWAY_SYSTEMD_SERVICE",
    "IBKR_DISPLAY_SYSTEMD_SERVICE",
    "IBKR_GATEWAY_COOKIE_FILE",
    "IBKR_DISPLAY",
    "IBKR_DISPLAY_SCREEN",
    "IBKR_IBC_HOME",
    "IBKR_IBC_INI",
    "IBKR_IBG_HOME",
    "IBKR_IBG_USER_DIR",
    "IBKR_TWS_SETTINGS_PATH",
    "IBKR_TWS_MAJOR_VERSION",
    "IBKR_GATEWAY_TZ",
    "IBKR_AUTO_RESTART_TIME",
    "IBKR_GATEWAY_TRADING_MODE",
    "IBKR_2FA_TIMEOUT_ACTION",
    "IBKR_GATEWAY_JAVA_HOME",
    "IBKR_GATEWAY_LOG_DIR",
    "PB_BASE_URL",
    "PB_PUBLIC_URL",
    "PORT",
]

lines = []
seen = set()
for key in preferred_order:
    if key in data:
        lines.append(f"{key}={data[key]}")
        seen.add(key)
for key in order:
    if key in data and key not in seen:
        lines.append(f"{key}={data[key]}")
        seen.add(key)
for key in sorted(data):
    if key not in seen:
        lines.append(f"{key}={data[key]}")

env_path.write_text("\n".join(lines) + "\n")
PY
  chmod 600 "$env_file"
}

render_ibc_config() {
  python3 - "$env_file" "$config_file" <<'PY'
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
config_path = Path(sys.argv[2])
data = {}
for raw_line in env_path.read_text().splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = raw_line.split("=", 1)
    data[key.strip()] = value

username = data.get("IBKR_USERNAME", "").strip()
password = data.get("IBKR_PASSWORD", "").strip()
if not username or not password:
    raise SystemExit("IBKR_USERNAME and IBKR_PASSWORD must exist before writing config.ini")

trading_mode = (data.get("IBKR_GATEWAY_TRADING_MODE") or data.get("IBKR_ENVIRONMENT") or "live").strip() or "live"
second_factor_timeout = (data.get("IBKR_2FA_WAIT") or "180").strip() or "180"
api_port = (data.get("IBGW_PORT") or "4001").strip() or "4001"
user_dir = (data.get("IBKR_TWS_SETTINGS_PATH") or data.get("IBKR_IBG_USER_DIR") or "").strip()
relogin = "yes" if (data.get("IBKR_2FA_TIMEOUT_ACTION", "exit").strip().lower() == "restart") else "no"
trusted_ips = data.get("IBKR_TRUSTED_API_CLIENT_IPS", "").strip()
auto_restart_time = data.get("IBKR_AUTO_RESTART_TIME", "08:05 PM")
if auto_restart_time is None:
    auto_restart_time = "08:05 PM"
auto_restart_time = str(auto_restart_time).strip()

lines = [
    "IbLoginId=" + username,
    "IbPassword=" + password,
    "TradingMode=" + trading_mode,
    "StoreSettingsOnServer=no",
    "AcceptNonBrokerageAccountWarning=yes",
    "DismissPasswordExpiryWarning=no",
    "ExistingSessionDetectedAction=primaryoverride",
    "ReloginAfterSecondFactorAuthenticationTimeout=" + relogin,
    "SecondFactorAuthenticationTimeout=" + second_factor_timeout,
    "OverrideTwsApiPort=" + api_port,
    "ReadOnlyApi=no",
    "AcceptIncomingConnectionAction=accept",
    "AllowBlindTrading=yes",
    "TrustedTwsApiClientIPs=" + trusted_ips,
    "AutoRestartTime=" + auto_restart_time,
    "IbDir=" + user_dir,
]

config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text("\n".join(lines) + "\n")
PY
  chmod 600 "$config_file"
}

install_packages
backup_env
install_ib_gateway
install_ibc

mapfile -t gateway_layout < <(discover_gateway_layout "$ibg_install_root")
if [[ "${#gateway_layout[@]}" -ne 2 ]]; then
  echo "Unable to locate an installed IB Gateway version under $ibg_install_root" >&2
  exit 1
fi

detected_ibg_home="${gateway_layout[0]}"
detected_tws_version="${gateway_layout[1]}"

mkdir -p "$remote_root" "$user_dir" "$log_dir"
migrate_env "$detected_ibg_home" "$detected_tws_version"
render_ibc_config

log "IB Gateway home: $detected_ibg_home"
log "IB Gateway major version: $detected_tws_version"
log "IBC config written to: $config_file"
log "Remote .env migrated and backed up at: ${env_file}.bak.${timestamp}"
SH
