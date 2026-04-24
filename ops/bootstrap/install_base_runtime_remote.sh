#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${IBKR_DEPLOY_HOST:-root@206.119.171.246}"
SKIP_CADDY=0
STATUS_ONLY=0

usage() {
  cat <<EOF
Usage: install_base_runtime_remote.sh [options]

Options:
  --host <host>     Override SSH target
  --skip-caddy      Install base packages only
  --status-only     Print installed package/tool versions and exit
  -h, --help        Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --skip-caddy)
      SKIP_CADDY=1
      shift
      ;;
    --status-only)
      STATUS_ONLY=1
      shift
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

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  ssh "$REMOTE_HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
for command_name in python3 pip3 jq rsync sqlite3 curl caddy; do
  printf '== %s ==\n' "$command_name"
  if command -v "$command_name" >/dev/null 2>&1; then
    command -v "$command_name"
    case "$command_name" in
      python3) python3 --version ;;
      pip3) pip3 --version ;;
      jq) jq --version ;;
      rsync)
        version_output="$(rsync --version 2>/dev/null || true)"
        printf '%s\n' "${version_output%%$'\n'*}"
        ;;
      sqlite3) sqlite3 --version ;;
      curl)
        version_output="$(curl --version 2>/dev/null || true)"
        printf '%s\n' "${version_output%%$'\n'*}"
        ;;
      caddy) caddy version ;;
    esac
  else
    printf 'missing\n'
  fi
  printf '%s\n' '---'
done
REMOTE
  exit 0
fi

ssh "$REMOTE_HOST" bash -s -- "$SKIP_CADDY" <<'REMOTE'
set -euo pipefail

skip_caddy="$1"

base_packages=(
  apt-transport-https
  ca-certificates
  curl
  debian-archive-keyring
  debian-keyring
  gnupg
  jq
  python3
  python3-pip
  python3-venv
  rsync
  sqlite3
  unzip
  xz-utils
)

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends "${base_packages[@]}"

if [[ "$skip_caddy" != "1" ]]; then
  install -m 0755 -d /usr/share/keyrings /etc/apt/sources.list.d
  rm -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1fsSL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    -o /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
  systemctl enable caddy >/dev/null 2>&1 || true
fi

python3 --version
if command -v caddy >/dev/null 2>&1; then
  caddy version
fi
REMOTE
