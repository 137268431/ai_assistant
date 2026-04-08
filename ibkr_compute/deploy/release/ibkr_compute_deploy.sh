#!/bin/bash
set -Eeuo pipefail

# ====================== 配置区 ======================
REMOTE_HOST="root@206.119.171.136"
REMOTE_DIR="/opt/ibkr_compute"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SERVICE_NAME="ibkr-compute"
VENV_DIR="$REMOTE_DIR/venv"
PORT=5100
PUBLIC_BASE_URL="${IBKR_COMPUTE_PUBLIC_URL:-https://ibkr-compute.lzw-glory.top}"
LOCAL_BASE_URL="http://localhost:${PORT}"
CADDY_DOMAIN="${IBKR_COMPUTE_DOMAIN:-ibkr-compute.lzw-glory.top}"
CADDY_SITE_FILE="/etc/caddy/conf.d/ibkr-compute.caddy"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/ibkr_compute_deploy_$(date +%Y%m%d_%H%M%S).log"
ENABLE_FILE_LOG="${IBKR_COMPUTE_DEPLOY_ENABLE_FILE_LOG:-0}"
# ====================================================

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

safe_pause_exit() {
    if [ -t 0 ]; then
        read -r -p "按回车键退出..." _ || true
    else
        echo -e "${YELLOW}无交互终端，3秒后自动退出...${NC}"
        sleep 3
    fi
}

on_error() {
    local exit_code="${1:-1}"
    local line_no="${2:-unknown}"
    local failed_command="${FAILED_COMMAND:-unknown}"

    trap - ERR

    echo ""
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}❌ 脚本执行失败${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  退出码:   ${exit_code}"
    echo -e "  行号:     ${line_no}"
    echo -e "  命令:     ${failed_command}"
    if [ "${ENABLE_FILE_LOG}" = "1" ]; then
        echo -e "  日志文件: ${LOG_FILE}"
        echo ""
        echo -e "${YELLOW}可直接查看：${NC}"
        echo -e "  tail -n 80 ${LOG_FILE}"
        echo -e "  sed -n '1,200p' ${LOG_FILE}"
    fi

    if [ -t 0 ]; then
        echo ""
        safe_pause_exit
    fi

    exit "$exit_code"
}

LOG_FILE="${IBKR_COMPUTE_DEPLOY_LOG_FILE:-$LOG_FILE}"
if [ "${ENABLE_FILE_LOG}" = "1" ] && [ "${IBKR_COMPUTE_DEPLOY_LOGGING:-0}" != "1" ]; then
    mkdir -p "$LOG_DIR"
    touch "$LOG_FILE"
    set +e
    IBKR_COMPUTE_DEPLOY_ENABLE_FILE_LOG=1 IBKR_COMPUTE_DEPLOY_LOGGING=1 IBKR_COMPUTE_DEPLOY_LOG_FILE="$LOG_FILE" bash "$0" "$@" 2>&1 | tee -a "$LOG_FILE"
    script_status=${PIPESTATUS[0]}
    exit "$script_status"
fi

trap 'exit_code=$?; FAILED_COMMAND=$BASH_COMMAND; on_error "$exit_code" "$LINENO"' ERR

if [ -n "${TERM:-}" ]; then
    clear 2>/dev/null || true
fi
echo -e "${GREEN}=============================================${NC}"
echo -e "${GREEN}        IBKR Compute 一键部署脚本              ${NC}"
echo -e "${GREEN}=============================================${NC}"
echo ""
if [ "${ENABLE_FILE_LOG}" = "1" ]; then
    echo -e "${YELLOW}📝 本次日志文件:${NC} ${LOG_FILE}"
    echo ""
else
    echo -e "${YELLOW}📝 文件日志:${NC} 关闭"
    echo -e "${YELLOW}   如需开启:${NC} IBKR_COMPUTE_DEPLOY_ENABLE_FILE_LOG=1 bash $0"
    echo ""
fi

if [ ! -f "$PROJECT_DIR/src/ibkr_compute/api/server.py" ]; then
    echo -e "${RED}❌ 未找到 src/ibkr_compute/api/server.py，请在 ibkr_compute 项目中执行${NC}"
    exit 1
fi

# 选择操作
echo -e "${YELLOW}🚀 请选择操作：${NC}"
echo "1) 首次部署 (创建目录+venv+安装依赖+上传+注册服务+启动)"
echo "2) 更新代码 (上传文件+重启服务)"
echo "3) 仅上传代码 (不重启)"
echo "4) 仅重启服务"
echo "5) 查看服务状态/日志"
if [ -t 0 ]; then
    read -r -p "请输入数字 [1-5]: " action_choice || action_choice="5"
else
    echo -e "${YELLOW}检测到无交互终端，默认执行 [5] 查看服务状态/日志${NC}"
    action_choice="5"
fi
echo ""

json_field() {
    local json="${1:-}"
    local key="${2:-}"
    local default_value="${3:-}"

    if [ -z "$json" ] || [ -z "$key" ]; then
        printf "%s" "$default_value"
        return 0
    fi

    JSON_INPUT="$json" python3 - "$key" "$default_value" <<'PY' 2>/dev/null || printf "%s" "$default_value"
import json
import os
import sys

key = sys.argv[1]
default = sys.argv[2]

try:
    data = json.loads(os.environ.get("JSON_INPUT", ""))
    value = data
    for part in key.split("."):
        if isinstance(value, dict):
            value = value.get(part, default)
        else:
            value = default
            break
    if value is None:
        value = default
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False))
    else:
        print(value)
except Exception:
    print(default)
PY
}

fetch_remote_json() {
    local url="$1"
    ssh "$REMOTE_HOST" "curl -ksS --connect-timeout 5 '$url' 2>/dev/null" || true
}

preview_text() {
    local value="${1:-}"

    if [ -z "$value" ]; then
        return 0
    fi

    printf "%s" "$value" | tr '\r\n\t' '   ' | tr -s ' ' | cut -c1-160
}

show_health_result() {
    local label="$1"
    local url="$2"
    local health_json="$3"
    local status
    local ok
    local engines
    local uptime
    local computes
    local errors
    local error_msg
    local proxy_source
    local raw_preview

    if [ -z "$health_json" ]; then
        echo -e "  ${label}: ${RED}● 未响应${NC}"
        echo -e "    URL: ${url}"
        return 1
    fi

    status=$(json_field "$health_json" "status" "")
    ok=$(json_field "$health_json" "ok" "")
    engines=$(json_field "$health_json" "engines" "-")
    uptime=$(json_field "$health_json" "uptime_s" "-")
    computes=$(json_field "$health_json" "compute_count" "-")
    errors=$(json_field "$health_json" "error_count" "-")
    error_msg=$(json_field "$health_json" "error" "")
    proxy_source=$(json_field "$health_json" "proxy_source" "")
    raw_preview=$(preview_text "$health_json")

    if [ "$status" = "running" ] || [ "$ok" = "True" ] || [ "$ok" = "true" ]; then
        echo -e "  ${label}: ${GREEN}● 正常${NC}"
    else
        echo -e "  ${label}: ${RED}● 异常${NC} (${status:-unknown})"
    fi
    echo -e "    URL: ${url}"
    echo -e "    engines=${engines}, uptime=${uptime}s, compute=${computes}, errors=${errors}"
    if [ -n "$proxy_source" ]; then
        echo -e "    proxy_source=${proxy_source}"
    fi
    if [ -n "$error_msg" ]; then
        echo -e "    error=${error_msg}"
    elif [ -z "$status" ] && [ -n "$raw_preview" ]; then
        echo -e "    raw=${raw_preview}"
    fi
    return 0
}

show_service_summary() {
    local service_status
    local caddy_status
    local service_enabled
    local main_pid
    local active_since
    local local_health_json
    local public_health_json
    local local_status
    local startup_ok="false"

    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}📋 当前服务状态摘要${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    service_status=$(ssh "$REMOTE_HOST" "systemctl is-active $SERVICE_NAME 2>/dev/null" || echo "unknown")
    caddy_status=$(ssh "$REMOTE_HOST" "if command -v caddy >/dev/null 2>&1; then systemctl is-active caddy 2>/dev/null || echo inactive; else echo not-installed; fi" || echo "unknown")
    service_enabled=$(ssh "$REMOTE_HOST" "systemctl is-enabled $SERVICE_NAME 2>/dev/null" || echo "unknown")
    main_pid=$(ssh "$REMOTE_HOST" "systemctl show -p MainPID --value $SERVICE_NAME 2>/dev/null" || echo "")
    active_since=$(ssh "$REMOTE_HOST" "systemctl show -p ActiveEnterTimestamp --value $SERVICE_NAME 2>/dev/null" || echo "")

    if [ "$service_status" = "active" ]; then
        echo -e "  systemd:   ${GREEN}● active${NC}"
    else
        echo -e "  systemd:   ${RED}● ${service_status}${NC}"
    fi
    if [ "$caddy_status" = "active" ]; then
        echo -e "  caddy:     ${GREEN}● active${NC}"
    else
        echo -e "  caddy:     ${YELLOW}● ${caddy_status}${NC}"
    fi
    echo -e "  开机自启:  ${service_enabled}"
    [ -n "$main_pid" ] && echo -e "  MainPID:   ${main_pid}"
    [ -n "$active_since" ] && echo -e "  启动时间:  ${active_since}"
    echo ""

    sleep 2
    local_health_json=$(fetch_remote_json "${LOCAL_BASE_URL}/health")
    public_health_json=$(fetch_remote_json "${PUBLIC_BASE_URL}/health")
    local_status=$(json_field "$local_health_json" "status" "")

    echo -e "${YELLOW}📋 IBKR Compute 健康检查：${NC}"
    show_health_result "本机直连" "${LOCAL_BASE_URL}/health" "$local_health_json" || true
    show_health_result "公网域名" "${PUBLIC_BASE_URL}/health" "$public_health_json" || true
    echo ""

    if [ "$service_status" = "active" ] && [ "$local_status" = "running" ]; then
        startup_ok="true"
        echo -e "  当前服务: ${GREEN}● 启动正常${NC}"
    else
        echo -e "  当前服务: ${RED}● 启动异常${NC}"
    fi
    echo -e "  判定条件: systemd=active 且 ${LOCAL_BASE_URL}/health 返回 status=running"
}

clean_local_artifacts() {
    echo -e "${YELLOW}🧹 清理本地 Python 缓存...${NC}"
    local found_any="0"

    while IFS= read -r path; do
        [ -n "$path" ] || continue
        echo -e "  [Local] 删除: $path"
        found_any="1"
    done < <(find "$PROJECT_DIR/src" \( -type f -name '*.pyc' -o -type d -name '__pycache__' \) | sort)

    if [ "$found_any" != "1" ]; then
        echo -e "  [Local] 无缓存残留"
    fi

    find "$PROJECT_DIR/src" -type f -name '*.pyc' -delete 2>/dev/null || true
    find "$PROJECT_DIR/src" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
}

# 上传Python文件
upload_code() {
    clean_local_artifacts

    echo -e "${YELLOW}🗑️ 清空远端代码目录...${NC}"
    ssh "$REMOTE_HOST" "
        mkdir -p '$REMOTE_DIR'
        for path in \
            '$REMOTE_DIR/src' \
            '$REMOTE_DIR/requirements.txt' \
            '$REMOTE_DIR/server.py' \
            '$REMOTE_DIR/pb_client.py' \
            '$REMOTE_DIR/config.py' \
            '$REMOTE_DIR/indicator_engine.py' \
            '$REMOTE_DIR/signal_generator.py' \
            '$REMOTE_DIR/daily_scanner.py'
        do
            if [ -e \"\$path\" ]; then
                echo \"[Remote] 删除: \$path\"
            fi
        done
        rm -rf '$REMOTE_DIR/src'
        rm -f '$REMOTE_DIR/requirements.txt' '$REMOTE_DIR/server.py' '$REMOTE_DIR/pb_client.py' '$REMOTE_DIR/config.py' '$REMOTE_DIR/indicator_engine.py' '$REMOTE_DIR/signal_generator.py' '$REMOTE_DIR/daily_scanner.py'
    "

    echo -e "${YELLOW}📤 上传代码文件...${NC}"
    scp -rp "$PROJECT_DIR/src" \
            "$PROJECT_DIR/requirements.txt" \
           "$REMOTE_HOST:$REMOTE_DIR/"
    echo -e "${GREEN}✅ 代码上传完成${NC}"
}

sync_remote_service() {
    echo -e "${YELLOW}⚙️ 同步 systemd 服务定义...${NC}"
    ssh $REMOTE_HOST "cat > /etc/systemd/system/${SERVICE_NAME}.service << 'EOF'
[Unit]
Description=IBKR Compute Indicator Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$REMOTE_DIR
Environment=PYTHONPATH=$REMOTE_DIR/src
ExecStart=$VENV_DIR/bin/python -m ibkr_compute.api.server
Restart=always
RestartSec=5
Environment=FLASK_ENV=production
Environment=PB_BASE_URL=http://localhost:8090

[Install]
WantedBy=multi-user.target
EOF"
}

ensure_remote_venv() {
    echo -e "${YELLOW}🐍 检查远端 Python 环境...${NC}"
    ssh "$REMOTE_HOST" "
        set -e
        mkdir -p '$REMOTE_DIR'

        install_venv_support() {
            if ! command -v apt-get >/dev/null 2>&1; then
                echo '[Remote] 当前系统没有 apt-get，无法自动安装 python venv 依赖'
                return 1
            fi

            py_venv_pkg=\$(python3 - <<'PY'
import sys
print(f'python{sys.version_info.major}.{sys.version_info.minor}-venv')
PY
)

            echo \"[Remote] 安装系统依赖: \${py_venv_pkg} python3-venv\"
            apt-get update
            if ! apt-get install -y \"\${py_venv_pkg}\" python3-venv; then
                echo \"[Remote] 安装 \${py_venv_pkg} 失败，回退仅安装 python3-venv\"
                apt-get install -y python3-venv
            fi
        }

        ensure_pip_ready() {
            [ -x '$VENV_DIR/bin/python' ] || return 1

            if '$VENV_DIR/bin/python' -m pip --version >/dev/null 2>&1; then
                return 0
            fi

            '$VENV_DIR/bin/python' -m ensurepip --upgrade >/dev/null 2>&1 || true
            '$VENV_DIR/bin/python' -m pip --version >/dev/null 2>&1
        }

        recreate_venv() {
            rm -rf '$VENV_DIR'
            python3 -m venv '$VENV_DIR'
        }

        if [ ! -x '$VENV_DIR/bin/python' ]; then
            echo '[Remote] 未检测到可用 venv，开始创建...'
            if ! python3 -m venv '$VENV_DIR'; then
                echo '[Remote] python3 -m venv 失败，尝试安装系统依赖...'
                install_venv_support
                recreate_venv
            fi
        fi

        if ! ensure_pip_ready; then
            echo '[Remote] 检测到残缺 venv，重建虚拟环境...'
            if ! recreate_venv; then
                echo '[Remote] 重建 venv 失败，尝试安装系统依赖...'
                install_venv_support
                recreate_venv
            fi
        fi

        if ! ensure_pip_ready; then
            echo '[Remote] venv 仍缺少 pip，安装系统依赖后再次重建...'
            install_venv_support
            recreate_venv
            ensure_pip_ready
        fi

        '$VENV_DIR/bin/python' -m pip --version
    "
    echo -e "${GREEN}✅ 远端 Python 环境就绪${NC}"
}

install_remote_requirements() {
    echo -e "${YELLOW}📦 检查依赖...${NC}"
    ssh "$REMOTE_HOST" "
        set -e
        cd '$REMOTE_DIR'

        missing_summary=\$(
            '$VENV_DIR/bin/python' - <<'PY'
from importlib import metadata
from pip._vendor.packaging.requirements import Requirement

missing = []

with open('requirements.txt', 'r', encoding='utf-8') as fh:
    for raw in fh:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue

        req = Requirement(line)
        try:
            installed = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            missing.append(f'{req.name}: missing, need {req.specifier or \"any\"}')
            continue

        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            missing.append(f'{req.name}: installed {installed}, need {req.specifier}')

if missing:
    print('\n'.join(missing))
    raise SystemExit(1)
PY
        ) || true

        if [ -z \"\$missing_summary\" ]; then
            echo '[Remote] requirements 已满足，跳过安装'
            exit 0
        fi

        echo '[Remote] 检测到缺失/版本不满足依赖:'
        printf '%s\n' \"\$missing_summary\"
        '$VENV_DIR/bin/python' -m pip install --disable-pip-version-check -r requirements.txt -q
        echo '[Remote] 依赖安装完成'
    "
    echo -e "${GREEN}✅ 依赖检查完成${NC}"
}

ensure_remote_caddy() {
    echo -e "${YELLOW}🌐 检查 Caddy 反向代理...${NC}"
    ssh "$REMOTE_HOST" "
        set -e
        changed=0

        if ! command -v caddy >/dev/null 2>&1; then
            echo '[Remote] 未安装 caddy，跳过域名反代配置'
            exit 0
        fi

        mkdir -p /etc/caddy/conf.d
        chmod 755 /etc/caddy /etc/caddy/conf.d >/dev/null 2>&1 || true

        if [ -f /etc/caddy/Caddyfile ] && grep -Fq '$CADDY_DOMAIN' /etc/caddy/Caddyfile && ! grep -Fq 'import /etc/caddy/conf.d/*.caddy' /etc/caddy/Caddyfile; then
            echo '[Remote] /etc/caddy/Caddyfile 已存在 ibkr-compute.lzw-glory.top，请手动确认 reverse_proxy 指向 127.0.0.1:$PORT'
        else
            if [ ! -f /etc/caddy/Caddyfile ]; then
                cat > /etc/caddy/Caddyfile <<'EOF'
import /etc/caddy/conf.d/*.caddy
EOF
                changed=1
            elif ! grep -Fq 'import /etc/caddy/conf.d/*.caddy' /etc/caddy/Caddyfile; then
                printf '\nimport /etc/caddy/conf.d/*.caddy\n' >> /etc/caddy/Caddyfile
                changed=1
            fi

            tmp_site_file=\$(mktemp)
            cat > \"\$tmp_site_file\" <<'EOF'
$CADDY_DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:$PORT
}
EOF
            caddy fmt --overwrite \"\$tmp_site_file\" >/dev/null 2>&1 || true

            if [ -f '$CADDY_SITE_FILE' ] && cmp -s \"\$tmp_site_file\" '$CADDY_SITE_FILE'; then
                rm -f \"\$tmp_site_file\"
                echo '[Remote] Caddy 站点配置未变化，跳过写入'
            else
                mv \"\$tmp_site_file\" '$CADDY_SITE_FILE'
                changed=1
                echo '[Remote] Caddy 站点配置已更新'
            fi
        fi

        if [ -f /etc/caddy/Caddyfile ] && [ \"\$(stat -c '%a' /etc/caddy/Caddyfile)\" != '644' ]; then
            chmod 644 /etc/caddy/Caddyfile
            changed=1
            echo '[Remote] 已修正 /etc/caddy/Caddyfile 权限为 644'
        fi

        if [ -f '$CADDY_SITE_FILE' ] && [ \"\$(stat -c '%a' '$CADDY_SITE_FILE')\" != '644' ]; then
            chmod 644 '$CADDY_SITE_FILE'
            changed=1
            echo '[Remote] 已修正 Caddy 站点文件权限为 644'
        fi

        if [ \"\$changed\" != '1' ]; then
            echo '[Remote] Caddy 配置无变化，跳过 reload'
            exit 0
        fi

        caddy fmt --overwrite /etc/caddy/Caddyfile >/dev/null 2>&1 || true
        [ -f '$CADDY_SITE_FILE' ] && caddy fmt --overwrite '$CADDY_SITE_FILE' >/dev/null 2>&1 || true
        caddy validate --config /etc/caddy/Caddyfile

        if systemctl cat caddy >/dev/null 2>&1; then
            systemctl enable caddy >/dev/null 2>&1 || true
            systemctl reload caddy || systemctl restart caddy
        fi
    "
    echo -e "${GREEN}✅ Caddy 检查完成${NC}"
}

# 首次部署
first_deploy() {
    echo -e "${YELLOW}📁 创建远程目录...${NC}"
    ssh $REMOTE_HOST "mkdir -p $REMOTE_DIR"

    upload_code

    ensure_remote_venv
    install_remote_requirements

    sync_remote_service
    ssh $REMOTE_HOST "systemctl daemon-reload && systemctl enable $SERVICE_NAME && systemctl start $SERVICE_NAME"
    ensure_remote_caddy

    echo -e "${GREEN}✅ 首次部署完成${NC}"
    echo ""
    ssh $REMOTE_HOST "systemctl status $SERVICE_NAME --no-pager -l" || true
}

# 更新代码+重启
update_and_restart() {
    upload_code

    ensure_remote_venv
    install_remote_requirements
    sync_remote_service

    echo -e "${YELLOW}🔄 重启服务...${NC}"
    ssh $REMOTE_HOST "systemctl daemon-reload && systemctl restart $SERVICE_NAME"

    echo -e "${GREEN}✅ 更新部署完成${NC}"
    echo ""
    ssh $REMOTE_HOST "systemctl status $SERVICE_NAME --no-pager -l" || true
}

# 执行
case $action_choice in
    1)
        first_deploy
        ;;
    2)
        update_and_restart
        ;;
    3)
        upload_code
        ;;
    4)
        echo -e "${YELLOW}🔄 重启服务...${NC}"
        ssh $REMOTE_HOST "systemctl restart $SERVICE_NAME"
        ssh $REMOTE_HOST "systemctl status $SERVICE_NAME --no-pager -l" || true
        ;;
    5)
        echo -e "${YELLOW}📋 服务状态：${NC}"
        ssh $REMOTE_HOST "systemctl status $SERVICE_NAME --no-pager -l" || true
        echo ""
        echo -e "${YELLOW}📋 最近日志 (20行)：${NC}"
        ssh $REMOTE_HOST "journalctl -u $SERVICE_NAME --no-pager -n 20" || true
        echo ""
        echo -e "${YELLOW}📋 健康检查：${NC}"
        ssh $REMOTE_HOST "curl -s http://localhost:5100/health 2>/dev/null | python3 -m json.tool" || echo -e "${RED}服务未响应${NC}"
        ;;
    *)
        echo -e "${RED}❌ 输入错误${NC}"; exit 1
        ;;
esac

echo ""
show_service_summary

echo ""
echo -e "${GREEN}=============================================${NC}"
echo -e "${GREEN}🎉 操作完成${NC}"
echo -e "${GREEN}=============================================${NC}"
echo ""
echo -e "${YELLOW}📡 服务端点 (直连, 仅内网):${NC}"
echo -e "  健康检查:  ${LOCAL_BASE_URL}/health"
echo -e "  引擎状态:  ${LOCAL_BASE_URL}/status"
echo -e "  触发计算:  POST ${LOCAL_BASE_URL}/compute"
echo -e "  触发扫描:  POST ${LOCAL_BASE_URL}/scan"
echo -e "  全量重算:  POST ${LOCAL_BASE_URL}/recompute"
echo ""
echo -e "${YELLOW}📡 公网直连域名:${NC}"
echo -e "  健康检查:  ${PUBLIC_BASE_URL}/health"
echo -e "  引擎状态:  ${PUBLIC_BASE_URL}/status"
echo -e "  触发计算:  POST ${PUBLIC_BASE_URL}/compute"
echo -e "  触发扫描:  POST ${PUBLIC_BASE_URL}/scan"
echo -e "  全量重算:  POST ${PUBLIC_BASE_URL}/recompute"
echo ""
echo -e "${YELLOW}🛠 排错查看:${NC}"
echo -e "  远端服务状态: ssh ${REMOTE_HOST} \"systemctl status ${SERVICE_NAME} --no-pager -l\""
echo -e "  远端最近日志: ssh ${REMOTE_HOST} \"journalctl -u ${SERVICE_NAME} --no-pager -n 100\""
echo -e "  持续跟踪日志: ssh ${REMOTE_HOST} \"journalctl -u ${SERVICE_NAME} -f\""
if [ "${ENABLE_FILE_LOG}" = "1" ]; then
    echo -e "  本地部署日志: tail -n 80 ${LOG_FILE}"
fi
echo ""
if [ "${ENABLE_FILE_LOG}" = "1" ]; then
    echo -e "${YELLOW}📝 完整日志:${NC} ${LOG_FILE}"
    echo ""
fi
safe_pause_exit
