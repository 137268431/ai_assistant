/// <reference path="./pb_data/types.d.ts" />

/**
 * feishu.pb.js
 * 飞书卡片按钮回调处理
 */

console.log("[FeishuPB] Hook 文件开始加载...");

// ── 飞书信号卡片回调 ──

routerAdd("POST", "/webhook/feishu/callback", (c) => {
    const { sendFeishuCallbackResponse, buildSignalCardV2, buildSignalDisplayData } = require(`${__hooks}/feishu_app.js`)

    try {
        const body = c.requestInfo().body || {}

        // ── 打印回调数据日志 ──
        console.log("[FeishuCallback] 接收回调数据:", JSON.stringify(body, null, 2))

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

        // ── 打印查出的信号记录 ──
        console.log("[FeishuCallback] 查出信号记录:", JSON.stringify(record, null, 2))

        if (!record) {
            return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "信号不存在" }, card: { type: "raw", data: null } }, updateToken)
        }

        // 使用公共方法构建展示数据（从 indicators 表补充 market_indexes）
        const d = buildSignalDisplayData(record)

        const currentStatus = record.get("status")
        const directionText = d.direction === "long" ? "做多 📈" : "做空 📉"
        const color = d.direction === "long" ? "green" : "red"

        // 构建展示字段（与 notifyNewSignal 完全一致的逻辑）
        var leftColumn = [], rightColumn = [], cardElements = []

        leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**标的:** " + d.symbol } })
        leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**方向:** " + directionText } })
        leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**涨幅:** " + d.changeDisplay } })
        leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**入场:** $" + d.entry.toFixed(2) } })
        leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止盈:** $" + d.take_profit.toFixed(2) } })
        leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止损:** $" + d.stop_loss.toFixed(2) } })

        rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**盈利:** +$" + d.formatAmount(d.tpProfit) } })
        rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**亏损:** -$" + d.formatAmount(d.slLoss) } })
        rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**风报比:** " + d.rr } })
        rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**股数:** " + d.shares } })

        if (d.atrText) {
            d.atrText.split("\n").forEach(function(line) {
                rightColumn.push({ tag: "div", text: { tag: "lark_md", content: line } })
            })
        }

        cardElements.push({ tag: "column_set", horizontal_spacing: "default", columns: [
            { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: leftColumn },
            { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: rightColumn }
        ]})

        // 信号ID/原因/大盘 合并到一个 column_set 控制间距
        var infoElements = []
        infoElements.push({ tag: "div", text: { tag: "lark_md", content: "**信号ID:** " + d.signal_id } })
        if (d.reason) {
            infoElements.push({ tag: "div", text: { tag: "lark_md", content: "**原因:** " + d.reason } })
        }
        if (d.marketInfoText) {
            infoElements.push({ tag: "div", text: { tag: "lark_md", content: d.marketInfoText } })
        }
        cardElements.push({ tag: "column_set", columns: [{ tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: infoElements }] })

        var msg, card

        // 根据状态获取展示信息
        function getStatusInfo(status) {
            var map = {
                "expired": { emoji: "⏰", text: "已过期" },
                "rejected": { emoji: "❌", text: "已拒绝" },
                "executed": { emoji: "✅", text: "已执行" },
                "pending": { emoji: "⏳", text: "待执行" },
                "closed": { emoji: "🔒", text: "已平仓" },
                "awaiting_confirm": { emoji: "⏳", text: "待确认" }
            }
            return map[status] || { emoji: "❓", text: status }
        }

        // 不可操作的状态（已最终态）
        var finalStatus = ["expired", "rejected", "executed", "closed"]
        if (finalStatus.indexOf(currentStatus) !== -1) {
            var info = getStatusInfo(currentStatus)
            msg = "该信号" + info.text + "，无法" + (action === "confirm" ? "确认" : "拒绝")
            card = buildSignalCardV2(d.symbol, directionText, info.emoji, info.text, currentStatus, color, msg, cardElements, d.us_time)
            return sendFeishuCallbackResponse(c, { toast: { type: "warning", content: msg }, card: { type: "raw", data: card } }, updateToken)
        }

        // 执行确认操作（只有 awaiting_confirm 才能确认）
        if (action === "confirm") {
            if (currentStatus === "pending") {
                msg = "⏳ 信号已确认，请勿重复操作"
                card = buildSignalCardV2(d.symbol, directionText, "⏳", "待执行", "pending", color, msg, cardElements, d.us_time)
                return sendFeishuCallbackResponse(c, { toast: { type: "info", content: msg }, card: { type: "raw", data: card } }, updateToken)
            }
            if (currentStatus !== "awaiting_confirm") {
                var info = getStatusInfo(currentStatus)
                msg = "该信号" + info.text + "，无法确认"
                card = buildSignalCardV2(d.symbol, directionText, info.emoji, info.text, currentStatus, color, msg, cardElements, d.us_time)
                return sendFeishuCallbackResponse(c, { toast: { type: "warning", content: msg }, card: { type: "raw", data: card } }, updateToken)
            }
            record.set("status", "pending")
            $app.save(record)
            console.log("[FeishuCallback] 确认成功，signalId:", signalId)
            card = buildSignalCardV2(d.symbol, directionText, "✅", "待执行", "pending", color, "✨ 确认成功，正在等待执行...", cardElements, d.us_time)
            return sendFeishuCallbackResponse(c, { toast: { type: "success", content: "确认成功" }, card: { type: "raw", data: card } }, updateToken)
        }

        // 执行拒绝操作（只有 awaiting_confirm 才能拒绝）
        if (action === "reject") {
            if (currentStatus === "rejected") {
                msg = "❌ 信号已拒绝，请勿重复操作"
                card = buildSignalCardV2(d.symbol, directionText, "❌", "已拒绝", "rejected", color, msg, cardElements, d.us_time)
                return sendFeishuCallbackResponse(c, { toast: { type: "info", content: msg }, card: { type: "raw", data: card } }, updateToken)
            }
            if (currentStatus === "pending") {
                msg = "⏳ 信号正在等待执行，无法拒绝"
                card = buildSignalCardV2(d.symbol, directionText, "⏳", "待执行", "pending", color, msg, cardElements, d.us_time)
                return sendFeishuCallbackResponse(c, { toast: { type: "warning", content: msg }, card: { type: "raw", data: card } }, updateToken)
            }
            if (currentStatus !== "awaiting_confirm") {
                var info = getStatusInfo(currentStatus)
                msg = "该信号" + info.text + "，无法拒绝"
                card = buildSignalCardV2(d.symbol, directionText, info.emoji, info.text, currentStatus, color, msg, cardElements, d.us_time)
                return sendFeishuCallbackResponse(c, { toast: { type: "warning", content: msg }, card: { type: "raw", data: card } }, updateToken)
            }
            record.set("status", "rejected")
            $app.save(record)
            console.log("[FeishuCallback] 拒绝成功，signalId:", signalId)
            card = buildSignalCardV2(d.symbol, directionText, "❌", "已拒绝", "rejected", color, "🚫 信号已拒绝，暂不执行", cardElements, d.us_time)
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

        // ── 打印回调数据日志 ──
        console.log("[FeishuOrderCallback] 接收回调数据:", JSON.stringify(body, null, 2))

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
