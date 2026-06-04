#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${MONITORING_DEPLOY_HOST:-${IBKR_DEPLOY_HOST:-root@206.119.171.246}}"
PROMETHEUS_VERSION="${PROMETHEUS_VERSION:-2.54.1}"
NODE_EXPORTER_VERSION="${NODE_EXPORTER_VERSION:-1.8.2}"
MONITORING_REMOTE_ROOT="${MONITORING_REMOTE_ROOT:-/opt/monitoring}"
GRAFANA_ENV_FILE="${GRAFANA_ENV_FILE:-/etc/monitoring/grafana.env}"
STATUS_ONLY=0
SKIP_GRAFANA=0
SKIP_PROMETHEUS=0
SKIP_NODE_EXPORTER=0

usage() {
  cat <<EOF_USAGE
Usage: install_monitoring_runtime_remote.sh [options]

Installs the monitoring runtime dependencies on the remote host without touching
IBKR Gateway, PocketBase, or application data.

Options:
  --host <host>                Override SSH target
  --prometheus-version <ver>   Prometheus version to install (default: $PROMETHEUS_VERSION)
  --node-exporter-version <v>  node_exporter version to install (default: $NODE_EXPORTER_VERSION)
  --remote-root <path>         Monitoring root on the remote host (default: $MONITORING_REMOTE_ROOT)
  --grafana-env-file <path>    Grafana secret env file (default: $GRAFANA_ENV_FILE)
  --skip-grafana              Skip Grafana package install and secret seeding
  --skip-prometheus           Skip Prometheus binary install
  --skip-node-exporter        Skip node_exporter binary install
  --status-only               Print installed package/tool status and exit
  -h, --help                  Show this help
EOF_USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --prometheus-version)
      PROMETHEUS_VERSION="${2:?missing version}"
      shift 2
      ;;
    --node-exporter-version)
      NODE_EXPORTER_VERSION="${2:?missing version}"
      shift 2
      ;;
    --remote-root)
      MONITORING_REMOTE_ROOT="${2:?missing remote root}"
      shift 2
      ;;
    --grafana-env-file)
      GRAFANA_ENV_FILE="${2:?missing grafana env file}"
      shift 2
      ;;
    --skip-grafana)
      SKIP_GRAFANA=1
      shift
      ;;
    --skip-prometheus)
      SKIP_PROMETHEUS=1
      shift
      ;;
    --skip-node-exporter)
      SKIP_NODE_EXPORTER=1
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
  ssh "$REMOTE_HOST" env MONITORING_REMOTE_ROOT="$MONITORING_REMOTE_ROOT" GRAFANA_ENV_FILE="$GRAFANA_ENV_FILE" 'bash -s' <<'REMOTE_STATUS'
set -euo pipefail
printf 'remote_root=%s\n' "$MONITORING_REMOTE_ROOT"
printf 'grafana_env_file=%s\n' "$GRAFANA_ENV_FILE"
printf '%s\n' '--- tools ---'
for command_name in curl tar gpg apt-get systemctl; do
  if command -v "$command_name" >/dev/null 2>&1; then
    printf '%s=%s\n' "$command_name" "$(command -v "$command_name")"
  else
    printf '%s=missing\n' "$command_name"
  fi
done
printf '%s\n' '--- binaries ---'
for binary_path in "$MONITORING_REMOTE_ROOT/bin/prometheus" "$MONITORING_REMOTE_ROOT/bin/promtool" "$MONITORING_REMOTE_ROOT/bin/node_exporter" /usr/share/grafana/bin/grafana; do
  if [[ -x "$binary_path" ]]; then
    printf '%s\n' "$binary_path"
    "$binary_path" --version 2>&1 | sed -n '1,3p' || true
  else
    printf 'missing:%s\n' "$binary_path"
  fi
  printf '%s\n' '---'
done
printf '%s\n' '--- users ---'
for user_name in prometheus node_exporter grafana; do
  if id "$user_name" >/dev/null 2>&1; then
    id "$user_name"
  else
    printf 'missing:%s\n' "$user_name"
  fi
done
printf '%s\n' '--- grafana-env ---'
if [[ -f "$GRAFANA_ENV_FILE" ]]; then
  ls -l "$GRAFANA_ENV_FILE"
  grep -E '^(GF_SECURITY_ADMIN_USER|GF_SECURITY_ADMIN_PASSWORD|GF_SECURITY_SECRET_KEY)=' "$GRAFANA_ENV_FILE" | sed -E 's/=(.*)$/=<redacted>/' || true
else
  printf 'missing:%s\n' "$GRAFANA_ENV_FILE"
fi
REMOTE_STATUS
  exit 0
fi

ssh "$REMOTE_HOST" \
  env \
    PROMETHEUS_VERSION="$PROMETHEUS_VERSION" \
    NODE_EXPORTER_VERSION="$NODE_EXPORTER_VERSION" \
    MONITORING_REMOTE_ROOT="$MONITORING_REMOTE_ROOT" \
    GRAFANA_ENV_FILE="$GRAFANA_ENV_FILE" \
    SKIP_GRAFANA="$SKIP_GRAFANA" \
    SKIP_PROMETHEUS="$SKIP_PROMETHEUS" \
    SKIP_NODE_EXPORTER="$SKIP_NODE_EXPORTER" \
  'bash -s' <<'REMOTE_INSTALL'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

base_packages=(
  apt-transport-https
  ca-certificates
  curl
  gnupg
  openssl
  tar
  gzip
)

apt-get update -y
apt-get install -y --no-install-recommends "${base_packages[@]}"

case "$(uname -m)" in
  x86_64|amd64)
    artifact_arch="amd64"
    ;;
  aarch64|arm64)
    artifact_arch="arm64"
    ;;
  *)
    echo "Unsupported architecture for Prometheus binaries: $(uname -m)" >&2
    exit 1
    ;;
esac

install -d -m 0755 "$MONITORING_REMOTE_ROOT/bin" "$MONITORING_REMOTE_ROOT/tmp" /var/lib/prometheus /etc/monitoring

ensure_user() {
  local user_name="$1"
  if ! id -u "$user_name" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$user_name"
  fi
}

install_prometheus() {
  local archive="prometheus-${PROMETHEUS_VERSION}.linux-${artifact_arch}.tar.gz"
  local url="https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}/${archive}"
  local tmp_dir
  tmp_dir="$(mktemp -d "$MONITORING_REMOTE_ROOT/tmp/prometheus.XXXXXX")"
  curl -fsSL "$url" -o "$tmp_dir/$archive"
  tar -xzf "$tmp_dir/$archive" -C "$tmp_dir"
  install -m 0755 "$tmp_dir/prometheus-${PROMETHEUS_VERSION}.linux-${artifact_arch}/prometheus" "$MONITORING_REMOTE_ROOT/bin/prometheus"
  install -m 0755 "$tmp_dir/prometheus-${PROMETHEUS_VERSION}.linux-${artifact_arch}/promtool" "$MONITORING_REMOTE_ROOT/bin/promtool"
  rm -rf "$tmp_dir"
}

install_node_exporter() {
  local archive="node_exporter-${NODE_EXPORTER_VERSION}.linux-${artifact_arch}.tar.gz"
  local url="https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/${archive}"
  local tmp_dir
  tmp_dir="$(mktemp -d "$MONITORING_REMOTE_ROOT/tmp/node-exporter.XXXXXX")"
  curl -fsSL "$url" -o "$tmp_dir/$archive"
  tar -xzf "$tmp_dir/$archive" -C "$tmp_dir"
  install -m 0755 "$tmp_dir/node_exporter-${NODE_EXPORTER_VERSION}.linux-${artifact_arch}/node_exporter" "$MONITORING_REMOTE_ROOT/bin/node_exporter"
  rm -rf "$tmp_dir"
}

install_grafana() {
  install -d -m 0755 /etc/apt/keyrings
  rm -f /etc/apt/keyrings/grafana.gpg
  curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
  chmod 0644 /etc/apt/keyrings/grafana.gpg
  cat > /etc/apt/sources.list.d/grafana.list <<'EOF_GRAFANA_REPO'
deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main
EOF_GRAFANA_REPO
  apt-get update -y
  apt-get install -y --no-install-recommends grafana
}

seed_grafana_env() {
  local env_dir
  env_dir="$(dirname "$GRAFANA_ENV_FILE")"
  install -d -m 0750 "$env_dir"
  if [[ ! -f "$GRAFANA_ENV_FILE" ]]; then
    {
      printf 'GF_SECURITY_ADMIN_USER=admin\n'
      printf 'GF_SECURITY_ADMIN_PASSWORD=%s\n' "$(openssl rand -hex 24 2>/dev/null || python3 - <<'PY'
import secrets
print(secrets.token_hex(24))
PY
)"
      printf 'GF_SECURITY_SECRET_KEY=%s\n' "$(openssl rand -hex 32 2>/dev/null || python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
    } > "$GRAFANA_ENV_FILE"
    chmod 0640 "$GRAFANA_ENV_FILE"
  fi
  if getent group grafana >/dev/null 2>&1; then
    chgrp grafana "$GRAFANA_ENV_FILE" || true
  fi
}

ensure_user prometheus
ensure_user node_exporter

if [[ "$SKIP_PROMETHEUS" != "1" ]]; then
  install_prometheus
fi

if [[ "$SKIP_NODE_EXPORTER" != "1" ]]; then
  install_node_exporter
fi

if [[ "$SKIP_GRAFANA" != "1" ]]; then
  install_grafana
  seed_grafana_env
fi

chown -R prometheus:prometheus /var/lib/prometheus
chmod 0755 "$MONITORING_REMOTE_ROOT" "$MONITORING_REMOTE_ROOT/bin"

printf '%s\n' 'Monitoring runtime install complete.'
if [[ -x "$MONITORING_REMOTE_ROOT/bin/prometheus" ]]; then
  "$MONITORING_REMOTE_ROOT/bin/prometheus" --version 2>&1 | sed -n '1p'
fi
if [[ -x "$MONITORING_REMOTE_ROOT/bin/node_exporter" ]]; then
  "$MONITORING_REMOTE_ROOT/bin/node_exporter" --version 2>&1 | sed -n '1p'
fi
if [[ -x /usr/share/grafana/bin/grafana ]]; then
  /usr/share/grafana/bin/grafana --version 2>&1 | sed -n '1p'
fi
REMOTE_INSTALL
