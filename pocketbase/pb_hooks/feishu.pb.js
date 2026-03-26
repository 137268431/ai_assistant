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
            return sendFeishuCallbackResponse(c, { challenge: body.challenge })
        }

        // 解析回调数据
        const event = body.event || {}
        const actionObj = event.action || {}
        const value = actionObj.value || body.value || {}
        const action = value.action || body.action || ""
        const signalId = value.signal_id || body.signal_id || ""

        console.log("[FeishuCallback] 收到回调请求, action:", action, "signalId:", signalId)

        if (!signalId) {
            return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "缺少信号ID" } })
        }

        const record = $app.findFirstRecordByFilter("signals", "id = {:id} || signal_id = {:id}", { id: signalId })
        if (!record) {
            return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "信号不存在" } })
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

        // 构建额外字段（与原始信号卡片一致的布局）
        var leftFields = [], rightFields = []
        leftFields.push({ tag: "div", text: { tag: "lark_md", content: "**入场:** $" + entry.toFixed(2) } })
        leftFields.push({ tag: "div", text: { tag: "lark_md", content: "**止盈:** $" + take_profit.toFixed(2) } })
        leftFields.push({ tag: "div", text: { tag: "lark_md", content: "**止损:** $" + stop_loss.toFixed(2) } })

        rightFields.push({ tag: "div", text: { tag: "lark_md", content: "**股数:** " + shares } })
        rightFields.push({ tag: "div", text: { tag: "lark_md", content: "**风报比:** " + rr } })

        var extraFields = [
            { tag: "column_set", columns: [
                { tag: "column", width: "weighted", weight: 1, elements: leftFields },
                { tag: "column", width: "weighted", weight: 1, elements: rightFields }
            ]}
        ]

        // 添加原因（如果有）
        if (extra.reason) {
            extraFields.push({ tag: "div", text: { tag: "lark_md", content: "**原因:** " + extra.reason } })
        }

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
            return sendFeishuCallbackResponse(c, { toast: { type: "warning", content: msg }, card: { type: "raw", data: card } })
        }

        // 执行确认操作
        if (action === "confirm") {
            if (currentStatus === "pending") {
                msg = "⏳ 信号已确认，请勿重复操作"
                card = buildSignalCardV2(symbol, directionText, "⏳", "待执行", "pending", color, msg, extraFields)
                return sendFeishuCallbackResponse(c, { toast: { type: "info", content: msg }, card: { type: "raw", data: card } })
            }
            record.set("status", "pending")
            $app.save(record)
            console.log("[FeishuCallback] 确认成功，signalId:", signalId)
            card = buildSignalCardV2(symbol, directionText, "✅", "待执行", "pending", color, "✨ 确认成功，正在等待执行...", extraFields)
            return sendFeishuCallbackResponse(c, { toast: { type: "success", content: "确认成功" }, card: { type: "raw", data: card } })
        }

        // 执行拒绝操作
        if (action === "reject") {
            if (currentStatus === "rejected") {
                msg = "❌ 信号已拒绝，请勿重复操作"
                card = buildSignalCardV2(symbol, directionText, "❌", "已拒绝", "rejected", color, msg, extraFields)
                return sendFeishuCallbackResponse(c, { toast: { type: "info", content: msg }, card: { type: "raw", data: card } })
            }
            record.set("status", "rejected")
            $app.save(record)
            console.log("[FeishuCallback] 拒绝成功，signalId:", signalId)
            card = buildSignalCardV2(symbol, directionText, "❌", "已拒绝", "rejected", color, "🚫 信号已拒绝，暂不执行", extraFields)
            return sendFeishuCallbackResponse(c, { toast: { type: "success", content: "拒绝成功" }, card: { type: "raw", data: card } })
        }

        // 未知操作
        return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "未知操作: " + action } })

    } catch (err) {
        console.error("[FeishuCallback] 处理失败:", err)
        console.error("[FeishuCallback] sendFeishuCallbackResponse type:", typeof sendFeishuCallbackResponse, "value:", sendFeishuCallbackResponse)
        return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "处理失败" } })
    }
})

// ── 飞书订单卡片回调 ──

routerAdd("POST", "/webhook/feishu/order/callback", (c) => {
    const { sendFeishuCallbackResponse, buildOrderCardV2 } = require(`${__hooks}/feishu_app.js`)

    try {
        const body = c.requestInfo().body || {}

        // Challenge 验证
        if (body.type === "url_verification" && body.challenge) {
            return sendFeishuCallbackResponse(c, { challenge: body.challenge })
        }

        const event = body.event || {}
        const actionObj = event.action || {}
        const value = actionObj.value || body.value || {}
        const action = value.action || body.action || ""
        const orderId = value.order_id || body.order_id || ""

        console.log("[FeishuOrderCallback] 收到回调请求, action:", action, "orderId:", orderId)

        if (!orderId) {
            return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "缺少订单ID" } })
        }

        const records = $app.findRecordsByFilter("orders", "unique_id = {:id}", "", 1, 0, { id: orderId })
        if (!records || records.length === 0) {
            return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "订单不存在" } })
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
            return sendFeishuCallbackResponse(c, { toast: { type: "warning", content: invalidStatusMap[currentStatus] }, card: { type: "raw", data: card } })
        }

        // 取消挂单
        if (action === "cancel") {
            if (currentStatus !== "Submitted") {
                msg = invalidStatusMap[currentStatus] || "无法取消"
                info = orderStatusMap[currentStatus] || { emoji: "⏰", text: currentStatus }
                card = buildOrderCardV2(symbol, directionText, info.emoji, info.text, currentStatus, color, msg)
                return sendFeishuCallbackResponse(c, { toast: { type: "error", content: msg }, card: { type: "raw", data: card } })
            }
            record.set("action", "cancel")
            $app.save(record)
            console.log("[FeishuOrderCallback] 取消成功，orderId:", orderId)
            card = buildOrderCardV2(symbol, directionText, "❌", "已取消", "Canceled", color, "取消指令已发送，等待执行")
            return sendFeishuCallbackResponse(c, { toast: { type: "success", content: "取消指令已发送" }, card: { type: "raw", data: card } })
        }

        // 平仓
        if (action === "close") {
            if (currentStatus !== "Filled") {
                msg = "只有成交的订单才能平仓"
                info = orderStatusMap[currentStatus] || { emoji: "⏰", text: currentStatus }
                card = buildOrderCardV2(symbol, directionText, info.emoji, info.text, currentStatus, color, msg)
                return sendFeishuCallbackResponse(c, { toast: { type: "error", content: msg }, card: { type: "raw", data: card } })
            }
            record.set("action", "close")
            $app.save(record)
            console.log("[FeishuOrderCallback] 平仓成功，orderId:", orderId)
            card = buildOrderCardV2(symbol, directionText, "🔒", "已平仓", "Closed", color, "平仓指令已发送，等待执行")
            return sendFeishuCallbackResponse(c, { toast: { type: "success", content: "平仓指令已发送" }, card: { type: "raw", data: card } })
        }

        // 未知操作
        return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "未知操作: " + action } })

    } catch (err) {
        console.error("[FeishuOrderCallback] 处理失败:", err)
        return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "处理失败" } })
    }
})

console.log("[FeishuPB] Hook 文件加载完成");
