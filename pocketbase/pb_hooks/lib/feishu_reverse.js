/**
 * feishu_reverse.js
 * 反转信号卡片构建与通知
 */

var feishuApp = require(`${__hooks}/lib/feishu_app.js`)
var reverseUtils = require(`${__hooks}/lib/reverse_utils.js`)
var envUtils = require(`${__hooks}/lib/environment.js`)

var PB_HOST = "https://pb.lzw-glory.top"

function extractDateFromIdentifier(value) {
    var match = String(value || "").match(/(?:^|_)(20\d{6})(?:_|$)/)
    if (!match) return ""
    var token = match[1]
    return token.slice(0, 4) + "-" + token.slice(4, 6) + "-" + token.slice(6, 8)
}

function extractDateFromBarTimeMs(barTimeMs) {
    var numeric = Number(barTimeMs)
    if (!numeric) return ""
    try {
        return new Date(numeric).toISOString().slice(0, 10)
    } catch (e) {
        return ""
    }
}

function resolvePageDate(reverse) {
    return extractDateFromBarTimeMs(reverse.bar_time_ms)
        || extractDateFromIdentifier(reverse.origin_signal_id)
        || extractDateFromIdentifier(reverse.signal_id)
        || extractDateFromIdentifier(reverse.trade_group_id)
        || ""
}

function buildOpenLinkButton(label, url) {
    return {
        tag: "button",
        text: { tag: "plain_text", content: label },
        type: "default",
        width: "fill",
        multi_url: {
            url: url,
            pc_url: url,
            ios_url: url,
            android_url: url
        }
    }
}

function toNumber(value) {
    var num = Number(value)
    return isNaN(num) ? 0 : num
}

function formatMoney(value) {
    var num = toNumber(value)
    return num ? "$" + num.toFixed(2) : "N/A"
}

function formatSignedMoney(value, sign) {
    var num = toNumber(value)
    if (!num) return "N/A"
    return (sign || "") + "$" + num.toFixed(2)
}

function calculateAmount(entryPrice, targetPrice, qty) {
    var entry = toNumber(entryPrice)
    var target = toNumber(targetPrice)
    var quantity = Math.abs(toNumber(qty))
    if (!entry || !target || !quantity) return 0
    return Math.abs(target - entry) * quantity
}

function formatStatusText(status) {
    var map = {
        pending: { emoji: "⏳", text: "待执行" },
        confirmed: { emoji: "✅", text: "已处理" },
        cancelled: { emoji: "❌", text: "已取消" },
        expired: { emoji: "⌛", text: "已过期" },
    }
    return map[status] || { emoji: "🔄", text: status || "未知" }
}

function formatActionText(actionType) {
    var map = {
        close: "平旧仓",
        cancel: "撤旧挂单",
        adjust_sl: "调止损",
        adjust_tp: "调止盈",
    }
    return map[actionType] || actionType || "N/A"
}

function formatReverseKind(reverseKind) {
    var map = {
        signal_conflict: "信号反转",
        indicator_conflict: "指标反转",
    }
    return map[reverseKind] || reverseKind || "N/A"
}

function formatTargetState(targetState) {
    var map = {
        pending_entry: "待成交挂单",
        filled_position: "已成交持仓",
    }
    return map[targetState] || targetState || "N/A"
}

function formatDirection(direction) {
    if (direction === "long") return "做多 📈"
    if (direction === "short") return "做空 📉"
    return direction || "N/A"
}

function buildReversePageUrl(reverse) {
    if (!reverse.id) return ""
    var url = PB_HOST + "/ibkr_reverse_signals.html?reverse_id=" + encodeURIComponent(reverse.id)
    if (reverse.environment) {
        url += "&environment=" + encodeURIComponent(reverse.environment)
    }
    var pageDate = resolvePageDate(reverse)
    if (pageDate) {
        url += "&date=" + encodeURIComponent(pageDate)
    }
    return url
}

function buildSignalPageUrl(reverse) {
    var signalId = reverse.signal_id || reverse.origin_signal_id
    if (!signalId) return ""
    var url = PB_HOST + "/ibkr_signals.html?signal_id=" + encodeURIComponent(signalId)
    if (reverse.environment) {
        url += "&environment=" + encodeURIComponent(reverse.environment)
    }
    var pageDate = resolvePageDate(reverse)
    if (pageDate) {
        url += "&date=" + encodeURIComponent(pageDate)
    }
    return url
}

function buildOrderPageUrl(reverse) {
    var pageDate = resolvePageDate(reverse)
    if (reverse.trade_group_id) {
        var detailUrl = PB_HOST + "/ibkr_order_details.html?trade_group_id=" + encodeURIComponent(reverse.trade_group_id)
        if (reverse.signal_id || reverse.origin_signal_id) {
            detailUrl += "&signal_id=" + encodeURIComponent(reverse.signal_id || reverse.origin_signal_id)
        }
        if (reverse.environment) {
            detailUrl += "&environment=" + encodeURIComponent(reverse.environment)
        }
        if (pageDate) {
            detailUrl += "&date=" + encodeURIComponent(pageDate)
        }
        return detailUrl
    }
    if (reverse.signal_id || reverse.origin_signal_id) {
        var listUrl = PB_HOST + "/ibkr_orders.html?signal_id=" + encodeURIComponent(reverse.signal_id || reverse.origin_signal_id)
        if (reverse.environment) {
            listUrl += "&environment=" + encodeURIComponent(reverse.environment)
        }
        if (pageDate) {
            listUrl += "&date=" + encodeURIComponent(pageDate)
        }
        return listUrl
    }
    return ""
}

function buildViewElements(reverse) {
    var columns = []
    var reverseUrl = buildReversePageUrl(reverse)
    var signalUrl = buildSignalPageUrl(reverse)
    var orderUrl = buildOrderPageUrl(reverse)

    if (reverseUrl) {
        columns.push({
            tag: "column",
            width: "weighted",
            weight: 1,
            elements: [buildOpenLinkButton("🔄 查看反转", reverseUrl)]
        })
    }
    if (signalUrl) {
        columns.push({
            tag: "column",
            width: "weighted",
            weight: 1,
            elements: [buildOpenLinkButton("📡 查看信号", signalUrl)]
        })
    }
    if (orderUrl) {
        columns.push({
            tag: "column",
            width: "weighted",
            weight: 1,
            elements: [buildOpenLinkButton("📋 查看订单", orderUrl)]
        })
    }

    if (columns.length === 0) return []
    return [{
        tag: "column_set",
        horizontal_spacing: "default",
        columns: columns
    }]
}

function buildReverseCard(recordOrData, options) {
    var reverse = reverseUtils.normalizeReverseRecord(recordOrData)
    var statusInfo = formatStatusText(reverse.status)
    var message = options && options.message
        ? options.message
        : (reverse.reason || (statusInfo.text + "，等待页面或 IBKR 查看"))
    var entryPrice = reverse.entry_price
    var tpPrice = toNumber(reverse.new_tp || reverse.take_profit)
    var slPrice = toNumber(reverse.new_sl || reverse.stop_loss)
    var tpAmount = calculateAmount(entryPrice, tpPrice, reverse.quantity)
    var slAmount = calculateAmount(entryPrice, slPrice, reverse.quantity)

    var elements = [{
        tag: "column_set",
        horizontal_spacing: "default",
        columns: [
            {
                tag: "column",
                width: "weighted",
                weight: 1,
                vertical_spacing: "2px",
                elements: [
                    { tag: "div", text: { tag: "lark_md", content: "**标的:** " + (reverse.symbol || "N/A") } },
                    { tag: "div", text: { tag: "lark_md", content: "**当前方向:** " + formatDirection(reverse.current_direction || reverse.direction) } },
                    reverse.new_direction ? { tag: "div", text: { tag: "lark_md", content: "**新信号方向:** " + formatDirection(reverse.new_direction) } } : null,
                    { tag: "div", text: { tag: "lark_md", content: "**类型:** " + formatReverseKind(reverse.reverse_kind) } },
                    { tag: "div", text: { tag: "lark_md", content: "**目标状态:** " + formatTargetState(reverse.target_state) } },
                    { tag: "div", text: { tag: "lark_md", content: "**动作:** " + formatActionText(reverse.action_type) } },
                    { tag: "div", text: { tag: "lark_md", content: "**强度 / 评分:** " + (reverse.strength || "N/A") + " / " + toNumber(reverse.score).toFixed(1) } },
                ].filter(Boolean)
            },
            {
                tag: "column",
                width: "weighted",
                weight: 1,
                vertical_spacing: "2px",
                elements: [
                    { tag: "div", text: { tag: "lark_md", content: "**交易组:** " + (reverse.trade_group_id || "N/A") } },
                    { tag: "div", text: { tag: "lark_md", content: "**主单ID:** " + (reverse.entry_order_unique_id || "N/A") } },
                    { tag: "div", text: { tag: "lark_md", content: "**原始OrderID:** " + (reverse.broker_order_id || "N/A") } },
                    { tag: "div", text: { tag: "lark_md", content: "**SignalID:** " + (reverse.signal_id || "N/A") } },
                    reverse.origin_signal_id && reverse.origin_signal_id !== reverse.signal_id
                        ? { tag: "div", text: { tag: "lark_md", content: "**触发信号ID:** " + reverse.origin_signal_id } }
                        : null,
                    { tag: "div", text: { tag: "lark_md", content: "**状态:** " + statusInfo.text } },
                    reverse.manual_requested_at
                        ? { tag: "div", text: { tag: "lark_md", content: "**人工执行请求:** " + reverse.manual_requested_at } }
                        : null,
                ].filter(Boolean)
            }
        ]
    }]

    if (reverse.triggered_signals && reverse.triggered_signals.length > 0) {
        elements.push({
            tag: "div",
            text: { tag: "lark_md", content: "**触发条件:** " + reverse.triggered_signals.join(", ") }
        })
    }

    if (entryPrice || tpPrice || slPrice) {
        elements.push({
            tag: "column_set",
            horizontal_spacing: "default",
            columns: [
                {
                    tag: "column",
                    width: "weighted",
                    weight: 1,
                    vertical_spacing: "2px",
                    elements: [
                        { tag: "div", text: { tag: "lark_md", content: "**入场价:** " + formatMoney(entryPrice) } },
                        { tag: "div", text: { tag: "lark_md", content: "**止盈 / 预估盈利:** " + formatMoney(tpPrice) + " / " + formatSignedMoney(tpAmount, "+") } },
                        { tag: "div", text: { tag: "lark_md", content: "**止损 / 预估亏损:** " + formatMoney(slPrice) + " / " + formatSignedMoney(slAmount, "-") } },
                    ]
                },
                {
                    tag: "column",
                    width: "weighted",
                    weight: 1,
                    vertical_spacing: "2px",
                    elements: [
                        reverse.old_sl || reverse.new_sl
                            ? { tag: "div", text: { tag: "lark_md", content: "**SL变更:** " + formatMoney(reverse.old_sl) + " → " + formatMoney(reverse.new_sl || reverse.old_sl) } }
                            : null,
                        reverse.old_tp || reverse.new_tp
                            ? { tag: "div", text: { tag: "lark_md", content: "**TP变更:** " + formatMoney(reverse.old_tp) + " → " + formatMoney(reverse.new_tp || reverse.old_tp) } }
                            : null,
                        reverse.result_status
                            ? { tag: "div", text: { tag: "lark_md", content: "**执行结果:** " + reverse.result_status } }
                            : null,
                    ].filter(Boolean)
                }
            ]
        })
    }

    if (message) {
        elements.push({
            tag: "div",
            text: { tag: "lark_md", content: "**说明:** " + message }
        })
    }

    var viewElements = buildViewElements(reverse)
    if (viewElements.length > 0) {
        elements.push({ tag: "hr" })
        viewElements.forEach(function(element) { elements.push(element) })
    }

    return {
        schema: "2.0",
        config: { update_multi: true },
        header: {
            title: { tag: "plain_text", content: envUtils.labelTitleWithEnvironment(statusInfo.emoji + " 反转信号 · " + (reverse.symbol || "-"), reverse.environment || envUtils.LIVE_ENVIRONMENT) },
            template: reverse.current_direction === "short" ? "red" : "green"
        },
        body: {
            direction: "vertical",
            elements: elements
        }
    }
}

function persistMessageId(recordOrData, messageId) {
    if (!messageId || typeof recordOrData.get !== "function") return
    reverseUtils.mergeReverseExtra(recordOrData, {
        feishu_reverse_message_id: messageId,
        feishu_reverse_card_version: 1,
    }, true)
}

function getMessageId(recordOrData) {
    var extra = reverseUtils.getReverseExtra(recordOrData)
    return extra.feishu_reverse_message_id || ""
}

function notifyReverseSignal(recordOrData, options) {
    var card = buildReverseCard(recordOrData, options || {})
    var result = feishuApp.sendCardToChatByTypeDetailed(card, "reverse")
    if (result.success && result.message_id) {
        persistMessageId(recordOrData, result.message_id)
    }
    return result
}

function notifyReverseStatus(action, recordOrData, options) {
    var card = buildReverseCard(recordOrData, options || {})
    var messageId = (options && options.messageId) || getMessageId(recordOrData)
    var result = messageId ? feishuApp.updateMessageCard(messageId, card) : feishuApp.sendCardToChatByTypeDetailed(card, "reverse")
    if (result.success && (result.message_id || messageId)) {
        persistMessageId(recordOrData, result.message_id || messageId)
    }
    console.log("[FeishuReverse] 卡片同步:", result.success ? "成功" : "失败", "action:", action, "message_id:", result.message_id || messageId || "-")
    return result
}

module.exports = {
    buildReverseCard: buildReverseCard,
    notifyReverseSignal: notifyReverseSignal,
    notifyReverseStatus: notifyReverseStatus,
}
