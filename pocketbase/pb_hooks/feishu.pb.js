/// <reference path="./pb_data/types.d.ts" />

/**
 * feishu.pb.js
 * 飞书卡片按钮回调处理
 */

console.log("[FeishuPB] Hook 文件开始加载...");

// ── 飞书卡片按钮回调 ──

routerAdd("POST", "/webhook/feishu/callback", (c) => {
    try {
        const body = c.requestInfo().body || {}

        // 飞书 callback payload 结构（新版 schema 2.0）：
        // {
        //   schema: "2.0",
        //   header: { event_type: "card.action.trigger", token: "xxx" },
        //   event: {
        //     operator: { open_id: "xxx" },
        //     action: {
        //       tag: "button",
        //       value: { action: "confirm", signal_id: "xxx" }
        //     }
        //   }
        // }
        const event = body.event || {}
        const actionObj = event.action || {}
        const value = actionObj.value || body.value || {}
        const action = value.action || body.action || ""
        const signalId = value.signal_id || body.signal_id || ""

        console.log("[FeishuCallback] 收到回调请求:")
        console.log("[FeishuCallback] 原始 body:", JSON.stringify(body))
        console.log("[FeishuCallback] 解析后 action:", action, "signalId:", signalId)

        if (!signalId) {
            return c.json(400, { error: "缺少信号ID" })
        }

        const record = $app.findFirstRecordByFilter("signals", "id = {:id} || signal_id = {:id}", { id: signalId })
        if (!record) {
            return c.json(404, { error: "信号不存在" })
        }

        const currentStatus = record.get("status")
        const symbol = record.get("symbol") || signalId
        const direction = record.get("direction") || "long"
        const directionText = direction === "long" ? "做多 📈" : "做空 📉"
        const color = direction === "long" ? "green" : "red"

        // 不可操作的状态
        const invalidStatus = ["expired", "rejected", "executed"]
        if (invalidStatus.includes(currentStatus)) {
            const statusTextMap = {
                expired: "已过期",
                rejected: "已拒绝",
                executed: "已执行"
            }
            return c.json(200, {
                code: 1,
                msg: `信号${statusTextMap[currentStatus]}，无法操作`,
                data: {
                    card: buildStatusCard(symbol, directionText, currentStatus, color, `该信号${statusTextMap[currentStatus]}，无法${action === "confirm" ? "确认" : "拒绝"}`)
                }
            })
        }

        // 执行确认操作
        if (action === "confirm") {
            if (currentStatus === "pending") {
                return c.json(200, {
                    code: 2,
                    msg: "信号已确认，无需重复操作",
                    data: {
                        card: buildStatusCard(symbol, directionText, "pending", color, "该信号已确认")
                    }
                })
            }
            record.set("status", "pending")
            $app.save(record)
            return c.json(200, {
                code: 0,
                msg: "确认成功",
                data: {
                    card: buildStatusCard(symbol, directionText, "pending", color, "确认成功，等待执行")
                }
            })
        }

        // 执行拒绝操作
        if (action === "reject") {
            if (currentStatus === "rejected") {
                return c.json(200, {
                    code: 2,
                    msg: "信号已拒绝，无需重复操作",
                    data: {
                        card: buildStatusCard(symbol, directionText, "rejected", color, "该信号已拒绝")
                    }
                })
            }
            record.set("status", "rejected")
            $app.save(record)
            return c.json(200, {
                code: 0,
                msg: "拒绝成功",
                data: {
                    card: buildStatusCard(symbol, directionText, "rejected", color, "已拒绝，信号无效")
                }
            })
        }

        return c.json(400, { error: "未知操作" })
    } catch (err) {
        console.error("[FeishuCallback] 处理失败:", err)
        return c.json(500, { error: String(err) })
    }
})

// ── 订单卡片按钮回调 ──

routerAdd("POST", "/webhook/feishu/order/callback", (c) => {
    try {
        const body = c.requestInfo().body || {}

        // 飞书 callback payload 结构（新版 schema 2.0）
        const event = body.event || {}
        const actionObj = event.action || {}
        const value = actionObj.value || body.value || {}
        const action = value.action || body.action || ""
        const orderId = value.order_id || body.order_id || ""

        console.log("[FeishuOrderCallback] 收到回调请求:")
        console.log("[FeishuOrderCallback] 原始 body:", JSON.stringify(body))
        console.log("[FeishuOrderCallback] 解析后 action:", action, "orderId:", orderId)

        if (!orderId) {
            return c.json(400, { error: "缺少订单ID" })
        }

        const records = $app.findRecordsByFilter("orders", "unique_id = {:id}", "", 1, 0, { id: orderId })
        if (!records || records.length === 0) {
            return c.json(404, { error: "订单不存在" })
        }

        const record = records[0]
        const currentStatus = record.get("status")
        const symbol = record.get("symbol") || orderId
        const direction = record.get("direction") || "long"
        const directionText = direction === "long" ? "做多 📈" : "做空 📉"
        const color = direction === "long" ? "green" : "red"

        // 不可操作的状态
        const invalidStatusMap = {
            Filled: "订单已成交，无法取消",
            Canceled: "订单已取消，无法重复操作",
            Closed: "订单已平仓，无法操作"
        }
        if (invalidStatusMap[currentStatus]) {
            return c.json(200, {
                code: 1,
                msg: invalidStatusMap[currentStatus],
                data: {
                    card: buildOrderStatusCard(symbol, directionText, currentStatus, color, invalidStatusMap[currentStatus])
                }
            })
        }

        // 取消挂单
        if (action === "cancel") {
            if (currentStatus !== "Submitted") {
                return c.json(200, {
                    code: 1,
                    msg: invalidStatusMap[currentStatus] || "无法取消",
                    data: {
                        card: buildOrderStatusCard(symbol, directionText, currentStatus, color, invalidStatusMap[currentStatus] || "无法取消")
                    }
                })
            }
            record.set("action", "cancel")
            $app.save(record)
            return c.json(200, {
                code: 0,
                msg: "取消指令已发送",
                data: {
                    card: buildOrderStatusCard(symbol, directionText, "Canceled", color, "取消指令已发送，等待执行")
                }
            })
        }

        // 平仓
        if (action === "close") {
            if (currentStatus !== "Filled") {
                return c.json(200, {
                    code: 1,
                    msg: "只有成交的订单才能平仓",
                    data: {
                        card: buildOrderStatusCard(symbol, directionText, currentStatus, color, "只有成交的订单才能平仓")
                    }
                })
            }
            record.set("action", "close")
            $app.save(record)
            return c.json(200, {
                code: 0,
                msg: "平仓指令已发送",
                data: {
                    card: buildOrderStatusCard(symbol, directionText, "Closed", color, "平仓指令已发送，等待执行")
                }
            })
        }

        return c.json(400, { error: "未知操作" })
    } catch (err) {
        console.error("[FeishuOrderCallback] 处理失败:", err)
        return c.json(500, { error: String(err) })
    }
})

// 构建状态卡片（用于回调更新）
function buildStatusCard(symbol, directionText, status, color, message) {
    const statusEmoji = {
        pending: "⏳",
        rejected: "❌",
        expired: "⏰",
        executed: "✅"
    }
    const statusText = {
        pending: "待执行",
        rejected: "已拒绝",
        expired: "已过期",
        executed: "已执行"
    }

    return {
        header: {
            title: { tag: "plain_text", content: statusEmoji[status] + " 信号状态 · " + symbol },
            template: color
        },
        elements: [
            { tag: "div", text: { tag: "lark_md", content: `**标的:** ${symbol}` } },
            { tag: "div", text: { tag: "lark_md", content: `**方向:** ${directionText}` } },
            { tag: "div", text: { tag: "lark_md", content: `**状态:** ${statusText[status] || status}` } },
            { tag: "hr" },
            { tag: "div", text: { tag: "lark_md", content: message, text_align: "center" } }
        ]
    }
}

// 构建订单状态卡片
function buildOrderStatusCard(symbol, directionText, status, color, message) {
    const statusEmoji = {
        Submitted: "⏳",
        Filled: "✅",
        Canceled: "❌",
        Closed: "🔒"
    }
    const statusText = {
        Submitted: "待成交",
        Filled: "已成交",
        Canceled: "已取消",
        Closed: "已平仓"
    }

    return {
        header: {
            title: { tag: "plain_text", content: statusEmoji[status] + " 订单状态 · " + symbol },
            template: color
        },
        elements: [
            { tag: "div", text: { tag: "lark_md", content: `**标的:** ${symbol}` } },
            { tag: "div", text: { tag: "lark_md", content: `**方向:** ${directionText}` } },
            { tag: "div", text: { tag: "lark_md", content: `**状态:** ${statusText[status] || status}` } },
            { tag: "hr" },
            { tag: "div", text: { tag: "lark_md", content: message, text_align: "center" } }
        ]
    }
}

// ── 飞书回调卡片更新 ──
// 注意：飞书卡片更新需要使用 patch_card API，这里返回的是新卡片内容
// 实际更新需要飞书 SDK 支持，这里提供结构化返回

console.log("[FeishuPB] Hook 文件加载完成");
