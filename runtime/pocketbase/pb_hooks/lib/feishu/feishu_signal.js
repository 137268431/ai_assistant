/**
 * feishu_signal.js
 * 飞书信号卡片构建、通知与回调处理
 */

var feishuApp = require(`${__hooks}/lib/feishu_app.js`)
var envUtils = require(`${__hooks}/lib/environment.js`)
var PB_HOST = "https://pb.lzw-glory.top"

function formatDateToken(dateToken) {
    if (!dateToken || !/^\d{8}$/.test(String(dateToken))) return ""
    var text = String(dateToken)
    return text.slice(0, 4) + "-" + text.slice(4, 6) + "-" + text.slice(6, 8)
}

function extractDateFromIdentifier(value) {
    var match = String(value || "").match(/(?:^|_)(20\d{6})(?:_|$)/)
    return match ? formatDateToken(match[1]) : ""
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

function resolveSignalPageDate(signalId, barTimeMs, usTime, cnTime) {
    return extractDateFromBarTimeMs(barTimeMs)
        || extractDateFromIdentifier(signalId)
        || extractDateFromIdentifier(usTime)
        || extractDateFromIdentifier(cnTime)
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

function buildSignalPageUrl(d) {
    if (!d || !d.signal_id) return ""
    var url = PB_HOST + "/ibkr_signals.html?signal_id=" + encodeURIComponent(d.signal_id)
    if (d.page_date) {
        url += "&date=" + encodeURIComponent(d.page_date)
    }
    return url
}

function buildSignalOrdersPageUrl(d) {
    if (!d || !d.signal_id) return ""
    var url = PB_HOST + "/orders.html?signal_id=" + encodeURIComponent(d.signal_id)
    if (d.page_date) {
        url += "&date=" + encodeURIComponent(d.page_date)
    }
    return url
}

function buildSignalViewElements(d) {
    var columns = []
    var signalUrl = buildSignalPageUrl(d)
    var ordersUrl = buildSignalOrdersPageUrl(d)

    if (signalUrl) {
        columns.push({
            tag: "column",
            width: "weighted",
            weight: 1,
            elements: [buildOpenLinkButton("📡 查看信号", signalUrl)]
        })
    }
    if (ordersUrl) {
        columns.push({
            tag: "column",
            width: "weighted",
            weight: 1,
            elements: [buildOpenLinkButton("📋 查看订单", ordersUrl)]
        })
    }

    if (columns.length === 0) return []
    return [{
        tag: "column_set",
        horizontal_spacing: "default",
        columns: columns
    }]
}

function getSignalExtra(recordOrData) {
    if (!recordOrData) return {}

    if (typeof recordOrData.getString === "function") {
        var raw = recordOrData.getString("extra") || ""
        if (raw) {
            try {
                var parsed = JSON.parse(raw)
                return parsed && typeof parsed === "object" ? parsed : {}
            } catch (e) {
                console.error("[FeishuSignal] 解析 extra JSON 失败:", e)
            }
        }
    }

    var value = (typeof recordOrData.get === "function") ? recordOrData.get("extra") : recordOrData.extra
    if (value && typeof value === "object") return value
    if (typeof value === "string" && value) {
        try {
            var parsedValue = JSON.parse(value)
            return parsedValue && typeof parsedValue === "object" ? parsedValue : {}
        } catch (e) {
            console.error("[FeishuSignal] 解析 extra 字符串失败:", e)
        }
    }

    return {}
}

function mergeSignalExtra(record, patch, saveAfterMerge) {
    var merged = {
        ...getSignalExtra(record),
        ...(patch || {}),
    }
    record.set("extra", merged)
    if (saveAfterMerge) {
        $app.save(record)
    }
    return merged
}

function resolveSignalStatusReasonText(status, recordOrData) {
    var extra = getSignalExtra(recordOrData)
    var get = (recordOrData && typeof recordOrData.get === "function")
        ? recordOrData.get.bind(recordOrData)
        : function(k) { return recordOrData ? recordOrData[k] : undefined }
    var raw = String(
        extra.status_reason
        || extra.initial_status_reason
        || extra.expired_reason
        || get("note")
        || ""
    ).trim()
    if (!raw) return ""

    var normalized = raw.toLowerCase()
    var reasonMap = {
        outside_trade_window: "当前不在交易窗口，系统已自动拒绝",
        outside_order_window: "当前已超出下单窗口，系统已自动拒绝",
        manual_confirmation_required: "等待人工确认",
        signal_expired: "信号已过期",
        manual_rejected: "已由用户手动拒绝",
        duplicate_existing_broker_order: "检测到账户已有同方向挂单，系统已自动拒绝",
        confirmed_by_user: "已由用户确认",
    }
    return reasonMap[normalized] || raw
}

function isSignalRecordLike(value) {
    return !!(value && typeof value.get === "function" && typeof value.set === "function")
}

function getSignalNotificationState(record) {
    var extra = getSignalExtra(record)
    return {
        extra: extra,
        signalId: String(record && record.get ? (record.get("signal_id") || "") : "").trim(),
        status: String(record && record.get ? (record.get("status") || "") : "").trim(),
        messageId: String(extra.feishu_signal_message_id || "").trim(),
        lastNotifyKey: String(extra.feishu_signal_notify_key || "").trim(),
        inflightKey: String(extra.feishu_signal_notify_inflight_key || "").trim(),
        inflightAtMs: Number(extra.feishu_signal_notify_inflight_at_ms || 0) || 0,
    }
}

function buildSignalNotificationKey(action, signalId, status) {
    return [
        "signal_notify_v1",
        String(action || "").trim(),
        String(signalId || "").trim(),
        String(status || "").trim(),
    ].join(":")
}

function beginSignalNotification(record, notifyKey) {
    if (!isSignalRecordLike(record) || !notifyKey) return
    mergeSignalExtra(record, {
        feishu_signal_notify_inflight_key: notifyKey,
        feishu_signal_notify_inflight_at_ms: Date.now(),
    }, true)
}

function finalizeSignalNotification(record, notifyKey, action, result) {
    if (!isSignalRecordLike(record) || !notifyKey) return
    var nowMs = Date.now()
    var existingExtra = getSignalExtra(record)
    var patch = {
        feishu_signal_notify_last_action: String(action || "").trim(),
        feishu_signal_notify_last_status: String(record.get("status") || "").trim(),
        feishu_signal_notify_last_result: result && result.success ? "success" : "failed",
        feishu_signal_notify_last_at_ms: nowMs,
    }

    if (result && result.success) {
        patch.feishu_signal_notify_key = notifyKey
        patch.feishu_signal_notify_sent_at_ms = nowMs
        patch.feishu_signal_notify_inflight_key = ""
        patch.feishu_signal_notify_inflight_at_ms = 0
        patch.feishu_signal_notify_error = ""
        if (result.message_id) {
            patch.feishu_signal_message_id = result.message_id
            patch.feishu_signal_card_version = 1
        }
        if (String(action || "").trim() === "new") {
            patch.feishu_signal_first_sent_at_ms = Number(existingExtra.feishu_signal_first_sent_at_ms || 0) || nowMs
        }
    } else {
        patch.feishu_signal_notify_error = String((result && result.error) || "unknown_error")
    }

    mergeSignalExtra(record, patch, true)
}

function shouldSkipSignalNotification(record, action) {
    if (!isSignalRecordLike(record)) {
        return { skip: false, notifyKey: "" }
    }

    var state = getSignalNotificationState(record)
    var notifyKey = buildSignalNotificationKey(action, state.signalId, state.status)
    var now = Date.now()
    var inflightTtlMs = 2 * 60 * 1000

    if (state.lastNotifyKey && state.lastNotifyKey === notifyKey) {
        console.log("[FeishuSignal] 跳过重复信号通知:", "signal_id:", state.signalId || "-", "action:", action, "status:", state.status || "-", "reason:", "same_notify_key")
        return {
            skip: true,
            notifyKey: notifyKey,
            result: { success: true, skipped: true, deduped: true, message_id: state.messageId || "" }
        }
    }

    if (state.inflightKey && state.inflightKey === notifyKey && state.inflightAtMs > 0 && (now - state.inflightAtMs) < inflightTtlMs) {
        console.log("[FeishuSignal] 跳过短时重复信号通知:", "signal_id:", state.signalId || "-", "action:", action, "status:", state.status || "-", "reason:", "inflight_recent")
        return {
            skip: true,
            notifyKey: notifyKey,
            result: { success: true, skipped: true, deduped: true, message_id: state.messageId || "" }
        }
    }

    return { skip: false, notifyKey: notifyKey, messageId: state.messageId }
}

function resolveSignalSourceInfo(extra, topLevelSource, topLevelSourceKind) {
    var sourceKey = String(
        (extra && (extra.signal_source || extra.source))
        || topLevelSource
        || ""
    ).trim().toLowerCase()
    var sourceKind = String((extra && extra.source_kind) || topLevelSourceKind || "").trim().toLowerCase()
    var info = {
        key: sourceKey || "unknown",
        label: "",
        detail: ""
    }

    if (sourceKey === "tradingview_webhook" || sourceKey === "tradingview" || sourceKey === "tv" || sourceKey === "webhook_tv") {
        info.label = "TradingView Webhook"
        info.detail = "来自 TradingView webhook 信号"
    } else if (sourceKey === "ibkr_compute_timeline" || sourceKey === "timeline" || sourceKey === "chart_timeline") {
        info.label = "IBKR 图表回放"
        info.detail = "来自缓存 bars 时间线重算"
    } else if (sourceKey === "ibkr_history_recompute" || sourceKey === "history_repair" || sourceKey === "recompute") {
        info.label = "IBKR 历史重算"
        info.detail = "来自历史回补/重算链路"
    } else if (sourceKey === "manual_order" || sourceKey === "manual" || sourceKey === "runtime_page" || sourceKey === "account_page") {
        info.label = "手动触发"
        info.detail = "来自账户页/人工操作"
    } else if (sourceKey === "ibkr_compute_realtime" || sourceKey === "ibkr_compute" || sourceKey === "ibkr_runtime" || sourceKey === "ibkr") {
        info.label = "IBKR 实时计算"
        info.detail = "来自 IBKR 实盘 bars 收盘计算"
    } else if (sourceKind === "computed") {
        info.label = "计算生成"
        info.detail = "来自系统计算链路"
    }

    if (extra && extra.signal_source_label) {
        info.label = String(extra.signal_source_label)
    }
    if (extra && extra.signal_source_detail) {
        info.detail = String(extra.signal_source_detail)
    }
    if (!info.label) {
        info.label = sourceKey ? sourceKey.toUpperCase() : "未知来源"
    }
    return info
}

function getSignalStatusInfo(status) {
    var map = {
        expired: { emoji: "⏰", text: "已过期" },
        rejected: { emoji: "❌", text: "已拒绝" },
        executed: { emoji: "✅", text: "已执行" },
        pending: { emoji: "⏳", text: "待执行" },
        closed: { emoji: "🔒", text: "已平仓" },
        awaiting_confirm: { emoji: "⏳", text: "待确认" }
    }
    return map[status] || { emoji: "❓", text: status || "未知" }
}

/**
 * 从 ibkr_signals record 构建展示用数据（回调和通知共用）
 * 兼容 PB record（有 get() 方法）和 plain object
 */
function buildSignalDisplayData(recordOrData) {
    var get = (typeof recordOrData.get === "function") ? recordOrData.get.bind(recordOrData) : function(k) { return recordOrData[k] }

    var symbol = get("symbol") || ""
    var direction = get("direction") || "long"
    var entry = Number(get("entry")) || 0
    var take_profit = Number(get("take_profit")) || 0
    var stop_loss = Number(get("stop_loss")) || 0
    var shares = Number(get("shares")) || 0
    var rr = get("rr") || "N/A"
    var signal_id = get("signal_id") || ""
    var us_time = get("us_time") || ""
    var cn_time = get("cn_time") || ""
    var extra = getSignalExtra(recordOrData)
    var environment = get("environment") || extra.environment || envUtils.LIVE_ENVIRONMENT
    var bar_time_ms = Number(get("bar_time_ms") || extra.bar_time_ms || 0) || 0
    var sourceInfo = resolveSignalSourceInfo(extra, get("source"), get("source_kind"))
    var status = get("status") || extra.current_status || ""
    var statusReason = resolveSignalStatusReasonText(status, recordOrData)

    var reason = extra.reason || get("reason") || ""
    var changeDisplay = "N/A"
    var dayPct = Number(extra.day_change_pct || 0)
    var prevPct = Number(extra.prev_close_change_pct || 0)
    var d7Pct = Number(extra.change_7d || 0)
    if (extra.day_change_pct !== undefined) {
        changeDisplay = (dayPct > 0 ? "+" : "") + dayPct.toFixed(2) + "%/" + (prevPct > 0 ? "+" : "") + prevPct.toFixed(2) + "%/" + (d7Pct > 0 ? "+" : "") + d7Pct.toFixed(2) + "%"
    }

    var isLong = direction === "long"
    var tpProfit = (isLong ? (take_profit - entry) : (entry - take_profit)) * shares
    var slLoss = (isLong ? (entry - stop_loss) : (stop_loss - entry)) * shares
    var formatAmount = function(a) {
        if (!a || a === 0) return "0"
        if (Math.abs(a) >= 10000) return (a / 10000).toFixed(2) + "w"
        return a.toFixed(2)
    }

    var atrText = null
    if (extra.atr_pct) {
        var atrLevel = extra.atr_pct >= 3 ? "高" : extra.atr_pct >= 1.5 ? "中" : "低"
        var atrEmoji = extra.atr_pct >= 3 ? "⚡" : extra.atr_pct >= 1.5 ? "〜" : "·"
        atrText = "**波动率:** " + atrEmoji + " " + atrLevel + " " + extra.atr_pct.toFixed(2) + "%"
        if (extra.sl_atr_ratio) {
            atrText += "\n**ATR止损:** " + extra.sl_atr_ratio.toFixed(1) + "倍"
        }
    } else if (extra.atr) {
        atrText = "**ATR:** " + extra.atr.toFixed(2)
    }

    var marketIndexes = extra.market_indexes || []
    if (marketIndexes.length === 0) {
        try {
            var fetched = []
            var marketSyms = ["SPY", "QQQ", "VIX"]
            for (var i = 0; i < marketSyms.length; i++) {
                var msym = marketSyms[i]
                try {
                    var recs = $app.findRecordsByFilter(
                        "ibkr_indicators",
                        "(environment = {:env} || environment = '') && symbol = {:sym}",
                        "-bar_time_ms",
                        1,
                        0,
                        { env: environment, sym: msym }
                    )
                    if (recs && recs.length > 0) {
                        var indExtraStr = recs[0].getString("extra")
                        var indExtra = {}
                        if (indExtraStr) {
                            try { indExtra = JSON.parse(indExtraStr) } catch (e) {}
                        }
                        var indChange = indExtra.day_change_pct
                        if (indChange !== undefined) {
                            fetched.push({ symbol: msym, change_pct: Number(indChange) })
                        }
                    }
                } catch (e) {}
            }
            if (fetched.length > 0) {
                marketIndexes = fetched
                console.log("[FeishuSignal] 从 ibkr_indicators 表查询 market_indexes:", JSON.stringify(fetched))
            }
        } catch (e) {
            console.log("[FeishuSignal] 查询 ibkr_indicators 表失败:", e)
        }
    }

    var marketInfoText = null
    if (marketIndexes.length > 0) {
        var spyD = marketIndexes.find(function(m) { return m.symbol === "SPY" })
        var qqqD = marketIndexes.find(function(m) { return m.symbol === "QQQ" })
        var vixD = marketIndexes.find(function(m) { return m.symbol === "VIX" })
        var mParts = []
        if (spyD) mParts.push("SPY: " + (spyD.change_pct > 0 ? "+" : "") + Number(spyD.change_pct).toFixed(2) + "%")
        if (qqqD) mParts.push("QQQ: " + (qqqD.change_pct > 0 ? "+" : "") + Number(qqqD.change_pct).toFixed(2) + "%")
        if (mParts.length > 0) marketInfoText = "**大盘:** " + mParts.join(" | ")
        if (vixD) {
            var v = Number(vixD.change_pct)
            var vixLevel = v >= 20 ? "🔴 恐慌" : v >= 10 ? "🟡 紧张" : "🟢 平稳"
            marketInfoText = marketInfoText ? (marketInfoText + "\n") : ""
            marketInfoText += "**VIX恐慌:** " + vixLevel + " " + (v > 0 ? "+" : "") + v.toFixed(1) + "%"
        }
    }

    return {
        symbol: symbol,
        direction: direction,
        entry: entry,
        take_profit: take_profit,
        stop_loss: stop_loss,
        shares: shares,
        rr: rr,
        signal_id: signal_id,
        environment: environment,
        status: status,
        us_time: us_time,
        cn_time: cn_time,
        bar_time_ms: bar_time_ms,
        page_date: resolveSignalPageDate(signal_id, bar_time_ms, us_time, cn_time),
        reason: reason,
        sourceKey: sourceInfo.key,
        sourceLabel: sourceInfo.label,
        sourceDetail: sourceInfo.detail,
        statusReason: statusReason,
        changeDisplay: changeDisplay,
        tpProfit: tpProfit,
        slLoss: slLoss,
        formatAmount: formatAmount,
        atrText: atrText,
        marketInfoText: marketInfoText,
        marketIndexes: marketIndexes,
        extra: extra
    }
}

function buildSignalInfoElements(d) {
    var leftColumn = []
    var rightColumn = []
    var elements = []
    var directionText = d.direction === "long" ? "做多 📈" : "做空 📉"

    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**标的:** " + d.symbol } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**方向:** " + directionText } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**涨幅:** " + d.changeDisplay } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**入场:** $" + d.entry.toFixed(2) } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止盈:** $" + d.take_profit.toFixed(2) } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止损:** $" + d.stop_loss.toFixed(2) } })

    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**盈利:** +$" + d.formatAmount(d.tpProfit) } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**亏损:** -$" + d.formatAmount(d.slLoss) } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**风报比:** " + d.rr } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**股数:** " + (d.shares || "N/A") } })
    if (d.atrText) {
        d.atrText.split("\n").forEach(function(line) {
            rightColumn.push({ tag: "div", text: { tag: "lark_md", content: line } })
        })
    }

    elements.push({
        tag: "column_set",
        horizontal_spacing: "default",
        columns: [
            { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: leftColumn },
            { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: rightColumn }
        ]
    })

    var infoElements = []
    infoElements.push({ tag: "div", text: { tag: "lark_md", content: "**信号ID:** " + d.signal_id } })
    if (d.sourceLabel) {
        var sourceContent = "**来源:** " + d.sourceLabel
        if (d.sourceDetail) {
            sourceContent += "\n**由来:** " + d.sourceDetail
        }
        infoElements.push({ tag: "div", text: { tag: "lark_md", content: sourceContent } })
    }
    if (d.reason) {
        infoElements.push({ tag: "div", text: { tag: "lark_md", content: "**原因:** " + d.reason } })
    }
    if (d.statusReason) {
        var reasonLabel = (d.status === "rejected" || d.status === "expired") ? "处理原因" : "状态原因"
        infoElements.push({ tag: "div", text: { tag: "lark_md", content: "**" + reasonLabel + ":** " + d.statusReason } })
    }
    if (d.marketInfoText) {
        infoElements.push({ tag: "div", text: { tag: "lark_md", content: d.marketInfoText } })
    }
    elements.push({
        tag: "column_set",
        columns: [{ tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: infoElements }]
    })

    return elements
}

function buildSignalCardV2(symbol, directionText, statusEmoji, statusText, status, color, message, extraFields, us_time) {
    var elements = []
    var displayData = arguments.length > 9 ? arguments[9] : null

    if (extraFields && extraFields.length > 0) {
        elements = extraFields.slice()
    } else {
        elements.push({ tag: "div", text: { tag: "lark_md", content: "**标的:** " + symbol } })
        elements.push({ tag: "div", text: { tag: "lark_md", content: "**方向:** " + directionText } })
    }

    elements.push({ tag: "hr" })
    elements.push({ tag: "div", text: { tag: "lark_md", content: "**状态:** " + statusText + " · " + message } })
    var viewElements = buildSignalViewElements(displayData)
    if (viewElements.length > 0) {
        elements.push({ tag: "hr" })
        viewElements.forEach(function(element) { elements.push(element) })
    }

    return {
        schema: "2.0",
        config: { update_multi: true },
        header: {
            title: { tag: "plain_text", content: envUtils.labelTitleWithEnvironment(statusEmoji + " " + statusText + " · " + symbol + " · " + (us_time || ""), displayData && displayData.environment) },
            template: color
        },
        body: {
            direction: "vertical",
            elements: elements
        }
    }
}

function buildSignalNotificationCard(signal) {
    var d = buildSignalDisplayData(signal)
    var directionText = d.direction === "long" ? "做多 📈" : "做空 📉"
    var color = d.direction === "long" ? "green" : "red"
    var signalStatus = (typeof signal.get === "function") ? signal.get("status") : (signal.status || "pending")
    var elements = buildSignalInfoElements(d)

    elements.push({ tag: "hr" })

    if (signalStatus === "awaiting_confirm") {
        elements.push({
            tag: "column_set",
            horizontal_spacing: "default",
            columns: [
                {
                    tag: "column",
                    width: "weighted",
                    weight: 1,
                    elements: [{
                        tag: "button",
                        text: { tag: "plain_text", content: "✅ 确认" },
                        type: "primary",
                        width: "fill",
                        behaviors: [{ type: "callback", value: { action: "confirm", signal_id: d.signal_id, environment: d.environment } }]
                    }]
                },
                {
                    tag: "column",
                    width: "weighted",
                    weight: 1,
                    elements: [{
                        tag: "button",
                        text: { tag: "plain_text", content: "❌ 拒绝" },
                        type: "danger",
                        width: "fill",
                        behaviors: [{ type: "callback", value: { action: "reject", signal_id: d.signal_id, environment: d.environment } }]
                    }]
                }
            ]
        })
    } else {
        elements.push({ tag: "div", text: { tag: "lark_md", content: "⚙️ **自动确认** · 信号已提交，等待执行" } })
    }

    var viewElements = buildSignalViewElements(d)
    if (viewElements.length > 0) {
        elements.push({ tag: "hr" })
        viewElements.forEach(function(element) { elements.push(element) })
    }

    return {
        schema: "2.0",
        config: { update_multi: true },
        header: {
            title: { tag: "plain_text", content: envUtils.labelTitleWithEnvironment((signalStatus === "awaiting_confirm" ? "🔔 新交易信号" : "⚙️ 自动确认") + " · " + d.symbol + " · " + d.us_time, d.environment) },
            template: color
        },
        body: {
            direction: "vertical",
            elements: elements
        }
    }
}

function getSignalStatusMessage(status) {
    var signal = arguments.length > 1 ? arguments[1] : null
    var statusReason = resolveSignalStatusReasonText(status, signal)
    if (status === "rejected" && statusReason) {
        return "信号已拒绝 · " + statusReason
    }
    if (status === "expired" && statusReason) {
        return "信号已过期 · " + statusReason
    }
    var map = {
        awaiting_confirm: "等待人工确认",
        pending: "信号已确认，等待执行",
        executed: "信号已执行",
        rejected: "信号已拒绝",
        expired: "信号已过期",
        closed: "信号已平仓"
    }
    return map[status] || ("信号状态已更新为 " + (status || "未知"))
}

function buildSignalStatusCard(signalOrRecord, options) {
    var opts = options || {}
    var get = (typeof signalOrRecord.get === "function") ? signalOrRecord.get.bind(signalOrRecord) : function(k) { return signalOrRecord[k] }
    var d = buildSignalDisplayData(signalOrRecord)
    var directionText = d.direction === "long" ? "做多 📈" : "做空 📉"
    var color = d.direction === "long" ? "green" : "red"
    var currentStatus = get("status") || opts.status || "pending"
    var info = getSignalStatusInfo(currentStatus)
    var message = opts.message || getSignalStatusMessage(currentStatus, signalOrRecord)

    return buildSignalCardV2(
        d.symbol,
        directionText,
        info.emoji,
        info.text,
        currentStatus,
        color,
        message,
        buildSignalInfoElements(d),
        d.us_time,
        d
    )
}

function notifyNewSignal(signal) {
    var dedup = shouldSkipSignalNotification(signal, "new")
    if (dedup.skip) {
        return dedup.result
    }
    beginSignalNotification(signal, dedup.notifyKey)
    var card = buildSignalNotificationCard(signal)
    var signalData = buildSignalDisplayData(signal)
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(signalData.environment || "", envUtils.LIVE_ENVIRONMENT)
    var result = feishuApp.sendCardToChatDetailed(card, runtimeEnvironment)
    finalizeSignalNotification(signal, dedup.notifyKey, "new", result)
    console.log(
        "[FeishuSignal] 信号通知发送:",
        result.success ? "成功" : "失败",
        "signal_id:", signalData.signal_id || "-",
        "status:", signalData.status || ((signal && typeof signal.get === "function") ? (signal.get("status") || "") : "") || "-",
        "source:", signalData.signal_source || signalData.source || "-",
        "message_id:", result.message_id || "-"
    )
    return result
}

function notifySignalStatus(action, signal, options) {
    var opts = options || {}
    var dedup = shouldSkipSignalNotification(signal, action)
    if (dedup.skip) {
        return dedup.result
    }
    beginSignalNotification(signal, dedup.notifyKey)
    var card = buildSignalStatusCard(signal, { message: opts.message })
    var messageId = opts.messageId || dedup.messageId || ""
    var signalData = buildSignalDisplayData(signal)
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(signalData.environment || "", envUtils.LIVE_ENVIRONMENT)
    var result = messageId ? feishuApp.updateMessageCard(messageId, card, runtimeEnvironment) : feishuApp.sendCardToChatDetailed(card, runtimeEnvironment)
    finalizeSignalNotification(signal, dedup.notifyKey, action, result)
    console.log(
        "[FeishuSignal] 信号卡片同步:",
        result.success ? "成功" : "失败",
        "action:", action,
        "signal_id:", signalData.signal_id || "-",
        "status:", signalData.status || ((signal && typeof signal.get === "function") ? (signal.get("status") || "") : "") || "-",
        "source:", signalData.signal_source || signalData.source || "-",
        "message_id:", result.message_id || messageId || "-"
    )
    return result
}

function handleSignalCardCallback(c, options) {
    var opts = options || {}
    var action = opts.action || ""
    var signalId = opts.signalId || ""
    var environment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var updateToken = opts.updateToken || null

    if (!signalId) {
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "error", content: "缺少信号ID" },
            card: { type: "raw", data: null }
        }, updateToken)
    }

    var record
    try {
        record = $app.findFirstRecordByFilter(
            "ibkr_signals",
            "(id = {:id} || signal_id = {:id}) && environment = {:env}",
            { id: signalId, env: environment }
        )
    } catch (err) {
        console.error("[FeishuSignalCallback] 查询信号失败:", err)
    }

    if (!record) {
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "error", content: "信号不存在" },
            card: { type: "raw", data: null }
        }, updateToken)
    }

    var d = buildSignalDisplayData(record)
    var currentStatus = record.get("status")
    var directionText = d.direction === "long" ? "做多 📈" : "做空 📉"
    var color = d.direction === "long" ? "green" : "red"
    var cardElements = buildSignalInfoElements(d)
    var msg
    var card

    console.log("[FeishuSignalCallback] 处理信号回调:", "signal_id=", signalId, "action=", action, "current_status=", currentStatus)

    var finalStatus = ["expired", "rejected", "executed", "closed"]
    if (finalStatus.indexOf(currentStatus) !== -1) {
        var finalInfo = getSignalStatusInfo(currentStatus)
        msg = "该信号" + finalInfo.text + "，无法" + (action === "confirm" ? "确认" : "拒绝")
        card = buildSignalCardV2(d.symbol, directionText, finalInfo.emoji, finalInfo.text, currentStatus, color, msg, cardElements, d.us_time, d)
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "warning", content: msg },
            card: { type: "raw", data: card }
        }, updateToken)
    }

    if (action === "confirm") {
        if (currentStatus === "pending") {
            msg = "⏳ 信号已确认，请勿重复操作"
            card = buildSignalCardV2(d.symbol, directionText, "⏳", "待执行", "pending", color, msg, cardElements, d.us_time, d)
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "info", content: msg },
                card: { type: "raw", data: card }
            }, updateToken)
        }
        if (currentStatus !== "awaiting_confirm") {
            var confirmInfo = getSignalStatusInfo(currentStatus)
            msg = "该信号" + confirmInfo.text + "，无法确认"
            card = buildSignalCardV2(d.symbol, directionText, confirmInfo.emoji, confirmInfo.text, currentStatus, color, msg, cardElements, d.us_time, d)
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "warning", content: msg },
                card: { type: "raw", data: card }
            }, updateToken)
        }

        record.set("status", "pending")
        $app.save(record)
        console.log("[FeishuSignalCallback] 确认成功，signal_id:", signalId)
        card = buildSignalCardV2(d.symbol, directionText, "✅", "待执行", "pending", color, "✨ 确认成功，正在等待执行...", cardElements, d.us_time, d)
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "success", content: "确认成功" },
            card: { type: "raw", data: card }
        }, updateToken)
    }

    if (action === "reject") {
        if (currentStatus === "rejected") {
            msg = "❌ 信号已拒绝，请勿重复操作"
            card = buildSignalCardV2(d.symbol, directionText, "❌", "已拒绝", "rejected", color, msg, cardElements, d.us_time, d)
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "info", content: msg },
                card: { type: "raw", data: card }
            }, updateToken)
        }
        if (currentStatus === "pending") {
            msg = "⏳ 信号正在等待执行，无法拒绝"
            card = buildSignalCardV2(d.symbol, directionText, "⏳", "待执行", "pending", color, msg, cardElements, d.us_time, d)
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "warning", content: msg },
                card: { type: "raw", data: card }
            }, updateToken)
        }
        if (currentStatus !== "awaiting_confirm") {
            var rejectInfo = getSignalStatusInfo(currentStatus)
            msg = "该信号" + rejectInfo.text + "，无法拒绝"
            card = buildSignalCardV2(d.symbol, directionText, rejectInfo.emoji, rejectInfo.text, currentStatus, color, msg, cardElements, d.us_time, d)
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "warning", content: msg },
                card: { type: "raw", data: card }
            }, updateToken)
        }

        record.set("status", "rejected")
        $app.save(record)
        console.log("[FeishuSignalCallback] 拒绝成功，signal_id:", signalId)
        card = buildSignalCardV2(d.symbol, directionText, "❌", "已拒绝", "rejected", color, "🚫 信号已拒绝，暂不执行", cardElements, d.us_time, d)
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "success", content: "拒绝成功" },
            card: { type: "raw", data: card }
        }, updateToken)
    }

    return feishuApp.sendFeishuCallbackResponse(c, {
        toast: { type: "error", content: "未知操作: " + action }
    }, updateToken)
}

module.exports = {
    getSignalExtra: getSignalExtra,
    mergeSignalExtra: mergeSignalExtra,
    getSignalStatusInfo: getSignalStatusInfo,
    getSignalStatusMessage: getSignalStatusMessage,
    buildSignalDisplayData: buildSignalDisplayData,
    buildSignalCardV2: buildSignalCardV2,
    buildSignalNotificationCard: buildSignalNotificationCard,
    buildSignalStatusCard: buildSignalStatusCard,
    notifyNewSignal: notifyNewSignal,
    notifySignalStatus: notifySignalStatus,
    handleSignalCardCallback: handleSignalCardCallback
}
