#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AI_ASSISTANT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIB_ROOT="$SCRIPT_DIR/lib"
REMOTE_HOST="${MONITORING_DEPLOY_HOST:-${IBKR_DEPLOY_HOST:-root@206.119.171.246}}"
MONITORING_REMOTE_ROOT="${MONITORING_REMOTE_ROOT:-/opt/monitoring}"
SYSTEMD_DIR="${MONITORING_SYSTEMD_DIR:-/etc/systemd/system}"
CADDY_MAIN_PATH="${MONITORING_CADDY_MAIN_PATH:-/etc/caddy/Caddyfile}"
CADDY_CONF_DIR="${MONITORING_CADDY_CONF_DIR:-/etc/caddy/conf.d}"
CADDY_SITE_FILE="${MONITORING_CADDY_SITE_FILE:-$CADDY_CONF_DIR/quant-monitor.lzw-glory.top.caddy}"
CADDY_SERVICE="${MONITORING_CADDY_SERVICE:-caddy}"
PUBLIC_BASE_URL="${MONITORING_PUBLIC_BASE_URL:-https://quant-monitor.lzw-glory.top}"
GRAFANA_ENV_FILE="${GRAFANA_ENV_FILE:-/etc/monitoring/grafana.env}"
LOCAL_MONITORING_ROOT="$AI_ASSISTANT_ROOT/runtime/monitoring"
LOCAL_CADDY_TEMPLATE_PATH="$AI_ASSISTANT_ROOT/ops/templates/caddy/quant-monitor.lzw-glory.top.caddy"
CADDY_RENDERED_PATH=""
DRY_RUN=0
PLAN_ONLY=0
STATUS_ONLY=0
NO_RESTART=0
SKIP_CADDY=0
SKIP_RELOAD=0
SKIP_CHECKS=0
SKIP_HEALTH=0
SKIP_PUBLIC_CHECK=0

source "$LIB_ROOT/common.sh"

cleanup() {
  if [[ -n "$CADDY_RENDERED_PATH" && -f "$CADDY_RENDERED_PATH" ]]; then
    rm -f "$CADDY_RENDERED_PATH"
  fi
}
trap cleanup EXIT

render_caddy_template() {
  CADDY_RENDERED_PATH="$(mktemp "${TMPDIR:-/tmp}/quant-monitor-caddy.XXXXXX")"
  python3 - "$LOCAL_CADDY_TEMPLATE_PATH" "$CADDY_RENDERED_PATH" <<'PY_RENDER_CADDY'
from pathlib import Path
import sys

template_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])
text = template_path.read_text()
target_path.write_text(text)
PY_RENDER_CADDY
}

usage() {
  cat <<EOF_USAGE
Usage: deploy_monitoring_stack.sh [options]

Deploys only the monitoring stack: Prometheus, node_exporter, Grafana
provisioning, and the quant-monitor Caddy site. It does not touch IBKR Gateway,
PocketBase, or application service units.

Options:
  --host <host>          Override SSH target
  --remote-root <path>   Monitoring root on the remote host (default: $MONITORING_REMOTE_ROOT)
  --public-base-url <u>  Public Grafana base URL for health checks (default: $PUBLIC_BASE_URL)
  --dry-run              Show rsync changes without mutating the remote host
  --plan-only            Print the plan and exit
  --status-only          Show monitoring service/file status and exit
  --no-restart           Deploy files but do not restart monitoring services
  --skip-caddy           Do not deploy or reload the public Grafana Caddy site
  --skip-reload          Validate Caddy config but skip Caddy reload
  --skip-checks          Skip remote syntax/config checks
  --skip-health          Skip final monitoring health check
  --skip-public-check    Skip public Grafana login-page health check
  -h, --help             Show this help
EOF_USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      REMOTE_HOST="${2:?missing host}"
      shift 2
      ;;
    --remote-root)
      MONITORING_REMOTE_ROOT="${2:?missing remote root}"
      shift 2
      ;;
    --public-base-url)
      PUBLIC_BASE_URL="${2:?missing public base url}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --plan-only)
      PLAN_ONLY=1
      shift
      ;;
    --status-only)
      STATUS_ONLY=1
      shift
      ;;
    --no-restart)
      NO_RESTART=1
      shift
      ;;
    --skip-caddy)
      SKIP_CADDY=1
      SKIP_PUBLIC_CHECK=1
      shift
      ;;
    --skip-reload)
      SKIP_RELOAD=1
      shift
      ;;
    --skip-checks)
      SKIP_CHECKS=1
      shift
      ;;
    --skip-health)
      SKIP_HEALTH=1
      shift
      ;;
    --skip-public-check)
      SKIP_PUBLIC_CHECK=1
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

if [[ "$NO_RESTART" -eq 1 ]]; then
  SKIP_RELOAD=1
fi

MONITORING_SERVICES=(prometheus.service node-exporter.service grafana-server.service feishu-alert-relay.service)

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  ssh "$REMOTE_HOST" \
    env \
      MONITORING_REMOTE_ROOT="$MONITORING_REMOTE_ROOT" \
      SYSTEMD_DIR="$SYSTEMD_DIR" \
      CADDY_SITE_FILE="$CADDY_SITE_FILE" \
      CADDY_SERVICE="$CADDY_SERVICE" \
      GRAFANA_ENV_FILE="$GRAFANA_ENV_FILE" \
    'bash -s' <<'REMOTE_STATUS'
set -euo pipefail
printf 'remote_root=%s\n' "$MONITORING_REMOTE_ROOT"
printf '%s\n' '--- services ---'
for service in prometheus.service node-exporter.service grafana-server.service feishu-alert-relay.service "$CADDY_SERVICE"; do
  systemctl show "$service" --property=Id,ActiveState,SubState,MainPID,UnitFileState --no-pager || true
  printf '%s\n' '---'
done
printf '%s\n' '--- files ---'
for path in \
  "$MONITORING_REMOTE_ROOT/prometheus/prometheus.yml" \
  "$MONITORING_REMOTE_ROOT/grafana/grafana.ini" \
  "$MONITORING_REMOTE_ROOT/grafana/provisioning/datasources/prometheus.yml" \
  "$MONITORING_REMOTE_ROOT/grafana/provisioning/dashboards/dashboards.yml" \
  "$MONITORING_REMOTE_ROOT/grafana/provisioning/alerting/contact-points.yml" \
  "$MONITORING_REMOTE_ROOT/grafana/provisioning/alerting/notification-policies.yml" \
  "$MONITORING_REMOTE_ROOT/grafana/provisioning/alerting/rules.json" \
  "$MONITORING_REMOTE_ROOT/grafana/dashboards/quant-monitoring-overview.json" \
  "$MONITORING_REMOTE_ROOT/feishu_alert_relay.py" \
  "$SYSTEMD_DIR/prometheus.service" \
  "$SYSTEMD_DIR/node-exporter.service" \
  "$SYSTEMD_DIR/grafana-server.service" \
  "$SYSTEMD_DIR/feishu-alert-relay.service" \
  "$CADDY_SITE_FILE" \
  "$GRAFANA_ENV_FILE"; do
  if [[ -e "$path" ]]; then
    ls -ld "$path"
  else
    printf 'missing:%s\n' "$path"
  fi
done
printf '%s\n' '--- listening-ports ---'
if command -v ss >/dev/null 2>&1; then
  ss -ltnp 2>/dev/null | awk 'NR == 1 || /:3000|:9090|:9100|:9812/' || true
else
  printf 'ss=missing\n'
fi
REMOTE_STATUS
  exit 0
fi

[[ -d "$LOCAL_MONITORING_ROOT/prometheus" ]] || deploy_die "Missing local Prometheus config directory: $LOCAL_MONITORING_ROOT/prometheus"
[[ -d "$LOCAL_MONITORING_ROOT/grafana" ]] || deploy_die "Missing local Grafana config directory: $LOCAL_MONITORING_ROOT/grafana"
[[ -f "$LOCAL_MONITORING_ROOT/feishu_alert_relay.py" ]] || deploy_die "Missing local Feishu alert relay: $LOCAL_MONITORING_ROOT/feishu_alert_relay.py"
for service in "${MONITORING_SERVICES[@]}"; do
  [[ -f "$LOCAL_MONITORING_ROOT/systemd/$service" ]] || deploy_die "Missing local systemd unit: $LOCAL_MONITORING_ROOT/systemd/$service"
done
if [[ "$SKIP_CADDY" -ne 1 ]]; then
  [[ -f "$LOCAL_CADDY_TEMPLATE_PATH" ]] || deploy_die "Missing local caddy template: $LOCAL_CADDY_TEMPLATE_PATH"
fi

deploy_log "Deployment plan"
deploy_log "  target: monitoring stack"
deploy_log "  host: $REMOTE_HOST"
deploy_log "  remote root: $MONITORING_REMOTE_ROOT"
deploy_log "  prometheus config: ${LOCAL_MONITORING_ROOT#$AI_ASSISTANT_ROOT/}/prometheus -> $MONITORING_REMOTE_ROOT/prometheus"
deploy_log "  grafana config: ${LOCAL_MONITORING_ROOT#$AI_ASSISTANT_ROOT/}/grafana -> $MONITORING_REMOTE_ROOT/grafana"
deploy_log "  feishu relay: ${LOCAL_MONITORING_ROOT#$AI_ASSISTANT_ROOT/}/feishu_alert_relay.py -> $MONITORING_REMOTE_ROOT/feishu_alert_relay.py"
deploy_log "  systemd units: ${MONITORING_SERVICES[*]} -> $SYSTEMD_DIR"
deploy_log "  restart monitoring services: $([[ "$NO_RESTART" -eq 1 ]] && printf 'no' || printf 'yes')"
deploy_log "  caddy site: $([[ "$SKIP_CADDY" -eq 1 ]] && printf 'skipped' || printf '%s -> %s' "${LOCAL_CADDY_TEMPLATE_PATH#$AI_ASSISTANT_ROOT/}" "$CADDY_SITE_FILE")"
deploy_log "  reload caddy: $([[ "$SKIP_CADDY" -eq 1 || "$SKIP_RELOAD" -eq 1 ]] && printf 'no' || printf 'yes')"
deploy_log "  public base url: $PUBLIC_BASE_URL"

if [[ "$PLAN_ONLY" -eq 1 ]]; then
  exit 0
fi

ensure_remote_dir "$MONITORING_REMOTE_ROOT"
sync_dir_scope "$LOCAL_MONITORING_ROOT/prometheus" "$MONITORING_REMOTE_ROOT/prometheus"
sync_dir_scope "$LOCAL_MONITORING_ROOT/grafana" "$MONITORING_REMOTE_ROOT/grafana"
sync_file_rsync "$LOCAL_MONITORING_ROOT/feishu_alert_relay.py" "$MONITORING_REMOTE_ROOT/feishu_alert_relay.py"
for service in "${MONITORING_SERVICES[@]}"; do
  sync_file_rsync "$LOCAL_MONITORING_ROOT/systemd/$service" "$SYSTEMD_DIR/$service"
done
if [[ "$SKIP_CADDY" -ne 1 ]]; then
  render_caddy_template
  sync_file_rsync "$CADDY_RENDERED_PATH" "$CADDY_SITE_FILE"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  exit 0
fi

ssh "$REMOTE_HOST" \
  env \
    MONITORING_REMOTE_ROOT="$MONITORING_REMOTE_ROOT" \
    SYSTEMD_DIR="$SYSTEMD_DIR" \
    CADDY_MAIN_PATH="$CADDY_MAIN_PATH" \
    CADDY_CONF_DIR="$CADDY_CONF_DIR" \
    CADDY_SITE_FILE="$CADDY_SITE_FILE" \
    CADDY_SERVICE="$CADDY_SERVICE" \
    GRAFANA_ENV_FILE="$GRAFANA_ENV_FILE" \
    NO_RESTART="$NO_RESTART" \
    SKIP_CADDY="$SKIP_CADDY" \
    SKIP_RELOAD="$SKIP_RELOAD" \
    SKIP_CHECKS="$SKIP_CHECKS" \
  'bash -s' <<'REMOTE_DEPLOY'
set -euo pipefail

ensure_user() {
  local user_name="$1"
  if ! id -u "$user_name" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$user_name"
  fi
}

ensure_caddy_conf_import() {
  local import_line="import ${CADDY_CONF_DIR}/*.caddy"
  if [[ ! -f "$CADDY_MAIN_PATH" ]]; then
    echo "Missing Caddyfile: $CADDY_MAIN_PATH" >&2
    exit 1
  fi
  if ! grep -Fxq "$import_line" "$CADDY_MAIN_PATH"; then
    local backup_path
    backup_path="${CADDY_MAIN_PATH}.bak.$(date -u '+%Y%m%dT%H%M%SZ')"
    cp "$CADDY_MAIN_PATH" "$backup_path"
    printf '\n%s\n' "$import_line" >> "$CADDY_MAIN_PATH"
    printf 'Added Caddy import to %s (backup: %s)\n' "$CADDY_MAIN_PATH" "$backup_path"
  fi
}

ensure_user prometheus
ensure_user node_exporter
install -d -m 0755 "$MONITORING_REMOTE_ROOT" "$MONITORING_REMOTE_ROOT/prometheus" "$MONITORING_REMOTE_ROOT/grafana"
install -d -m 0755 /var/lib/prometheus /var/lib/grafana /var/lib/grafana/plugins /var/log/grafana
chown -R prometheus:prometheus /var/lib/prometheus
if id grafana >/dev/null 2>&1; then
  chown -R grafana:grafana /var/lib/grafana /var/log/grafana
fi
chmod 0644 "$SYSTEMD_DIR/prometheus.service" "$SYSTEMD_DIR/node-exporter.service" "$SYSTEMD_DIR/grafana-server.service" "$SYSTEMD_DIR/feishu-alert-relay.service"
chmod 0755 "$MONITORING_REMOTE_ROOT/feishu_alert_relay.py"

if [[ "$SKIP_CADDY" != "1" ]]; then
  install -d -m 0755 "$CADDY_CONF_DIR"
  chmod 0644 "$CADDY_SITE_FILE"
  ensure_caddy_conf_import
fi

if [[ "$SKIP_CHECKS" != "1" ]]; then
  for binary_path in "$MONITORING_REMOTE_ROOT/bin/prometheus" "$MONITORING_REMOTE_ROOT/bin/promtool" "$MONITORING_REMOTE_ROOT/bin/node_exporter"; do
    if [[ ! -x "$binary_path" ]]; then
      echo "Missing executable: $binary_path. Run ops/bootstrap/install_monitoring_runtime_remote.sh first." >&2
      exit 1
    fi
  done
  if [[ ! -x /usr/share/grafana/bin/grafana ]]; then
    echo "Missing Grafana executable: /usr/share/grafana/bin/grafana. Run ops/bootstrap/install_monitoring_runtime_remote.sh first." >&2
    exit 1
  fi
  if [[ ! -f "$GRAFANA_ENV_FILE" ]]; then
    echo "Missing Grafana env file: $GRAFANA_ENV_FILE. Run ops/bootstrap/install_monitoring_runtime_remote.sh first." >&2
    exit 1
  fi
  "$MONITORING_REMOTE_ROOT/bin/promtool" check config "$MONITORING_REMOTE_ROOT/prometheus/prometheus.yml"
  if command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify "$SYSTEMD_DIR/prometheus.service" "$SYSTEMD_DIR/node-exporter.service" "$SYSTEMD_DIR/grafana-server.service" "$SYSTEMD_DIR/feishu-alert-relay.service"
  fi
  PYTHONPATH=/opt/ibkr_api/src python3 "$MONITORING_REMOTE_ROOT/feishu_alert_relay.py" --help >/dev/null
  if [[ "$SKIP_CADDY" != "1" && -x "$(command -v caddy 2>/dev/null || true)" ]]; then
    caddy validate --config "$CADDY_MAIN_PATH"
  elif [[ "$SKIP_CADDY" != "1" ]]; then
    echo "Missing caddy executable. Install Caddy or re-run with --skip-caddy." >&2
    exit 1
  fi
fi

systemctl daemon-reload
if [[ "$NO_RESTART" != "1" ]]; then
  systemctl enable prometheus.service node-exporter.service grafana-server.service feishu-alert-relay.service >/dev/null
  systemctl restart prometheus.service node-exporter.service grafana-server.service feishu-alert-relay.service
fi

if [[ "$SKIP_CADDY" != "1" && "$SKIP_RELOAD" != "1" ]]; then
  systemctl reload "$CADDY_SERVICE"
fi
REMOTE_DEPLOY

if [[ "$SKIP_HEALTH" -ne 1 && "$NO_RESTART" -ne 1 ]]; then
  health_args=(--host "$REMOTE_HOST" --remote-root "$MONITORING_REMOTE_ROOT" --public-url "$PUBLIC_BASE_URL" --caddy-site-file "$CADDY_SITE_FILE" --grafana-env-file "$GRAFANA_ENV_FILE")
  [[ "$SKIP_PUBLIC_CHECK" -eq 1 ]] && health_args+=(--skip-public)
  [[ "$SKIP_CADDY" -eq 1 ]] && health_args+=(--skip-caddy)
  python3 "$AI_ASSISTANT_ROOT/ops/monitoring/health/check_monitoring_stack.py" "${health_args[@]}"
fi
