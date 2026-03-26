/// <reference path="./pb_data/types.d.ts" />

/**
 * feishu.pb.js
 * 飞书卡片按钮回调处理
 */

console.log("[FeishuPB] Hook 文件开始加载...");

// ── 飞书信号卡片回调 ──

routerAdd("POST", "/webhook/feishu/callback", (c) => {
    const { sendFeishuCallbackResponse, buildSignalCardV2 } = require(`${__hooks}/feishu_app.js`)

    try {
        const body = c.requestInfo().body || {}

        // Challenge 验证
        if (body.type === "url_verification" && body.challenge) {
            console.log("[FeishuPB] Challenge 验证成功:", body.challenge)
            return sendFeishuCallbackResponse(c, { challenge: body.challenge }, null)
        }

        // 解析回调数据
        const event = body.event || {}
        const updateToken = event.token || null
        const actionObj = event.action || {}
        const value = actionObj.value || body.value || {}
        const action = value.action || body.action || ""
        const signalId = value.signal_id || body.signal_id || ""

        console.log("[FeishuCallback] 收到回调请求, action:", action, "signalId:", signalId, "updateToken:", updateToken ? "存在" : "无")

        if (!signalId) {
            return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "缺少信号ID" }, card: { type: "raw", data: null } }, updateToken)
        }

        const record = $app.findFirstRecordByFilter("signals", "id = {:id} || signal_id = {:id}", { id: signalId })
        if (!record) {
            return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "信号不存在" }, card: { type: "raw", data: null } }, updateToken)
        }

        const currentStatus = record.get("status")
        const symbol = record.get("symbol") || signalId
        const direction = record.get("direction") || "long"
        const directionText = direction === "long" ? "做多" : "做空"
        const color = direction === "long" ? "green" : "red"

        // 获取信号完整信息
        const entry = record.get("entry") || 0
        const take_profit = record.get("take_profit") || 0
        const stop_loss = record.get("stop_loss") || 0
        const shares = record.get("shares") || 0
        const rr = record.get("rr") || "N/A"
        const extraStr = record.get("extra") || "{}"
        const extra = (typeof extraStr === "string") ? JSON.parse(extraStr) : extraStr

        // 计算盈亏
        const isLong = direction === "long"
        const tpProfit = (isLong ? (take_profit - entry) : (entry - take_profit)) * shares
        const slLoss = (isLong ? (entry - stop_loss) : (stop_loss - entry)) * shares
        const formatAmount = function(a) {
            if (!a || a === 0) return "0"
            if (Math.abs(a) >= 10000) return (a / 10000).toFixed(2) + "w"
            return a.toFixed(2)
        }

        // 涨幅显示
        var changeDisplay = null
        if (extra.day_change_pct !== undefined) {
            var dayPct = Number(extra.day_change_pct || 0)
            var prevPct = Number(extra.prev_close_change_pct || 0)
            var d7Pct = Number(extra.change_7d || 0)
            changeDisplay = (dayPct > 0 ? "+" : "") + dayPct.toFixed(2) + "%/" + (prevPct > 0 ? "+" : "") + prevPct.toFixed(2) + "%/" + (d7Pct > 0 ? "+" : "") + d7Pct.toFixed(2) + "%"
        }

        // 波动率
        var atrLine = null
        if (extra.atr_pct) {
            var atrLevel = extra.atr_pct >= 3 ? "高" : extra.atr_pct >= 1.5 ? "中" : "低"
            var atrEmoji = extra.atr_pct >= 3 ? "⚡" : extra.atr_pct >= 1.5 ? "~" : "·"
            atrLine = "**波动率:** " + atrEmoji + " " + atrLevel + " " + extra.atr_pct.toFixed(2) + "%"
            if (extra.sl_atr_ratio) {
                atrLine += "\n**ATR止损:** " + extra.sl_atr_ratio.toFixed(1) + "倍"
            }
        }

        // 大盘信息
        var marketInfoText = null
        if (extra.market_indexes && extra.market_indexes.length > 0) {
            var spyData = extra.market_indexes.find(function(m) { return m.symbol === "SPY" })
            var qqqData = extra.market_indexes.find(function(m) { return m.symbol === "QQQ" })
            var vixData = extra.market_indexes.find(function(m) { return m.symbol === "VIX" })
            var parts = []
            if (spyData) parts.push("SPY: " + (spyData.change_pct > 0 ? "+" : "") + spyData.change_pct.toFixed(2) + "%")
            if (qqqData) parts.push("QQQ: " + (qqqData.change_pct > 0 ? "+" : "") + qqqData.change_pct.toFixed(2) + "%")
            if (parts.length > 0) marketInfoText = "**大盘:** " + parts.join(" | ")
            if (vixData) {
                var vixLevel = vixData.change_pct >= 20 ? "🔴 恐慌" : vixData.change_pct >= 10 ? "🟡 紧张" : "🟢 平稳"
                marketInfoText += "\n**VIX恐慌:** " + vixLevel + " " + (vixData.change_pct > 0 ? "+" : "") + vixData.change_pct.toFixed(1) + "%"
            }
        }

        // 构建与原始信号一致的字段布局
        var leftFields = [], rightFields = [], allFields = []

        // 涨幅（如果有）
        if (changeDisplay) {
            allFields.push({ tag: "div", text: { tag: "lark_md", content: "**涨幅:** " + changeDisplay } })
        }

        // 入场/止盈/止损
        leftFields.push({ tag: "div", text: { tag: "lark_md", content: "**入场:** $" + entry.toFixed(2) } })
        leftFields.push({ tag: "div", text: { tag: "lark_md", content: "**止盈:** $" + take_profit.toFixed(2) } })
        leftFields.push({ tag: "div", text: { tag: "lark_md", content: "**止损:** $" + stop_loss.toFixed(2) } })

        // 盈亏/风报比/股数
        rightFields.push({ tag: "div", text: { tag: "lark_md", content: "**盈利:** +$" + formatAmount(tpProfit) } })
        rightFields.push({ tag: "div", text: { tag: "lark_md", content: "**亏损:** -$" + formatAmount(slLoss) } })
        rightFields.push({ tag: "div", text: { tag: "lark_md", content: "**风报比:** " + rr } })
        rightFields.push({ tag: "div", text: { tag: "lark_md", content: "**股数:** " + shares } })

        // 波动率（如果有）
        if (atrLine) {
            rightFields.push({ tag: "div", text: { tag: "lark_md", content: atrLine.replace(/\n/g, "\n") } })
        }

        // 第一行：左侧字段
        var extraFields = []
        if (leftFields.length > 0) {
            allFields.push({ tag: "column_set", columns: [
                { tag: "column", width: "weighted", weight: 1, elements: leftFields }
            ]})
        }

        // 第二行：右侧字段（盈亏/风报比等）
        if (rightFields.length > 0) {
            allFields.push({ tag: "column_set", columns: [
                { tag: "column", width: "weighted", weight: 1, elements: rightFields }
            ]})
        }

        // 原因（如果有）
        if (extra.reason) {
            allFields.push({ tag: "div", text: { tag: "lark_md", content: "**原因:** " + extra.reason } })
        }

        // 大盘信息（如果有）
        if (marketInfoText) {
            allFields.push({ tag: "div", text: { tag: "lark_md", content: marketInfoText } })
        }

        // 信号ID
        allFields.push({ tag: "div", text: { tag: "lark_md", content: "**信号ID:** " + signalId } })

        var statusEmoji, statusText, msg, card

        // 不可操作的状态
        var invalidStatus = ["expired", "rejected", "executed"]
        if (invalidStatus.indexOf(currentStatus) !== -1) {
            if (currentStatus === "expired") {
                statusEmoji = "⏰"; statusText = "已过期"
            } else if (currentStatus === "rejected") {
                statusEmoji = "❌"; statusText = "已拒绝"
            } else {
                statusEmoji = "✅"; statusText = "已执行"
            }
            msg = "该信号" + statusText + "，无法" + (action === "confirm" ? "确认" : "拒绝")
            card = buildSignalCardV2(symbol, directionText, statusEmoji, statusText, currentStatus, color, msg, extraFields)
            return sendFeishuCallbackResponse(c, { toast: { type: "warning", content: msg }, card: { type: "raw", data: card } }, updateToken)
        }

        // 执行确认操作
        if (action === "confirm") {
            if (currentStatus === "pending") {
                msg = "⏳ 信号已确认，请勿重复操作"
                card = buildSignalCardV2(symbol, directionText, "⏳", "待执行", "pending", color, msg, extraFields)
                return sendFeishuCallbackResponse(c, { toast: { type: "info", content: msg }, card: { type: "raw", data: card } }, updateToken)
            }
            record.set("status", "pending")
            $app.save(record)
            console.log("[FeishuCallback] 确认成功，signalId:", signalId)
            card = buildSignalCardV2(symbol, directionText, "✅", "待执行", "pending", color, "✨ 确认成功，正在等待执行...", extraFields)
            return sendFeishuCallbackResponse(c, { toast: { type: "success", content: "确认成功" }, card: { type: "raw", data: card } }, updateToken)
        }

        // 执行拒绝操作
        if (action === "reject") {
            if (currentStatus === "rejected") {
                msg = "❌ 信号已拒绝，请勿重复操作"
                card = buildSignalCardV2(symbol, directionText, "❌", "已拒绝", "rejected", color, msg, extraFields)
                return sendFeishuCallbackResponse(c, { toast: { type: "info", content: msg }, card: { type: "raw", data: card } }, updateToken)
            }
            record.set("status", "rejected")
            $app.save(record)
            console.log("[FeishuCallback] 拒绝成功，signalId:", signalId)
            card = buildSignalCardV2(symbol, directionText, "❌", "已拒绝", "rejected", color, "🚫 信号已拒绝，暂不执行", extraFields)
            return sendFeishuCallbackResponse(c, { toast: { type: "success", content: "拒绝成功" }, card: { type: "raw", data: card } }, updateToken)
        }

        // 未知操作
        return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "未知操作: " + action } }, updateToken)

    } catch (err) {
        console.error("[FeishuCallback] 处理失败:", err)
        return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "处理失败" } }, updateToken)
    }
})

// ── 飞书订单卡片回调 ──

routerAdd("POST", "/webhook/feishu/order/callback", (c) => {
    const { sendFeishuCallbackResponse, buildOrderCardV2 } = require(`${__hooks}/feishu_app.js`)

    try {
        const body = c.requestInfo().body || {}

        // Challenge 验证
        if (body.type === "url_verification" && body.challenge) {
            return sendFeishuCallbackResponse(c, { challenge: body.challenge }, null)
        }

        const event = body.event || {}
        const updateToken = event.token || null
        const actionObj = event.action || {}
        const value = actionObj.value || body.value || {}
        const action = value.action || body.action || ""
        const orderId = value.order_id || body.order_id || ""

        console.log("[FeishuOrderCallback] 收到回调请求, action:", action, "orderId:", orderId, "updateToken:", updateToken ? "存在" : "无")

        if (!orderId) {
            return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "缺少订单ID" }, card: { type: "raw", data: null } }, updateToken)
        }

        const records = $app.findRecordsByFilter("orders", "unique_id = {:id}", "", 1, 0, { id: orderId })
        if (!records || records.length === 0) {
            return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "订单不存在" }, card: { type: "raw", data: null } }, updateToken)
        }

        const record = records[0]
        const currentStatus = record.get("status")
        const symbol = record.get("symbol") || orderId
        const direction = record.get("direction") || "long"
        const directionText = direction === "long" ? "做多" : "做空"
        const color = direction === "long" ? "green" : "red"

        var msg, card, info

        // 不可操作的状态
        var invalidStatusMap = {
            Filled: "订单已成交，无法取消",
            Canceled: "订单已取消，无法操作",
            Closed: "订单已平仓，无法操作"
        }
        var orderStatusMap = {
            Submitted: { emoji: "⏳", text: "待成交" },
            Filled: { emoji: "✅", text: "已成交" },
            Canceled: { emoji: "❌", text: "已取消" },
            Closed: { emoji: "🔒", text: "已平仓" }
        }
        if (invalidStatusMap[currentStatus]) {
            info = orderStatusMap[currentStatus] || { emoji: "⏰", text: currentStatus }
            card = buildOrderCardV2(symbol, directionText, info.emoji, info.text, currentStatus, color, invalidStatusMap[currentStatus])
            return sendFeishuCallbackResponse(c, { toast: { type: "warning", content: invalidStatusMap[currentStatus] }, card: { type: "raw", data: card } }, updateToken)
        }

        // 取消挂单
        if (action === "cancel") {
            if (currentStatus !== "Submitted") {
                msg = invalidStatusMap[currentStatus] || "无法取消"
                info = orderStatusMap[currentStatus] || { emoji: "⏰", text: currentStatus }
                card = buildOrderCardV2(symbol, directionText, info.emoji, info.text, currentStatus, color, msg)
                return sendFeishuCallbackResponse(c, { toast: { type: "error", content: msg }, card: { type: "raw", data: card } }, updateToken)
            }
            record.set("action", "cancel")
            $app.save(record)
            console.log("[FeishuOrderCallback] 取消成功，orderId:", orderId)
            card = buildOrderCardV2(symbol, directionText, "❌", "已取消", "Canceled", color, "取消指令已发送，等待执行")
            return sendFeishuCallbackResponse(c, { toast: { type: "success", content: "取消指令已发送" }, card: { type: "raw", data: card } }, updateToken)
        }

        // 平仓
        if (action === "close") {
            if (currentStatus !== "Filled") {
                msg = "只有成交的订单才能平仓"
                info = orderStatusMap[currentStatus] || { emoji: "⏰", text: currentStatus }
                card = buildOrderCardV2(symbol, directionText, info.emoji, info.text, currentStatus, color, msg)
                return sendFeishuCallbackResponse(c, { toast: { type: "error", content: msg }, card: { type: "raw", data: card } }, updateToken)
            }
            record.set("action", "close")
            $app.save(record)
            console.log("[FeishuOrderCallback] 平仓成功，orderId:", orderId)
            card = buildOrderCardV2(symbol, directionText, "🔒", "已平仓", "Closed", color, "平仓指令已发送，等待执行")
            return sendFeishuCallbackResponse(c, { toast: { type: "success", content: "平仓指令已发送" }, card: { type: "raw", data: card } }, updateToken)
        }

        // 未知操作
        return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "未知操作: " + action } }, updateToken)

    } catch (err) {
        console.error("[FeishuOrderCallback] 处理失败:", err)
        return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "处理失败" } }, updateToken)
    }
})

console.log("[FeishuPB] Hook 文件加载完成");
