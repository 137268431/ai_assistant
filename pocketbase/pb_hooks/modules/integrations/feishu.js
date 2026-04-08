/// <reference path="./pb_data/types.d.ts" />

/**
 * feishu.pb.js
 * 飞书卡片按钮统一回调入口
 */

console.log("[FeishuPB] Hook 文件开始加载...")

routerAdd("POST", "/webhook/feishu/callback", (c) => {
    const { sendFeishuCallbackResponse } = require(`${__hooks}/lib/feishu_app.js`)
    const { handle2faCardCallback } = require(`${__hooks}/lib/feishu_2fa.js`)
    const { handleSignalCardCallback } = require(`${__hooks}/lib/feishu_signal.js`)
    const { handleOrderCardCallback } = require(`${__hooks}/lib/feishu_order.js`)

    try {
        const body = c.requestInfo().body || {}

        console.log("[FeishuCallback] 接收回调数据:", JSON.stringify(body, null, 2))

        if (body.type === "url_verification" && body.challenge) {
            console.log("[FeishuPB] Challenge 验证成功:", body.challenge)
            return sendFeishuCallbackResponse(c, { challenge: body.challenge }, null)
        }

        const event = body.event || {}
        const actionObj = event.action || {}
        const value = actionObj.value || body.value || {}
        const updateToken = event.token || null
        const action = value.action || body.action || ""
        const signalId = value.signal_id || body.signal_id || ""
        const orderId = value.order_id || body.order_id || ""
        const environment = value.environment || body.environment || ""

        console.log("[FeishuCallback] 收到回调请求, action:", action, "signalId:", signalId, "orderId:", orderId, "updateToken:", updateToken ? "存在" : "无")

        if (action && action.indexOf("ibkr_2fa_") === 0) {
            return handle2faCardCallback(c, {
                action: action,
                environment: environment,
                updateToken: updateToken,
                forceRestart: value.force_restart === true || ["1", "true", "yes", "on"].includes(String(value.force_restart || "").trim().toLowerCase()),
            })
        }

        if (orderId) {
            return handleOrderCardCallback(c, {
                action: action,
                orderId: orderId,
                environment: environment,
                updateToken: updateToken
            })
        }

        if (signalId) {
            return handleSignalCardCallback(c, {
                action: action,
                signalId: signalId,
                environment: environment,
                updateToken: updateToken
            })
        }

        return sendFeishuCallbackResponse(c, {
            toast: { type: "error", content: "缺少 signal_id 或 order_id" },
            card: { type: "raw", data: null }
        }, updateToken)
    } catch (err) {
        console.error("[FeishuCallback] 处理失败:", err)
        return sendFeishuCallbackResponse(c, { toast: { type: "error", content: "处理失败" } }, null)
    }
})

console.log("[FeishuPB] Hook 文件加载完成")
