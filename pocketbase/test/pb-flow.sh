#!/bin/bash
#
# PocketBase API 交互式测试脚本
# 按执行流程测试：信号流 / 订单流 / 逆向信号流
# 信号和订单状态由 PB 定时调度自动处理
#

set -e

# ============================================================
# 配置
# ============================================================
BASE_URL="${PB_BASE_URL:-https://pb.lzw-glory.top}"
TEST_SYMBOL="${TEST_SYMBOL:-AAPL}"
TODAY=$(date +%Y-%m-%d)
TEST_DATE="${TODAY}"
TEST_TIME=$(date +%H:%M:%S)
TEST_DIRECTION="long"  # 默认做多

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

# ============================================================
# 工具函数
# ============================================================

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

cache_set() {
    local key=$1
    local value=$2
    printf "%s" "$value" > "/tmp/${key}"
}

cache_get() {
    local key=$1
    cat "/tmp/${key}" 2>/dev/null || true
}

clear_order_relation_cache() {
    rm -f /tmp/pb_trade_group_id /tmp/pb_entry_* /tmp/pb_tp_* /tmp/pb_sl_* 2>/dev/null || true
}

seed_order_relation_cache() {
    local sig_id=$1
    local base="test_${sig_id}"
    local entry_unique_id="${base}_entry"
    cache_set "pb_trade_group_id" "${entry_unique_id}"
    cache_set "pb_entry_unique_id" "${entry_unique_id}"
    cache_set "pb_entry_broker_id" "IB_${base}_entry"
    cache_set "pb_tp_unique_id" "${base}_take_profit"
    cache_set "pb_tp_broker_id" "IB_${base}_take_profit"
    cache_set "pb_sl_unique_id" "${base}_stop_loss"
    cache_set "pb_sl_broker_id" "IB_${base}_stop_loss"
    cache_set "pb_ord_id" "${entry_unique_id}"
    cache_set "pb_ord_latest" "${entry_unique_id}"
}

ensure_order_relation_cache() {
    local sig_id
    sig_id=$(cache_get "pb_sig_latest")
    [ -z "$sig_id" ] && sig_id=$(cache_get "pb_sig_id")
    [ -z "$sig_id" ] && return 1

    local entry_unique_id
    entry_unique_id=$(cache_get "pb_entry_unique_id")
    if [ -z "$entry_unique_id" ]; then
        seed_order_relation_cache "$sig_id"
    fi
}

# 将北京时间转换为美国东部时间日期
get_us_date() {
    local cn_datetime="${TEST_DATE} ${TEST_TIME}"
    local ts=$(TZ=Asia/Shanghai date -j -f "%Y-%m-%d %H:%M:%S" "${cn_datetime}" +%s 2>/dev/null) || ts=$(date +%s)
    TZ=America/New_York date -r ${ts} +%Y-%m-%d 2>/dev/null || echo "${TEST_DATE}"
}

# 执行 curl 请求（仅执行，返回结果）
do_curl() {
    local method=$1
    local url=$2
    local data=$3

    if [ -n "$data" ]; then
        curl -s -X ${method} "${url}" -H "Content-Type: application/json" -d "${data}"
    else
        curl -s -X ${method} "${url}"
    fi
}

# 打印并执行 curl
curl_exec() {
    local method=$1
    local url=$2
    local data=$3
    local label=${4:-""}

    echo -e "${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" >&2
    [ -n "$label" ] && echo -e "${MAGENTA}📤 ${label}${NC}" >&2 || echo -e "${MAGENTA}📤 CURL 请求:${NC}" >&2
    echo -e "  ${CYAN}curl -X ${method} ${url}${NC}" >&2
    if [ -n "$data" ]; then
        echo -e "  ${CYAN}Body:${NC}" >&2
        echo "$data" | jq '.' 2>/dev/null >&2 || echo "$data" >&2
    fi
    echo -e "${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" >&2

    do_curl "$method" "$url" "$data"
}

show_banner() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║          PocketBase API 交互式测试脚本               ║${NC}"
    echo -e "${CYAN}║          信号/订单/逆向信号 完整流程测试              ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${CYAN}API地址:${NC}   ${GREEN}${BASE_URL}${NC}"
    echo -e "  ${CYAN}测试时间:${NC}   ${GREEN}${TEST_DATE} ${TEST_TIME}${NC}"
    echo -e "  ${CYAN}测试标的:${NC}   ${GREEN}${TEST_SYMBOL}${NC}"
    echo -e "  ${CYAN}测试方向:${NC}   ${GREEN}${TEST_DIRECTION}${NC}"
    echo ""
}

show_menu() {
    echo ""
    echo -e "${CYAN}╔═══════════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║                        📡 信号流程                                  ║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║${NC}  ${MAGENTA}[1]${NC} 发送信号到PB      ${CYAN}│${NC}  ${MAGENTA}[2]${NC} 查询信号状态(pending)         ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  ${MAGENTA}[3]${NC} 飞书-确认信号    ${CYAN}│${NC}  ${MAGENTA}[4]${NC} 飞书-拒绝信号             ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  ${MAGENTA}[5]${NC} QC确认信号→创建交易组Init(signals/ack)                         ${CYAN}║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║                        📦 订单流程                                  ║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║${NC}  ${MAGENTA}[6]${NC} QC同步Entry Submitted ${CYAN}│${NC}  ${MAGENTA}[7]${NC} Entry Filled+激活TP/SL    ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  ${MAGENTA}[8]${NC} TP成交+SL取消     ${CYAN}│${NC}  ${MAGENTA}[9]${NC} SL成交+TP取消          ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  ${MAGENTA}[O]${NC} 飞书-取消订单    ${CYAN}│${NC}  ${MAGENTA}[P]${NC} 飞书-平仓订单           ${CYAN}║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║                        ⚡ 逆向信号                                  ║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║${NC}  ${MAGENTA}[A]${NC} 计算逆向信号    ${CYAN}│${NC}  ${MAGENTA}[B]${NC} 查询逆向信号               ${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}  ${MAGENTA}[C]${NC} 确认逆向信号                                                      ${CYAN}║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║                        📊 指标数据                                  ║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║${NC}  ${MAGENTA}[D]${NC} 发送指标数据(webhook/tv indicator)                              ${CYAN}║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║                        🔧 工具                                      ║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║${NC}  ${CYAN}[E]${NC} 查询测试信号  ${CYAN}│${NC}  ${CYAN}[F]${NC} 查询测试订单  ${CYAN}│${NC}  ${YELLOW}[X]${NC} 清理测试数据    ${CYAN}║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║                        ⚙️ 设置                                      ║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║${NC}  ${GREEN}[G]${NC} 设置测试时间  ${CYAN}│${NC}  ${GREEN}[S]${NC} 设置测试标的  ${CYAN}│${NC}  ${GREEN}[T]${NC} 设置测试方向      ${CYAN}║${NC}"
    echo -e "${CYAN}╠═══════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║                        ${RED}[Q] 退出${NC}                                    ║${NC}"
    echo -e "${CYAN}╚═══════════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -n "输入选项后按 Enter 确认: "
}

# 生成测试数据
generate_test_data() {
    local datetime="${TEST_DATE} ${TEST_TIME}"
    # 使用北京时间生成时间戳
    local ts=$(TZ=Asia/Shanghai date -j -f "%Y-%m-%d %H:%M:%S" "${datetime}" +%s 2>/dev/null) || ts=$(date +%s)
    local timestamp_ms=${ts}000
    local time_str=$(date -j -f "%Y-%m-%d %H:%M:%S" "${datetime}" +%H%M%S 2>/dev/null || date +%H%M%S)
    # cn_time = 北京时间
    local cn_time="${TEST_DATE} ${TEST_TIME}"
    # us_time = 美国东部时间 (自动处理夏令时/冬令时，13/12小时时差)
    local us_time=$(TZ=America/New_York date -r ${ts} +"%Y-%m-%d %H:%M:%S" 2>/dev/null || echo "${TEST_DATE} ${TEST_TIME}")
    local signal_id="${TEST_SYMBOL}_${TEST_DATE//-/}_${time_str}_sig"

    local base_price=100
    local entry_price=$(echo "scale=2; $base_price + $RANDOM % 50" | bc 2>/dev/null || echo "100.00")
    local stop_loss
    local take_profit
    local limit_price
    if [ "${TEST_DIRECTION}" = "short" ]; then
        stop_loss=$(echo "scale=2; $entry_price * 1.02" | bc 2>/dev/null || echo "102.00")
        take_profit=$(echo "scale=2; $entry_price * 0.95" | bc 2>/dev/null || echo "95.00")
        limit_price=$(echo "scale=2; $entry_price * 0.999" | bc 2>/dev/null || echo "${entry_price}")
    else
        stop_loss=$(echo "scale=2; $entry_price * 0.98" | bc 2>/dev/null || echo "98.00")
        take_profit=$(echo "scale=2; $entry_price * 1.05" | bc 2>/dev/null || echo "105.00")
        limit_price=$(echo "scale=2; $entry_price * 1.001" | bc 2>/dev/null || echo "${entry_price}")
    fi

    echo "${signal_id}|${entry_price}|${stop_loss}|${take_profit}|${timestamp_ms}|${limit_price}|${us_time}|${cn_time}"
}

build_phase_time() {
    local offset_min=${1:-0}
    local datetime="${TEST_DATE} ${TEST_TIME}"
    local base_ts=$(TZ=Asia/Shanghai date -j -f "%Y-%m-%d %H:%M:%S" "${datetime}" +%s 2>/dev/null || date -d "${datetime}" +%s 2>/dev/null || date +%s)
    local phase_ts=$((base_ts + offset_min * 60))
    local timestamp_ms=$((phase_ts * 1000))
    local cn_time=$(TZ=Asia/Shanghai date -r ${phase_ts} +"%Y-%m-%d %H:%M:%S" 2>/dev/null || TZ=Asia/Shanghai date -d "@${phase_ts}" +"%Y-%m-%d %H:%M:%S" 2>/dev/null || echo "${TEST_DATE} ${TEST_TIME}")
    local us_time=$(TZ=America/New_York date -r ${phase_ts} +"%Y-%m-%d %H:%M:%S" 2>/dev/null || TZ=America/New_York date -d "@${phase_ts}" +"%Y-%m-%d %H:%M:%S" 2>/dev/null || echo "${TEST_DATE} ${TEST_TIME}")
    echo "${timestamp_ms}|${us_time}|${cn_time}"
}

# ============================================================
# 辅助函数
# ============================================================

# 按日期检查是否存在测试数据
check_today_test_data() {
    local us_date=$(get_us_date)

    # 查询信号
    local sig_response=$(curl_exec "GET" "${BASE_URL}/api/collections/signals/records?filter=(script_tag~'test'||signal_id~'_sig')&&date='${us_date}'&perPage=100" "" "检查-查询信号")
    local sig_count=$(echo "$sig_response" | jq '.items | length' 2>/dev/null || echo "0")

    # 查询订单
    local ord_response=$(curl_exec "GET" "${BASE_URL}/api/collections/orders/records?filter=(unique_id~'_sig'||unique_id~'_test')&&symbol='${TEST_SYMBOL}'&perPage=100" "" "检查-查询订单")
    local ord_count=$(echo "$ord_response" | jq '.items | length' 2>/dev/null || echo "0")

    # 查询订单详情
    local det_response=$(curl_exec "GET" "${BASE_URL}/api/collections/order_details/records?filter=(order_id~'_sig'||order_id~'_test')&perPage=100" "" "检查-查询订单详情")
    local det_count=$(echo "$det_response" | jq '.items | length' 2>/dev/null || echo "0")

    # 查询反转信号
    local rev_response=$(curl_exec "GET" "${BASE_URL}/api/collections/reverse_signals/records?filter=date='${us_date}'&perPage=100" "" "检查-查询反转信号")
    local rev_count=$(echo "$rev_response" | jq '.items | length' 2>/dev/null || echo "0")

    if [ "$sig_count" -gt 0 ] || [ "$ord_count" -gt 0 ] || [ "$det_count" -gt 0 ] || [ "$rev_count" -gt 0 ]; then
        echo ""
        echo -e "${YELLOW}发现 ${TEST_SYMBOL} 今日测试数据 (US日期: ${us_date}):${NC}"
        [ "$sig_count" -gt 0 ] && echo -e "  ${YELLOW}信号:${NC} $sig_count 条"
        [ "$ord_count" -gt 0 ] && echo -e "  ${YELLOW}订单:${NC} $ord_count 条"
        [ "$det_count" -gt 0 ] && echo -e "  ${YELLOW}订单详情:${NC} $det_count 条"
        [ "$rev_count" -gt 0 ] && echo -e "  ${YELLOW}反转信号:${NC} $rev_count 条"
        return 1
    fi
    return 0
}

# 清理所有测试数据（不限日期）
cleanup_today_data() {
    echo ""
    echo -e "${YELLOW}═══ 清理所有测试数据（不限日期）══════${NC}"

    # ── 信号 ──
    local sig_response=$(curl_exec "GET" "${BASE_URL}/api/collections/signals/records?filter=(script_tag~'test'||signal_id~'_sig')&perPage=200" "" "清理-查询所有信号")
    local sig_count=$(echo "$sig_response" | jq '.items | length' 2>/dev/null || echo "0")
    local deleted_sig=0

    if [ "$sig_count" -gt 0 ]; then
        local deleted_sig_ids=$(echo "$sig_response" | jq -r '.items[].id' 2>/dev/null)
        [ -n "$deleted_sig_ids" ] && deleted_sig=$(echo "$deleted_sig_ids" | wc -l | tr -d ' ')
        if [ "$deleted_sig" -gt 0 ]; then
            echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo -e "${CYAN}🗑️  删除信号 ($deleted_sig 条):${NC}"
            echo "$sig_response" | jq -r '.items[].signal_id' 2>/dev/null | while read sid; do
                [ -n "$sid" ] && echo -e "    ${RED}✗${NC} $sid"
            done
            for sig_id in $deleted_sig_ids; do
                curl_exec "DELETE" "${BASE_URL}/api/collections/signals/records/${sig_id}" "" "删除信号" > /dev/null 2>&1
            done
        fi
    fi

    # ── 订单 ──
    local ord_response=$(curl_exec "GET" "${BASE_URL}/api/collections/orders/records?filter=(unique_id~'_sig'||unique_id~'_test')&perPage=200" "" "清理-查询所有订单")
    local ord_count=$(echo "$ord_response" | jq '.items | length' 2>/dev/null || echo "0")
    local deleted_ord=0

    if [ "$ord_count" -gt 0 ]; then
        local deleted_ord_ids=$(echo "$ord_response" | jq -r '.items[].id' 2>/dev/null)
        [ -n "$deleted_ord_ids" ] && deleted_ord=$(echo "$deleted_ord_ids" | wc -l | tr -d ' ')
        if [ "$deleted_ord" -gt 0 ]; then
            echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo -e "${CYAN}🗑️  删除订单 ($deleted_ord 条):${NC}"
            echo "$ord_response" | jq -r '.items[].unique_id' 2>/dev/null | while read uid; do
                [ -n "$uid" ] && echo -e "    ${RED}✗${NC} $uid"
            done
            for ord_id in $deleted_ord_ids; do
                curl_exec "DELETE" "${BASE_URL}/api/collections/orders/records/${ord_id}" "" "删除订单" > /dev/null 2>&1
            done
        fi
    fi

    # ── 订单详情（order_details）──
    local det_response=$(curl_exec "GET" "${BASE_URL}/api/collections/order_details/records?filter=(order_id~'_sig'||order_id~'_test')&perPage=200" "" "清理-查询所有订单详情")
    local det_count=$(echo "$det_response" | jq '.items | length' 2>/dev/null || echo "0")
    local deleted_det=0

    if [ "$det_count" -gt 0 ]; then
        local deleted_det_ids=$(echo "$det_response" | jq -r '.items[].id' 2>/dev/null)
        [ -n "$deleted_det_ids" ] && deleted_det=$(echo "$deleted_det_ids" | wc -l | tr -d ' ')
        if [ "$deleted_det" -gt 0 ]; then
            echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo -e "${CYAN}🗑️  删除订单详情 ($deleted_det 条):${NC}"
            echo "$det_response" | jq -r '.items[].order_id' 2>/dev/null | while read oid; do
                [ -n "$oid" ] && echo -e "    ${RED}✗${NC} $oid"
            done
            for det_id in $deleted_det_ids; do
                curl_exec "DELETE" "${BASE_URL}/api/collections/order_details/records/${det_id}" "" "删除订单详情" > /dev/null 2>&1
            done
        fi
    fi

    # ── 反转信号 ──
    local rev_response=$(curl_exec "GET" "${BASE_URL}/api/collections/reverse_signals/records?perPage=200" "" "清理-查询所有反转信号")
    local rev_count=$(echo "$rev_response" | jq '.items | length' 2>/dev/null || echo "0")
    local deleted_rev=0

    if [ "$rev_count" -gt 0 ]; then
        local deleted_rev_ids=$(echo "$rev_response" | jq -r '.items[].id' 2>/dev/null)
        [ -n "$deleted_rev_ids" ] && deleted_rev=$(echo "$deleted_rev_ids" | wc -l | tr -d ' ')
        if [ "$deleted_rev" -gt 0 ]; then
            echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo -e "${CYAN}🗑️  删除反转信号 ($deleted_rev 条):${NC}"
            echo "$rev_response" | jq -r '.items[].id' 2>/dev/null | while read rid; do
                [ -n "$rid" ] && echo -e "    ${RED}✗${NC} $rid"
            done
            for rev_id in $deleted_rev_ids; do
                curl_exec "DELETE" "${BASE_URL}/api/collections/reverse_signals/records/${rev_id}" "" "删除反转信号" > /dev/null 2>&1
            done
        fi
    fi

    # 清理缓存
    rm -f /tmp/pb_sig_* /tmp/pb_ord_* /tmp/pb_rev_* /tmp/pb_trade_group_id /tmp/pb_entry_* /tmp/pb_tp_* /tmp/pb_sl_* 2>/dev/null

    echo ""
    echo -e "${GREEN}✓ 清理完成（所有日期）${NC}"
    echo -e "  信号: ${GREEN}${deleted_sig:-0}${NC} | 订单: ${GREEN}${deleted_ord:-0}${NC} | 订单详情: ${GREEN}${deleted_det:-0}${NC} | 反转信号: ${GREEN}${deleted_rev:-0}${NC}"
}

# 提示并清理
prompt_cleanup() {
    echo ""
    check_today_test_data
    if [ $? -eq 0 ]; then
        return 0
    fi

    echo -n "是否清理 ${TEST_SYMBOL} 今日测试数据? [y/N], 按 Enter 确认: "
    read confirm
    if [[ "$confirm" =~ ^[yY]$ ]]; then
        cleanup_today_data
        return 0
    fi
    return 1
}

# ============================================================
# 📡 信号流程测试
# ============================================================

# 1. 发送信号到 PB
test_1_send_signal() {
    echo ""
    echo -e "${MAGENTA}═══ 📡 步骤1: 发送信号到 PB ═══${NC}"

    # 先检查并清理
    prompt_cleanup || true

    local data=$(generate_test_data)
    local signal_id=$(echo "$data" | cut -d'|' -f1)
    local entry_price=$(echo "$data" | cut -d'|' -f2)
    local stop_loss=$(echo "$data" | cut -d'|' -f3)
    local take_profit=$(echo "$data" | cut -d'|' -f4)
    local timestamp_ms=$(echo "$data" | cut -d'|' -f5)
    local limit_price=$(echo "$data" | cut -d'|' -f6)
    local us_time=$(echo "$data" | cut -d'|' -f7)
    local cn_time=$(echo "$data" | cut -d'|' -f8)

    local json=$(cat <<EOF
{
  "type": "signal",
  "symbol": "${TEST_SYMBOL}",
  "direction": "${TEST_DIRECTION}",
  "entry": ${entry_price},
  "stop_loss": ${stop_loss},
  "take_profit": ${take_profit},
  "limit_price": ${limit_price},
  "shares": 100,
  "rr": "1.5:1",
  "signal": "test_signal",
  "exchange": "NASDAQ",
  "interval": "5",
  "signal_id": "${signal_id}",
  "us_time": "${us_time}",
  "cn_time": "${cn_time}",
  "extra": {
    "reason": "测试信号",
    "bar_time_ms": ${timestamp_ms},
    "bar_index": 1000,
    "chart_tf": "5",
    "script_tag": "test_script_v1",
    "atr": 0.5,
    "atr_pct": 0.25,
    "day_change_pct": 1.5,
    "prev_close_change_pct": 0.8,
    "change_7d": -1.2,
    "sd_zone": "normal",
    "sd_trend": "up",
    "dtp_dir": "bullish",
    "dtp_phase": "confirmed",
    "crsi_state": "normal",
    "sl_atr_ratio": 2.0,
    "sl_dist_pct": 0.68
  }
}
EOF
)

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  signal_id: ${GREEN}${signal_id}${NC}"
    echo -e "  symbol: ${GREEN}${TEST_SYMBOL}${NC}"
    echo -e "  direction: ${GREEN}${TEST_DIRECTION}${NC}"
    echo -e "  entry: ${GREEN}${entry_price}${NC}, limit: ${GREEN}${limit_price}${NC}"
    echo -e "  SL: ${GREEN}${stop_loss}${NC}, TP: ${GREEN}${take_profit}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    echo -e "${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${MAGENTA}📤 发送信号${NC}"
    echo -e "  ${CYAN}curl -X POST ${BASE_URL}/webhook/tv${NC}"
    echo -e "  ${CYAN}Body:${NC}"
    echo "$json" | jq '.' 2>/dev/null || echo "$json"
    echo -e "${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    response=$(do_curl "POST" "${BASE_URL}/webhook/tv" "$json")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"

    # 检查响应中的 ok 字段
    local ok_val=$(echo "$response" | jq -r '.ok' 2>/dev/null)
    if [ "$ok_val" = "true" ] || [ "$ok_val" = "1" ]; then
        log_success "信号发送成功"
        clear_order_relation_cache
        # 保存到缓存文件
        cache_set "pb_sig_id" "${signal_id}"
        cache_set "pb_sig_data" "${entry_price}|${stop_loss}|${take_profit}|${timestamp_ms}|${limit_price}|${us_time}|${cn_time}"
        cache_set "pb_sig_latest" "${signal_id}"
    else
        log_error "信号发送失败"
    fi
}

# 2. 查询信号状态
test_2_query_signals() {
    echo ""
    echo -e "${MAGENTA}═══ 📡 步骤2: 查询信号状态 ═══${NC}"

    # 显示当前操作目标
    local cur_sig=$(cat /tmp/pb_sig_id 2>/dev/null || echo "未设置")
    local us_date=$(get_us_date)
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作信号:${NC} ${GREEN}${cur_sig}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    log_info "查询 ${TEST_DATE} (US: ${us_date}) 的测试信号..."

    # 使用 collection API 直接查询（只查询测试信号）
    response=$(curl_exec "GET" "${BASE_URL}/api/collections/signals/records?sort=-created&filter=(script_tag~'test'||signal_id~'_sig')&&date='${us_date}'&perPage=100" "" "查询测试信号")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"

    local count=$(echo "$response" | jq '.items | length' 2>/dev/null || echo "0")
    log_info "共 $count 条信号"

    # 保存最新信号ID
    if [ "$count" -gt 0 ]; then
        local latest_sig=$(echo "$response" | jq -r '.items[0].signal_id' 2>/dev/null)
        cache_set "pb_sig_latest" "$latest_sig"
        cache_set "pb_sig_id" "$latest_sig"
        local latest_data=$(echo "$response" | jq -r '.items[0] | "\(.entry)|\(.stop_loss)|\(.take_profit)|\(.bar_time_ms)|\(.limit_price)|\(.us_time)|\(.cn_time)"' 2>/dev/null)
        cache_set "pb_sig_data" "$latest_data"
    fi
}

# 3. 飞书-确认信号
test_3_feishu_confirm() {
    echo ""
    echo -e "${MAGENTA}═══ 📡 步骤3: 飞书-确认信号 ═══${NC}"

    local sig_id=$(cat /tmp/pb_sig_id 2>/dev/null || echo "")
    if [ -z "$sig_id" ] || [ ! -f /tmp/pb_sig_latest ]; then
        log_error "没有信号ID，请先发送信号"
        return 1
    fi

    sig_id=$(cat /tmp/pb_sig_latest)

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  signal_id: ${GREEN}${sig_id}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}注意: 需要飞书机器人配置 callback_url${NC}"

    local confirm_json="{\"action\": \"confirm\", \"signal_id\": \"${sig_id}\"}"
    response=$(curl_exec "POST" "${BASE_URL}/webhook/feishu/callback" "${confirm_json}" "飞书-确认信号")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"
}

# 4. 飞书-拒绝信号
test_4_feishu_reject() {
    echo ""
    echo -e "${MAGENTA}═══ 📡 步骤4: 飞书-拒绝信号 ═══${NC}"

    local sig_id=$(cat /tmp/pb_sig_id 2>/dev/null || echo "")
    if [ -z "$sig_id" ] || [ ! -f /tmp/pb_sig_latest ]; then
        log_error "没有信号ID，请先发送信号"
        return 1
    fi

    sig_id=$(cat /tmp/pb_sig_latest)

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  signal_id: ${GREEN}${sig_id}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}⚠️ 警告: 拒绝操作不可逆!${NC}"
    echo -n "确认拒绝信号? [y/N], 按 Enter 确认: "
    read confirm
    [[ ! "$confirm" =~ ^[yY]$ ]] && { log_info "已取消"; return 0; }

    log_info "拒绝信号: ${sig_id}"

    local reject_json="{\"action\": \"reject\", \"signal_id\": \"${sig_id}\"}"
    response=$(curl_exec "POST" "${BASE_URL}/webhook/feishu/callback" "${reject_json}" "飞书-拒绝信号")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"
}

# 5. QC确认信号 (signals/ack)
test_5_qc_ack_signal() {
    echo ""
    echo -e "${MAGENTA}═══ 📡 步骤5: QC确认信号 → 创建交易组 Init ═══${NC}"

    local sig_id
    sig_id=$(cache_get "pb_sig_latest")
    [ -z "$sig_id" ] && sig_id=$(cache_get "pb_sig_id")
    if [ -z "$sig_id" ]; then
        log_error "没有信号ID，请先发送信号"
        return 1
    fi

    local sig_data
    sig_data=$(cache_get "pb_sig_data")
    local entry_price=$(echo "$sig_data" | cut -d'|' -f1)
    local stop_loss=$(echo "$sig_data" | cut -d'|' -f2)
    local take_profit=$(echo "$sig_data" | cut -d'|' -f3)
    local limit_price=$(echo "$sig_data" | cut -d'|' -f5)
    [ -z "$limit_price" ] && limit_price="$entry_price"
    seed_order_relation_cache "$sig_id"
    local phase_data=$(build_phase_time 0)
    local timestamp_ms=$(echo "$phase_data" | cut -d'|' -f1)
    local sig_us_time=$(echo "$phase_data" | cut -d'|' -f2)
    local sig_cn_time=$(echo "$phase_data" | cut -d'|' -f3)

    local entry_unique_id
    local trade_group_id
    local entry_broker_id
    local tp_unique_id
    local sl_unique_id
    entry_unique_id=$(cache_get "pb_entry_unique_id")
    trade_group_id=$(cache_get "pb_trade_group_id")
    entry_broker_id=$(cache_get "pb_entry_broker_id")
    tp_unique_id=$(cache_get "pb_tp_unique_id")
    sl_unique_id=$(cache_get "pb_sl_unique_id")

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  signal_id: ${GREEN}${sig_id}${NC}"
    echo -e "  → trade_group_id: ${GREEN}${trade_group_id}${NC}"
    echo -e "  → entry_unique_id: ${GREEN}${entry_unique_id}${NC}"
    echo -e "  → tp_unique_id: ${GREEN}${tp_unique_id}${NC}"
    echo -e "  → sl_unique_id: ${GREEN}${sl_unique_id}${NC}"
    echo -e "  → broker_order_id: ${GREEN}${entry_broker_id}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local json=$(cat <<EOF
{
  "signal_id": "${sig_id}",
  "status": "executed",
  "note": "QC确认测试信号",
  "order": {
    "unique_id": "${entry_unique_id}",
    "order_id": "${entry_broker_id}",
    "broker_order_id": "${entry_broker_id}",
    "order_type": "Entry",
    "direction": "${TEST_DIRECTION}",
    "position_side": "${TEST_DIRECTION}",
    "role": "entry",
    "relation_status": "active",
    "trade_group_id": "${trade_group_id}",
    "entry_order_unique_id": "${entry_unique_id}",
    "quantity": 100,
    "limit_price": ${limit_price},
    "status": "Init",
    "filled_qty": 0,
    "fill_price": 0,
    "stop_loss": ${stop_loss},
    "take_profit": ${take_profit},
    "order_time": "${sig_us_time}",
    "us_time": "${sig_us_time}",
    "cn_time": "${sig_cn_time}",
    "bar_time_ms": ${timestamp_ms},
    "extra": {
      "reason": "QC确认测试信号",
      "created_via": "pb-flow.sh",
      "scenario": "signal_ack_init"
    }
  },
  "child_orders": [
    {
      "unique_id": "${tp_unique_id}",
      "order_type": "TakeProfit",
      "direction": "${TEST_DIRECTION}",
      "position_side": "${TEST_DIRECTION}",
      "role": "take_profit",
      "relation_status": "planned",
      "trade_group_id": "${trade_group_id}",
      "entry_order_unique_id": "${entry_unique_id}",
      "parent_order_unique_id": "${entry_unique_id}",
      "sibling_order_unique_id": "${sl_unique_id}",
      "quantity": 100,
      "limit_price": ${take_profit},
      "status": "Init",
      "filled_qty": 0,
      "fill_price": 0,
      "order_time": "${sig_us_time}",
      "us_time": "${sig_us_time}",
      "cn_time": "${sig_cn_time}",
      "bar_time_ms": ${timestamp_ms},
      "extra": {
        "reason": "预创建止盈单",
        "created_via": "pb-flow.sh",
        "scenario": "signal_ack_init_tp"
      }
    },
    {
      "unique_id": "${sl_unique_id}",
      "order_type": "StopLoss",
      "direction": "${TEST_DIRECTION}",
      "position_side": "${TEST_DIRECTION}",
      "role": "stop_loss",
      "relation_status": "planned",
      "trade_group_id": "${trade_group_id}",
      "entry_order_unique_id": "${entry_unique_id}",
      "parent_order_unique_id": "${entry_unique_id}",
      "sibling_order_unique_id": "${tp_unique_id}",
      "quantity": 100,
      "limit_price": ${stop_loss},
      "status": "Init",
      "filled_qty": 0,
      "fill_price": 0,
      "order_time": "${sig_us_time}",
      "us_time": "${sig_us_time}",
      "cn_time": "${sig_cn_time}",
      "bar_time_ms": ${timestamp_ms},
      "extra": {
        "reason": "预创建止损单",
        "created_via": "pb-flow.sh",
        "scenario": "signal_ack_init_sl"
      }
    }
  ]
}
EOF
)

    # 通过 signals/ack hook 处理状态流转
    response=$(curl_exec "POST" "${BASE_URL}/api/custom/signals/ack" "$json" "QC确认信号→创建交易组")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"

    local success_val=$(echo "$response" | jq -r '.success' 2>/dev/null)
    if [ "$success_val" = "true" ]; then
        log_success "信号确认成功，交易组已创建(Entry/TP/SL = Init)"
        cache_set "pb_ord_latest" "${entry_unique_id}"
        cache_set "pb_ord_id" "${entry_unique_id}"
    fi
}

# ============================================================
# 📦 订单流程测试
# ============================================================

# 6. QC同步订单 Submitted
test_6_qc_order_submitted() {
    echo ""
    echo -e "${MAGENTA}═══ 📦 步骤6: QC同步订单 Submitted ═══${NC}"

    ensure_order_relation_cache || { log_error "没有信号缓存，请先执行步骤1和5"; return 1; }

    local order_id
    local trade_group_id
    local entry_broker_id
    order_id=$(cache_get "pb_entry_unique_id")
    trade_group_id=$(cache_get "pb_trade_group_id")
    entry_broker_id=$(cache_get "pb_entry_broker_id")

    local sig_data
    sig_data=$(cache_get "pb_sig_data")
    local entry_price=$(echo "$sig_data" | cut -d'|' -f1)
    local stop_loss=$(echo "$sig_data" | cut -d'|' -f2)
    local take_profit=$(echo "$sig_data" | cut -d'|' -f3)
    local limit_price=$(echo "$sig_data" | cut -d'|' -f5)
    [ -z "$limit_price" ] && limit_price="$entry_price"
    local phase_data=$(build_phase_time 1)
    local timestamp_ms=$(echo "$phase_data" | cut -d'|' -f1)
    local sig_us_time=$(echo "$phase_data" | cut -d'|' -f2)
    local sig_cn_time=$(echo "$phase_data" | cut -d'|' -f3)

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  订单: ${GREEN}${order_id}${NC}"
    echo -e "  status: ${GREEN}Submitted${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local json=$(cat <<EOF
{
  "unique_id": "${order_id}",
  "order_type": "Entry",
  "order_id": "${entry_broker_id}",
  "broker_order_id": "${entry_broker_id}",
  "symbol": "${TEST_SYMBOL}",
  "direction": "${TEST_DIRECTION}",
  "position_side": "${TEST_DIRECTION}",
  "role": "entry",
  "relation_status": "active",
  "trade_group_id": "${trade_group_id}",
  "entry_order_unique_id": "${order_id}",
  "quantity": 100,
  "limit_price": ${limit_price},
  "status": "Submitted",
  "filled_qty": 0,
  "fill_price": 0,
  "tp_price": ${take_profit},
  "sl_price": ${stop_loss},
  "rr_ratio": 1.5,
  "signal_id": "$(cache_get "pb_sig_id")",
  "us_time": "${sig_us_time}",
  "cn_time": "${sig_cn_time}",
  "bar_time_ms": ${timestamp_ms},
  "extra": {
    "reason": "QC同步Submitted",
    "scenario": "entry_submitted"
  }
}
EOF
)

    response=$(curl_exec "POST" "${BASE_URL}/api/custom/orders/upsert" "$json" "QC同步订单Submitted")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"

    if echo "$response" | jq -r '.ok // .success // .error' 2>/dev/null | grep -qv "false\|error"; then
        cache_set "pb_ord_latest" "${order_id}"
        cache_set "pb_ord_id" "${order_id}"
    fi
}

# 7. QC同步订单 Filled
test_7_qc_order_filled() {
    echo ""
    echo -e "${MAGENTA}═══ 📦 步骤7: Entry Filled + 激活 TP/SL ═══${NC}"

    ensure_order_relation_cache || { log_error "没有信号缓存，请先执行步骤1和5"; return 1; }

    local order_id
    local trade_group_id
    local entry_broker_id
    local tp_order_id
    local sl_order_id
    local tp_broker_id
    local sl_broker_id
    order_id=$(cache_get "pb_entry_unique_id")
    trade_group_id=$(cache_get "pb_trade_group_id")
    entry_broker_id=$(cache_get "pb_entry_broker_id")
    tp_order_id=$(cache_get "pb_tp_unique_id")
    sl_order_id=$(cache_get "pb_sl_unique_id")
    tp_broker_id=$(cache_get "pb_tp_broker_id")
    sl_broker_id=$(cache_get "pb_sl_broker_id")

    local sig_data
    sig_data=$(cache_get "pb_sig_data")
    local entry_price=$(echo "$sig_data" | cut -d'|' -f1)
    local stop_loss=$(echo "$sig_data" | cut -d'|' -f2)
    local take_profit=$(echo "$sig_data" | cut -d'|' -f3)
    local limit_price=$(echo "$sig_data" | cut -d'|' -f5)
    [ -z "$limit_price" ] && limit_price="$entry_price"
    local phase_data=$(build_phase_time 2)
    local timestamp_ms=$(echo "$phase_data" | cut -d'|' -f1)
    local sig_us_time=$(echo "$phase_data" | cut -d'|' -f2)
    local sig_cn_time=$(echo "$phase_data" | cut -d'|' -f3)
    local fill_price
    if [ "${TEST_DIRECTION}" = "short" ]; then
        fill_price=$(echo "scale=2; ${entry_price} * 0.999" | bc 2>/dev/null || echo "${entry_price}")
    else
        fill_price=$(echo "scale=2; ${entry_price} * 1.001" | bc 2>/dev/null || echo "${entry_price}")
    fi

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  订单: ${GREEN}${order_id}${NC}"
    echo -e "  status: ${GREEN}Filled${NC}"
    echo -e "  fill_price: ${GREEN}${fill_price}${NC}"
    echo -e "  TP/SL: ${GREEN}${tp_order_id}${NC} / ${GREEN}${sl_order_id}${NC} -> Submitted"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local json=$(cat <<EOF
{
  "unique_id": "${order_id}",
  "order_type": "Entry",
  "order_id": "${entry_broker_id}",
  "broker_order_id": "${entry_broker_id}",
  "symbol": "${TEST_SYMBOL}",
  "direction": "${TEST_DIRECTION}",
  "position_side": "${TEST_DIRECTION}",
  "role": "entry",
  "relation_status": "active",
  "trade_group_id": "${trade_group_id}",
  "entry_order_unique_id": "${order_id}",
  "quantity": 100,
  "limit_price": ${limit_price},
  "status": "Filled",
  "filled_qty": 100,
  "fill_price": ${fill_price},
  "tp_price": ${take_profit},
  "sl_price": ${stop_loss},
  "pnl": 0,
  "commission": 1.0,
  "rr_ratio": 1.5,
  "signal_id": "$(cache_get "pb_sig_id")",
  "fill_time": "${sig_us_time}",
  "us_time": "${sig_us_time}",
  "cn_time": "${sig_cn_time}",
  "bar_time_ms": ${timestamp_ms},
  "extra": {
    "reason": "QC同步Filled",
    "scenario": "entry_filled"
  }
}
EOF
)

    response=$(curl_exec "POST" "${BASE_URL}/api/custom/orders/upsert" "$json" "QC同步订单Filled")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"

    local tp_submit_json=$(cat <<EOF
{
  "unique_id": "${tp_order_id}",
  "order_type": "TakeProfit",
  "order_id": "${tp_broker_id}",
  "broker_order_id": "${tp_broker_id}",
  "symbol": "${TEST_SYMBOL}",
  "direction": "${TEST_DIRECTION}",
  "position_side": "${TEST_DIRECTION}",
  "trade_group_id": "${trade_group_id}",
  "entry_order_unique_id": "${order_id}",
  "parent_order_unique_id": "${order_id}",
  "sibling_order_unique_id": "${sl_order_id}",
  "role": "take_profit",
  "relation_status": "active",
  "quantity": 100,
  "limit_price": ${take_profit},
  "status": "Submitted",
  "filled_qty": 0,
  "fill_price": 0,
  "signal_id": "$(cache_get "pb_sig_id")",
  "us_time": "${sig_us_time}",
  "cn_time": "${sig_cn_time}",
  "bar_time_ms": ${timestamp_ms},
  "extra": {
    "reason": "Entry Filled 后激活 TP",
    "scenario": "tp_activated"
  }
}
EOF
)

    local sl_submit_json=$(cat <<EOF
{
  "unique_id": "${sl_order_id}",
  "order_type": "StopLoss",
  "order_id": "${sl_broker_id}",
  "broker_order_id": "${sl_broker_id}",
  "symbol": "${TEST_SYMBOL}",
  "direction": "${TEST_DIRECTION}",
  "position_side": "${TEST_DIRECTION}",
  "trade_group_id": "${trade_group_id}",
  "entry_order_unique_id": "${order_id}",
  "parent_order_unique_id": "${order_id}",
  "sibling_order_unique_id": "${tp_order_id}",
  "role": "stop_loss",
  "relation_status": "active",
  "quantity": 100,
  "limit_price": ${stop_loss},
  "status": "Submitted",
  "filled_qty": 0,
  "fill_price": 0,
  "signal_id": "$(cache_get "pb_sig_id")",
  "us_time": "${sig_us_time}",
  "cn_time": "${sig_cn_time}",
  "bar_time_ms": ${timestamp_ms},
  "extra": {
    "reason": "Entry Filled 后激活 SL",
    "scenario": "sl_activated"
  }
}
EOF
)

    local tp_submit_response=$(curl_exec "POST" "${BASE_URL}/api/custom/orders/upsert" "$tp_submit_json" "激活止盈TP")
    echo "$tp_submit_response" | jq '.' 2>/dev/null || echo "$tp_submit_response"

    local sl_submit_response=$(curl_exec "POST" "${BASE_URL}/api/custom/orders/upsert" "$sl_submit_json" "激活止损SL")
    echo "$sl_submit_response" | jq '.' 2>/dev/null || echo "$sl_submit_response"
}

# 8. 同步订单止盈 TP
test_8_qc_order_takeprofit() {
    echo ""
    echo -e "${MAGENTA}═══ 📦 步骤8: 同步 TP 成交 + SL 对手单取消 ═══${NC}"

    ensure_order_relation_cache || { log_error "没有信号缓存，请先执行步骤1和5"; return 1; }

    local order_id
    local trade_group_id
    local tp_order_id
    local sl_order_id
    local tp_broker_id
    local sl_broker_id
    order_id=$(cache_get "pb_entry_unique_id")
    trade_group_id=$(cache_get "pb_trade_group_id")
    tp_order_id=$(cache_get "pb_tp_unique_id")
    sl_order_id=$(cache_get "pb_sl_unique_id")
    tp_broker_id=$(cache_get "pb_tp_broker_id")
    sl_broker_id=$(cache_get "pb_sl_broker_id")

    local sig_data
    sig_data=$(cache_get "pb_sig_data")
    local entry_price=$(echo "$sig_data" | cut -d'|' -f1)
    local stop_loss=$(echo "$sig_data" | cut -d'|' -f2)
    local take_profit=$(echo "$sig_data" | cut -d'|' -f3)
    local limit_price=$(echo "$sig_data" | cut -d'|' -f5)
    [ -z "$limit_price" ] && limit_price="$entry_price"
    local phase_data=$(build_phase_time 5)
    local timestamp_ms=$(echo "$phase_data" | cut -d'|' -f1)
    local sig_us_time=$(echo "$phase_data" | cut -d'|' -f2)
    local sig_cn_time=$(echo "$phase_data" | cut -d'|' -f3)
    local tp_fill_price=${take_profit}
    local tp_pnl
    if [ "${TEST_DIRECTION}" = "short" ]; then
        tp_pnl=$(echo "scale=2; (${entry_price} - ${tp_fill_price}) * 100" | bc 2>/dev/null || echo "500")
    else
        tp_pnl=$(echo "scale=2; (${tp_fill_price} - ${entry_price}) * 100" | bc 2>/dev/null || echo "500")
    fi

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  原始订单: ${GREEN}${order_id}${NC}"
    echo -e "  TP订单: ${GREEN}${tp_order_id}${NC}"
    echo -e "  SL对手单: ${GREEN}${sl_order_id}${NC}"
    echo -e "  order_type: ${GREEN}TakeProfit${NC}"
    echo -e "  status: ${GREEN}Filled${NC}"
    echo -e "  tp_fill_price: ${GREEN}${tp_fill_price}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local json=$(cat <<EOF
{
  "unique_id": "${tp_order_id}",
  "order_type": "TakeProfit",
  "order_id": "${tp_broker_id}",
  "broker_order_id": "${tp_broker_id}",
  "symbol": "${TEST_SYMBOL}",
  "direction": "${TEST_DIRECTION}",
  "position_side": "${TEST_DIRECTION}",
  "trade_group_id": "${trade_group_id}",
  "entry_order_unique_id": "${order_id}",
  "parent_order_unique_id": "${order_id}",
  "sibling_order_unique_id": "${sl_order_id}",
  "role": "take_profit",
  "relation_status": "closed",
  "quantity": 100,
  "limit_price": ${tp_fill_price},
  "status": "Filled",
  "filled_qty": 100,
  "fill_price": ${tp_fill_price},
  "pnl": ${tp_pnl},
  "commission": 1.0,
  "signal_id": "$(cache_get "pb_sig_id")",
  "fill_time": "${sig_us_time}",
  "us_time": "${sig_us_time}",
  "cn_time": "${sig_cn_time}",
  "bar_time_ms": ${timestamp_ms},
  "extra": {
    "reason": "TP止盈触发，SL 对手单将取消",
    "entry_price": ${entry_price},
    "scenario": "take_profit_filled"
  }
}
EOF
)

    response=$(curl_exec "POST" "${BASE_URL}/api/custom/orders/upsert" "$json" "同步订单止盈TP")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"

    local sl_cancel_json=$(cat <<EOF
{
  "unique_id": "${sl_order_id}",
  "order_type": "StopLoss",
  "order_id": "${sl_broker_id}",
  "broker_order_id": "${sl_broker_id}",
  "symbol": "${TEST_SYMBOL}",
  "direction": "${TEST_DIRECTION}",
  "position_side": "${TEST_DIRECTION}",
  "trade_group_id": "${trade_group_id}",
  "entry_order_unique_id": "${order_id}",
  "parent_order_unique_id": "${order_id}",
  "sibling_order_unique_id": "${tp_order_id}",
  "role": "stop_loss",
  "relation_status": "closed",
  "quantity": 100,
  "limit_price": ${stop_loss},
  "status": "Canceled",
  "filled_qty": 0,
  "fill_price": 0,
  "signal_id": "$(cache_get "pb_sig_id")",
  "us_time": "${sig_us_time}",
  "cn_time": "${sig_cn_time}",
  "bar_time_ms": ${timestamp_ms},
  "extra": {
    "reason": "TP 成交后取消对手 SL",
    "entry_price": ${entry_price},
    "scenario": "tp_counterpart_cancel"
  }
}
EOF
)

    local sl_cancel_response=$(curl_exec "POST" "${BASE_URL}/api/custom/orders/upsert" "$sl_cancel_json" "同步对手SL取消")
    echo "$sl_cancel_response" | jq '.' 2>/dev/null || echo "$sl_cancel_response"
}

# 9. 同步订单止损 SL
test_9_qc_order_stoploss() {
    echo ""
    echo -e "${MAGENTA}═══ 📦 步骤9: 同步 SL 成交 + TP 对手单取消 ═══${NC}"

    ensure_order_relation_cache || { log_error "没有信号缓存，请先执行步骤1和5"; return 1; }

    local order_id
    local trade_group_id
    local tp_order_id
    local sl_order_id
    local tp_broker_id
    local sl_broker_id
    order_id=$(cache_get "pb_entry_unique_id")
    trade_group_id=$(cache_get "pb_trade_group_id")
    tp_order_id=$(cache_get "pb_tp_unique_id")
    sl_order_id=$(cache_get "pb_sl_unique_id")
    tp_broker_id=$(cache_get "pb_tp_broker_id")
    sl_broker_id=$(cache_get "pb_sl_broker_id")

    local sig_data
    sig_data=$(cache_get "pb_sig_data")
    local entry_price=$(echo "$sig_data" | cut -d'|' -f1)
    local stop_loss=$(echo "$sig_data" | cut -d'|' -f2)
    local take_profit=$(echo "$sig_data" | cut -d'|' -f3)
    local limit_price=$(echo "$sig_data" | cut -d'|' -f5)
    [ -z "$limit_price" ] && limit_price="$entry_price"
    local phase_data=$(build_phase_time 6)
    local timestamp_ms=$(echo "$phase_data" | cut -d'|' -f1)
    local sig_us_time=$(echo "$phase_data" | cut -d'|' -f2)
    local sig_cn_time=$(echo "$phase_data" | cut -d'|' -f3)
    local sl_fill_price=${stop_loss}
    local sl_pnl
    if [ "${TEST_DIRECTION}" = "short" ]; then
        sl_pnl=$(echo "scale=2; (${entry_price} - ${sl_fill_price}) * 100" | bc 2>/dev/null || echo "-200")
    else
        sl_pnl=$(echo "scale=2; (${sl_fill_price} - ${entry_price}) * 100" | bc 2>/dev/null || echo "-200")
    fi

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  原始订单: ${GREEN}${order_id}${NC}"
    echo -e "  SL订单: ${GREEN}${sl_order_id}${NC}"
    echo -e "  TP对手单: ${GREEN}${tp_order_id}${NC}"
    echo -e "  order_type: ${GREEN}StopLoss${NC}"
    echo -e "  status: ${GREEN}Filled${NC}"
    echo -e "  sl_fill_price: ${GREEN}${sl_fill_price}${NC}"
    echo -e "  pnl: ${RED}${sl_pnl}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local json=$(cat <<EOF
{
  "unique_id": "${sl_order_id}",
  "order_type": "StopLoss",
  "order_id": "${sl_broker_id}",
  "broker_order_id": "${sl_broker_id}",
  "symbol": "${TEST_SYMBOL}",
  "direction": "${TEST_DIRECTION}",
  "position_side": "${TEST_DIRECTION}",
  "trade_group_id": "${trade_group_id}",
  "entry_order_unique_id": "${order_id}",
  "parent_order_unique_id": "${order_id}",
  "sibling_order_unique_id": "${tp_order_id}",
  "role": "stop_loss",
  "relation_status": "closed",
  "quantity": 100,
  "limit_price": ${sl_fill_price},
  "status": "Filled",
  "filled_qty": 100,
  "fill_price": ${sl_fill_price},
  "pnl": ${sl_pnl},
  "commission": 1.0,
  "signal_id": "$(cache_get "pb_sig_id")",
  "fill_time": "${sig_us_time}",
  "us_time": "${sig_us_time}",
  "cn_time": "${sig_cn_time}",
  "bar_time_ms": ${timestamp_ms},
  "extra": {
    "reason": "SL止损触发，TP 对手单将取消",
    "entry_price": ${entry_price},
    "sl_price": ${stop_loss},
    "scenario": "stop_loss_filled"
  }
}
EOF
)

    response=$(curl_exec "POST" "${BASE_URL}/api/custom/orders/upsert" "$json" "同步订单止损SL")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"

    local tp_cancel_json=$(cat <<EOF
{
  "unique_id": "${tp_order_id}",
  "order_type": "TakeProfit",
  "order_id": "${tp_broker_id}",
  "broker_order_id": "${tp_broker_id}",
  "symbol": "${TEST_SYMBOL}",
  "direction": "${TEST_DIRECTION}",
  "position_side": "${TEST_DIRECTION}",
  "trade_group_id": "${trade_group_id}",
  "entry_order_unique_id": "${order_id}",
  "parent_order_unique_id": "${order_id}",
  "sibling_order_unique_id": "${sl_order_id}",
  "role": "take_profit",
  "relation_status": "closed",
  "quantity": 100,
  "limit_price": ${take_profit},
  "status": "Canceled",
  "filled_qty": 0,
  "fill_price": 0,
  "signal_id": "$(cache_get "pb_sig_id")",
  "us_time": "${sig_us_time}",
  "cn_time": "${sig_cn_time}",
  "bar_time_ms": ${timestamp_ms},
  "extra": {
    "reason": "SL 成交后取消对手 TP",
    "entry_price": ${entry_price},
    "scenario": "sl_counterpart_cancel"
  }
}
EOF
)

    local tp_cancel_response=$(curl_exec "POST" "${BASE_URL}/api/custom/orders/upsert" "$tp_cancel_json" "同步对手TP取消")
    echo "$tp_cancel_response" | jq '.' 2>/dev/null || echo "$tp_cancel_response"
}

# O. 飞书-取消订单
test_o_cancel_order() {
    echo ""
    echo -e "${MAGENTA}═══ 📦 O: 飞书-取消订单 ═══${NC}"

    local order_id
    order_id=$(cache_get "pb_entry_unique_id")
    if [ -z "$order_id" ]; then
        log_error "没有订单ID"
        return 1
    fi

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  订单: ${GREEN}${order_id}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}⚠️ 警告: 取消操作不可逆!${NC}"
    echo -n "确认取消? [y/N], 按 Enter 确认: "
    read confirm
    [[ ! "$confirm" =~ ^[yY]$ ]] && { log_info "已取消"; return 0; }

    log_info "取消订单: ${order_id}"

    response=$(curl_exec "GET" "${BASE_URL}/webhook/order/cancel?id=${order_id}" "" "飞书-取消订单")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"
}

# P. 飞书-平仓订单
test_p_close_order() {
    echo ""
    echo -e "${MAGENTA}═══ 📦 P: 飞书-平仓订单 ═══${NC}"

    local order_id
    order_id=$(cache_get "pb_entry_unique_id")
    if [ -z "$order_id" ]; then
        log_error "没有订单ID"
        return 1
    fi

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  订单: ${GREEN}${order_id}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}⚠️ 警告: 平仓操作不可逆!${NC}"
    echo -n "确认平仓? [y/N], 按 Enter 确认: "
    read confirm
    [[ ! "$confirm" =~ ^[yY]$ ]] && { log_info "已取消"; return 0; }

    log_info "平仓订单: ${order_id}"

    response=$(curl_exec "GET" "${BASE_URL}/webhook/order/close?id=${order_id}" "" "飞书-平仓订单")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"
}

# ============================================================
# ⚡ 逆向信号测试
# ============================================================

# A. 计算逆向信号
test_a_calc_reverse() {
    echo ""
    echo -e "${MAGENTA}═══ ⚡ A: 计算逆向信号 ═══${NC}"

    local sig_id=$(cache_get "pb_sig_id")
    local ord_id=$(cache_get "pb_entry_unique_id")

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  symbol: ${GREEN}${TEST_SYMBOL}${NC}"
    echo -e "  direction: ${GREEN}${TEST_DIRECTION}${NC}"
    echo -e "  关联信号: ${GREEN}${sig_id}${NC}"
    echo -e "  关联订单: ${GREEN}${ord_id}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local json=$(cat <<EOF
{"symbol": "${TEST_SYMBOL}", "direction": "${TEST_DIRECTION}"}
EOF
)

    response=$(curl_exec "POST" "${BASE_URL}/api/custom/reverse/calculate" "$json" "计算逆向信号")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"

    local rev_id=$(echo "$response" | jq -r '.signal.id' 2>/dev/null)
    if [ -n "$rev_id" ] && [ "$rev_id" != "null" ]; then
        cache_set "pb_rev_latest" "$rev_id"
        cache_set "pb_rev_id" "$rev_id"
    fi
}

# B. 查询逆向信号
test_b_query_reverse() {
    echo ""
    echo -e "${MAGENTA}═══ ⚡ B: 查询逆向信号 ═══${NC}"

    local rev_id=$(cache_get "pb_rev_id")
    [ -z "$rev_id" ] && rev_id="未设置"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前逆向信号:${NC} ${GREEN}${rev_id}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    response=$(curl_exec "GET" "${BASE_URL}/api/custom/reverse/pending" "" "查询逆向信号")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"

    local count=$(echo "$response" | jq '.signals | length' 2>/dev/null || echo "0")
    log_info "共 $count 条逆向信号"

    # 保存最新的
    if [ "$count" -gt 0 ]; then
        local latest_rev=$(echo "$response" | jq -r '.signals[0].id' 2>/dev/null)
        cache_set "pb_rev_latest" "$latest_rev"
        cache_set "pb_rev_id" "$latest_rev"
    fi
}

# C. 确认逆向信号
test_c_ack_reverse() {
    echo ""
    echo -e "${MAGENTA}═══ ⚡ C: 确认逆向信号 ═══${NC}"

    local rev_id=$(cache_get "pb_rev_id")
    if [ -z "$rev_id" ]; then
        local rev_pending=$(curl_exec "GET" "${BASE_URL}/api/custom/reverse/pending" "" "获取逆向信号ID")
        rev_id=$(echo "$rev_pending" | jq -r '.signals[0].id' 2>/dev/null)
    fi

    if [ -z "$rev_id" ] || [ "$rev_id" = "null" ]; then
        log_error "没有逆向信号"
        return 1
    fi
    cache_set "pb_rev_latest" "$rev_id"
    cache_set "pb_rev_id" "$rev_id"

    local sig_id=$(cache_get "pb_sig_id")
    local ord_id=$(cache_get "pb_entry_broker_id")
    local trade_group_id=$(cache_get "pb_trade_group_id")
    local entry_unique_id=$(cache_get "pb_entry_unique_id")

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  reverse_id: ${GREEN}${rev_id}${NC}"
    echo -e "  关联信号: ${GREEN}${sig_id}${NC}"
    echo -e "  关联订单: ${GREEN}${ord_id}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local json=$(cat <<EOF
{
  "signal_id": "${rev_id}",
  "status": "confirmed",
  "reason": "测试确认",
  "order_id": "${ord_id}",
  "signal_id_orig": "${sig_id}",
  "trade_group_id": "${trade_group_id}",
  "entry_order_unique_id": "${entry_unique_id}"
}
EOF
)

    response=$(curl_exec "POST" "${BASE_URL}/api/custom/reverse/ack" "$json" "确认逆向信号")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"
}

# ============================================================
# 📊 指标数据测试
# ============================================================

# D. 发送指标数据
test_d_send_indicator() {
    echo ""
    echo -e "${MAGENTA}═══ 📊 D: 发送指标数据 ═══${NC}"

    local datetime="${TEST_DATE} ${TEST_TIME}"
    local ts=$(TZ=Asia/Shanghai date -j -f "%Y-%m-%d %H:%M:%S" "${datetime}" +%s 2>/dev/null) || ts=$(date +%s)
    local timestamp_ms=${ts}000
    local cn_time="${TEST_DATE} ${TEST_TIME}"
    local us_time=$(TZ=America/New_York date -r ${ts} +"%Y-%m-%d %H:%M:%S" 2>/dev/null || echo "${TEST_DATE} ${TEST_TIME}")

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前操作:${NC}"
    echo -e "  symbol: ${GREEN}${TEST_SYMBOL}${NC}"
    echo -e "  cn_time: ${GREEN}${cn_time}${NC}"
    echo -e "  us_time: ${GREEN}${us_time}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local json=$(cat <<EOF
{
  "type": "indicator",
  "symbol": "${TEST_SYMBOL}",
  "exchange": "NASDAQ",
  "interval": "5",
  "script_tag": "test_script",
  "us_time": "${us_time}",
  "cn_time": "${cn_time}",
  "bar_time_ms": ${timestamp_ms},
  "bar_index": 1000,
  "extra": {
    "close": 150.50,
    "high": 151.00,
    "low": 149.50,
    "open": 150.00,
    "volume": 50000000,
    "day_change_pct": 1.25,
    "atr": 0.85,
    "crsi": 65.0,
    "crsi_ob": true,
    "crsi_os": false,
    "fractal_bull": true,
    "fractal_bear": false,
    "ema_bull_touch": true,
    "ema_bear_touch": false
  }
}
EOF
)

    response=$(curl_exec "POST" "${BASE_URL}/webhook/tv" "$json" "发送指标数据")

    echo "$response" | jq '.' 2>/dev/null || echo "$response"
}

# ============================================================
# 🔧 工具
# ============================================================

# E. 查询测试信号
test_e_list_signals() {
    echo ""
    echo -e "${CYAN}═══ 🔧 E: 查询测试信号 ═══${NC}"

    local sig_id=$(cat /tmp/pb_sig_id 2>/dev/null || echo "未设置")
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前信号ID:${NC} ${GREEN}${sig_id}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local us_date=$(get_us_date)
    log_info "查询 ${TEST_DATE} (US: ${us_date}) 测试信号（script_tag~test 或 signal_id~_sig）"
    # 过滤：script_tag 包含 test 或 signal_id 包含 _sig
    local response=$(curl_exec "GET" "${BASE_URL}/api/collections/signals/records?sort=-created&filter=(script_tag~'test'||signal_id~'_sig')&&date='${us_date}'&perPage=100" "" "查询测试信号")
    echo "$response" | jq '.' 2>/dev/null || echo "$response"
}

# F. 查询测试订单
test_f_list_orders() {
    echo ""
    echo -e "${CYAN}═══ 🔧 F: 查询测试订单 ═══${NC}"

    local ord_id=$(cache_get "pb_entry_unique_id")
    [ -z "$ord_id" ] && ord_id="未设置"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}📤 当前订单ID:${NC} ${GREEN}${ord_id}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    # 过滤：unique_id 包含 _sig 或 symbol = TEST_SYMBOL
    local response=$(curl_exec "GET" "${BASE_URL}/api/collections/orders/records?sort=-created&filter=((unique_id~'_sig'||unique_id~'_test'||trade_group_id~'_sig'||entry_order_unique_id~'_sig')&&symbol='${TEST_SYMBOL}')&perPage=100" "" "查询测试订单")
    echo "$response" | jq '.' 2>/dev/null || echo "$response"
}

# ============================================================
# ⚙️ 设置
# ============================================================

# G. 设置测试日期和时间
set_g_date() {
    echo ""
    echo -e "${GREEN}═══ ⚙️ 设置测试日期和时间 ═══${NC}"
    echo -e "当前: ${TEST_DATE} ${TEST_TIME}"
    echo ""
    echo -e "  ${YELLOW}[1]${NC} 今天           - $(date +%Y-%m-%d) ${GREEN}(默认日期)${NC}"
    echo -e "  ${YELLOW}[2]${NC} 昨天           - $(date -v-1d +%Y-%m-%d 2>/dev/null || date -d yesterday +%Y-%m-%d)"
    echo -e "  ${YELLOW}[3]${NC} 前天           - $(date -v-2d +%Y-%m-%d 2>/dev/null || date -d '2 days ago' +%Y-%m-%d)"
    echo -e "  ${YELLOW}[4]${NC} 上周一         - $(date -v-mon +%Y-%m-%d 2>/dev/null || date -d 'last monday' +%Y-%m-%d)"
    echo -e "  ${YELLOW}[5]${NC} 自定义输入日期"
    echo -n "选择日期 [1-5](默认1), 按 Enter 确认: "
    read choice

    case "$choice" in
        2) TEST_DATE=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d yesterday +%Y-%m-%d) ;;
        3) TEST_DATE=$(date -v-2d +%Y-%m-%d 2>/dev/null || date -d '2 days ago' +%Y-%m-%d) ;;
        4) TEST_DATE=$(date -v-mon +%Y-%m-%d 2>/dev/null || date -d 'last monday' +%Y-%m-%d) ;;
        5) echo -n "输入日期 (YYYY-MM-DD): " && read TEST_DATE ;;
        *) TEST_DATE=$(date +%Y-%m-%d) ;;
    esac

    echo ""
    echo -e "  ${YELLOW}[1]${NC} 当前时间       - $(date +%H:%M:%S) ${GREEN}(默认)${NC}"
    echo -e "  ${YELLOW}[2]${NC} 开盘时间       - 09:30:00"
    echo -e "  ${YELLOW}[3]${NC} 盘中时间       - 13:00:00"
    echo -e "  ${YELLOW}[4]${NC} 收盘时间       - 16:00:00"
    echo -e "  ${YELLOW}[5]${NC} 自定义输入时间"
    echo -n "选择时间 [1-5](默认1), 按 Enter 确认: "
    read choice

    case "$choice" in
        2) TEST_TIME="09:30:00" ;;
        3) TEST_TIME="13:00:00" ;;
        4) TEST_TIME="16:00:00" ;;
        5) echo -n "输入时间 (HH:MM:SS): " && read TEST_TIME ;;
        *) TEST_TIME=$(date +%H:%M:%S) ;;
    esac

    log_info "测试时间: ${TEST_DATE} ${TEST_TIME}"
}

# S. 设置测试标的
set_s_symbol() {
    echo ""
    echo -e "${GREEN}═══ ⚙️ 设置测试标的 ═══${NC}"
    echo -e "当前: ${TEST_SYMBOL}"
    echo -e "  ${YELLOW}[1]${NC} TEST   - 测试用"
    echo -e "  ${YELLOW}[2]${NC} AAPL  - Apple ${GREEN}(默认)${NC}"
    echo -e "  ${YELLOW}[3]${NC} TSLA  - Tesla"
    echo -e "  ${YELLOW}[4]${NC} SPY   - S&P 500 ETF"
    echo -e "  ${YELLOW}[5]${NC} QQQ   - Nasdaq ETF"
    echo -e "  ${YELLOW}[6]${NC} NVDA  - Nvidia"
    echo -e "  ${YELLOW}[7]${NC} AMD   - AMD"
    echo -e "  ${YELLOW}[8]${NC} META  - Meta"
    echo -e "  ${YELLOW}[9]${NC} 自定义"
    echo -n "选择 [1-9](默认2), 按 Enter 确认: "
    read choice

    case "$choice" in
        1) TEST_SYMBOL="TEST" ;;
        3) TEST_SYMBOL="TSLA" ;;
        4) TEST_SYMBOL="SPY" ;;
        5) TEST_SYMBOL="QQQ" ;;
        6) TEST_SYMBOL="NVDA" ;;
        7) TEST_SYMBOL="AMD" ;;
        8) TEST_SYMBOL="META" ;;
        9) echo -n "输入标的: " && read TEST_SYMBOL ;;
        *) TEST_SYMBOL="AAPL" ;;
    esac

    log_info "测试标的: ${TEST_SYMBOL}"
}

# T. 设置测试方向
set_t_direction() {
    echo ""
    echo -e "${GREEN}═══ ⚙️ 设置测试方向 ═══${NC}"
    echo -e "当前: ${TEST_DIRECTION}"
    echo -e "  ${YELLOW}[1]${NC} long   - 做多 ${GREEN}(默认)${NC}"
    echo -e "  ${YELLOW}[2]${NC} short  - 做空"
    echo -n "选择 [1-2](默认1), 按 Enter 确认: "
    read choice

    case "$choice" in
        2) TEST_DIRECTION="short" ;;
        *) TEST_DIRECTION="long" ;;
    esac

    log_info "测试方向: ${TEST_DIRECTION}"
}

# ============================================================
# 主循环
# ============================================================

check_deps() {
    command -v jq >/dev/null 2>&1 || log_warn "jq 未安装 (brew install jq)"
    command -v bc >/dev/null 2>&1 || log_warn "bc 未安装 (brew install bc)"
}

main() {
    check_deps
    show_banner

    while true; do
        show_menu

        # 读取输入（等待回车确认）
        read key

        # 显示确认信息
        echo ""
        echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -n "执行: "

        case "$key" in
            1) echo -e "${MAGENTA}发送信号到 PB${NC}" ;;
            2) echo -e "${MAGENTA}查询信号状态${NC}" ;;
            3) echo -e "${MAGENTA}飞书-确认信号${NC}" ;;
            4) echo -e "${MAGENTA}飞书-拒绝信号${NC}" ;;
            5) echo -e "${MAGENTA}QC确认信号 → 创建交易组Init${NC}" ;;
            6) echo -e "${MAGENTA}QC同步 Entry Submitted${NC}" ;;
            7) echo -e "${MAGENTA}Entry Filled + 激活 TP/SL${NC}" ;;
            8) echo -e "${MAGENTA}同步 TP 成交 + SL 对手单取消${NC}" ;;
            9) echo -e "${MAGENTA}同步 SL 成交 + TP 对手单取消${NC}" ;;
            O|o) echo -e "${MAGENTA}飞书-取消订单${NC}" ;;
            P|p) echo -e "${MAGENTA}飞书-平仓订单${NC}" ;;
            A|a) echo -e "${MAGENTA}计算逆向信号${NC}" ;;
            B|b) echo -e "${MAGENTA}查询逆向信号${NC}" ;;
            C|c) echo -e "${MAGENTA}确认逆向信号${NC}" ;;
            D|d) echo -e "${MAGENTA}发送指标数据${NC}" ;;
            E|e) echo -e "${CYAN}查询所有信号${NC}" ;;
            F|f) echo -e "${CYAN}查询所有订单${NC}" ;;
            X|x) echo -e "${YELLOW}清理所有测试数据${NC}" ;;
            G|g) echo -e "${GREEN}设置测试日期${NC}" ;;
            S|s) echo -e "${GREEN}设置测试标的${NC}" ;;
            T|t) echo -e "${GREEN}设置测试方向${NC}" ;;
            Q|q)
                echo -e "${RED}退出${NC}"
                log_info "退出"
                exit 0
                ;;
            *)
                echo -e "${RED}无效选项: $key${NC}"
                echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                echo ""
                echo -n "按 Enter 继续..."
                read
                continue
                ;;
        esac
        echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

        # 执行操作
        case "$key" in
            1) test_1_send_signal ;;
            2) test_2_query_signals ;;
            3) test_3_feishu_confirm ;;
            4) test_4_feishu_reject ;;
            5) test_5_qc_ack_signal ;;
            6) test_6_qc_order_submitted ;;
            7) test_7_qc_order_filled ;;
            8) test_8_qc_order_takeprofit ;;
            9) test_9_qc_order_stoploss ;;
            O|o) test_o_cancel_order ;;
            P|p) test_p_close_order ;;
            A|a) test_a_calc_reverse ;;
            B|b) test_b_query_reverse ;;
            C|c) test_c_ack_reverse ;;
            D|d) test_d_send_indicator ;;
            E|e) test_e_list_signals ;;
            F|f) test_f_list_orders ;;
            X|x) cleanup_today_data ;;
            G|g) set_g_date ;;
            S|s) set_s_symbol ;;
            T|t) set_t_direction ;;
        esac

        echo ""
        echo -n "按 Enter 继续..."
        read
    done
}

main "$@"
